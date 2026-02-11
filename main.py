import os
import json
import uuid
from datetime import datetime
from typing import Dict, Any, List, Optional

from fastapi import FastAPI, HTTPException
import gspread
from google.oauth2.service_account import Credentials

# =========================
# CONFIG (ENV VARS)
# =========================
TENANTS_SHEET_ID = os.getenv("TENANTS_SHEET_ID", "").strip()  # Sheet central: Tenants/Content/BookingRules/Bookings/Defaults
GCP_CREDENTIALS_JSON = os.getenv("GCP_CREDENTIALS_JSON", "").strip()

TAB_TENANTS = os.getenv("TAB_TENANTS", "Tenants").strip()
TAB_CONTENT = os.getenv("TAB_CONTENT", "Content").strip()
TAB_RULES = os.getenv("TAB_RULES", "BookingRules").strip()
TAB_BOOKINGS = os.getenv("TAB_BOOKINGS", "Bookings").strip()
TAB_DEFAULTS = os.getenv("TAB_DEFAULTS", "Defaults").strip()

# Tabs dentro de cada Orders Sheet (por tenant)
ORDERS_TAB_MENU = os.getenv("ORDERS_TAB_MENU", "Menu").strip()
ORDERS_TAB_ORDERS = os.getenv("ORDERS_TAB_ORDERS", "Orders").strip()

# Admin token opcional para endpoints sensibles (no lo usamos aún)
ADMIN_TOKEN = os.getenv("ADMIN_TOKEN", "").strip()

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive.readonly",
]

app = FastAPI()


# =========================
# GOOGLE SHEETS HELPERS
# =========================
def _require_env():
    if not TENANTS_SHEET_ID:
        raise RuntimeError("Missing TENANTS_SHEET_ID env var")
    if not GCP_CREDENTIALS_JSON:
        raise RuntimeError("Missing GCP_CREDENTIALS_JSON env var")


def get_gspread_client():
    _require_env()
    info = json.loads(GCP_CREDENTIALS_JSON)
    creds = Credentials.from_service_account_info(info, scopes=SCOPES)
    return gspread.authorize(creds)


def open_tenants_sheet():
    gc = get_gspread_client()
    return gc.open_by_key(TENANTS_SHEET_ID)


def open_sheet_by_id(sheet_id: str):
    gc = get_gspread_client()
    return gc.open_by_key(sheet_id)


def norm(v: Any) -> str:
    return str(v).strip() if v is not None else ""


def str_to_bool(v: Any) -> bool:
    """
    Acepta TRUE/true, 1, yes, y, si, etc.
    Sirve para valores que vienen como texto desde Sheets.
    """
    if v is None:
        return False
    s = str(v).strip().lower()
    return s in ["true", "1", "yes", "y", "si", "sí"]


def read_records_header_row(ws: gspread.Worksheet, header_row: int = 1) -> List[Dict[str, Any]]:
    """
    Lee registros usando una fila específica como headers.
    - header_row=1: headers en primera fila (lo típico).
    - header_row=3: headers en la fila 3 (saltando fila 2 en español).
    Retorna lista de dicts.
    """
    values = ws.get_all_values()
    if not values:
        return []

    idx = header_row - 1
    if idx < 0 or idx >= len(values):
        return []

    headers = [h.strip() for h in values[idx]]
    out: List[Dict[str, Any]] = []

    # data empieza en la fila siguiente al header_row
    for row in values[idx + 1 :]:
        # si la fila está totalmente vacía, saltar
        if not any(cell.strip() for cell in row):
            continue

        # mapear celdas a headers (si faltan columnas, completar con "")
        item: Dict[str, Any] = {}
        for i, h in enumerate(headers):
            if not h:
                continue
            item[h] = row[i].strip() if i < len(row) else ""

        # si el dict quedó vacío, saltar
        if any(str(v).strip() for v in item.values()):
            out.append(item)

    return out


def append_row(ws: gspread.Worksheet, row: List[Any]):
    """
    Agrega una fila al final.
    """
    ws.append_row(row, value_input_option="USER_ENTERED")


# =========================
# LOADERS (TENANTS SHEET)
# =========================
def load_tenants() -> Dict[str, Dict[str, Any]]:
    """
    Tenants sheet (headers en fila 1):
      tenant_id | name | business_type | orders_sheet_id | bookings_enabled | orders_enabled | ...
    """
    sh = open_tenants_sheet()
    ws = sh.worksheet(TAB_TENANTS)

    rows = read_records_header_row(ws, header_row=1)

    out: Dict[str, Dict[str, Any]] = {}
    for r in rows:
        tid = norm(r.get("tenant_id", "")).lower()
        if not tid:
            continue

        # booleans normalizados
        r["bookings_enabled_bool"] = str_to_bool(r.get("bookings_enabled"))
        r["orders_enabled_bool"] = str_to_bool(r.get("orders_enabled"))
        r["active_bool"] = str_to_bool(r.get("active"))

        out[tid] = r

    return out


