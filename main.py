import os
import json
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

import gspread
from google.oauth2.service_account import Credentials


# =========================
# CONFIG
# =========================

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]

# Tenants demo (edita aquí los Sheet IDs reales por tenant)
TENANTS: Dict[str, Dict[str, Any]] = {
    "resto_demo": {
        "tenant_id": "resto_demo",
        "name": "Resto Demo",
        "business_type": "restaurant",
        # Sheet con pestañas: "Menu" y "Orders"
        # (en tu caso se llama orders_resto_demo, pero el ID es lo importante)
        "orders_sheet_id": "IRVsxDn7kPx_OVDvs99Gn0T3G7ia7NO5juAjS9MGofd4",
        "timezone": "America/La_Paz",
        "active": "TRUE",
        "orders_enabled": "TRUE",
        "bookings_enabled": "TRUE",
        "admin_chat_id": "",
        "admin_whatsapp": "",
        "bot_token": "",
        "webhook_secret": "",
    },
    "salon_demo": {
        "tenant_id": "salon_demo",
        "name": "Salon Demo",
        "business_type": "salon",
        "orders_sheet_id": "",
        "timezone": "America/La_Paz",
        "active": "TRUE",
        "orders_enabled": "FALSE",
        "bookings_enabled": "TRUE",
        "admin_chat_id": "",
        "admin_whatsapp": "",
        "bot_token": "",
        "webhook_secret": "",
    },
    "spa_demo": {
        "tenant_id": "spa_demo",
        "name": "Spa Demo",
        "business_type": "spa",
        "orders_sheet_id": "",
        "timezone": "America/La_Paz",
        "active": "TRUE",
        "orders_enabled": "FALSE",
        "bookings_enabled": "TRUE",
        "admin_chat_id": "",
        "admin_whatsapp": "",
        "bot_token": "",
        "webhook_secret": "",
    },
}


# =========================
# FASTAPI
# =========================

app = FastAPI(title="Proyecto Reservas + Orders API", version="1.0.0")


# =========================
# MODELS
# =========================

class OrderItemIn(BaseModel):
    sku: str
    qty: int = Field(..., ge=1, le=50)


class CreateOrderIn(BaseModel):
    tenant_id: str
    customer_name: str
    customer_contact: str
    delivery_type: str = Field(..., description="pickup o delivery (texto libre permitido)")
    requested_time: str = Field(..., description="Texto libre, ej: 'ahora' o '19:30'")
    items: List[OrderItemIn]
    notes: str = ""
    source: str = "api"


class CreateOrderOut(BaseModel):
    ok: bool
    tenant_id: str
    order_id: str
    status: str
    total_amount: float


class MarkPaidIn(BaseModel):
    tenant_id: str
    order_id: str


class MarkPaidOut(BaseModel):
    ok: bool
    tenant_id: str
    order_id: str
    new_status: str


# =========================
# HELPERS - AUTH / CLIENT
# =========================

def get_gspread_client() -> gspread.client.Client:
    creds_json = os.getenv("GCP_CREDENTIALS_JSON", "").strip()
    if not creds_json:
        raise RuntimeError("Missing env var GCP_CREDENTIALS_JSON")

    try:
        info = json.loads(creds_json)
    except Exception as e:
        raise RuntimeError(f"Invalid GCP_CREDENTIALS_JSON: {e}")

    creds = Credentials.from_service_account_info(info, scopes=SCOPES)
    return gspread.Client(auth=creds)


# =========================
# HELPERS - TENANTS
# =========================

def get_tenant(tenant_id: str) -> Dict[str, Any]:
    t = TENANTS.get(tenant_id)
    if not t:
        raise HTTPException(status_code=404, detail=f"Unknown tenant_id: {tenant_id}")
    if str(t.get("active", "TRUE")).upper() != "TRUE":
        raise HTTPException(status_code=400, detail=f"Tenant inactive: {tenant_id}")
    return t


def ensure_orders_enabled(tenant: Dict[str, Any]) -> None:
    if str(tenant.get("orders_enabled", "FALSE")).upper() != "TRUE":
        raise HTTPException(status_code=400, detail=f"Orders not enabled for tenant: {tenant['tenant_id']}")
    if not tenant.get("orders_sheet_id"):
        raise HTTPException(status_code=400, detail=f"orders_sheet_id not configured for tenant: {tenant['tenant_id']}")


# =========================
# HELPERS - SHEETS PARSING
# =========================

REQUIRED_MENU_HEADERS = ["sku", "name", "price", "active", "category"]
REQUIRED_ORDERS_HEADERS = [
    "order_id", "created_at", "tenant_id", "customer_name", "customer_contact",
    "items", "notes", "delivery_type", "requested_time", "status", "source", "total_amount"
]

