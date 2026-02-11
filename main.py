import os
import json
import uuid
from datetime import datetime
from typing import Dict, Any, List, Optional

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field
import gspread
from google.oauth2.service_account import Credentials

# =========================
# CONFIG (ENV VARS)
# =========================
SHEET_ID = os.getenv("TENANTS_SHEET_ID", "").strip()  # Spreadsheet principal (tenants/config)
GCP_CREDENTIALS_JSON = os.getenv("GCP_CREDENTIALS_JSON", "").strip()

TAB_TENANTS = os.getenv("TAB_TENANTS", "Tenants").strip()
TAB_CONTENT = os.getenv("TAB_CONTENT", "Content").strip()
TAB_RULES = os.getenv("TAB_RULES", "BookingRules").strip()
TAB_BOOKINGS = os.getenv("TAB_BOOKINGS", "Bookings").strip()
TAB_DEFAULTS = os.getenv("TAB_DEFAULTS", "Defaults").strip()

# Admin token opcional para endpoints sensibles (no lo usamos aún)
ADMIN_TOKEN = os.getenv("ADMIN_TOKEN", "").strip()

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive.readonly",
]

app = FastAPI(title="proyecto-reservas", version="0.1.0")


# =========================
# UTILS
# =========================
def norm(x: Any) -> str:
    return str(x).strip()


def to_bool(x: Any) -> bool:
    s = norm(x).lower()
    return s in ("true", "1", "yes", "y", "si", "sí", "ok", "activo")


def utc_iso() -> str:
    return datetime.utcnow().isoformat()


# =========================
# GOOGLE SHEETS HELPERS
# =========================
def _require_env():
    if not SHEET_ID:
        raise RuntimeError("Missing TENANTS_SHEET_ID env var")
    if not GCP_CREDENTIALS_JSON:
        raise RuntimeError("Missing GCP_CREDENTIALS_JSON env var")


def get_gspread_client():
    _require_env()
    info = json.loads(GCP_CREDENTIALS_JSON)
    creds = Credentials.from_service_account_info(info, scopes=SCOPES)
    return gspread.authorize(creds)


def open_sheet_by_id(sheet_id: str):
    gc = get_gspread_client()
    return gc.open_by_key(sheet_id)


def read_records_simple(sheet_id: str, tab_name: str) -> List[Dict[str, Any]]:
    """
    Para hojas 'normales' (sin fila extra en español).
    Usa fila 1 como headers.
    """
    sh = open_sheet_by_id(sheet_id)
    ws = sh.worksheet(tab_name)
    return ws.get_all_records()


def read_records_from_row(sheet_id: str, tab_name: str, header_row: int = 1, start_row: int = 3) -> List[Dict[str, Any]]:
    """
    Lee una worksheet usando:
      - header_row (por defecto 1)
      - start_row (por defecto 3) para saltar fila 2 en español.

    Ideal para:
      - Menu
      - Orders
    cuando tienes fila 2 con descripciones.
    """
    sh = open_sheet_by_id(sheet_id)
    ws = sh.worksheet(tab_name)

    values = ws.get_all_values()
    if not values:
        return []

    if len(values) < header_row:
        return []

    headers = values[header_row - 1]
    headers = [norm(h) for h in headers]

    out: List[Dict[str, Any]] = []
    for i in range(start_row - 1, len(values)):
        row = values[i]
        # Asegurar largo igual a headers
        if len(row) < len(headers):
            row = row + [""] * (len(headers) - len(row))
        elif len(row) > len(headers):
            row = row[:len(headers)]

        # Si fila completamente vacía, saltar
        if all(norm(c) == "" for c in row):
            continue

        rec = {}
        for h, c in zip(headers, row):
            if norm(h) == "":
                continue
            rec[h] = c
        out.append(rec)

    return out


# =========================
# LOADERS (TENANTS / RULES / CONTENT)
# =========================
def load_tenants() -> Dict[str, Dict[str, Any]]:
    rows = read_records_simple(SHEET_ID, TAB_TENANTS)
    out: Dict[str, Dict[str, Any]] = {}

    for r in rows:
        tid = norm(r.get("tenant_id", "")).lower()
        if not tid:
            continue

        # Normalizar booleans (acepta TRUE/FALSE)
        r["bookings_enabled_bool"] = to_bool(r.get("bookings_enabled", False))
        r["orders_enabled_bool"] = to_bool(r.get("orders_enabled", False))
        r["active_bool"] = to_bool(r.get("active", True))

        out[tid] = r

    return out


def load_defaults(scope: Optional[str] = None) -> Dict[str, str]:
    rows = read_records_simple(SHEET_ID, TAB_DEFAULTS)
    d: Dict[str, str] = {}
    for r in rows:
        sc = norm(r.get("scope", "")).lower()
        k = norm(r.get("key", ""))
        v = norm(r.get("value", ""))
        if not sc or not k:
            continue
        if scope and sc != scope.lower():
            continue
        d[k] = v
    return d


