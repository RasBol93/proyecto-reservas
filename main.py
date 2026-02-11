import os
import json
import uuid
import re
import unicodedata
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

import gspread
from google.oauth2.service_account import Credentials
from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel, Field


# =========================
# Helpers: normalize / parsing
# =========================

def normalize(s: Any) -> str:
    if s is None:
        return ""
    s = str(s).strip().lower()
    s = unicodedata.normalize("NFD", s)
    s = "".join(ch for ch in s if unicodedata.category(ch) != "Mn")  # remove accents
    s = re.sub(r"[^\w\s-]", "", s)  # remove punctuation (keep words, spaces, -)
    s = re.sub(r"\s+", " ", s).strip()
    return s


def parse_bool(v: Any) -> bool:
    if isinstance(v, bool):
        return v
    s = normalize(v)
    return s in ("true", "1", "yes", "si", "sí", "y", "on")


def parse_int(v: Any, default: int = 0) -> int:
    try:
        if v is None or str(v).strip() == "":
            return default
        return int(float(str(v).strip()))
    except Exception:
        return default


def parse_float(v: Any) -> float:
    if v is None:
        raise ValueError("empty")
    s = str(v).strip()
    if s == "":
        raise ValueError("empty")
    # allow "25", "25.0", "25,0"
    s = s.replace(",", ".")
    return float(s)


# =========================
# Google Sheets reading helpers
# =========================

def detect_header_row(values: List[List[Any]], required_headers: List[str], scan_rows: int = 10) -> int:
    """
    Returns 0-based index of header row where all required_headers exist (normalized).
    Raises if not found.
    """
    required_norm = [normalize(h) for h in required_headers]
    for i in range(min(scan_rows, len(values))):
        row = values[i]
        row_norm = [normalize(x) for x in row]
        if all(h in row_norm for h in required_norm):
            return i
    raise RuntimeError(f"Header row not found. Required: {required_headers}")


def read_records_manual(ws: gspread.Worksheet, required_headers: List[str]) -> List[Dict[str, Any]]:
    """
    Reads a worksheet with possibly shifted headers:
    - Finds header row
    - Builds records from subsequent rows
    """
    values = ws.get_all_values()
    if not values:
        return []

    header_idx = detect_header_row(values, required_headers=required_headers)
    headers = values[header_idx]
    headers_norm = [normalize(h) for h in headers]

    # Map header -> col index using normalized names
    col_index: Dict[str, int] = {}
    for idx, h in enumerate(headers_norm):
        if h != "":
            col_index[h] = idx

    out: List[Dict[str, Any]] = []
    for r in values[header_idx + 1:]:
        if not any(str(x).strip() for x in r):
            continue
        rec: Dict[str, Any] = {}
        for h in col_index.keys():
            c = col_index[h]
            rec[h] = r[c] if c < len(r) else ""
        out.append(rec)
    return out


# =========================
# Config loading
# =========================

TENANTS_TAB = "Tenants"
CONTENT_TAB = "Content"
BOOKING_RULES_TAB = "BookingRules"
BOOKINGS_TAB = "Bookings"
DEFAULTS_TAB = "Defaults"

ORDERS_TAB = "Orders"
MENU_TAB = "Menu"

REQUIRED_TENANTS_HEADERS = ["tenant_id", "orders_sheet_id", "active"]
REQUIRED_MENU_HEADERS = ["sku", "name", "price", "active", "category"]


def get_gspread_client() -> gspread.Client:
    creds_json = os.getenv("GCP_CREDENTIALS_JSON")
    if not creds_json:
        raise RuntimeError("Missing env var: GCP_CREDENTIALS_JSON")

    try:
        info = json.loads(creds_json)
    except Exception as e:
        raise RuntimeError(f"GCP_CREDENTIALS_JSON is not valid JSON: {e}")

    scopes = [
        "https://www.googleapis.com/auth/spreadsheets",
        "https://www.googleapis.com/auth/drive",
    ]
    creds = Credentials.from_service_account_info(info, scopes=scopes)
    return gspread.Client(auth=creds)


def get_config_spreadsheet_id() -> str:
    # User decided to standardize on RESERVACIONES_CONFIG
    cfg = os.getenv("RESERVACIONES_CONFIG")
    if not cfg:
        raise RuntimeError("Missing env var: RESERVACIONES_CONFIG")
    return cfg.strip()