def normalize(s: Any) -> str:
    if s is None:
        return ""
    s = str(s).strip().lower()
    # normalización simple suficiente para headers técnicos exactos
    return s


def detect_header_row(values: List[List[Any]], required_headers: List[str], max_scan: int = 10) -> int:
    """
    Devuelve el índice (0-based) de la fila que contiene todos los required_headers.
    """
    req = set(required_headers)
    for i, row in enumerate(values[:max_scan]):
        norm = [normalize(x) for x in row]
        if req.issubset(set(norm)):
            return i
    return -1


def read_records_manual(ws: gspread.Worksheet, required_headers: List[str]) -> List[Dict[str, Any]]:
    values = ws.get_all_values()
    if not values:
        return []

    header_idx = detect_header_row(values, required_headers)
    if header_idx == -1:
        raise HTTPException(
            status_code=500,
            detail=f"Header row not found in worksheet '{ws.title}'. Required: {required_headers}"
        )

    headers = [normalize(h) for h in values[header_idx]]
    header_pos = {h: idx for idx, h in enumerate(headers) if h}

    records: List[Dict[str, Any]] = []
    for row in values[header_idx + 1:]:
        if not any(str(c).strip() for c in row):
            continue
        rec: Dict[str, Any] = {}
        for h in required_headers:
            idx = header_pos.get(h)
            rec[h] = row[idx] if idx is not None and idx < len(row) else ""
        records.append(rec)
    return records


def get_worksheet_by_title_or_first(sh: gspread.Spreadsheet, title: str) -> gspread.Worksheet:
    try:
        return sh.worksheet(title)
    except Exception:
        # fallback: primera hoja
        return sh.get_worksheet(0)


# =========================
# BUSINESS LOGIC - MENU
# =========================

def parse_price_to_float(p: Any) -> float:
    """
    Acepta '32', '32.5', '32,5', 'Bs 32' (intentando limpiar).
    """
    if p is None:
        return 0.0
    s = str(p).strip()
    s = s.replace("Bs", "").replace("bs", "").replace(" ", "")
    s = s.replace(",", ".")
    try:
        return float(s)
    except:
        return 0.0


def load_menu_items(client: gspread.client.Client, tenant: Dict[str, Any]) -> List[Dict[str, Any]]:
    sh = client.open_by_key(tenant["orders_sheet_id"])
    ws = get_worksheet_by_title_or_first(sh, "Menu")
    records = read_records_manual(ws, REQUIRED_MENU_HEADERS)

    # Importante: solo activos (TRUE). La fila 2 en español NO debe tener active=TRUE.
    items = []
    for r in records:
        active = str(r.get("active", "")).strip().upper()
        if active == "TRUE":
            items.append({
                "sku": str(r.get("sku", "")).strip(),
                "name": str(r.get("name", "")).strip(),
                "price": str(r.get("price", "")).strip(),
                "active": "TRUE",
                "category": str(r.get("category", "")).strip(),
            })
    return items


def build_price_map(menu_items: List[Dict[str, Any]]) -> Dict[str, float]:
    mp: Dict[str, float] = {}
    for it in menu_items:
        sku = str(it.get("sku", "")).strip()
        mp[sku] = parse_price_to_float(it.get("price", "0"))
    return mp


def calc_total_amount(items: List[OrderItemIn], price_map: Dict[str, float]) -> float:
    total = 0.0
    for it in items:
        if it.sku not in price_map:
            raise HTTPException(status_code=400, detail=f"Unknown sku in order: {it.sku}")
        total += price_map[it.sku] * int(it.qty)
    # redondeo a 2 decimales (puedes cambiarlo)
    return round(total, 2)


# =========================
# BUSINESS LOGIC - ORDERS
# =========================

def ensure_orders_sheet_headers(ws: gspread.Worksheet) -> None:
    """
    Si la hoja está vacía, crea headers técnicos en fila 1.
    Si ya existe, valida que estén (de forma flexible) detectándolos.
    """
    values = ws.get_all_values()
    if not values:
        ws.append_row(REQUIRED_ORDERS_HEADERS)
        return

    idx = detect_header_row(values, REQUIRED_ORDERS_HEADERS)
    if idx == -1:
        # Si hay algo pero no hay headers técnicos detectables, no tocamos para no romper.
        raise HTTPException(
            status_code=500,
            detail=f"Orders sheet '{ws.title}' missing required headers: {REQUIRED_ORDERS_HEADERS}"
        )


