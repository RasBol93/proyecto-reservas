import os
import json
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

import gspread
from google.oauth2.service_account import Credentials


# =========================
# Config
# =========================

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive.readonly",
]

# EDITA ESTO: pon aquí tus IDs reales de Google Sheets
TENANTS: Dict[str, Dict[str, str]] = {
    "resto_demo": {
        "spreadsheet_id": "PON_AQUI_EL_SPREADSHEET_ID_DE_ORDERS_RESTO_DEMO",
        "menu_sheet": "Menu",
        "orders_sheet": "Orders",
    },
    # Ejemplo futuro:
    # "salon_demo": {
    #     "spreadsheet_id": "PON_AQUI_EL_SPREADSHEET_ID_DE_ORDERS_SALON_DEMO",
    #     "menu_sheet": "Menu",
    #     "orders_sheet": "Orders",
    # },
}

TECH_HEADERS_MENU = ["sku", "name", "price", "active", "category"]
TECH_HEADERS_ORDERS = [
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


# =========================
# Helpers Google Sheets
# =========================

def get_gspread_client() -> gspread.client.Client:
    raw = os.getenv("GCP_CREDENTIALS_JSON")
    if not raw:
        raise RuntimeError("Missing env var GCP_CREDENTIALS_JSON")

    info = json.loads(raw)
    creds = Credentials.from_service_account_info(info, scopes=SCOPES)
    return gspread.Client(auth=creds)


def open_ws(tenant_id: str):
    cfg = TENANTS.get(tenant_id)
    if not cfg:
        raise HTTPException(status_code=404, detail=f"Unknown tenant_id: {tenant_id}")

    gc = get_gspread_client()
    sh = gc.open_by_key(cfg["spreadsheet_id"])

    menu_ws = sh.worksheet(cfg.get("menu_sheet", "Menu"))
    orders_ws = sh.worksheet(cfg.get("orders_sheet", "Orders"))
    return menu_ws, orders_ws


def normalize_bool(v: Any) -> bool:
    if v is None:
        return False
    s = str(v).strip().lower()
    return s in ("true", "1", "yes", "y", "si", "sí")


def parse_price_to_int(v: Any) -> int:
    """
    Convierte precio a int (Bs). Acepta "32", "32.0", "32,0", " 32 ", etc.
    Si no se puede parsear -> lanza ValueError
    """
    if v is None:
        raise ValueError("price is empty")

    s = str(v).strip()
    if s == "":
        raise ValueError("price is empty")

    # Reemplaza coma decimal por punto
    s = s.replace(",", ".")

    # Quita posibles "Bs" o símbolos simples (por si acaso)
    s = s.replace("bs", "").replace("Bs", "").replace("B", "").replace("$", "").strip()

    # float -> int (si viene "32.0")
    n = float(s)
    return int(round(n))


def get_menu_items(menu_ws) -> List[Dict[str, Any]]:
    """
    Lee Menu y devuelve solo items activos (active=TRUE).
    Asume:
    - Fila 1: headers técnicos exactos (sku,name,price,active,category)
    - Fila 2: puede estar en español, PERO active NO debe ser TRUE
    """
    values = menu_ws.get_all_values()
    if not values or len(values) < 1:
        return []

    header = [h.strip() for h in values[0]]
    # Validación ligera: que existan columnas mínimas
    for h in TECH_HEADERS_MENU:
        if h not in header:
            raise HTTPException(
                status_code=500,
                detail=f"Menu sheet missing header '{h}'. Row1 must be: {TECH_HEADERS_MENU}",
            )

    idx = {name: header.index(name) for name in TECH_HEADERS_MENU}

    items: List[Dict[str, Any]] = []
    for r in values[1:]:
        # fila vacía
        if not any([c.strip() for c in r if isinstance(c, str)]):
            continue

        sku = (r[idx["sku"]] if idx["sku"] < len(r) else "").strip()
        name = (r[idx["name"]] if idx["name"] < len(r) else "").strip()
        price_raw = (r[idx["price"]] if idx["price"] < len(r) else "").strip()
        active_raw = (r[idx["active"]] if idx["active"] < len(r) else "").strip()
        category = (r[idx["category"]] if idx["category"] < len(r) else "").strip()

        # Reglas para saltar filas "no-producto"
        if sku == "" or name == "":
            continue
        if not normalize_bool(active_raw):
            continue

        # Precio robusto: si falla, saltamos el producto (mejor que romper /menu)
        try:
            price_int = parse_price_to_int(price_raw)
        except Exception:
            continue

        items.append(
            {
                "sku": sku,
                "name": name,
                "price": str(price_int),
                "active": "TRUE",
                "category": category or "Sin categoría",
            }
        )

    return items


def build_price_map(menu_ws) -> Dict[str, int]:
    """
    Mapa sku -> price_int, usando TODAS las filas con sku+price válidos (no solo activos),
    para poder calcular totales aunque luego alguien desactive un ítem.
    """
    values = menu_ws.get_all_values()
    if not values:
        return {}

    header = [h.strip() for h in values[0]]
    for h in TECH_HEADERS_MENU:
        if h not in header:
            raise HTTPException(
                status_code=500,
                detail=f"Menu sheet missing header '{h}'. Row1 must be: {TECH_HEADERS_MENU}",
            )
    idx_sku = header.index("sku")
    idx_price = header.index("price")

    price_map: Dict[str, int] = {}
    for r in values[1:]:
        sku = (r[idx_sku] if idx_sku < len(r) else "").strip()
        price_raw = (r[idx_price] if idx_price < len(r) else "").strip()
        if not sku:
            continue
        try:
            price_map[sku] = parse_price_to_int(price_raw)
        except Exception:
            # ignora filas no válidas (ej: fila 2 en español)
            continue
    return price_map


def ensure_orders_headers(orders_ws):
    values = orders_ws.get_all_values()
    if not values:
        orders_ws.append_row(TECH_HEADERS_ORDERS)
        return

    header = [h.strip() for h in values[0]]
    # si faltan headers técnicos, no intentamos “arreglar” silenciosamente
    for h in TECH_HEADERS_ORDERS:
        if h not in header:
            raise HTTPException(
                status_code=500,
                detail=f"Orders sheet missing header '{h}'. Row1 must include: {TECH_HEADERS_ORDERS}",
            )


def find_order_row_by_id(orders_ws, order_id: str) -> Optional[int]:
    """
    Devuelve el número de fila (1-indexed) donde está el order_id.
    Asume que order_id está en la columna A.
    """
    col = orders_ws.col_values(1)  # columna A
    for i, v in enumerate(col, start=1):
        if v == order_id:
            return i
    return None


# =========================
# API Models
# =========================

class OrderItem(BaseModel):
    sku: str
    qty: int = Field(ge=1, le=99)


class CreateOrderPayload(BaseModel):
    tenant_id: str
    customer_name: str
    customer_contact: str
    delivery_type: str = Field(default="pickup")  # pickup/delivery
    requested_time: str = Field(default="ahora")
    items: List[OrderItem]
    notes: str = Field(default="")
    source: str = Field(default="api")


class MarkPaidPayload(BaseModel):
    tenant_id: str
    order_id: str


# =========================
# FastAPI App
# =========================

app = FastAPI(title="Proyecto Reservas - Demo Pedidos", version="1.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/")
def root():
    return {"ok": True, "service": "proyecto-reservas", "tenants": list(TENANTS.keys())}


@app.get("/debug/tenants")
def debug_tenants():
    return {"ok": True, "tenants": TENANTS}


@app.get("/menu")
def get_menu(tenant_id: str):
    menu_ws, _ = open_ws(tenant_id)
    items = get_menu_items(menu_ws)
    return {"ok": True, "tenant_id": tenant_id, "count": len(items), "items": items}


@app.post("/orders/create")
def create_order(payload: CreateOrderPayload):
    menu_ws, orders_ws = open_ws(payload.tenant_id)
    ensure_orders_headers(orders_ws)

    price_map = build_price_map(menu_ws)

    # Calcula total
    total = 0
    missing: List[str] = []
    for it in payload.items:
        if it.sku not in price_map:
            missing.append(it.sku)
            continue
        total += price_map[it.sku] * int(it.qty)

    if missing:
        raise HTTPException(
            status_code=400,
            detail=f"Unknown SKU(s): {missing}. Check Menu sheet (sku/price).",
        )

    order_id = uuid.uuid4().hex[:7]
    created_at = datetime.now(timezone.utc).isoformat()

    row = [
        order_id,
        created_at,
        payload.tenant_id,
        payload.customer_name,
        payload.customer_contact,
        json.dumps([{"sku": it.sku, "qty": it.qty} for it in payload.items], ensure_ascii=False),
        payload.notes,
        payload.delivery_type,
        payload.requested_time,
        "PENDING_PAYMENT",
        payload.source,
        str(total),
    ]

    orders_ws.append_row(row, value_input_option="RAW")

    return {
        "ok": True,
        "tenant_id": payload.tenant_id,
        "order_id": order_id,
        "status": "PENDING_PAYMENT",
        "total_amount": total,
    }


@app.post("/orders/mark_paid")
def mark_paid(payload: MarkPaidPayload):
    _, orders_ws = open_ws(payload.tenant_id)
    ensure_orders_headers(orders_ws)

    row_idx = find_order_row_by_id(orders_ws, payload.order_id)
    if not row_idx:
        raise HTTPException(status_code=404, detail="order_id not found")

    # status está en la columna J (10) según TECH_HEADERS_ORDERS
    status_col = TECH_HEADERS_ORDERS.index("status") + 1
    orders_ws.update_cell(row_idx, status_col, "PAID")

    return {
        "ok": True,
        "tenant_id": payload.tenant_id,
        "order_id": payload.order_id,
        "new_status": "PAID",
    }