def load_tenants(gc: gspread.Client) -> Dict[str, Dict[str, Any]]:
    """
    Reads Tenants tab from config spreadsheet and returns dict by tenant_id.
    """
    config_id = get_config_spreadsheet_id()
    sh = gc.open_by_key(config_id)
    ws = sh.worksheet(TENANTS_TAB)

    rows = read_records_manual(ws, required_headers=REQUIRED_TENANTS_HEADERS)
    tenants: Dict[str, Dict[str, Any]] = {}
    for r in rows:
        tenant_id = str(r.get("tenant_id", "")).strip()
        if not tenant_id:
            continue
        active = parse_bool(r.get("active", "TRUE"))
        if not active:
            continue

        orders_sheet_id = str(r.get("orders_sheet_id", "")).strip()
        if not orders_sheet_id:
            continue

        tenants[tenant_id] = {
            "tenant_id": tenant_id,
            "orders_sheet_id": orders_sheet_id,
        }
    return tenants


def get_tenant_orders_spreadsheet(gc: gspread.Client, tenant_id: str) -> gspread.Spreadsheet:
    tenants = load_tenants(gc)
    if tenant_id not in tenants:
        raise HTTPException(status_code=404, detail=f"Unknown or inactive tenant_id: {tenant_id}")
    return gc.open_by_key(tenants[tenant_id]["orders_sheet_id"])


# =========================
# Menu + Total calculation
# =========================

def read_menu_map(orders_sh: gspread.Spreadsheet) -> Tuple[Dict[str, Dict[str, Any]], Dict[str, List[Dict[str, Any]]]]:
    """
    Returns:
    - menu_by_sku: {sku: {sku,name,price,category}}
    - categories: {category: [items]}
    Only includes active=TRUE items (and skips the Spanish row if active is not TRUE).
    """
    menu_ws = orders_sh.worksheet(MENU_TAB)
    rows = read_records_manual(menu_ws, required_headers=REQUIRED_MENU_HEADERS)

    menu_by_sku: Dict[str, Dict[str, Any]] = {}
    categories: Dict[str, List[Dict[str, Any]]] = {}

    for r in rows:
        sku = str(r.get("sku", "")).strip()
        name = str(r.get("name", "")).strip()
        category = str(r.get("category", "")).strip()

        try:
            active = parse_bool(r.get("active", "FALSE"))
        except Exception:
            active = False

        if not sku or not name or not category:
            continue
        if not active:
            continue

        try:
            price = parse_float(r.get("price", ""))
        except Exception:
            # If price is invalid, we skip the item (better than breaking the API)
            continue

        item = {"sku": sku, "name": name, "price": price, "category": category}
        menu_by_sku[sku] = item
        categories.setdefault(category, []).append(item)

    return menu_by_sku, categories


def compute_total_amount(items: List[Dict[str, Any]], menu_by_sku: Dict[str, Dict[str, Any]]) -> float:
    total = 0.0
    for it in items:
        sku = str(it.get("sku", "")).strip()
        qty = parse_int(it.get("qty", 0), default=0)
        if not sku or qty <= 0:
            continue
        if sku not in menu_by_sku:
            raise HTTPException(status_code=400, detail=f"Unknown SKU in items: {sku}")
        total += float(menu_by_sku[sku]["price"]) * qty
    return round(total, 2)


# =========================
# FastAPI models
# =========================

class OrderItem(BaseModel):
    sku: str = Field(..., examples=["H01"])
    qty: int = Field(..., ge=1, examples=[2])


class CreateOrderRequest(BaseModel):
    tenant_id: str
    customer_name: str
    customer_contact: str
    items: List[OrderItem]
    notes: Optional[str] = ""
    delivery_type: Optional[str] = "pickup"  # pickup | delivery
    requested_time: Optional[str] = "ahora"
    source: Optional[str] = "api"


class CreateOrderResponse(BaseModel):
    ok: bool
    order_id: str
    total_amount: float
    currency: str = "BOB"


class MarkPaidRequest(BaseModel):
    tenant_id: str
    order_id: str
    source: Optional[str] = "api"


# =========================
# App
# =========================

app = FastAPI(title="Proyecto Reservas API", version="1.0.0")


@app.get("/")
def root():
    # so Render healthchecks don't show 404
    return {"ok": True, "service": "proyecto-reservas"}


@app.get("/menu")
def get_menu(tenant_id: str = Query(..., description="Tenant id, e.g., resto_demo")):
    """
    Returns menu grouped by categories, reading from the tenant's orders spreadsheet tab 'Menu'.
    """
    try:
        gc = get_gspread_client()
        orders_sh = get_tenant_orders_spreadsheet(gc, tenant_id)
        _, categories = read_menu_map(orders_sh)

        # ensure stable ordering (optional)
        categories_sorted: Dict[str, List[Dict[str, Any]]] = {}
        for cat in sorted(categories.keys(), key=lambda x: x.lower()):
            categories_sorted[cat] = sorted(categories[cat], key=lambda x: x["name"].lower())

        return {"ok": True, "tenant_id": tenant_id, "categories": categories_sorted}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Internal error in /menu: {e}")


