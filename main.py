import os
import json
import uuid
from datetime import datetime, timezone
from typing import Dict, Any, List

from fastapi import FastAPI, HTTPException
import gspread
from google.oauth2.service_account import Credentials
from google.auth.transport.requests import AuthorizedSession
from gspread.client import Client

# =========================
# CONFIG (ENV VARS)
# =========================
TENANTS_SHEET_ID = os.getenv("TENANTS_SHEET_ID", "").strip()  # Spreadsheet config (reservaciones_config)
GCP_CREDENTIALS_JSON = os.getenv("GCP_CREDENTIALS_JSON", "").strip()

TAB_TENANTS = os.getenv("TAB_TENANTS", "Tenants").strip()

# Dentro del spreadsheet de cada tenant (orders_sheet_id)
TAB_MENU = os.getenv("TAB_MENU", "Menu").strip()
TAB_ORDERS = os.getenv("TAB_ORDERS", "Orders").strip()

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive.readonly",
]

app = FastAPI()


# =========================
# HELPERS
# =========================
def _require_env():
    if not TENANTS_SHEET_ID:
        raise RuntimeError("Missing TENANTS_SHEET_ID env var")
    if not GCP_CREDENTIALS_JSON:
        raise RuntimeError("Missing GCP_CREDENTIALS_JSON env var")


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def norm(v: Any) -> str:
    return str(v).strip() if v is not None else ""


def parse_bool(v: Any) -> bool:
    """
    Convierte valores típicos de Google Sheets a booleano.
    Acepta: TRUE/FALSE, true/false, 1/0, yes/no, y/n, si/no, etc.
    """
    if v is None:
        return False
    if isinstance(v, bool):
        return v
    s = str(v).strip().lower()
    if s in ("true", "1", "yes", "y", "si", "sí", "on"):
        return True
    if s in ("false", "0", "no", "n", "off", ""):
        return False
    return False


def get_gspread_client() -> Client:
    """
    Cliente gspread robusto (google-auth).
    Evita problemas raros con gspread.authorize en algunos entornos.
    """
    _require_env()
    info = json.loads(GCP_CREDENTIALS_JSON)
    creds = Credentials.from_service_account_info(info, scopes=SCOPES)
    session = AuthorizedSession(creds)
    gc = Client(auth=creds)
    gc.session = session
    return gc


def open_sheet_by_id(sheet_id: str):
    gc = get_gspread_client()
    return gc.open_by_key(sheet_id)


def ws_read_records(ws, header_row: int, start_row: int) -> List[Dict[str, Any]]:
    """
    Lee una worksheet:
      - header_row: fila donde están encabezados (1)
      - start_row: primera fila de datos (2 o 3)
    """
    values = ws.get_all_values()
    if not values or len(values) < header_row:
        return []

    headers = [h.strip() for h in values[header_row - 1]]
    if not any(headers):
        return []

    if len(values) < start_row:
        return []

    out: List[Dict[str, Any]] = []
    for row in values[start_row - 1:]:
        if not any(cell.strip() for cell in row):
            continue
        row_extended = row + [""] * (len(headers) - len(row))
        item = {headers[i]: row_extended[i].strip() for i in range(len(headers))}
        out.append(item)
    return out


def ws_get_headers(ws) -> List[str]:
    values = ws.get_all_values()
    if not values:
        return []
    return [h.strip() for h in values[0]]


def find_col_index(headers: List[str], name: str) -> int:
    """
    Devuelve índice 1-based (como usa gspread) para un header.
    """
    target = name.strip().lower()
    for i, h in enumerate(headers):
        if h.strip().lower() == target:
            return i + 1
    return -1


# =========================
# TENANTS
# =========================
def load_tenants() -> Dict[str, Dict[str, Any]]:
    sh = open_sheet_by_id(TENANTS_SHEET_ID)
    ws = sh.worksheet(TAB_TENANTS)

    # Tenants: headers fila 1, datos desde fila 2
    rows = ws_read_records(ws, header_row=1, start_row=2)

    out: Dict[str, Dict[str, Any]] = {}
    for r in rows:
        tid = norm(r.get("tenant_id")).lower()
        if not tid:
            continue

        # booleans robustos
        r["active_bool"] = parse_bool(r.get("active"))
        r["orders_enabled_bool"] = parse_bool(r.get("orders_enabled"))
        r["bookings_enabled_bool"] = parse_bool(r.get("bookings_enabled"))

        out[tid] = r
    return out


def get_tenant_or_404(tenant_id: str) -> Dict[str, Any]:
    tenants = load_tenants()
    tid = tenant_id.strip().lower()
    if tid not in tenants:
        raise HTTPException(status_code=404, detail=f"Unknown tenant_id: {tid}")
    t = tenants[tid]
    if not t.get("active_bool", True):
        raise HTTPException(status_code=400, detail=f"Tenant inactive: {tid}")
    return t


def get_orders_sheet_id_or_400(tenant: Dict[str, Any]) -> str:
    sid = norm(tenant.get("orders_sheet_id"))
    if not sid:
        raise HTTPException(status_code=400, detail=f"orders_sheet_id missing for tenant: {tenant.get('tenant_id')}")
    return sid


# =========================
# ENDPOINTS
# =========================
@app.get("/")
def root():
    return {"status": "ok", "service": "proyecto-reservas", "ts": utc_now_iso()}