def load_booking_rules_for_tenant(tenant_id: str) -> Dict[str, str]:
    rows = read_records_simple(SHEET_ID, TAB_RULES)
    rules: Dict[str, str] = {}
    tid = tenant_id.lower().strip()
    for r in rows:
        r_tid = norm(r.get("tenant_id", "")).lower()
        if r_tid != tid:
            continue
        key = norm(r.get("rule_key", ""))
        val = norm(r.get("value", ""))
        if key:
            rules[key] = val
    return rules


def get_effective_rules(tenant_id: str) -> Dict[str, Any]:
    defaults = load_defaults(scope="booking_rule")
    overrides = load_booking_rules_for_tenant(tenant_id)
    effective = dict(defaults)
    effective.update(overrides)
    return effective


def load_content_for_tenant(tenant_id: str) -> Dict[str, Dict[str, str]]:
    rows = read_records_simple(SHEET_ID, TAB_CONTENT)
    out: Dict[str, Dict[str, str]] = {}
    tid = tenant_id.lower().strip()
    for r in rows:
        r_tid = norm(r.get("tenant_id", "")).lower()
        if r_tid != tid:
            continue
        ck = norm(r.get("content_key", ""))
        tp = norm(r.get("type", "")).lower()
        val = norm(r.get("value", ""))
        if not ck:
            continue
        out[ck] = {"type": tp, "value": val}
    return out


# =========================
# MENU + ORDERS HELPERS
# =========================
def get_orders_sheet_id_for_tenant(tenant_id: str) -> str:
    tenants = load_tenants()
    tid = tenant_id.lower().strip()
    if tid not in tenants:
        raise HTTPException(status_code=404, detail=f"Unknown tenant_id: {tid}")

    t = tenants[tid]

    if not t.get("active_bool", True):
        raise HTTPException(status_code=400, detail=f"Tenant inactive: {tid}")

    if not t.get("orders_enabled_bool", False):
        raise HTTPException(status_code=400, detail=f"Orders not enabled for tenant: {tid}")

    sheet_id = norm(t.get("orders_sheet_id", ""))
    if not sheet_id:
        raise HTTPException(status_code=400, detail=f"Missing orders_sheet_id for tenant: {tid}")

    return sheet_id


def load_menu_items(tenant_id: str) -> List[Dict[str, Any]]:
    orders_sheet_id = get_orders_sheet_id_for_tenant(tenant_id)

    # Lee Menu desde fila 3 (saltando fila 2 en español)
    rows = read_records_from_row(orders_sheet_id, "Menu", header_row=1, start_row=3)

    items: List[Dict[str, Any]] = []
    for r in rows:
        # headers esperados: sku, name, price, active, category
        active_val = r.get("active", "")
        if not to_bool(active_val):
            continue

        sku = norm(r.get("sku", ""))
        name = norm(r.get("name", ""))
        price_raw = norm(r.get("price", ""))
        category = norm(r.get("category", ""))

        if not sku or not name:
            continue

        # Convertir precio a número si se puede
        price: Optional[float] = None
        try:
            if price_raw != "":
                price = float(price_raw)
        except Exception:
            price = None

        items.append(
            {
                "sku": sku,
                "name": name,
                "price": price,
                "category": category,
                "active": True,
            }
        )

    return items


def append_order_row(tenant_id: str, order: Dict[str, Any]) -> None:
    orders_sheet_id = get_orders_sheet_id_for_tenant(tenant_id)
    sh = open_sheet_by_id(orders_sheet_id)
    ws = sh.worksheet("Orders")

    # Headers están en fila 1 (inglés)
    headers = ws.row_values(1)
    headers = [norm(h) for h in headers]

    def getv(key: str) -> str:
        return norm(order.get(key, ""))

    # Construir fila en el orden de los headers
    row_out: List[str] = []
    for h in headers:
        if h == "":
            row_out.append("")
            continue
        row_out.append(getv(h))

    # Agregar al final
    ws.append_row(row_out, value_input_option="USER_ENTERED")


