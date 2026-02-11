import os
import json
from datetime import datetime
from typing import Dict, Any, List, Optional
from uuid import uuid4

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field
import gspread
from google.oauth2.service_account import Credentials

# =========================
# CONFIG (ENV VARS)
# =========================
SHEET_ID = os.getenv("TENANTS_SHEET_ID", "").strip()  # sigue llamándose así por compatibilidad
GCP_CREDENTIALS_JSON = os.getenv("GCP_CREDENTIALS_JSON", "").strip()

TAB_TENANTS = os.getenv("TAB_TENANTS", "Tenants").strip()
TAB_CONTENT = os.getenv("TAB_CONTENT", "Content").strip()
TAB_RULES = os.getenv("TAB_RULES", "BookingRules").strip()
TAB_BOOKINGS = os.getenv("TAB_BOOKINGS", "Bookings").strip()
TAB_DEFAULTS = os.getenv("TAB_DEFAULTS", "Defaults").strip()

# Tabs dentro del "orders_sheet_id" (spreadsheet por tenant)
TAB_MENU = os.getenv("TAB_MENU", "Menu").strip()
TAB_ORDERS = os.getenv("TAB_ORDERS", "Orders").strip()

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


def open_config_sheet():
    return open_sheet_by_id(SHEET_ID)


def norm(s: Any) -> str:
    return str(s).strip()


def parse_bool(val: Any) -> bool:
    s = str(val).strip().lower()
    return s in ("true", "1", "yes", "y", "si", "sí")


def parse_number(val: Any) -> Optional[float]:
    s = str(val).strip()
    if s == "":
        return None
    try:
        return float(s)
    except ValueError:
        return None


def read_records(tab_name: str) -> List[Dict[str, Any]]:
    """
    Lee registros del sheet de CONFIG (TENANTS_SHEET_ID) usando get_all_records()
    (headers en fila 1).
    """
    sh = open_config_sheet()
    ws = sh.worksheet(tab_name)
    return ws.get_all_records()


def read_table_records(
    worksheet,
    header_row: int = 1,        # fila 1 = headers técnicos
    data_start_row: int = 3,    # fila 2 = descripciones humanas (se ignora), datos desde fila 3
    stop_at_first_blank: bool = False
) -> List[Dict[str, Any]]:
    """
    Lee una hoja Google Sheet donde:
      - header_row contiene headers (fila 1)
      - data_start_row es donde empiezan los datos (fila 3)
      - fila 2 puede tener descripciones en español sin afectar el backend
    Devuelve lista de dicts {header: value}.
    """
    values = worksheet.get_all_values()
    if not values:
        return []

    if len(values) < header_row:
        return []

    headers = values[header_row - 1]
    headers = [h.strip() for h in headers]

    if not any(headers):
        return []

    records: List[Dict[str, Any]] = []

    for row_idx in range(data_start_row - 1, len(values)):
        row = values[row_idx]

        if len(row) < len(headers):
            row = row + [""] * (len(headers) - len(row))
        else:
            row = row[:len(headers)]

        if all((cell or "").strip() == "" for cell in row):
            if stop_at_first_blank:
                break
            else:
                continue

        rec: Dict[str, Any] = {}
        for h, v in zip(headers, row):
            if h == "":
                continue
            rec[h] = v.strip() if isinstance(v, str) else v

        records.append(rec)

    return records


def get_tenants_map() -> Dict[str, Dict[str, Any]]:
    rows = read_records(TAB_TENANTS)
    out: Dict[str, Dict[str, Any]] = {}
    for r in rows:
        tid = norm(r.get("tenant_id", "")).lower()
        if not tid:
            continue
        out[tid] = r
    return out


# =========================
# LOADERS (CONFIG)
# =========================
def load_tenants() -> Dict[str, Dict[str, Any]]:
    return get_tenants_map()


