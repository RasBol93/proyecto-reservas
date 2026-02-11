import os
import json
import uuid
import re
import unicodedata
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel, Field

import gspread
from google.oauth2.service_account import Credentials


# ============================================================
# ENV
# ============================================================
# Debe ser el Spreadsheet ID del Google Sheet "reservaciones_config"
CONFIG_SPREADSHEET_ID = os.getenv("CONFIG_SPREADSHEET_ID", "").strip()

# Credenciales service account en JSON (string)
GCP_CREDENTIALS_JSON = os.getenv("GCP_CREDENTIALS_JSON", "").strip()

if not CONFIG_SPREADSHEET_ID:
    # No hacemos crash al importar, pero sí al primer request útil.
    print("WARN: CONFIG_SPREADSHEET_ID is empty. Set it in Render Environment.")
if not GCP_CREDENTIALS_JSON:
    print("WARN: GCP_CREDENTIALS_JSON is empty. Set it in Render Environment.")

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]

# Nombres de pestañas (según tus screenshots)
TENANTS_TAB = "Tenants"
CONTENT_TAB = "Content"
BOOKING_RULES_TAB = "BookingRules"
DEFAULTS_TAB = "Defaults"

ORDERS_TAB = "Orders"
MENU_TAB = "Menu"


# ============================================================
# HELPERS (normalización / lectura robusta)
# ============================================================
def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()

def normalize(s: str) -> str:
    if s is None:
        return ""
    s = str(s).strip().lower()
    s = unicodedata.normalize("NFD", s)
    s = "".join(ch for ch in s if unicodedata.category(ch) != "Mn")
    s = re.sub(r"[^\w\s-]", " ", s)  # saca puntuación
    s = re.sub(r"\s+", " ", s).strip()
    return s

def to_bool(val: Any) -> bool:
    if isinstance(val, bool):
        return val
    if val is None:
        return False
    s = str(val).strip().lower()
    return s in ("true", "1", "yes", "y", "si", "sí")

def to_number(val: Any) -> Optional[float]:
    if val is None:
        return None
    if isinstance(val, (int, float)):
        return float(val)
    s = str(val).strip()
    if not s:
        return None
    # soporta "25", "25.0", "25,5"
    s = s.replace(",", ".")
    try:
        return float(s)
    except:
        return None

def detect_header_row(values: List[List[Any]], required_headers: List[str], max_scan: int = 10) -> int:
    """
    Busca la fila de headers dentro de las primeras max_scan filas.
    Devuelve índice 0-based de la fila header.
    """
    req = [normalize(h) for h in required_headers]
    for i in range(min(max_scan, len(values))):
        row = [normalize(c) for c in values[i]]
        if all(h in row for h in req):
            return i
    return 0

def read_records_manual(ws, required_headers: List[str]) -> List[Dict[str, Any]]:
    """
    Lee una worksheet detectando la fila header, y devolviendo lista de dicts.
    """
    values = ws.get_all_values()
    if not values:
        return []
    header_idx = detect_header_row(values, required_headers=required_headers)
    headers = [normalize(h) for h in values[header_idx]]
    records = []
    for r in values[header_idx + 1:]:
        if not any(str(x).strip() for x in r):
            continue
        row_dict = {}
        for j, h in enumerate(headers):
            if not h:
                continue
            row_dict[h] = r[j] if j < len(r) else ""
        records.append(row_dict)
    return records

def find_row_index_by_key(ws, key_col_header: str, key_value: str) -> Optional[int]:
    """
    Busca la fila (1-based) donde la columna key_col_header == key_value.
    Detecta header.
    """
    values = ws.get_all_values()
    if not values:
        return None

    header_idx = detect_header_row(values, required_headers=[key_col_header])
    headers = [normalize(h) for h in values[header_idx]]
    try:
        key_col = headers.index(normalize(key_col_header))
    except ValueError:
        return None

    for i in range(header_idx + 1, len(values)):
        row = values[i]
        cell = row[key_col] if key_col < len(row) else ""
        if str(cell).strip() == str(key_value).strip():
            return i + 1  # 1-based for gspread
    return None

def update_cell_by_header(ws, row_1based: int, header_name: str, new_value: Any) -> None:
    values = ws.get_all_values()
    if not values:
        raise ValueError("Worksheet empty; cannot update.")
    header_idx = detect_header_row(values, required_headers=[header_name])
    headers = [normalize(h) for h in values[header_idx]]
    try:
        col_idx = headers.index(normalize(header_name)) + 1  # 1-based
    except ValueError:
        raise ValueError(f"Header not found: {header_name}")
    ws.update_cell(row_1based, col_idx, new_value)


# ============================================================
# GOOGLE CLIENT
# ============================================================
def get_gspread_client() -> gspread.Client:
    if not GCP_CREDENTIALS_JSON:
        raise HTTPException(status_code=500, detail="Missing GCP_CREDENTIALS_JSON in environment.")
    info = json.loads(GCP_CREDENTIALS_JSON)
    creds = Credentials.from_service_account_info(info, scopes=SCOPES)
    return gspread.Client(auth=creds)

