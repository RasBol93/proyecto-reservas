import os
import json
import re
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

import gspread
from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel, Field

# =========================
# Config & helpers
# =========================

APP_NAME = "proyecto-reservas"

ENV_CONFIG_SPREADSHEET_ID = "RESERVACIONES_CONFIG"  # <- ID del spreadsheet "reservaciones_config"
ENV_GCP_CREDS_JSON = "GCP_CREDENTIALS_JSON"          # <- JSON service account (string)


def now_iso_utc() -> str:
    return datetime.now(timezone.utc).isoformat()


def to_bool(v: Any) -> bool:
    if isinstance(v, bool):
        return v
    if v is None:
        return False
    s = str(v).strip().lower()
    return s in ("true", "1", "yes", "y", "si", "sí", "on")


def normalize(s: Any) -> str:
    if s is None:
        return ""
    s = str(s).strip().lower()
    # quitar tildes simple
    replacements = {
        "á": "a", "é": "e", "í": "i", "ó": "o", "ú": "u",
        "ä": "a", "ë": "e", "ï": "i", "ö": "o", "ü": "u",
        "ñ": "n",
    }
    for a, b in replacements.items():
        s = s.replace(a, b)
    # quitar puntuación
    s = re.sub(r"[^\w\s-]", "", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s


def detect_header_row(values: List[List[Any]], required_headers: List[str], max_scan: int = 10) -> int:
    """
    Detecta en qué fila (1-indexed) están los headers técnicos.
    Busca en las primeras max_scan filas.
    """
    req = [normalize(h) for h in required_headers]
    scan = values[:max_scan]
    for idx, row in enumerate(scan, start=1):
        row_norm = [normalize(x) for x in row]
        if all(h in row_norm for h in req):
            return idx
    # fallback: fila 1
    return 1


def read_records_manual(ws: gspread.Worksheet, required_headers: List[str]) -> List[Dict[str, Any]]:
    """
    Lee todos los registros de una worksheet detectando headers.
    Devuelve lista de dicts (header -> value).
    """
    values = ws.get_all_values()
    if not values:
        return []
    header_row = detect_header_row(values, required_headers=required_headers)
    headers = values[header_row - 1]
    headers_norm = [normalize(h) for h in headers]

    records: List[Dict[str, Any]] = []
    for row in values[header_row:]:
        if not any(str(x).strip() for x in row):
            continue
        d: Dict[str, Any] = {}
        for i, h in enumerate(headers_norm):
            if h == "":
                continue
            d[h] = row[i] if i < len(row) else ""
        records.append(d)
    return records


def get_gspread_client() -> gspread.Client:
    creds = os.getenv(ENV_GCP_CREDS_JSON, "").strip()
    if not creds:
        raise RuntimeError(f"Missing env var: {ENV_GCP_CREDS_JSON}")

    try:
        info = json.loads(creds)
    except Exception as e:
        raise RuntimeError(f"{ENV_GCP_CREDS_JSON} is not valid JSON: {e}")

    scopes = [
        "https://www.googleapis.com/auth/spreadsheets",
        "https://www.googleapis.com/auth/drive",
    ]
    gc = gspread.service_account_from_dict(info, scopes=scopes)
    return gc


def get_config_spreadsheet(gc: gspread.Client) -> gspread.Spreadsheet:
    sid = os.getenv(ENV_CONFIG_SPREADSHEET_ID, "").strip()
    if not sid:
        raise RuntimeError(f"Missing env var: {ENV_CONFIG_SPREADSHEET_ID}")

    try:
        return gc.open_by_key(sid)
    except gspread.exceptions.SpreadsheetNotFound:
        raise RuntimeError(
            f"SpreadsheetNotFound: No se encontró el spreadsheet del config con ID={sid}. "
            "Verifica que el ID sea correcto y que el spreadsheet esté compartido con el service account."
        )


def get_ws(spreadsheet: gspread.Spreadsheet, title: str) -> gspread.Worksheet:
    try:
        return spreadsheet.worksheet(title)
    except Exception:
        raise RuntimeError(f"No existe la pestaña '{title}' en el spreadsheet '{spreadsheet.title}'")


# Cache simple en memoria (opcional)
_TENANTS_CACHE: Dict[str, Dict[str, Any]] = {}
_TENANTS_CACHE_AT: Optional[str] = None


def load_tenants(gc: gspread.Client, force: bool = False) -> Dict[str, Dict[str, Any]]:
    """
    Lee Tenants desde el spreadsheet de config:
    columnas esperadas:
      tenant_id, name, business_type, orders_sheet_id, bookings_enabled, orders_enabled,
      bot_token, webhook_secret, admin_chat_id, timezone, active, admin_whatsapp
    """
    global _TENANTS_CACHE, _TENANTS_CACHE_AT
    if _TENANTS_CACHE and not force:
        return _TENANTS_CACHE

    sh = get_config_spreadsheet(gc)
    ws = get_ws(sh, "Tenants")
    records = read_records_manual(ws, required_headers=["tenant_id", "orders_sheet_id", "active"])

    tenants: Dict[str, Dict[str, Any]] = {}
    for r in records:
        tid = str(r.get("tenant_id", "")).strip()
        if not tid:
            continue
        tenants[tid] = {
            "tenant_id": tid,
            "name": r.get("name", ""),
            "business_type": r.get("business_type", ""),
            "orders_sheet_id": str(r.get("orders_sheet_id", "")).strip(),
            "bookings_enabled": to_bool(r.get("bookings_enabled", "")),
            "orders_enabled": to_bool(r.get("orders_enabled", "")),
            "bot_token": r.get("bot_token", ""),
            "webhook_secret": r.get("webhook_secret", ""),
            "admin_chat_id": r.get("admin_chat_id", ""),
            "timezone": r.get("timezone", "America/La_Paz"),
            "active": to_bool(r.get("active", "")),
            "admin_whatsapp": r.get("admin_whatsapp", ""),
        }

    _TENANTS_CACHE = tenants
    _TENANTS_CACHE_AT = now_iso_utc()
    return tenants


def get_tenant_or_404(gc: gspread.Client, tenant_id: str) -> Dict[str, Any]:
    tenants = load_tenants(gc)
    t = tenants.get(tenant_id)
    if not t or not t.get("active", False):
        raise HTTPException(status_code=404, detail=f"Tenant not found or inactive: {tenant_id}")
    return t


def open_orders_spreadsheet(gc: gspread.Client, tenant: Dict[str, Any]) -> gspread.Spreadsheet:
    sid = tenant.get("orders_sheet_id", "").strip()
    if not sid:
        raise HTTPException(status_code=500, detail=f"Tenant {tenant['tenant_id']} missing orders_sheet_id")
    try:
        return gc.open_by_key(sid)
    except gspread.exceptions.SpreadsheetNotFound:
        raise HTTPException(
            status_code=500,
            detail=f"SpreadsheetNotFound for orders_sheet_id={sid}. "
                   "Verifica que el spreadsheet exista y esté compartido con el service account."
        )


def load_menu_index(orders_sh: gspread.Spreadsheet) -> Dict[str, Dict[str, Any]]:
    """
    Carga Menu y devuelve índice por sku:
      { "H01": {"sku":"H01","name":"...","price":25.0,"category":"Hamburguesas"} }
    Solo filas con active=TRUE.
    """
    ws = get_ws(orders_sh, "Menu")
    rows = read_records_manual(ws, required_headers=["sku", "name", "price", "active", "category"])

    idx: Dict[str, Dict[str, Any]] = {}
    for r in rows:
        sku = str(r.get("sku", "")).strip()
        if not sku:
            continue

        # Importante: fila 2 (human) debe tener active != TRUE para no entrar aquí
        if not to_bool(r.get("active", "")):
            continue

        price_raw = str(r.get("price", "")).strip()
        try:
            price = float(price_raw)
        except Exception:
            # si el precio no es número, ignoramos
            continue

        idx[sku] = {
            "sku": sku,
            "name": r.get("name", ""),
            "price": price,
            "category": r.get("category", ""),
        }

    return idx


def group_menu_by_category(menu_idx: Dict[str, Dict[str, Any]]) -> Dict[str, List[Dict[str, Any]]]:
    cats: Dict[str, List[Dict[str, Any]]] = {}
    for sku, item in menu_idx.items():
        cat = item.get("category", "") or "Otros"
        cats.setdefault(cat, []).append({
            "sku": item["sku"],
            "name": item.get("name", ""),
            "price": item.get("price", 0),
            "category": cat,
        })
    # orden por nombre dentro de categoría
    for cat in cats:
        cats[cat] = sorted(cats[cat], key=lambda x: normalize(x.get("name", "")))
    return cats


def calc_total_amount(items: List[Dict[str, Any]], menu_idx: Dict[str, Dict[str, Any]]) -> float:
    total = 0.0
    for it in items:
        sku = str(it.get("sku", "")).strip()
        qty = it.get("qty", 0)

        if not sku:
            raise HTTPException(status_code=422, detail="Item missing sku")
        if sku not in menu_idx:
            raise HTTPException(status_code=422, detail=f"Unknown sku in items: {sku}")

        try:
            qty_i = int(qty)
        except Exception:
            raise HTTPException(status_code=422, detail=f"Invalid qty for sku={sku}: {qty}")
        if qty_i <= 0:
            raise HTTPException(status_code=422, detail=f"qty must be >= 1 for sku={sku}")

        price = float(menu_idx[sku]["price"])
        total += price * qty_i

    # Si trabajas con enteros (BOB), devolvemos redondeo a 2 decimales
    return round(total, 2)


def ensure_orders_headers(ws: gspread.Worksheet, required: List[str]) -> List[str]:
    """
    Verifica headers técnicos en fila 1. Si faltan, lanza error con mensaje claro.
    Devuelve lista headers (normalizados) de fila 1.
    """
    values = ws.get_all_values()
    if not values or not values[0]:
        raise HTTPException(status_code=500, detail="Orders sheet is empty or missing headers in row 1")

    headers = values[0]
    headers_norm = [normalize(h) for h in headers]

    missing = [h for h in required if normalize(h) not in headers_norm]
    if missing:
        raise HTTPException(
            status_code=500,
            detail=f"Orders sheet missing required headers in row 1: {missing}. "
                   f"Headers actuales: {headers}"
        )
    return headers_norm


def append_order_row(
    orders_sh: gspread.Spreadsheet,
    tenant_id: str,
    order_id: str,
    customer_name: str,
    customer_contact: str,
    items: List[Dict[str, Any]],
    notes: str,
    delivery_type: str,
    requested_time: str,
    status: str,
    source: str,
    total_amount: float,
):
    ws = get_ws(orders_sh, "Orders")
    headers_norm = ensure_orders_headers(
        ws,
        required=[
            "order_id", "created_at", "tenant_id", "customer_name", "customer_contact",
            "items", "notes", "delivery_type", "requested_time", "status", "source", "total_amount"
        ],
    )

    # Armamos row respetando el orden de headers existentes
    created_at = now_iso_utc()
    payload_map: Dict[str, Any] = {
        "order_id": order_id,
        "created_at": created_at,
        "tenant_id": tenant_id,
        "customer_name": customer_name,
        "customer_contact": customer_contact,
        "items": json.dumps(items, ensure_ascii=False),
        "notes": notes,
        "delivery_type": delivery_type,
        "requested_time": requested_time,
        "status": status,
        "source": source,
        "total_amount": total_amount,
    }

    # Fila con la misma cantidad de columnas que el header original (fila 1)
    header_raw = ws.row_values(1)
    row: List[Any] = []
    for h_raw in header_raw:
        h = normalize(h_raw)
        row.append(payload_map.get(h, ""))

    ws.append_row(row, value_input_option="USER_ENTERED")


def update_order_status(orders_sh: gspread.Spreadsheet, order_id: str, new_status: str) -> bool:
    ws = get_ws(orders_sh, "Orders")
    values = ws.get_all_values()
    if not values:
        return False

    headers = values[0]
    headers_norm = [normalize(h) for h in headers]

    if "order_id" not in headers_norm or "status" not in headers_norm:
        raise HTTPException(status_code=500, detail="Orders sheet must have order_id and status headers in row 1")

    col_order_id = headers_norm.index("order_id") + 1
    col_status = headers_norm.index("status") + 1

    for r_idx in range(2, len(values) + 1):
        oid = ws.cell(r_idx, col_order_id).value
        if str(oid).strip() == order_id:
            ws.update_cell(r_idx, col_status, new_status)
            return True
    return False


def gen_order_id() -> str:
    # simple id (hex corto)
    import secrets
    return secrets.token_hex(4)


# =========================
# API Models
# =========================

class OrderItem(BaseModel):
    sku: str = Field(..., min_length=1)
    qty: int = Field(..., ge=1)

class OrderCreateIn(BaseModel):
    tenant_id: str
    customer_name: str
    customer_contact: str
    items: List[OrderItem]
    notes: Optional[str] = ""
    delivery_type: Optional[str] = "pickup"   # pickup / delivery
    requested_time: Optional[str] = "ahora"
    source: Optional[str] = "api"

class OrderCreateOut(BaseModel):
    ok: bool
    order_id: str
    total_amount: float
    currency: str = "BOB"

class MarkPaidIn(BaseModel):
    tenant_id: str
    order_id: str

class MarkPaidOut(BaseModel):
    ok: bool
    order_id: str
    status: str


# =========================
# FastAPI App
# =========================

app = FastAPI(title=APP_NAME, version="1.0.0")


@app.get("/")
def root():
    return {"ok": True, "service": APP_NAME}


@app.post("/admin/reload_tenants")
def admin_reload_tenants():
    """
    Útil para cuando cambias Tenants/Config y quieres recargar cache sin redeploy.
    """
    gc = get_gspread_client()
    load_tenants(gc, force=True)
    return {"ok": True, "cached_at": _TENANTS_CACHE_AT, "tenants_count": len(_TENANTS_CACHE)}


@app.get("/menu")
def get_menu(tenant_id: str = Query(..., description="tenant_id, ej: resto_demo")):
    gc = get_gspread_client()
    tenant = get_tenant_or_404(gc, tenant_id)

    if not tenant.get("orders_enabled", False):
        raise HTTPException(status_code=400, detail=f"Orders not enabled for tenant: {tenant_id}")

    orders_sh = open_orders_spreadsheet(gc, tenant)
    menu_idx = load_menu_index(orders_sh)
    categories = group_menu_by_category(menu_idx)

    return {"ok": True, "tenant_id": tenant_id, "categories": categories}


@app.post("/orders/create", response_model=OrderCreateOut)
def create_order(payload: OrderCreateIn):
    gc = get_gspread_client()
    tenant = get_tenant_or_404(gc, payload.tenant_id)

    if not tenant.get("orders_enabled", False):
        raise HTTPException(status_code=400, detail=f"Orders not enabled for tenant: {payload.tenant_id}")

    orders_sh = open_orders_spreadsheet(gc, tenant)
    menu_idx = load_menu_index(orders_sh)

    # Convertimos a dicts básicos para cálculo
    items_list = [{"sku": it.sku.strip(), "qty": int(it.qty)} for it in payload.items]
    total_amount = calc_total_amount(items_list, menu_idx)

    order_id = gen_order_id()

    append_order_row(
        orders_sh=orders_sh,
        tenant_id=payload.tenant_id,
        order_id=order_id,
        customer_name=payload.customer_name.strip(),
        customer_contact=payload.customer_contact.strip(),
        items=items_list,
        notes=(payload.notes or "").strip(),
        delivery_type=(payload.delivery_type or "pickup").strip(),
        requested_time=(payload.requested_time or "ahora").strip(),
        status="PENDING_PAYMENT",  # flujo: PENDING_PAYMENT -> PAID
        source=(payload.source or "api").strip(),
        total_amount=total_amount,
    )

    return OrderCreateOut(ok=True, order_id=order_id, total_amount=total_amount, currency="BOB")


@app.post("/orders/mark_paid", response_model=MarkPaidOut)
def mark_paid(payload: MarkPaidIn):
    gc = get_gspread_client()
    tenant = get_tenant_or_404(gc, payload.tenant_id)

    if not tenant.get("orders_enabled", False):
        raise HTTPException(status_code=400, detail=f"Orders not enabled for tenant: {payload.tenant_id}")

    orders_sh = open_orders_spreadsheet(gc, tenant)
    updated = update_order_status(orders_sh, payload.order_id, "PAID")

    if not updated:
        raise HTTPException(status_code=404, detail=f"Order not found: {payload.order_id}")

    return MarkPaidOut(ok=True, order_id=payload.order_id, status="PAID")