def load_defaults(scope: Optional[str] = None) -> Dict[str, str]:
    """
    Defaults sheet (headers en fila 1):
      scope | key | value
    """
    sh = open_tenants_sheet()
    ws = sh.worksheet(TAB_DEFAULTS)
    rows = read_records_header_row(ws, header_row=1)

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
    """
    BookingRules sheet (headers en fila 1):
      tenant_id | rule_key | value
    """
    sh = open_tenants_sheet()
    ws = sh.worksheet(TAB_RULES)
    rows = read_records_header_row(ws, header_row=1)

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
    """
    Priority:
      BookingRules(tenant) > Defaults(scope=booking_rule)
    """
    defaults = load_defaultss = load_defaults(scope="booking_rule")
    overrides = load_booking_rules_for_tenant(tenant_id)

    effective = dict(defaults)
    effective.update(overrides)
    return effective


def load_content_for_tenant(tenant_id: str) -> Dict[str, Dict[str, str]]:
    """
    Content sheet (headers en fila 1):
      tenant_id | content_key | type | value
    Returns dict: {content_key: {"type":..., "value":...}}
    """
    sh = open_tenants_sheet()
    ws = sh.worksheet(TAB_CONTENT)
    rows = read_records_header_row(ws, header_row=1)

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
# LOADERS (ORDERS SHEET por tenant)
# =========================
def _get_tenant_or_404(tenant_id: str) -> Dict[str, Any]:
    tenants = load_tenants()
    tid = tenant_id.lower().strip()
    if tid not in tenants:
        raise HTTPException(status_code=404, detail=f"Unknown tenant_id: {tid}")
    t = tenants[tid]
    if not t.get("active_bool", True):
        raise HTTPException(status_code=400, detail=f"Tenant inactive: {tid}")
    return t


def load_menu_for_tenant(tenant_id: str) -> List[Dict[str, Any]]:
    """
    Lee Menu desde el Orders Sheet del tenant.
    IMPORTANT: headers están en fila 3 (fila 2 es español), por eso header_row=3.
    """
    tenant = _get_tenant_or_404(tenant_id)

    if not tenant.get("orders_enabled_bool", False):
        raise HTTPException(status_code=400, detail=f"Orders not enabled for tenant: {tenant_id}")

    orders_sheet_id = norm(tenant.get("orders_sheet_id"))
    if not orders_sheet_id:
        raise HTTPException(status_code=400, detail=f"Missing orders_sheet_id for tenant: {tenant_id}")

    sh = open_sheet_by_id(orders_sheet_id)
    ws = sh.worksheet(ORDERS_TAB_MENU)

    rows = read_records_header_row(ws, header_row=3)

    # Filtrar solo active = TRUE (si existe la columna)
    out = []
    for r in rows:
        active_val = r.get("active", "")
        if str_to_bool(active_val):
            out.append(r)
    return out


def _orders_ws_for_tenant(tenant_id: str) -> gspread.Worksheet:
    tenant = _get_tenant_or_404(tenant_id)

    if not tenant.get("orders_enabled_bool", False):
        raise HTTPException(status_code=400, detail=f"Orders not enabled for tenant: {tenant_id}")

    orders_sheet_id = norm(tenant.get("orders_sheet_id"))
    if not orders_sheet_id:
        raise HTTPException(status_code=400, detail=f"Missing orders_sheet_id for tenant: {tenant_id}")

    sh = open_sheet_by_id(orders_sheet_id)
    return sh.worksheet(ORDERS_TAB_ORDERS)


def _orders_headers(ws: gspread.Worksheet) -> List[str]:
    """
    Headers del Orders sheet en fila 3 (fila 2 es español).
    """
    values = ws.get_all_values()
    if len(values) < 3:
        return []
    headers = [h.strip() for h in values[2]]  # fila 3 => índice 2
    return headers


def create_order_row_payload(headers: List[str], data: Dict[str, Any]) -> List[Any]:
    """
    Construye una fila (lista) respetando el orden de headers del sheet.
    Si falta una columna, deja vacío.
    """
    row = []
    for h in headers:
        if not h:
            row.append("")
            continue
        row.append(data.get(h, ""))
    return row


# =========================
# ENDPOINTS
# =========================
@app.get("/")
def root():
    return {"status": "ok", "service": "proyecto-reservas", "ts": datetime.utcnow().isoformat()}