def append_order_row(
    client: gspread.client.Client,
    tenant: Dict[str, Any],
    order_id: str,
    payload: CreateOrderIn,
    status: str,
    total_amount: float,
) -> None:
    sh = client.open_by_key(tenant["orders_sheet_id"])
    ws = get_worksheet_by_title_or_first(sh, "Orders")
    ensure_orders_sheet_headers(ws)

    created_at = datetime.now(timezone.utc).isoformat()

    # Items en texto (como tu ejemplo)
    items_text = str([{"sku": it.sku, "qty": it.qty} for it in payload.items])

    row = [
        order_id,
        created_at,
        payload.tenant_id,
        payload.customer_name,
        payload.customer_contact,
        items_text,
        payload.notes,
        payload.delivery_type,
        payload.requested_time,
        status,
        payload.source,
        str(total_amount),
    ]
    ws.append_row(row, value_input_option="RAW")


def find_order_row_index(ws: gspread.Worksheet, order_id: str) -> Optional[int]:
    """
    Busca order_id y devuelve el índice de fila (1-based en Sheets).
    """
    values = ws.get_all_values()
    if not values:
        return None

    header_idx = detect_header_row(values, REQUIRED_ORDERS_HEADERS)
    if header_idx == -1:
        return None

    headers = [normalize(h) for h in values[header_idx]]
    try:
        col_order_id = headers.index("order_id") + 1  # 1-based
        # Buscar desde header+2 hacia abajo
        for i in range(header_idx + 2, len(values) + 1):
            cell_val = ws.cell(i, col_order_id).value
            if str(cell_val).strip() == order_id:
                return i
    except:
        return None
    return None


def update_order_status(
    client: gspread.client.Client,
    tenant: Dict[str, Any],
    order_id: str,
    new_status: str,
) -> None:
    sh = client.open_by_key(tenant["orders_sheet_id"])
    ws = get_worksheet_by_title_or_first(sh, "Orders")

    values = ws.get_all_values()
    if not values:
        raise HTTPException(status_code=404, detail=f"Order not found: {order_id}")

    header_idx = detect_header_row(values, REQUIRED_ORDERS_HEADERS)
    if header_idx == -1:
        raise HTTPException(status_code=500, detail="Orders headers not found")

    headers = [normalize(h) for h in values[header_idx]]
    if "status" not in headers:
        raise HTTPException(status_code=500, detail="Orders sheet missing 'status' column")

    row_idx = find_order_row_index(ws, order_id)
    if not row_idx:
        raise HTTPException(status_code=404, detail=f"Order not found: {order_id}")

    col_status = headers.index("status") + 1
    ws.update_cell(row_idx, col_status, new_status)


# =========================
# ROUTES
# =========================

@app.get("/")
def root():
    return {"ok": True, "service": "proyecto-reservas", "version": "1.0.0"}


@app.get("/debug/tenants")
def debug_tenants():
    tenant_ids = list(TENANTS.keys())
    sample = TENANTS[tenant_ids[0]] if tenant_ids else {}
    # añade flags bool para debug
    sample_out = dict(sample)
    sample_out["active_bool"] = str(sample_out.get("active", "FALSE")).upper() == "TRUE"
    sample_out["orders_enabled_bool"] = str(sample_out.get("orders_enabled", "FALSE")).upper() == "TRUE"
    sample_out["bookings_enabled_bool"] = str(sample_out.get("bookings_enabled", "FALSE")).upper() == "TRUE"
    return {"ok": True, "count": len(tenant_ids), "tenant_ids": tenant_ids, "sample": sample_out}


@app.get("/menu")
def get_menu(tenant_id: str):
    tenant = get_tenant(tenant_id)
    ensure_orders_enabled(tenant)

    client = get_gspread_client()
    items = load_menu_items(client, tenant)
    return {"ok": True, "tenant_id": tenant_id, "count": len(items), "items": items}


@app.post("/orders/create", response_model=CreateOrderOut)
def create_order(payload: CreateOrderIn):
    tenant = get_tenant(payload.tenant_id)
    ensure_orders_enabled(tenant)

    client = get_gspread_client()

    # 1) cargar menú + mapa de precios
    menu_items = load_menu_items(client, tenant)
    price_map = build_price_map(menu_items)

    # 2) calcular total_amount
    total_amount = calc_total_amount(payload.items, price_map)

    # 3) crear order_id + status
    order_id = uuid.uuid4().hex[:7]
    status = "PENDING_PAYMENT"

    # 4) guardar en Orders (incluye total_amount)
    append_order_row(client, tenant, order_id, payload, status, total_amount)

    return CreateOrderOut(
        ok=True,
        tenant_id=payload.tenant_id,
        order_id=order_id,
        status=status,
        total_amount=total_amount,
    )


@app.post("/orders/mark_paid", response_model=MarkPaidOut)
def mark_paid(payload: MarkPaidIn):
    tenant = get_tenant(payload.tenant_id)
    ensure_orders_enabled(tenant)

    client = get_gspread_client()
    update_order_status(client, tenant, payload.order_id, "PAID")

    return MarkPaidOut(ok=True, tenant_id=payload.tenant_id, order_id=payload.order_id, new_status="PAID")
