import os
import json
import re
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

import gspread
from google.oauth2.service_account import Credentials


# -----------------------------
# FastAPI app
# -----------------------------
app = FastAPI(title="proyecto-reservas", version="1.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]

GOOGLE_SHEET_ID_RE = re.compile(r"^[a-zA-Z0-9-_]{20,}$")


# -----------------------------
# Models
# -----------------------------
class OrderItem(BaseModel):
    sku: str
    qty: int = Field(ge=1, le=99)


class CreateOrderRequest(BaseModel):
    tenant_id: str
    customer_name: str
    customer_contact: str
    items: List[OrderItem]
    delivery_type: str = "pickup"  # pickup / delivery (texto libre para demo)
    requested_time: str = "ahora"
    notes: str = ""
    source: str = "api"


class CreateOrderResponse(BaseModel):
    ok: bool
    tenant_id: str
    order_id: str
    total_amount: float
    currency: str = "BOB"
    status: str = "PENDING_PAYMENT"


class MarkPaidRequest(BaseModel):
    tenant_id: str
    order_id: str


class MarkPaidResponse(BaseModel):
    ok: bool
    tenant_id: str
    order_id: str
    new_status: str


# -----------------------------
# Helpers: normalization + sheet reading
# -----------------------------
def normalize(s: Any) -> str:
    if s is None:
        return ""
    return str(s).strip()


def detect_header_row(values: List[List[Any]], required_headers: List[str], max_scan: int = 20) -> int:
    """
    Finds the header row index (0-based) where all required_headers exist (case-insensitive).
    """
    req = [h.strip().lower() for h in required_headers]
    for i in range(min(max_scan, len(values))):
        row = [str(x).strip().lower() for x in values[i]]
        if all(h in row for h in req):
            return i
    return 0


def read_records_manual(ws: gspread.Worksheet, required_headers: List[str]) -> List[Dict[str, Any]]:
    """
    Reads all rows, detects header row automatically, returns list of dicts.
    """
    values = ws.get_all_values()
    if not values:
        return []

    header_idx = detect_header_row(values, required_headers=required_headers)
    header = [str(h).strip() for h in values[header_idx]]
    rows = values[header_idx + 1 :]

    records: List[Dict[str, Any]] = []
    for r in rows:
        if not any(str(x).strip() for x in r):
            continue
        obj: Dict[str, Any] = {}
        for c_idx, key in enumerate(header):
            if not key:
                continue
            obj[key] = r[c_idx] if c_idx < len(r) else ""
        records.append(obj)
    return records


# -----------------------------
# Google auth + config loading
# -----------------------------
def get_gspread_client() -> gspread.Client:
    creds_json = os.getenv("GCP_CREDENTIALS_JSON")
    if not creds_json:
        raise RuntimeError("Missing env var: GCP_CREDENTIALS_JSON")

    try:
        info = json.loads(creds_json)
    except Exception as ex:
        raise RuntimeError("GCP_CREDENTIALS_JSON is not valid JSON") from ex

    creds = Credentials.from_service_account_info(info, scopes=SCOPES)
    return gspread.Client(auth=creds)


def get_config_identifier() -> str:
    """
    Returns the spreadsheet ID or spreadsheet title for the config spreadsheet.
    Accepts multiple env var names (case-sensitive, so we check many).
    """
    candidates = [
        "RESERVACIONES_CONFIG",
        "reservaciones_config",
        "TENANTS_SHEET_ID",
        "tenants_sheet_id",
        "TENANTS_CONFIG",
        "tenants_config",
    ]
    for k in candidates:
        v = os.getenv(k)
        if v and v.strip():
            return v.strip()

    raise RuntimeError(
        "Missing env var: RESERVACIONES_CONFIG (or fallback TENANTS_SHEET_ID / reservaciones_config etc.)"
    )


def open_spreadsheet(gc: gspread.Client, identifier: str) -> gspread.Spreadsheet:
    """
    Opens spreadsheet by ID if it looks like an ID, otherwise opens by title (name).
    """
    if GOOGLE_SHEET_ID_RE.match(identifier):
        # Treat as spreadsheet ID
        return gc.open_by_key(identifier)
    # Treat as spreadsheet title (name)
    return gc.open(identifier)


def load_tenant_row(gc: gspread.Client, config_sh: gspread.Spreadsheet, tenant_id: str) -> Dict[str, Any]:
    """
    Reads config spreadsheet -> worksheet "Tenants" -> find tenant_id row.
    Required headers: tenant_id, orders_sheet_id
    """
    try:
        ws = config_sh.worksheet("Tenants")
    except Exception:
        raise RuntimeError("Config spreadsheet missing worksheet: Tenants")

    rows = read_records_manual(ws, required_headers=["tenant_id"])
    tid_norm = tenant_id.strip()

    for r in rows:
        if normalize(r.get("tenant_id")) == tid_norm:
            return r

    raise HTTPException(status_code=404, detail=f"tenant_id not found in Tenants: {tenant_id}")


def get_orders_sheet_id(tenant_row: Dict[str, Any]) -> str:
    sid = normalize(tenant_row.get("orders_sheet_id"))
    if not sid:
        raise HTTPException(
            status_code=400,
            detail="Tenant is missing orders_sheet_id in Tenants sheet",
        )
    return sid


def get_bool(v: Any) -> bool:
    s = normalize(v).lower()
    return s in ("true", "1", "yes", "y", "si", "sí")


def load_menu_price_map(orders_sh: gspread.Spreadsheet) -> Dict[str, float]:
    """
    Reads orders spreadsheet -> worksheet "Menu"
    Headers expected: sku, name, price, active, category
    Uses only rows with active == TRUE
    """
    try:
        ws = orders_sh.worksheet("Menu")
    except Exception:
        # Some users use "Menus" - but your screenshot shows "Menu"
        try:
            ws = orders_sh.worksheet("Menus")
        except Exception:
            raise HTTPException(status_code=500, detail="Orders spreadsheet missing Menu worksheet")

    rows = read_records_manual(ws, required_headers=["sku", "price", "active"])
    price_map: Dict[str, float] = {}

    for r in rows:
        sku = normalize(r.get("sku"))
        if not sku:
            continue
        active = normalize(r.get("active")).upper() == "TRUE"
        if not active:
            continue

        price_raw = normalize(r.get("price"))
        try:
            price = float(price_raw.replace(",", "."))
        except Exception:
            # Skip invalid prices
            continue

        price_map[sku] = price

    return price_map


def compute_total_amount(items: List[OrderItem], price_map: Dict[str, float]) -> float:
    total = 0.0
    for it in items:
        if it.sku not in price_map:
            raise HTTPException(status_code=400, detail=f"Unknown SKU (not in active Menu): {it.sku}")
        total += price_map[it.sku] * int(it.qty)
    # round to 2 decimals
    return float(f"{total:.2f}")


def ensure_orders_headers(ws: gspread.Worksheet) -> None:
    """
    Ensures the Orders sheet has the needed headers.
    Your screenshot shows these headers already:
    order_id, created_at, tenant_id, customer_name, customer_contact, items, notes,
    delivery_type, requested_time, status, source, total_amount
    We'll not rewrite if present.
    """
    required = [
        "order_id",
        "created_at",
        "tenant_id",
        "customer_name",
        "customer_contact",
        "items",
        "notes",
        "delivery_type",
        "requested_time",
        "status",
        "source",
        "total_amount",
    ]
    values = ws.get_all_values()
    if not values:
        ws.append_row(required, value_input_option="RAW")
        return

    header_idx = detect_header_row(values, required_headers=["order_id", "tenant_id"], max_scan=5)
    header = [str(x).strip() for x in values[header_idx]]
    header_lower = [h.lower() for h in header]

    if all(h in header_lower for h in required):
        return

    # If header exists but missing some columns, we append missing columns to the right.
    missing = [h for h in required if h not in header_lower]
    if missing:
        # update header row cells
        new_header = header + missing
        ws.update(f"A{header_idx+1}", [new_header])


def find_order_row_index(ws: gspread.Worksheet, order_id: str) -> Optional[int]:
    """
    Returns 1-based row number (Google Sheets row index) where order_id matches.
    Searches in column A (assumes order_id is in first column).
    """
    col = ws.col_values(1)  # column A
    for i, v in enumerate(col, start=1):
        if normalize(v) == order_id:
            return i
    return None


# -----------------------------
# Routes
# -----------------------------
@app.get("/")
def root():
    return {"ok": True, "service": "proyecto-reservas"}


@app.get("/menu")
def get_menu(tenant_id: str = Query(...)):
    """
    Returns menu grouped by category for the tenant.
    Reads tenant config from reservaciones_config (Tenants tab),
    then opens orders spreadsheet and reads Menu worksheet.
    """
    gc = get_gspread_client()
    cfg_identifier = get_config_identifier()

    try:
        config_sh = open_spreadsheet(gc, cfg_identifier)
    except Exception as ex:
        raise HTTPException(
            status_code=500,
            detail=f"Cannot open config spreadsheet from env ({cfg_identifier}). "
                   f"Make sure it's shared with the service account and the value is correct (ID or title).",
        ) from ex

    tenant_row = load_tenant_row(gc, config_sh, tenant_id)
    orders_sheet_id = get_orders_sheet_id(tenant_row)

    try:
        orders_sh = gc.open_by_key(orders_sheet_id)
    except Exception as ex:
        raise HTTPException(
            status_code=500,
            detail=f"Cannot open orders spreadsheet for tenant. orders_sheet_id={orders_sheet_id}. "
                   f"Share this spreadsheet with the service account.",
        ) from ex

    # Read Menu rows
    try:
        menu_ws = orders_sh.worksheet("Menu")
    except Exception:
        try:
            menu_ws = orders_sh.worksheet("Menus")
        except Exception as ex:
            raise HTTPException(status_code=500, detail="Orders sheet missing Menu worksheet") from ex

    rows = read_records_manual(menu_ws, required_headers=["sku", "name", "price", "active", "category"])

    items: List[Dict[str, Any]] = []
    for r in rows:
        sku = normalize(r.get("sku"))
        if not sku:
            continue
        if normalize(r.get("active")).upper() != "TRUE":
            continue

        price_raw = normalize(r.get("price"))
        try:
            price = float(price_raw.replace(",", "."))
        except Exception:
            continue

        items.append(
            {
                "sku": sku,
                "name": normalize(r.get("name")),
                "price": price,
                "category": normalize(r.get("category")) or "Otros",
            }
        )

    # Group by category (nice for Telegram buttons)
    grouped: Dict[str, List[Dict[str, Any]]] = {}
    for it in items:
        grouped.setdefault(it["category"], []).append(it)

    return {"ok": True, "tenant_id": tenant_id, "categories": grouped}


@app.post("/orders/create", response_model=CreateOrderResponse)
def create_order(payload: CreateOrderRequest):
    """
    Creates an order in tenant orders spreadsheet -> worksheet "Orders".
    Computes total_amount from Menu sheet prices (active items only).
    """
    gc = get_gspread_client()
    cfg_identifier = get_config_identifier()

    try:
        config_sh = open_spreadsheet(gc, cfg_identifier)
    except Exception as ex:
        raise HTTPException(
            status_code=500,
            detail=f"Cannot open config spreadsheet from env ({cfg_identifier}). "
                   f"Share it with service account and verify env var value.",
        ) from ex

    tenant_row = load_tenant_row(gc, config_sh, payload.tenant_id)

    # Optional flag in Tenants: orders_enabled
    if "orders_enabled" in tenant_row and not get_bool(tenant_row.get("orders_enabled")):
        raise HTTPException(status_code=403, detail="Orders disabled for this tenant")

    orders_sheet_id = get_orders_sheet_id(tenant_row)

    try:
        orders_sh = gc.open_by_key(orders_sheet_id)
    except Exception as ex:
        raise HTTPException(
            status_code=500,
            detail=f"Cannot open orders spreadsheet for tenant. orders_sheet_id={orders_sheet_id}. "
                   f"Share this spreadsheet with the service account.",
        ) from ex

    # Build price map from Menu
    price_map = load_menu_price_map(orders_sh)
    total_amount = compute_total_amount(payload.items, price_map)

    # Open Orders worksheet
    try:
        orders_ws = orders_sh.worksheet("Orders")
    except Exception:
        # fallback
        try:
            orders_ws = orders_sh.worksheet("ORDERS")
        except Exception as ex:
            raise HTTPException(status_code=500, detail="Orders spreadsheet missing Orders worksheet") from ex

    ensure_orders_headers(orders_ws)

    # Create order_id
    order_id = os.urandom(4).hex()  # 8 chars

    created_at = datetime.now(timezone.utc).isoformat()

    items_json = json.dumps([{"sku": it.sku, "qty": it.qty} for it in payload.items], ensure_ascii=False)

    row = [
        order_id,
        created_at,
        payload.tenant_id,
        payload.customer_name,
        payload.customer_contact,
        items_json,
        payload.notes,
        payload.delivery_type,
        payload.requested_time,
        "PENDING_PAYMENT",
        payload.source,
        total_amount,
    ]

    orders_ws.append_row(row, value_input_option="RAW")

    return CreateOrderResponse(
        ok=True,
        tenant_id=payload.tenant_id,
        order_id=order_id,
        total_amount=total_amount,
        status="PENDING_PAYMENT",
    )


@app.post("/orders/mark_paid", response_model=MarkPaidResponse)
def mark_paid(payload: MarkPaidRequest):
    """
    Updates an existing order row status -> PAID.
    """
    gc = get_gspread_client()
    cfg_identifier = get_config_identifier()

    try:
        config_sh = open_spreadsheet(gc, cfg_identifier)
    except Exception as ex:
        raise HTTPException(
            status_code=500,
            detail=f"Cannot open config spreadsheet from env ({cfg_identifier}).",
        ) from ex

    tenant_row = load_tenant_row(gc, config_sh, payload.tenant_id)
    orders_sheet_id = get_orders_sheet_id(tenant_row)

    try:
        orders_sh = gc.open_by_key(orders_sheet_id)
    except Exception as ex:
        raise HTTPException(status_code=500, detail="Cannot open tenant orders spreadsheet") from ex

    try:
        orders_ws = orders_sh.worksheet("Orders")
    except Exception:
        try:
            orders_ws = orders_sh.worksheet("ORDERS")
        except Exception as ex:
            raise HTTPException(status_code=500, detail="Orders spreadsheet missing Orders worksheet") from ex

    ensure_orders_headers(orders_ws)

    row_idx = find_order_row_index(orders_ws, payload.order_id)
    if not row_idx:
        raise HTTPException(status_code=404, detail=f"order_id not found: {payload.order_id}")

    # Find "status" column index by reading header row
    values = orders_ws.get_all_values()
    if not values:
        raise HTTPException(status_code=500, detail="Orders sheet is empty")

    header_idx = detect_header_row(values, required_headers=["order_id", "status"], max_scan=5)
    header = [str(x).strip().lower() for x in values[header_idx]]

    try:
        status_col = header.index("status") + 1  # 1-based
    except ValueError:
        raise HTTPException(status_code=500, detail="Orders sheet missing 'status' column")

    orders_ws.update_cell(row_idx, status_col, "PAID")

    return MarkPaidResponse(ok=True, tenant_id=payload.tenant_id, order_id=payload.order_id, new_status="PAID")