# ---------- DEBUG ----------
@app.get("/debug/tenants")
def debug_tenants():
    try:
        tenants = load_tenants()
        sample = None
        if tenants:
            # agarrar uno cualquiera, pero preferir resto_demo si existe
            if "resto_demo" in tenants:
                sample = tenants["resto_demo"]
            else:
                sample = tenants[list(tenants.keys())[0]]

        return {
            "ok": True,
            "count": len(tenants),
            "tenant_ids": sorted(list(tenants.keys())),
            "sample": sample,
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
        _ = _get_tenant_or_404(tenant_id)
        defaults = load_defaults(scope="booking_rule")
        overrides = load_booking_rules_for_tenant(tenant_id)
        effective = dict(defaults)
        effective.update(overrides)

        return {
            "ok": True,
            "tenant_id": tenant_id.lower().strip(),
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
        _ = _get_tenant_or_404(tenant_id)
        content = load_content_for_tenant(tenant_id)
        return {"ok": True, "tenant_id": tenant_id.lower().strip(), "count": len(content), "content": content}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ---------- ORDERS API ----------
@app.get("/menu")
def get_menu(tenant_id: str):
    """
    GET /menu?tenant_id=resto_demo
    Devuelve el menú activo desde Orders Sheet -> pestaña Menu.
    (Headers en fila 3)
    """
    try:
        menu = load_menu_for_tenant(tenant_id)
        return {"ok": True, "tenant_id": tenant_id.lower().strip(), "count": len(menu), "items": menu}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/orders/create")
def create_order(payload: Dict[str, Any]):
    """
    Crea una orden en Orders Sheet -> pestaña Orders.
    Espera payload tipo:
    {
      "tenant_id": "resto_demo",
      "customer_name": "Juan",
      "customer_contact": "+591...",
      "items": "H01 x2, P01 x1",
      "notes": "sin cebolla",
      "delivery_type": "pickup",
      "requested_time": "20:30",
      "source": "telegram"
    }

    Nota: el sheet tiene headers en fila 3. La fila 2 puede estar en español y NO afecta.
    """
    try:
        tenant_id = norm(payload.get("tenant_id")).lower().strip()
        if not tenant_id:
            raise HTTPException(status_code=400, detail="Missing tenant_id")

        ws = _orders_ws_for_tenant(tenant_id)
        headers = _orders_headers(ws)
        if not headers:
            raise HTTPException(status_code=400, detail="Orders sheet missing headers in row 3")

        now = datetime.utcnow().isoformat()
        order_id = f"ord_{uuid.uuid4().hex[:10]}"

        # Datos base
        row_data = {
            "order_id": order_id,
            "created_at": now,
            "tenant_id": tenant_id,
            "customer_name": norm(payload.get("customer_name")),
            "customer_contact": norm(payload.get("customer_contact")),
            "items": norm(payload.get("items")),
            "notes": norm(payload.get("notes")),
            "delivery_type": norm(payload.get("delivery_type")),
            "requested_time": norm(payload.get("requested_time")),
            "status": "PENDING_PAYMENT",
            "source": norm(payload.get("source")) or "api",
            "total_amount": norm(payload.get("total_amount")),  # opcional (si lo calculas en backend luego)
        }

        row = create_order_row_payload(headers, row_data)
        append_row(ws, row)

        return {"ok": True, "tenant_id": tenant_id, "order_id": order_id, "status": "PENDING_PAYMENT"}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/orders/mark_paid")
def mark_order_paid(payload: Dict[str, Any]):
    """
    Marca una orden como PAID.

    Espera:
    {
      "tenant_id": "resto_demo",
      "order_id": "ord_...."
    }
    """
    try:
        tenant_id = norm(payload.get("tenant_id")).lower().strip()
        order_id = norm(payload.get("order_id")).strip()

        if not tenant_id:
            raise HTTPException(status_code=400, detail="Missing tenant_id")
        if not order_id:
            raise HTTPException(status_code=400, detail="Missing order_id")

        ws = _orders_ws_for_tenant(tenant_id)
        headers = _orders_headers(ws)
        if not headers:
            raise HTTPException(status_code=400, detail="Orders sheet missing headers in row 3")

        # Ubicar columnas relevantes
        try:
            col_order_id = headers.index("order_id") + 1
        except ValueError:
            raise HTTPException(status_code=400, detail="Orders sheet missing 'order_id' column")

        try:
            col_status = headers.index("status") + 1
        except ValueError:
            raise HTTPException(status_code=400, detail="Orders sheet missing 'status' column")

        # Buscar order_id en la columna order_id (datos empiezan en fila 4)
        order_id_values = ws.col_values(col_order_id)
        # order_id_values[0]=fila1, [1]=fila2, [2]=fila3 headers, data desde [3]
        target_row = None
        for i in range(3, len(order_id_values)):  # fila 4 => índice 3
            if order_id_values[i].strip() == order_id:
                target_row = i + 1  # convertir índice a número de fila
                break

        if not target_row:
            raise HTTPException(status_code=404, detail=f"Order not found: {order_id}")

        ws.update_cell(target_row, col_status, "PAID")

        return {"ok": True, "tenant_id": tenant_id, "order_id": order_id, "status": "PAID"}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