def update_order_status(tenant_id: str, order_id: str, new_status: str) -> Dict[str, Any]:
    orders_sheet_id = get_orders_sheet_id_for_tenant(tenant_id)
    sh = open_sheet_by_id(orders_sheet_id)
    ws = sh.worksheet("Orders")

    values = ws.get_all_values()
    if not values or len(values) < 3:
        raise HTTPException(status_code=404, detail="No orders data found")

    headers = values[0]
    headers = [norm(h) for h in headers]

    # Encontrar columnas
    try:
        col_order_id = headers.index("order_id")
    except ValueError:
        raise HTTPException(status_code=400, detail="Orders sheet missing 'order_id' header in row 1")

    try:
        col_status = headers.index("status")
    except ValueError:
        raise HTTPException(status_code=400, detail="Orders sheet missing 'status' header in row 1")

    # Buscar order_id desde fila 3 (saltando fila 2)
    for i in range(2, len(values)):  # i=2 es fila 3 (0-index)
        row = values[i]
        if len(row) <= col_order_id:
            continue
        if norm(row[col_order_id]) == norm(order_id):
            # update cell (fila real = i+1, col real = col_status+1)
            ws.update_cell(i + 1, col_status + 1, new_status)
            return {"ok": True, "order_id": order_id, "status": new_status}

    raise HTTPException(status_code=404, detail=f"order_id not found: {order_id}")


# =========================
# API MODELS
# =========================
class CreateOrderRequest(BaseModel):
    tenant_id: str = Field(..., description="Tenant id, ej: resto_demo")
    customer_name: str = Field(..., description="Nombre del cliente")
    customer_contact: str = Field("", description="WhatsApp/teléfono del cliente (opcional)")
    items: str = Field(..., description="Pedido (texto). Ej: '2x Burger, 1x Cola'")
    notes: str = Field("", description="Observaciones (opcional)")
    delivery_type: str = Field("pickup", description="pickup / delivery (por ahora puede quedar)")
    requested_time: str = Field("", description="Hora solicitada (texto libre)")
    source: str = Field("telegram", description="Origen: telegram/whatsapp/web/etc")
    total_amount: str = Field("", description="Monto total (texto o número)")


class MarkPaidRequest(BaseModel):
    tenant_id: str = Field(..., description="Tenant id, ej: resto_demo")
    order_id: str = Field(..., description="ID del pedido")
    status: str = Field("PAID", description="Nuevo estado (PAID por defecto)")


# =========================
# ENDPOINTS
# =========================
@app.get("/")
def root():
    return {"status": "ok", "service": "proyecto-reservas", "ts": utc_iso()}


@app.get("/debug/tenants")
def debug_tenants():
    try:
        tenants = load_tenants()
        return {
            "ok": True,
            "count": len(tenants),
            "tenant_ids": sorted(list(tenants.keys())),
            "sample": tenants.get(sorted(list(tenants.keys()))[0]) if tenants else {},
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/debug/defaults")
def debug_defaults():
    try:
        defaults = load_defaults(scope="booking_rule")
        return {"ok": True, "count": len(defaults), "defaults": defaults}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/debug/rules/{tenant_id}")
def debug_rules(tenant_id: str):
    try:
        tenants = load_tenants()
        tid = tenant_id.lower().strip()
        if tid not in tenants:
            raise HTTPException(status_code=404, detail=f"Unknown tenant_id: {tid}")

        defaults = load_defaults(scope="booking_rule")
        overrides = load_booking_rules_for_tenant(tid)
        effective = dict(defaults)
        effective.update(overrides)

        return {
            "ok": True,
            "tenant_id": tid,
            "defaults_count": len(defaults),
            "overrides_count": len(overrides),
            "overrides": overrides,
            "effective": effective,
        }
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/debug/content/{tenant_id}")
def debug_content(tenant_id: str):
    try:
        tenants = load_tenants()
        tid = tenant_id.lower().strip()
        if tid not in tenants:
            raise HTTPException(status_code=404, detail=f"Unknown tenant_id: {tid}")

        content = load_content_for_tenant(tid)
        return {"ok": True, "tenant_id": tid, "count": len(content), "content": content}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/menu")
def get_menu(tenant_id: str):
    try:
        tid = tenant_id.lower().strip()
        items = load_menu_items(tid)
        return {"ok": True, "tenant_id": tid, "count": len(items), "items": items}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/orders/create")
def create_order(req: CreateOrderRequest):
    try:
        tid = req.tenant_id.lower().strip()
        _ = get_orders_sheet_id_for_tenant(tid)  # valida tenant, enabled, sheet id

        order_id = uuid.uuid4().hex[:12]
        created_at = utc_iso()

        order = {
            "order_id": order_id,
            "created_at": created_at,
            "tenant_id": tid,
            "customer_name": req.customer_name,
            "customer_contact": req.customer_contact,
            "items": req.items,
            "notes": req.notes,
            "delivery_type": req.delivery_type,
            "requested_time": req.requested_time,
            "status": "PENDING_PAYMENT",
            "source": req.source,
            "total_amount": req.total_amount,
        }

        append_order_row(tid, order)
        return {"ok": True, "order": order}

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/orders/mark_paid")
def mark_paid(req: MarkPaidRequest):
    try:
        tid = req.tenant_id.lower().strip()
        result = update_order_status(tid, req.order_id, req.status)
        return result
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
