import os
import json
import re
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

import gspread
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field


# ============================================================
# ENV
# ============================================================
# Ahora tu variable en Render se llama "RESERVACIONES_CONFIG"
# y su valor es el SPREADSHEET ID del doc "reservaciones_config".
CONFIG_SPREADSHEET_ID = (
    os.getenv("RESERVACIONES_CONFIG", "").strip()
    or os.getenv("TENANTS_SHEET_ID", "").strip()  # fallback por compatibilidad
)

GCP_CREDENTIALS_JSON = os.getenv("GCP_CREDENTIALS_JSON", "").strip()
ADMIN_TOKEN = os.getenv("ADMIN_TOKEN", "").strip()

# Tabs esperadas
TENANTS_TAB = "Tenants"
ORDERS_TAB = "Orders"
MENU_TAB = "Menu"

# ============================================================
# APP
# ============================================================
app = FastAPI(title="Reservas + Orders API", version="1.0.0")


# ============================================================
# UTILIDADES
# ============================================================
def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def normalize(s: Any) -> str:
    if s is None:
        return ""
    s = str(s).strip().lower()
    # quitar tildes
    replacements = {
        "á": "a", "é": "e", "í": "i", "ó": "o", "ú": "u", "ü": "u", "ñ": "n"
    }
    for a, b in replacements.items():
        s = s.replace(a, b)
    # quitar puntuación
    s = re.sub(r"[^\w\s]", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s


def parse_bool(v: Any) -> bool:
    if isinstance(v, bool):
        return v
    if v is None:
        return False
    s = str(v).strip().lower()
    return s in ("true", "1", "yes", "y", "si", "sí", "ok")


def parse_number(v: Any) -> float:
    if v is None:
        return 0.0
    if isinstance(v, (int, float)):
        return float(v)
    s = str(v).strip()
    # deja solo números, coma, punto, signo
    s = re.sub(r"[^0-9,.\-]", "", s)
    # si tiene coma sin punto, asumir coma decimal
    if "," in s and "." not in s:
        s = s.replace(",", ".")
    try:
        return float(s)
    except Exception:
        return 0.0


def get_gspread_client() -> gspread.Client:
    if not GCP_CREDENTIALS_JSON:
        raise RuntimeError("Missing env var: GCP_CREDENTIALS_JSON")
    if not CONFIG_SPREADSHEET_ID:
        raise RuntimeError("Missing env var: RESERVACIONES_CONFIG (or TENANTS_SHEET_ID fallback)")

    creds_dict = json.loads(GCP_CREDENTIALS_JSON)
    gc = gspread.service_account_from_dict(
        creds_dict,
        scopes=[
            "https://www.googleapis.com/auth/spreadsheets",
            "https://www.googleapis.com/auth/drive",
        ],
    )
    return gc


def detect_header_row(values: List[List[Any]], required_headers: List[str]) -> int:
    req = [normalize(h) for h in required_headers]
    for i, row in enumerate(values[:30]):  # buscar en primeras 30 filas
        row_norm = [normalize(x) for x in row]
        if all(h in row_norm for h in req):
            return i
    return 0


def read_records_manual(ws: gspread.Worksheet, required_headers: List[str]) -> List[Dict[str, Any]]:
    values = ws.get_all_values()
    if not values:
        return []

    header_idx = detect_header_row(values, required_headers)
    headers = values[header_idx]
    headers_norm = [normalize(h) for h in headers]

    out: List[Dict[str, Any]] = []
    for row in values[header_idx + 1 :]:
        if not any(str(c).strip() for c in row):
            continue
        rec: Dict[str, Any] = {}
        for j, h in enumerate(headers_norm):
            if not h:
                continue
            rec[h] = row[j] if j < len(row) else ""
        out.append(rec)
    return out


def open_ws_by_title(sh: gspread.Spreadsheet, title: str) -> gspread.Worksheet:
    try:
        return sh.worksheet(title)
    except gspread.WorksheetNotFound:
        raise HTTPException(status_code=500, detail=f"Worksheet not found: {title}")


def load_tenants_index(gc: gspread.Client) -> Dict[str, Dict[str, Any]]:
    """
    Lee reservaciones_config -> pestaña Tenants
    Retorna dict: tenant_id -> row dict (keys normalizadas)
    """
    try:
        config_sh = gc.open_by_key(CONFIG_SPREADSHEET_ID)
    except Exception as e:
        raise HTTPException(
            status_code=500,
            detail=f"Cannot open config spreadsheet (RESERVACIONES_CONFIG). Error: {str(e)}"
        )

    tenants_ws = open_ws_by_title(config_sh, TENANTS_TAB)

    required = ["tenant_id", "orders_sheet_id", "orders_enabled", "active"]
    rows = read_records_manual(tenants_ws, required_headers=required)

    idx: Dict[str, Dict[str, Any]] = {}
    for r in rows:
        tid = normalize(r.get("tenant_id", ""))
        if not tid:
            continue
        idx[tid] = r
    return idx


def get_tenant_config(gc: gspread.Client, tenant_id: str) -> Dict[str, Any]:
    tenants = load_tenants_index(gc)
    tid = normalize(tenant_id)
    if tid not in tenants:
        raise HTTPException(status_code=404, detail=f"Tenant not found: {tenant_id}")
    cfg = tenants[tid]

    if not parse_bool(cfg.get("active")):
        raise HTTPException(status_code=400, detail=f"Tenant inactive: {tenant_id}")

    return cfg


def open_orders_spreadsheet(gc: gspread.Client, tenant_id: str) -> gspread.Spreadsheet:
    cfg = get_tenant_config(gc, tenant_id)

    if not parse_bool(cfg.get("orders_enabled")):
        raise HTTPException(status_code=400, detail=f"Orders not enabled for tenant: {tenant_id}")

    orders_sheet_id = str(cfg.get("orders_sheet_id", "")).strip()
    if not orders_sheet_id:
        raise HTTPException(status_code=500, detail=f"orders_sheet_id missing for tenant: {tenant_id}")

    try:
        return gc.open_by_key(orders_sheet_id)
    except gspread.SpreadsheetNotFound:
        raise HTTPException(
            status_code=500,
            detail=f"orders_sheet_id not found / no access for tenant {tenant_id}. (Share sheet with service account)"
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error opening orders sheet: {str(e)}")


def load_menu_map(menu_ws: gspread.Worksheet) -> Dict[str, Dict[str, Any]]:
    """
    Lee Menu con headers técnicos en fila 1: sku, name, price, active, category
    y fila 2 puede ser "bonita" siempre que active NO sea TRUE (tal como definimos).
    Devuelve mapa sku-> {price, name, category}
    """
    required = ["sku", "name", "price", "active", "category"]
    rows = read_records_manual(menu_ws, required_headers=required)

    menu: Dict[str, Dict[str, Any]] = {}
    for r in rows:
        sku = str(r.get("sku", "")).strip()
        if not sku:
            continue
        if not parse_bool(r.get("active")):
            continue

        menu[sku] = {
            "sku": sku,
            "name": r.get("name", ""),
            "category": r.get("category", ""),
            "price": parse_number(r.get("price")),
        }
    return menu


def ensure_orders_headers(orders_ws: gspread.Worksheet) -> Tuple[int, List[str]]:
    """
    Busca la fila de headers del Orders sheet.
    Retorna (header_row_index_1based, headers_norm_list)
    """
    values = orders_ws.get_all_values()
    if not values:
        raise HTTPException(status_code=500, detail="Orders sheet is empty (no headers)")

    required = [
        "order_id", "created_at", "tenant_id", "customer_name", "customer_contact",
        "items", "notes", "delivery_type", "requested_time", "status", "source", "total_amount"
    ]
    header_idx0 = detect_header_row(values, required_headers=required)
    headers = values[header_idx0]
    headers_norm = [normalize(h) for h in headers]
    return (header_idx0 + 1, headers_norm)


def find_column(headers_norm: List[str], col_name: str) -> int:
    """
    Retorna índice 0-based de la columna col_name (normalizada), o -1 si no existe.
    """
    target = normalize(col_name)
    for i, h in enumerate(headers_norm):
        if h == target:
            return i
    return -1


def append_order_row(
    orders_ws: gspread.Worksheet,
    headers_norm: List[str],
    row_data: Dict[str, Any],
) -> None:
    """
    Construye una fila en el orden de headers y la appendea.
    """
    row: List[Any] = []
    for h in headers_norm:
        if not h:
            row.append("")
            continue
        row.append(row_data.get(h, ""))
    orders_ws.append_row(row, value_input_option="USER_ENTERED")


def update_order_status(
    orders_ws: gspread.Worksheet,
    header_row_1based: int,
    headers_norm: List[str],
    order_id: str,
    new_status: str,
) -> bool:
    """
    Busca order_id y actualiza columna status. Retorna True si actualizó.
    """
    col_order_id = find_column(headers_norm, "order_id")
    col_status = find_column(headers_norm, "status")
    if col_order_id < 0 or col_status < 0:
        raise HTTPException(status_code=500, detail="Orders headers missing order_id/status")

    values = orders_ws.get_all_values()
    # recorrer desde data row (header_row_1based + 1)
    for r_idx0 in range(header_row_1based, len(values)):
        row = values[r_idx0]
        if col_order_id < len(row) and str(row[col_order_id]).strip() == str(order_id).strip():
            # update status
            cell_row = r_idx0 + 1
            cell_col = col_status + 1
            orders_ws.update_cell(cell_row, cell_col, new_status)
            return True
    return False


# ============================================================
# MODELOS
# ============================================================
class OrderItem(BaseModel):
    sku: str
    qty: int = Field(ge=1)


class OrderCreateRequest(BaseModel):
    tenant_id: str
    customer_name: str = ""
    customer_contact: str = ""
    items: List[OrderItem]
    notes: str = ""
    delivery_type: str = "pickup"  # pickup/delivery
    requested_time: str = ""
    source: str = "api"


class OrderCreateResponse(BaseModel):
    ok: bool
    tenant_id: str
    order_id: str
    total_amount: float
    status: str


class MarkPaidRequest(BaseModel):
    tenant_id: str
    order_id: str


class MarkPaidResponse(BaseModel):
    ok: bool
    tenant_id: str
    order_id: str
    new_status: str


# ============================================================
# ENDPOINTS
# ============================================================
@app.get("/health")
def health():
    return {
        "ok": True,
        "time": utc_now_iso(),
        "config_env_present": bool(CONFIG_SPREADSHEET_ID),
    }


@app.get("/menu")
def get_menu(tenant_id: str):
    gc = get_gspread_client()
    sh = open_orders_spreadsheet(gc, tenant_id)
    menu_ws = open_ws_by_title(sh, MENU_TAB)

    menu_map = load_menu_map(menu_ws)
    # devolver lista ordenada por category/name
    items = list(menu_map.values())
    items.sort(key=lambda x: (str(x.get("category", "")), str(x.get("name", ""))))
    return {
        "ok": True,
        "tenant_id": tenant_id,
        "count": len(items),
        "items": items,
    }


@app.post("/orders/create", response_model=OrderCreateResponse)
def create_order(req: OrderCreateRequest):
    gc = get_gspread_client()
    sh = open_orders_spreadsheet(gc, req.tenant_id)
    orders_ws = open_ws_by_title(sh, ORDERS_TAB)
    menu_ws = open_ws_by_title(sh, MENU_TAB)

    # cargar menú para calcular total
    menu_map = load_menu_map(menu_ws)

    total = 0.0
    normalized_items: List[Dict[str, Any]] = []
    for it in req.items:
        sku = str(it.sku).strip()
        qty = int(it.qty)

        if sku not in menu_map:
            raise HTTPException(status_code=400, detail=f"SKU not found or inactive in Menu: {sku}")

        price = float(menu_map[sku]["price"])
        line_total = price * qty
        total += line_total

        normalized_items.append({"sku": sku, "qty": qty})

    # headers y append
    header_row_1based, headers_norm = ensure_orders_headers(orders_ws)

    order_id = uuid.uuid4().hex[:8]  # corto para demo
    row_data = {
        "order_id": order_id,
        "created_at": utc_now_iso(),
        "tenant_id": req.tenant_id,
        "customer_name": req.customer_name,
        "customer_contact": req.customer_contact,
        "items": json.dumps(normalized_items, ensure_ascii=False),
        "notes": req.notes,
        "delivery_type": req.delivery_type,
        "requested_time": req.requested_time,
        "status": "PENDING_PAYMENT",
        "source": req.source,
        "total_amount": round(total, 2),
    }

    append_order_row(orders_ws, headers_norm, row_data)

    return OrderCreateResponse(
        ok=True,
        tenant_id=req.tenant_id,
        order_id=order_id,
        total_amount=round(total, 2),
        status="PENDING_PAYMENT",
    )


@app.post("/orders/mark_paid", response_model=MarkPaidResponse)
def mark_paid(req: MarkPaidRequest):
    gc = get_gspread_client()
    sh = open_orders_spreadsheet(gc, req.tenant_id)
    orders_ws = open_ws_by_title(sh, ORDERS_TAB)

    header_row_1based, headers_norm = ensure_orders_headers(orders_ws)

    updated = update_order_status(
        orders_ws=orders_ws,
        header_row_1based=header_row_1based,
        headers_norm=headers_norm,
        order_id=req.order_id,
        new_status="PAID",
    )
    if not updated:
        raise HTTPException(status_code=404, detail=f"Order not found: {req.order_id}")

    return MarkPaidResponse(
        ok=True,
        tenant_id=req.tenant_id,
        order_id=req.order_id,
        new_status="PAID",
    )