def open_config_spreadsheet(gc: gspread.Client):
    if not CONFIG_SPREADSHEET_ID:
        raise HTTPException(status_code=500, detail="Missing CONFIG_SPREADSHEET_ID in environment.")
    try:
        return gc.open_by_key(CONFIG_SPREADSHEET_ID)
    except Exception as e:
        # 404 casi siempre es "no compartiste con service account" o id incorrecto
        raise HTTPException(status_code=500, detail=f"Cannot open CONFIG_SPREADSHEET_ID. Share sheet with service account. Error: {str(e)}")

def get_tenant_cfg(tenant_id: str) -> Dict[str, Any]:
    gc = get_gspread_client()
    cfg_sh = open_config_spreadsheet(gc)

    try:
        tenants_ws = cfg_sh.worksheet(TENANTS_TAB)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Config sheet missing tab '{TENANTS_TAB}'. Error: {str(e)}")

    # Importante: en tu screenshot la columna se llama orders_sheet_id
    required = ["tenant_id", "orders_sheet_id", "orders_enabled", "active"]
    rows = read_records_manual(tenants_ws, required_headers=required)

    tid = str(tenant_id).strip()
    for r in rows:
        if str(r.get("tenant_id", "")).strip() != tid:
            continue
        if not to_bool(r.get("active", "TRUE")):
            raise HTTPException(status_code=400, detail=f"Tenant inactive: {tenant_id}")
        return {
            "tenant_id": tid,
            "name": r.get("name", ""),
            "business_type": r.get("business_type", ""),
            "orders_sheet_id": str(r.get("orders_sheet_id", "")).strip(),
            "orders_enabled": to_bool(r.get("orders_enabled", "FALSE")),
            "bookings_enabled": to_bool(r.get("bookings_enabled", "FALSE")),
            "timezone": r.get("timezone", "America/La_Paz"),
            "admin_chat_id": r.get("admin_chat_id", ""),
            "admin_whatsapp": r.get("admin_whatsapp", ""),
        }

    raise HTTPException(status_code=404, detail=f"Tenant not found: {tenant_id}")

def open_orders_spreadsheet_for_tenant(tenant_id: str):
    cfg = get_tenant_cfg(tenant_id)
    if not cfg.get("orders_enabled", False):
        raise HTTPException(status_code=400, detail=f"Orders not enabled for tenant: {tenant_id}")

    orders_sheet_id = cfg.get("orders_sheet_id", "").strip()
    if not orders_sheet_id:
        raise HTTPException(
            status_code=500,
            detail=f"Tenant '{tenant_id}' has empty orders_sheet_id in '{TENANTS_TAB}' tab."
        )

    gc = get_gspread_client()
    try:
        sh = gc.open_by_key(orders_sheet_id)
    except Exception as e:
        raise HTTPException(
            status_code=500,
            detail=(
                f"Cannot open orders spreadsheet for tenant '{tenant_id}'. "
                f"Make sure you shared it with the service account. Error: {str(e)}"
            )
        )

    # Tabs según tu sheet: Orders y Menu
    try:
        orders_ws = sh.worksheet(ORDERS_TAB)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Orders sheet missing tab '{ORDERS_TAB}'. Error: {str(e)}")

    try:
        menu_ws = sh.worksheet(MENU_TAB)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Orders sheet missing tab '{MENU_TAB}'. Error: {str(e)}")

    return menu_ws, orders_ws


# ============================================================
# MODELS
# ============================================================
class MenuItem(BaseModel):
    sku: str
    name: str
    price: float
    active: bool = True
    category: str = ""

class OrderItem(BaseModel):
    sku: str
    qty: int = Field(..., ge=1)

class OrderCreateRequest(BaseModel):
    tenant_id: str
    customer_name: str
    customer_contact: str
    items: List[OrderItem]
    delivery_type: str = "pickup"
    requested_time: str = "ahora"
    notes: str = ""
    source: str = "api"

class OrderCreateResponse(BaseModel):
    ok: bool
    tenant_id: str
    order_id: str
    total_amount: float

class MarkPaidRequest(BaseModel):
    tenant_id: str
    order_id: str

class MarkPaidResponse(BaseModel):
    ok: bool
    tenant_id: str
    order_id: str
    new_status: str