@app.get("/debug/tenants")
def debug_tenants():
    tenants = load_tenants()
    sample_key = sorted(list(tenants.keys()))[0] if tenants else None
    sample = tenants.get(sample_key) if sample_key else None
    return {"ok": True, "count": len(tenants), "tenant_ids": sorted(list(tenants.keys())), "sample": sample}


@app.get("/menu")
def get_menu(tenant_id: str):
    tenant = get_tenant_or_404(tenant_id)

    # Si esto vuelve a fallar, será porque orders_enabled_bool quedó False
    if not tenant.get("orders_enabled_bool", False):
        raise HTTPException(status_code=400, detail=f"Orders not enabled for tenant: {tenant_id}")

    orders_sheet_id = get_orders_sheet_id_or_400(tenant)
    sh_orders = open_sheet_by_id(orders_sheet_id)

    try:
        ws_menu = sh_orders.worksheet(TAB_MENU)
    except Exception:
        raise HTTPException(status_code=404, detail=f"Menu tab not found in tenant sheet: {TAB_MENU}")

    # Menu: headers fila 1, fila 2 descriptiva, datos reales desde fila 3
    rows = ws_read_records(ws_menu, header_row=1, start_row=3)

    items = []
    for r in rows:
        if parse_bool(r.get("active")):
            items.append(r)

    return {"ok": True, "tenant_id": tenant_id, "count": len(items), "items": items}


@app.post("/orders/create")
def create_order(payload: Dict[str, Any]):
    tenant_id = norm(payload.get("tenant_id")).lower()
    if not tenant_id:
        raise HTTPException(status_code=400, detail="tenant_id is required")

    tenant = get_tenant_or_404(tenant_id)
    if not tenant.get("orders_enabled_bool", False):
        raise HTTPException(status_code=400, detail=f"Orders not enabled for tenant: {tenant_id}")

    orders_sheet_id = get_orders_sheet_id_or_400(tenant)
    sh_orders = open_sheet_by_id(orders_sheet_id)

    try:
        ws_orders = sh_orders.worksheet(TAB_ORDERS)
    except Exception:
        raise HTTPException(status_code=404, detail=f"Orders tab not found in tenant sheet: {TAB_ORDERS}")

    headers = ws_get_headers(ws_orders)
    if not headers or not any(headers):
        raise HTTPException(status_code=400, detail="Orders sheet has no headers in row 1")

    order_id = norm(payload.get("order_id")) or str(uuid.uuid4())[:8]

    record = {
        "order_id": order_id,
        "created_at": utc_now_iso(),
        "tenant_id": tenant_id,
        "customer_name": norm(payload.get("customer_name")),
        "customer_contact": norm(payload.get("customer_contact")),
        "items": norm(payload.get("items")),  # texto o json string
        "notes": norm(payload.get("notes")),
        "delivery_type": norm(payload.get("delivery_type")) or "pickup",
        "requested_time": norm(payload.get("requested_time")),
        "status": "PENDING_PAYMENT",
        "source": norm(payload.get("source")) or "telegram",
        "total_amount": norm(payload.get("total_amount")),
    }

    # Construimos la fila en el orden exacto de los headers
    row = [record.get(h.strip(), "") for h in headers]
    ws_orders.append_row(row, value_input_option="USER_ENTERED")

    return {"ok": True, "tenant_id": tenant_id, "order_id": order_id, "status": "PENDING_PAYMENT"}


@app.post("/orders/mark_paid")
def mark_order_paid(payload: Dict[str, Any]):
    tenant_id = norm(payload.get("tenant_id")).lower()
    order_id = norm(payload.get("order_id"))
    if not tenant_id or not order_id:
        raise HTTPException(status_code=400, detail="tenant_id and order_id are required")

    tenant = get_tenant_or_404(tenant_id)
    if not tenant.get("orders_enabled_bool", False):
        raise HTTPException(status_code=400, detail=f"Orders not enabled for tenant: {tenant_id}")

    orders_sheet_id = get_orders_sheet_id_or_400(tenant)
    sh_orders = open_sheet_by_id(orders_sheet_id)

    try:
        ws_orders = sh_orders.worksheet(TAB_ORDERS)
    except Exception:
        raise HTTPException(status_code=404, detail=f"Orders tab not found in tenant sheet: {TAB_ORDERS}")

    values = ws_orders.get_all_values()
    if len(values) < 3:
        raise HTTPException(status_code=400, detail="Orders sheet has no data rows (expected row 3+)")

    headers = [h.strip() for h in values[0]]
    col_order_id = find_col_index(headers, "order_id")
    col_status = find_col_index(headers, "status")

    if col_order_id == -1:
        raise HTTPException(status_code=400, detail="Orders sheet missing 'order_id' header in row 1")
    if col_status == -1:
        raise HTTPException(status_code=400, detail="Orders sheet missing 'status' header in row 1")

    # Orders: datos reales desde fila 3 (fila 2 es descriptiva)
    found_row_idx = -1
    for i in range(2, len(values)):  # i=2 corresponde a fila 3
        row = values[i]
        # aseguramos largo suficiente
        row_extended = row + [""] * (len(headers) - len(row))
        if row_extended[col_order_id - 1].strip() == order_id:
            found_row_idx = i + 1  # convertir índice de lista a fila real (1-based)
            break

    if found_row_idx == -1:
        raise HTTPException(status_code=404, detail=f"order_id not found: {order_id}")

    ws_orders.update_cell(found_row_idx, col_status, "PAID")
    return {"ok": True, "tenant_id": tenant_id, "order_id": order_id, "new_status": "PAID"}