def load_defaults(scope: Optional[str] = None) -> Dict[str, str]:
    """
    Defaults sheet:
      scope | key | value
    """
    rows = read_records(TAB_DEFAULTS)
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
    BookingRules sheet:
      tenant_id | rule_key | value
    """
    rows = read_records(TAB_RULES)
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
    defaults = load_defaults(scope="booking_rule")
    overrides = load_booking_rules_for_tenant(tenant_id)

    effective = dict(defaults)
    effective.update(overrides)
    return effective


def load_content_for_tenant(tenant_id: str) -> Dict[str, Dict[str, str]]:
    """
    Content sheet:
      tenant_id | content_key | type | value
    Returns dict: {content_key: {"type":..., "value":...}}
    """
    rows = read_records(TAB_CONTENT)
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
# ORDERS HELPERS
# =========================
def get_tenant_or_404(tenant_id: str) -> Dict[str, Any]:
    tenants = load_tenants()
    tid = tenant_id.lower().strip()
    if tid not in tenants:
        raise HTTPException(status_code=404, detail=f"Unknown tenant_id: {tid}")

    t = tenants[tid]
    if not parse_bool(t.get("active", True)):
        raise HTTPException(status_code=400, detail=f"Tenant is inactive: {tid}")
    return t


def open_orders_sheet_for_tenant(tenant: Dict[str, Any]):
    """
    Abre el spreadsheet del tenant donde están las pestañas Menu/Orders.
    En tu Tenants sheet, la columna se llama orders_sheet_id.
    """
    sheet_id = norm(tenant.get("orders_sheet_id", "")).strip()
    if not sheet_id:
        raise HTTPException(status_code=500, detail="Tenant missing orders_sheet_id")
    return open_sheet_by_id(sheet_id)


def load_menu_for_tenant(tenant: Dict[str, Any]) -> List[Dict[str, Any]]:
    sh_orders = open_orders_sheet_for_tenant(tenant)
    ws = sh_orders.worksheet(TAB_MENU)
    rows = read_table_records(ws, header_row=1, data_start_row=3)

    # Validación mínima de headers esperados
    required = {"sku", "name", "price", "active", "category"}
    if rows:
        headers_present = set(rows[0].keys())
        missing = required - headers_present
        if missing:
            raise HTTPException(status_code=500, detail=f"Menu missing headers: {sorted(list(missing))}")

    # Filtra activos
    active_rows = [r for r in rows if parse_bool(r.get("active", ""))]
    # Normaliza sku
    for r in active_rows:
        r["sku"] = norm(r.get("sku", "")).strip()
    # Elimina los que no tienen sku
    active_rows = [r for r in active_rows if r.get("sku")]
    return active_rows


def build_menu_response(menu_rows: List[Dict[str, Any]]) -> Dict[str, Any]:
    """
    Devuelve:
    {
      "categories": [
        {"name": "Hamburguesas", "items": [{"sku","name","price"}...]}
      ]
    }
    """
    cats: Dict[str, List[Dict[str, Any]]] = {}
    for r in menu_rows:
        cat = norm(r.get("category", "")).strip() or "Otros"
        sku = norm(r.get("sku", "")).strip()
        name = norm(r.get("name", "")).strip()
        price = parse_number(r.get("price", ""))
        if not sku or not name or price is None:
            continue
        cats.setdefault(cat, []).append({"sku": sku, "name": name, "price": price})

    # Orden estable: por nombre de categoría, y dentro por name
    categories = []
    for cat_name in sorted(cats.keys()):
        items = sorted(cats[cat_name], key=lambda x: x["name"])
        categories.append({"name": cat_name, "items": items})

    return {"categories": categories}


def make_order_id() -> str:
    # simple y único: ORD-<8chars>
    return "ORD-" + uuid4().hex[:8].upper()


def now_iso_utc() -> str:
    return datetime.utcnow().isoformat()


def safe_json_dumps(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False)


def append_order_row(tenant: Dict[str, Any], row_dict: Dict[str, Any]) -> None:
    """
    Inserta una fila en la pestaña Orders respetando headers de fila 1.
    Si hay columnas extra (ej: delivery_type), simplemente se dejan en blanco
    a menos que row_dict traiga valores.
    """
    sh_orders = open_orders_sheet_for_tenant(tenant)
    ws = sh_orders.worksheet(TAB_ORDERS)

    values = ws.get_all_values()
    if not values:
        raise HTTPException(status_code=500, detail="Orders sheet is empty (missing headers)")

    headers = [h.strip() for h in values[0]]  # fila 1 headers técnicos
    if not any(headers):
        raise HTTPException(status_code=500, detail="Orders headers row is blank")

    row_out = []
    for h in headers:
        if h == "":
            row_out.append("")
            continue
        v = row_dict.get(h, "")
        row_out.append(v)

    ws.append_row(row_out, value_input_option="USER_ENTERED")


def update_order_status_by_id(tenant: Dict[str, Any], order_id: str, new_status: str) -> Dict[str, Any]:
    """
    Busca order_id en la pestaña Orders y actualiza la columna status.
    Devuelve {"found": bool, "row": int, "old_status": str, "new_status": str}
    """
    sh_orders = open_orders_sheet_for_tenant(tenant)
    ws = sh_orders.worksheet(TAB_ORDERS)

    values = ws.get_all_values()
    if not values or len(values) < 3:
        return {"found": False}

    headers = [h.strip() for h in values[0]]
    try:
        col_order_id = headers.index("order_id") + 1
    except ValueError:
        raise HTTPException(status_code=500, detail="Orders missing header: order_id")

    try:
        col_status = headers.index("status") + 1
    except ValueError:
        raise HTTPException(status_code=500, detail="Orders missing header: status")

    # data desde fila 3
    for row_idx in range(3, len(values) + 1):
        row = values[row_idx - 1]
        if len(row) < col_order_id:
            continue
        if norm(row[col_order_id - 1]).strip() == order_id:
            old_status = norm(row[col_status - 1]).strip() if len(row) >= col_status else ""
            ws.update_cell(row_idx, col_status, new_status)
            return {"found": True, "row": row_idx, "old_status": old_status, "new_status": new_status}

    return {"found": False}


# =========================
# REQUEST MODELS
# =========================
class OrderItem(BaseModel):
    sku: str = Field(..., min_length=1)
    qty: int = Field(..., ge=1, le=4)


class CreateOrderRequest(BaseModel):
    tenant_id: str = Field(..., min_length=1)
    customer_name: str = Field(..., min_length=1)
    customer_contact: str = Field(..., min_length=1)
    items: List[OrderItem]
    requested_time: str = Field(..., min_length=1)
    notes: Optional[str] = ""
    # futuro (opcional)
    delivery_type: Optional[str] = ""


class MarkPaidRequest(BaseModel):
    tenant_id: str = Field(..., min_length=1)
    order_id: str = Field(..., min_length=1)
    admin_chat_id: str = Field(..., min_length=1)


# =========================
# ENDPOINTS (E2.1 + Orders MVP)
# =========================
@app.get("/")
def root():
    return {"status": "ok", "service": "proyecto-reservas", "ts": datetime.utcnow().isoformat()}


@app.get("/debug/tenants")
def debug_tenants():
    try:
        tenants = load_tenants()
        return {
            "ok": True,
            "count": len(tenants),
            "tenant_ids": sorted(list(tenants.keys())),
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


# =========================
# ORDERS MVP ENDPOINTS
# =========================
@app.get("/menu")
def get_menu(tenant_id: str):
    """
    GET /menu?tenant_id=resto_demo
    Lee Menu desde fila 3 (fila 2 es texto en español) y devuelve categorías + items activos.
    """
    try:
        tenant = get_tenant_or_404(tenant_id)

        if not parse_bool(tenant.get("orders_enabled", False)):
            raise HTTPException(status_code=400, detail=f"Orders not enabled for tenant: {tenant_id}")

        menu_rows = load_menu_for_tenant(tenant)
        payload = build_menu_response(menu_rows)

        return {"ok": True, "tenant_id": tenant_id.lower().strip(), **payload}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/orders/create")
def create_order(req: CreateOrderRequest):
    """
    Crea un pedido:
      - Valida skus y active=TRUE
      - Recalcula total leyendo Menu
      - Guarda en Orders (append)
      - status=PENDING_PAYMENT
    """
    try:
        tenant_id = req.tenant_id.lower().strip()
        tenant = get_tenant_or_404(tenant_id)

        if not parse_bool(tenant.get("orders_enabled", False)):
            raise HTTPException(status_code=400, detail=f"Orders not enabled for tenant: {tenant_id}")

        # Load menu and map sku -> (name, price)
        menu_rows = load_menu_for_tenant(tenant)
        menu_map: Dict[str, Dict[str, Any]] = {}
        for r in menu_rows:
            sku = norm(r.get("sku", "")).strip()
            price = parse_number(r.get("price", ""))
            name = norm(r.get("name", "")).strip()
            if sku and name and price is not None:
                menu_map[sku] = {"name": name, "price": price}

        # Validate items and compute total
        items_out = []
        total = 0.0

        for it in req.items:
            sku = norm(it.sku).strip()
            qty = int(it.qty)
            if sku not in menu_map:
                raise HTTPException(status_code=400, detail=f"Invalid SKU or inactive: {sku}")
            price = float(menu_map[sku]["price"])
            name = menu_map[sku]["name"]
            line_total = price * qty
            total += line_total
            items_out.append({"sku": sku, "name": name, "qty": qty, "unit_price": price, "line_total": line_total})

        order_id = make_order_id()
        created_at = now_iso_utc()

        # Row dict aligned to headers. Extra columns in sheet will be blank unless included.
        row_dict: Dict[str, Any] = {
            "order_id": order_id,
            "created_at": created_at,
            "tenant_id": tenant_id,
            "customer_name": req.customer_name.strip(),
            "customer_contact": req.customer_contact.strip(),
            "items": safe_json_dumps(items_out),   # guardamos JSON como texto
            "notes": (req.notes or "").strip(),
            "delivery_type": (req.delivery_type or "").strip(),  # opcional/futuro
            "requested_time": req.requested_time.strip(),
            "status": "PENDING_PAYMENT",
            "source": "telegram",
            "total_amount": round(total, 2),
        }

        append_order_row(tenant, row_dict)

        return {
            "ok": True,
            "tenant_id": tenant_id,
            "order_id": order_id,
            "status": "PENDING_PAYMENT",
            "total_amount": round(total, 2),
            "items": items_out,
        }

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/orders/mark_paid")
def mark_paid(req: MarkPaidRequest):
    """
    Marca el pedido como PAID.
    Seguridad:
      - Valida admin_chat_id contra Tenants.admin_chat_id
    Idempotente:
      - Si ya está PAID, responde ok igual.
    """
    try:
        tenant_id = req.tenant_id.lower().strip()
        tenant = get_tenant_or_404(tenant_id)

        expected_admin_chat_id = norm(tenant.get("admin_chat_id", "")).strip()
        if not expected_admin_chat_id:
            raise HTTPException(status_code=500, detail="admin_chat_id is not set for this tenant")

        if norm(req.admin_chat_id).strip() != expected_admin_chat_id:
            raise HTTPException(status_code=403, detail="Invalid admin_chat_id for this tenant")

        order_id = norm(req.order_id).strip()

        result = update_order_status_by_id(tenant, order_id, "PAID")
        if not result.get("found"):
            raise HTTPException(status_code=404, detail=f"Order not found: {order_id}")

        old_status = norm(result.get("old_status", "")).strip()
        # Idempotencia: si ya era PAID, igual devolvemos ok
        return {
            "ok": True,
            "tenant_id": tenant_id,
            "order_id": order_id,
            "old_status": old_status,
            "new_status": "PAID",
            "row": result.get("row"),
        }

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