def ensure_orders_headers_and_get_ws(orders_sh: gspread.Spreadsheet) -> gspread.Worksheet:
    """
    Ensures Orders sheet exists and has headers (at least the ones we use).
    If your Orders sheet already exists with your headers, it will just use it.
    """
    try:
        ws = orders_sh.worksheet(ORDERS_TAB)
    except Exception:
        ws = orders_sh.add_worksheet(title=ORDERS_TAB, rows=1000, cols=20)

    # If empty, write headers
    values = ws.get_all_values()
    if not values:
        headers = [
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
        ws.append_row(headers)
    return ws


@app.post("/orders/create", response_model=CreateOrderResponse)
def create_order(payload: CreateOrderRequest):
    """
    Creates an order in the tenant Orders sheet and calculates total_amount from Menu prices.
    """
    try:
        gc = get_gspread_client()
        orders_sh = get_tenant_orders_spreadsheet(gc, payload.tenant_id)

        menu_by_sku, _ = read_menu_map(orders_sh)
        if not menu_by_sku:
            raise HTTPException(status_code=400, detail="Menu is empty or has no active items.")

        items_dicts = [it.model_dump() for it in payload.items]
        total_amount = compute_total_amount(items_dicts, menu_by_sku)

        order_id = uuid.uuid4().hex[:8]
        created_at = datetime.now(timezone.utc).isoformat()

        orders_ws = ensure_orders_headers_and_get_ws(orders_sh)

        # Find header positions to append correctly even if sheet has extra columns
        all_values = orders_ws.get_all_values()
        header_row = all_values[0] if all_values else []
        header_norm = [normalize(h) for h in header_row]

        def col_pos(header_name: str) -> Optional[int]:
            hn = normalize(header_name)
            return header_norm.index(hn) if hn in header_norm else None

        # Build row with dynamic size
        row_len = max(len(header_row), 12)
        row = [""] * row_len

        def setv(h: str, v: Any):
            p = col_pos(h)
            if p is None:
                return
            if p >= len(row):
                row.extend([""] * (p + 1 - len(row)))
            row[p] = str(v)

        setv("order_id", order_id)
        setv("created_at", created_at)
        setv("tenant_id", payload.tenant_id)
        setv("customer_name", payload.customer_name)
        setv("customer_contact", payload.customer_contact)
        setv("items", json.dumps(items_dicts, ensure_ascii=False))
        setv("notes", payload.notes or "")
        setv("delivery_type", payload.delivery_type or "pickup")
        setv("requested_time", payload.requested_time or "ahora")
        setv("status", "PENDING_PAYMENT")
        setv("source", payload.source or "api")
        setv("total_amount", total_amount)

        orders_ws.append_row(row, value_input_option="USER_ENTERED")

        return CreateOrderResponse(ok=True, order_id=order_id, total_amount=total_amount)

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Internal error in /orders/create: {e}")


@app.post("/orders/mark_paid")
def mark_paid(payload: MarkPaidRequest):
    """
    Marks an existing order as PAID in Orders sheet by order_id.
    """
    try:
        gc = get_gspread_client()
        orders_sh = get_tenant_orders_spreadsheet(gc, payload.tenant_id)
        orders_ws = ensure_orders_headers_and_get_ws(orders_sh)

        values = orders_ws.get_all_values()
        if not values:
            raise HTTPException(status_code=404, detail="Orders sheet is empty")

        headers = values[0]
        headers_norm = [normalize(h) for h in headers]

        if "order_id" not in headers_norm:
            raise HTTPException(status_code=500, detail="Orders sheet missing 'order_id' column")
        if "status" not in headers_norm:
            raise HTTPException(status_code=500, detail="Orders sheet missing 'status' column")

        order_col = headers_norm.index("order_id") + 1  # 1-based
        status_col = headers_norm.index("status") + 1

        # Search for order_id
        found_row = None
        for i, row in enumerate(values[1:], start=2):  # sheet row index starts at 1; + header row
            oid = row[order_col - 1] if (order_col - 1) < len(row) else ""
            if str(oid).strip() == payload.order_id.strip():
                found_row = i
                break

        if not found_row:
            raise HTTPException(status_code=404, detail=f"Order not found: {payload.order_id}")

        orders_ws.update_cell(found_row, status_col, "PAID")
        return {"ok": True, "tenant_id": payload.tenant_id, "order_id": payload.order_id, "status": "PAID"}

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Internal error in /orders/mark_paid: {e}")


@app.get("/diag/env")
def diag_env():
    """
    Quick diagnostic to verify required env vars exist (does not print secrets).
    """
    return {
        "ok": True,
        "has_GCP_CREDENTIALS_JSON": bool(os.getenv("GCP_CREDENTIALS_JSON")),
        "has_RESERVACIONES_CONFIG": bool(os.getenv("RESERVACIONES_CONFIG")),
    }