# ============================================================
# BUSINESS: MENU + TOTAL CALC
# ============================================================
def load_menu_map(menu_ws) -> Dict[str, Dict[str, Any]]:
    """
    Devuelve un dict: sku -> {name, price, category, active}
    Lee de la pestaña Menu con headers: sku, name, price, active, category
    """
    required = ["sku", "name", "price", "active", "category"]
    rows = read_records_manual(menu_ws, required_headers=required)

    menu = {}
    for r in rows:
        sku = str(r.get("sku", "")).strip()
        if not sku:
            continue

        # active puede venir como TRUE/FALSE
        active = to_bool(r.get("active", "FALSE"))
        if not active:
            continue

        price = to_number(r.get("price"))
        if price is None:
            # si falta precio, lo ignoramos para no romper todo
            continue

        menu[sku] = {
            "sku": sku,
            "name": str(r.get("name", "")).strip(),
            "price": float(price),
            "category": str(r.get("category", "")).strip(),
            "active": True,
        }
    return menu

def calc_total_amount(order_items: List[OrderItem], menu_map: Dict[str, Dict[str, Any]]) -> float:
    total = 0.0
    for it in order_items:
        sku = it.sku.strip()
        if sku not in menu_map:
            raise HTTPException(status_code=400, detail=f"SKU not found in Menu or inactive: {sku}")
        price = float(menu_map[sku]["price"])
        total += price * int(it.qty)
    # redondeo simple para mostrar
    return round(total, 2)

def append_order_row(orders_ws, row: Dict[str, Any]) -> None:
    """
    Inserta una fila en Orders respetando headers existentes.
    Headers esperados según tu screenshot:
    order_id, created_at, tenant_id, customer_name, customer_contact,
    items, notes, delivery_type, requested_time, status, source, total_amount
    """
    values = orders_ws.get_all_values()
    if not values:
        raise HTTPException(status_code=500, detail="Orders worksheet is empty; missing headers.")

    header_idx = detect_header_row(values, required_headers=["order_id", "tenant_id", "status"])
    headers = [normalize(h) for h in values[header_idx]]

    def getv(h: str) -> Any:
        return row.get(normalize(h), "")

    out_row = []
    for h in headers:
        out_row.append(getv(h))

    orders_ws.append_row(out_row, value_input_option="USER_ENTERED")


# ============================================================
# FASTAPI
# ============================================================
app = FastAPI(title="Proyecto Reservas - Orders Demo")


@app.get("/")
def health():
    return {"status": "ok"}


@app.get("/menu")
def get_menu(tenant_id: str = Query(...)):
    menu_ws, _ = open_orders_spreadsheet_for_tenant(tenant_id)
    menu_map = load_menu_map(menu_ws)

    # formato simple para bot: categories + items
    categories: Dict[str, List[Dict[str, Any]]] = {}
    for sku, it in menu_map.items():
        cat = it.get("category", "") or "General"
        categories.setdefault(cat, []).append({
            "sku": it["sku"],
            "name": it["name"],
            "price": it["price"],
            "category": cat,
        })

    # ordenar por nombre dentro de categoría
    for cat in categories:
        categories[cat] = sorted(categories[cat], key=lambda x: x["name"].lower())

    return {
        "tenant_id": tenant_id,
        "categories": sorted(categories.keys()),
        "items_by_category": categories,
        "total_items": len(menu_map),
    }


@app.post("/orders/create", response_model=OrderCreateResponse)
def create_order(payload: OrderCreateRequest):
    tenant_id = payload.tenant_id.strip()

    menu_ws, orders_ws = open_orders_spreadsheet_for_tenant(tenant_id)
    menu_map = load_menu_map(menu_ws)

    total_amount = calc_total_amount(payload.items, menu_map)

    order_id = uuid.uuid4().hex[:8]
    created_at = now_iso()

    # Guardamos items como JSON string en la columna items
    items_json = json.dumps([{"sku": it.sku.strip(), "qty": int(it.qty)} for it in payload.items], ensure_ascii=False)

    row = {
        "order_id": order_id,
        "created_at": created_at,
        "tenant_id": tenant_id,
        "customer_name": payload.customer_name.strip(),
        "customer_contact": payload.customer_contact.strip(),
        "items": items_json,
        "notes": payload.notes.strip(),
        "delivery_type": payload.delivery_type.strip(),
        "requested_time": payload.requested_time.strip(),
        "status": "PENDING_PAYMENT",
        "source": payload.source.strip(),
        "total_amount": total_amount,
    }

    append_order_row(orders_ws, row)

    return OrderCreateResponse(ok=True, tenant_id=tenant_id, order_id=order_id, total_amount=total_amount)


@app.post("/orders/mark_paid", response_model=MarkPaidResponse)
def mark_paid(payload: MarkPaidRequest):
    tenant_id = payload.tenant_id.strip()
    order_id = payload.order_id.strip()

    _, orders_ws = open_orders_spreadsheet_for_tenant(tenant_id)

    row_idx = find_row_index_by_key(orders_ws, "order_id", order_id)
    if not row_idx:
        raise HTTPException(status_code=404, detail=f"Order not found: {order_id}")

    update_cell_by_header(orders_ws, row_idx, "status", "PAID")

    return MarkPaidResponse(ok=True, tenant_id=tenant_id, order_id=order_id, new_status="PAID")
