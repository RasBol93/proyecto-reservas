import os
import json
import re
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional
from collections import deque

import requests
import gspread
from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel, Field

# =========================
# Config & helpers
# =========================

APP_NAME = "proyecto-reservas"

ENV_CONFIG_SPREADSHEET_ID = "RESERVACIONES_CONFIG"  # <- ID del spreadsheet "reservaciones_config"
ENV_GCP_CREDS_JSON = "GCP_CREDENTIALS_JSON"          # <- JSON service account (string)
ENV_ADMIN_TOKEN = "ADMIN_TOKEN"                      # <- token simple para endpoints admin

MAX_ITEMS_PER_ORDER = 30
MAX_NAME_LEN = 80
MAX_CONTACT_LEN = 30
MAX_NOTES_LEN = 500
MAX_REQUESTED_TIME_LEN = 60
MAX_SOURCE_LEN = 20

TENANT_ID_RE = re.compile(r"^[a-z0-9_]{2,40}$")
ORDER_ID_RE = re.compile(r"^[a-f0-9]{8}$")  # secrets.token_hex(4) => 8 chars hex

ALLOWED_DELIVERY_TYPES = {"pickup", "delivery"}
ALLOWED_SOURCES = {"api", "swagger", "telegram", "whatsapp"}

# Rate limits (por tenant, por minuto) - ajustables
RL_MENU_PER_MIN = 120
RL_CREATE_PER_MIN = 60
RL_MARKPAID_PER_MIN = 60

# Telegram
TG_API_BASE = "https://api.telegram.org/bot"
TG_TIMEOUT_SEC = 12  # timeout requests a Telegram


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
    replacements = {
        "á": "a", "é": "e", "í": "i", "ó": "o", "ú": "u",
        "ä": "a", "ë": "e", "ï": "i", "ö": "o", "ü": "u",
        "ñ": "n",
    }
    for a, b in replacements.items():
        s = s.replace(a, b)
    s = re.sub(r"[^\w\s-]", "", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s


def log_event(event: str, **fields: Any) -> None:
    safe = {k: v for k, v in fields.items()
            if k not in ("creds", "token", "GCP_CREDENTIALS_JSON", "ADMIN_TOKEN", "bot_token")}
    print(json.dumps({"ts": now_iso_utc(), "event": event, **safe}, ensure_ascii=False))


# =========================
# Rate limiting (simple)
# =========================

class TenantRateLimiter:
    def __init__(self):
        self.buckets: Dict[str, deque] = {}
        self.window_sec = 60

    def hit(self, key: str, limit: int):
        now = time.time()
        dq = self.buckets.setdefault(key, deque())
        while dq and (now - dq[0]) > self.window_sec:
            dq.popleft()
        if len(dq) >= limit:
            raise HTTPException(status_code=429, detail="Rate limit exceeded for this tenant")
        dq.append(now)


_rate_limiter = TenantRateLimiter()


def validate_tenant_id(tenant_id: str) -> None:
    tid = (tenant_id or "").strip()
    if not TENANT_ID_RE.match(tid):
        raise HTTPException(status_code=422, detail="Invalid tenant_id format")


def validate_order_id(order_id: str) -> None:
    oid = (order_id or "").strip().lower()
    if not ORDER_ID_RE.match(oid):
        raise HTTPException(status_code=422, detail="Invalid order_id format")


def validate_delivery_type(v: str) -> str:
    dv = (v or "pickup").strip().lower()
    if dv not in ALLOWED_DELIVERY_TYPES:
        raise HTTPException(status_code=422, detail=f"delivery_type must be one of {sorted(ALLOWED_DELIVERY_TYPES)}")
    return dv


def validate_source(v: str) -> str:
    sv = (v or "api").strip().lower()
    if len(sv) > MAX_SOURCE_LEN:
        raise HTTPException(status_code=422, detail="source too long")
    if sv not in ALLOWED_SOURCES:
        raise HTTPException(status_code=422, detail=f"source must be one of {sorted(ALLOWED_SOURCES)}")
    return sv


def validate_requested_time(v: str) -> str:
    rv = (v or "ahora").strip()
    if len(rv) > MAX_REQUESTED_TIME_LEN:
        raise HTTPException(status_code=422, detail="requested_time too long")
    return rv


def validate_contact(contact: str) -> None:
    c = (contact or "").strip()
    if len(c) > MAX_CONTACT_LEN:
        raise HTTPException(status_code=422, detail="customer_contact too long")
    if not re.match(r"^\+?\d{6,20}$", c):
        raise HTTPException(status_code=422, detail="customer_contact must be digits (optionally starting with +)")


def require_admin_token(token: str) -> None:
    expected = os.getenv(ENV_ADMIN_TOKEN, "").strip()
    if not expected:
        raise HTTPException(status_code=500, detail="ADMIN_TOKEN is not configured in env")
    if (token or "").strip() != expected:
        raise HTTPException(status_code=403, detail="Invalid admin token")


# =========================
# Sheets helpers
# =========================

def detect_header_row(values: List[List[Any]], required_headers: List[str], max_scan: int = 10) -> int:
    req = [normalize(h) for h in required_headers]
    scan = values[:max_scan]
    for idx, row in enumerate(scan, start=1):
        row_norm = [normalize(x) for x in row]
        if all(h in row_norm for h in req):
            return idx
    return 1


def read_records_manual(ws: gspread.Worksheet, required_headers: List[str]) -> List[Dict[str, Any]]:
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
    return gspread.service_account_from_dict(info, scopes=scopes)


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


# Cache simple en memoria
_TENANTS_CACHE: Dict[str, Dict[str, Any]] = {}
_TENANTS_CACHE_AT: Optional[str] = None


def load_tenants(gc: gspread.Client, force: bool = False) -> Dict[str, Dict[str, Any]]:
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
            "bot_token": (r.get("bot_token", "") or "").strip(),
            "webhook_secret": (r.get("webhook_secret", "") or "").strip(),
            "admin_chat_id": str(r.get("admin_chat_id", "")).strip(),
            "timezone": r.get("timezone", "America/La_Paz"),
            "active": to_bool(r.get("active", "")),
            "admin_whatsapp": r.get("admin_whatsapp", ""),
        }

    _TENANTS_CACHE = tenants
    _TENANTS_CACHE_AT = now_iso_utc()
    log_event("tenants_loaded", cached_at=_TENANTS_CACHE_AT, tenants_count=len(tenants))
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
    ws = get_ws(orders_sh, "Menu")
    rows = read_records_manual(ws, required_headers=["sku", "name", "price", "active", "category"])

    idx: Dict[str, Dict[str, Any]] = {}
    for r in rows:
        sku = str(r.get("sku", "")).strip()
        if not sku:
            continue
        if not to_bool(r.get("active", "")):
            continue

        price_raw = str(r.get("price", "")).strip()
        try:
            price = float(price_raw)
        except Exception:
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
    for _, item in menu_idx.items():
        cat = item.get("category", "") or "Otros"
        cats.setdefault(cat, []).append({
            "sku": item["sku"],
            "name": item.get("name", ""),
            "price": item.get("price", 0),
            "category": cat,
        })
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

    return round(total, 2)


def ensure_orders_headers(ws: gspread.Worksheet, required: List[str]) -> List[str]:
    values = ws.get_all_values()
    if not values or not values[0]:
        raise HTTPException(status_code=500, detail="Orders sheet is empty or missing headers in row 1")

    headers = values[0]
    headers_norm = [normalize(h) for h in headers]

    missing = [h for h in required if normalize(h) not in headers_norm]
    if missing:
        raise HTTPException(
            status_code=500,
            detail=f"Orders sheet missing required headers in row 1: {missing}. Headers actuales: {headers}"
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
    ensure_orders_headers(
        ws,
        required=[
            "order_id", "created_at", "tenant_id", "customer_name", "customer_contact",
            "items", "notes", "delivery_type", "requested_time", "status", "source", "total_amount"
        ],
    )

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

    header_raw = ws.row_values(1)
    row: List[Any] = []
    for h_raw in header_raw:
        h = normalize(h_raw)
        row.append(payload_map.get(h, ""))

    ws.append_row(row, value_input_option="USER_ENTERED")


def update_order_status(orders_sh: gspread.Spreadsheet, order_id: str, new_status: str) -> Dict[str, Any]:
    ws = get_ws(orders_sh, "Orders")
    values = ws.get_all_values()
    if not values:
        return {"found": False}

    headers = values[0]
    headers_norm = [normalize(h) for h in headers]

    if "order_id" not in headers_norm or "status" not in headers_norm:
        raise HTTPException(status_code=500, detail="Orders sheet must have order_id and status headers in row 1")

    col_order_id = headers_norm.index("order_id") + 1
    col_status = headers_norm.index("status") + 1

    for r_idx in range(2, len(values) + 1):
        oid = ws.cell(r_idx, col_order_id).value
        if str(oid).strip() == order_id:
            old_status = ws.cell(r_idx, col_status).value or ""
            if normalize(old_status) != normalize(new_status):
                ws.update_cell(r_idx, col_status, new_status)
            return {"found": True, "old_status": old_status}

    return {"found": False}


def gen_order_id() -> str:
    import secrets
    return secrets.token_hex(4)


# =========================
# Telegram helpers (Long Polling)
# =========================

# offset por tenant (RAM). Si reinicia el server, se reinicia el offset (ok para demo).
_TG_OFFSETS: Dict[str, int] = {}


def tg_call(token: str, method: str, payload: Dict[str, Any]) -> Dict[str, Any]:
    url = f"{TG_API_BASE}{token}/{method}"
    try:
        r = requests.post(url, json=payload, timeout=TG_TIMEOUT_SEC)
        data = r.json()
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Telegram API error: {e}")
    if not isinstance(data, dict) or not data.get("ok", False):
        # Telegram devuelve ok=false con description
        desc = (data or {}).get("description", "unknown telegram error")
        raise HTTPException(status_code=502, detail=f"Telegram API not ok: {desc}")
    return data


def tg_send_message(token: str, chat_id: str, text: str, reply_markup: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    payload: Dict[str, Any] = {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }
    if reply_markup:
        payload["reply_markup"] = reply_markup
    return tg_call(token, "sendMessage", payload)


def tg_answer_callback(token: str, callback_query_id: str, text: str = "OK") -> None:
    tg_call(token, "answerCallbackQuery", {"callback_query_id": callback_query_id, "text": text})


def tg_edit_message(token: str, chat_id: str, message_id: int, text: str) -> None:
    tg_call(token, "editMessageText", {
        "chat_id": chat_id,
        "message_id": message_id,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    })


def fmt_items(items_list: List[Dict[str, Any]], menu_idx: Dict[str, Dict[str, Any]]) -> str:
    lines = []
    for it in items_list:
        sku = it["sku"]
        qty = int(it["qty"])
        name = menu_idx.get(sku, {}).get("name", sku)
        price = float(menu_idx.get(sku, {}).get("price", 0))
        lines.append(f"• {qty} × {name}  (SKU {sku})  = {qty*price:.2f} BOB")
    return "\n".join(lines)


def build_paid_button_cb(tenant_id: str, order_id: str, admin_chat_id: str) -> str:
    # callback_data máx 64 bytes aprox; esto entra bien.
    return f"PAID|{tenant_id}|{order_id}|{admin_chat_id}"


def parse_callback_data(data: str) -> Dict[str, str]:
    parts = (data or "").split("|")
    if len(parts) != 4:
        return {}
    return {"action": parts[0], "tenant_id": parts[1], "order_id": parts[2], "admin_chat_id": parts[3]}


def mark_paid_internal(gc: gspread.Client, tenant_id: str, order_id: str, admin_chat_id: str) -> Dict[str, Any]:
    validate_tenant_id(tenant_id)
    validate_order_id(order_id)

    tenant = get_tenant_or_404(gc, tenant_id)
    if not tenant.get("orders_enabled", False):
        raise HTTPException(status_code=400, detail=f"Orders not enabled for tenant: {tenant_id}")

    expected_admin_chat_id = str(tenant.get("admin_chat_id", "")).strip()
    if not expected_admin_chat_id:
        raise HTTPException(status_code=500, detail="admin_chat_id is not set for this tenant")
    if str(admin_chat_id).strip() != expected_admin_chat_id:
        raise HTTPException(status_code=403, detail="Invalid admin_chat_id for this tenant")

    orders_sh = open_orders_spreadsheet(gc, tenant)
    result = update_order_status(orders_sh, order_id, "PAID")
    if not result.get("found"):
        raise HTTPException(status_code=404, detail=f"Order not found: {order_id}")

    old_status = str(result.get("old_status", "") or "")
    old_norm = normalize(old_status)

    if old_norm == "paid":
        return {"ok": True, "already_paid": True, "old_status": old_status}

    if old_norm not in ("pending_payment", "pendingpayment", ""):
        raise HTTPException(status_code=409, detail=f"Cannot mark paid from status={old_status}")

    return {"ok": True, "already_paid": False, "old_status": old_status}


# =========================
# API Models
# =========================

class AdminTokenIn(BaseModel):
    token: str

class OrderItem(BaseModel):
    sku: str = Field(..., min_length=1)
    qty: int = Field(..., ge=1)

class OrderCreateIn(BaseModel):
    tenant_id: str
    customer_name: str
    customer_contact: str
    items: List[OrderItem]
    notes: Optional[str] = ""
    delivery_type: Optional[str] = "pickup"
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
    admin_chat_id: str  # <- blindaje clave

class MarkPaidOut(BaseModel):
    ok: bool
    order_id: str
    status: str
    old_status: Optional[str] = None
    already_paid: Optional[bool] = None

class TelegramPollIn(BaseModel):
    token: str
    tenant_id: str

class TelegramTestSendIn(BaseModel):
    token: str
    tenant_id: str
    text: str


# =========================
# FastAPI App
# =========================

app = FastAPI(title=APP_NAME, version="1.3.0")


@app.get("/")
def root():
    return {"ok": True, "service": APP_NAME}


@app.post("/admin/reload_tenants")
def admin_reload_tenants(payload: AdminTokenIn):
    require_admin_token(payload.token)
    gc = get_gspread_client()
    load_tenants(gc, force=True)
    return {"ok": True, "cached_at": _TENANTS_CACHE_AT, "tenants_count": len(_TENANTS_CACHE)}


@app.get("/menu")
def get_menu(tenant_id: str = Query(..., description="tenant_id, ej: resto_demo")):
    validate_tenant_id(tenant_id)
    _rate_limiter.hit(f"menu:{tenant_id}", RL_MENU_PER_MIN)

    gc = get_gspread_client()
    tenant = get_tenant_or_404(gc, tenant_id)

    if not tenant.get("orders_enabled", False):
        raise HTTPException(status_code=400, detail=f"Orders not enabled for tenant: {tenant_id}")

    orders_sh = open_orders_spreadsheet(gc, tenant)
    menu_idx = load_menu_index(orders_sh)
    categories = group_menu_by_category(menu_idx)

    log_event("menu_ok", tenant_id=tenant_id, categories=len(categories))
    return {"ok": True, "tenant_id": tenant_id, "categories": categories}


@app.post("/orders/create", response_model=OrderCreateOut)
def create_order(payload: OrderCreateIn):
    validate_tenant_id(payload.tenant_id)
    _rate_limiter.hit(f"create:{payload.tenant_id}", RL_CREATE_PER_MIN)

    gc = get_gspread_client()
    tenant = get_tenant_or_404(gc, payload.tenant_id)

    if not tenant.get("orders_enabled", False):
        raise HTTPException(status_code=400, detail=f"Orders not enabled for tenant: {payload.tenant_id}")

    name = (payload.customer_name or "").strip()
    if not name or len(name) > MAX_NAME_LEN:
        raise HTTPException(status_code=422, detail="customer_name missing or too long")

    contact = (payload.customer_contact or "").strip()
    validate_contact(contact)

    if not payload.items or len(payload.items) > MAX_ITEMS_PER_ORDER:
        raise HTTPException(status_code=422, detail=f"items must be 1..{MAX_ITEMS_PER_ORDER}")

    notes = (payload.notes or "").strip()
    if len(notes) > MAX_NOTES_LEN:
        raise HTTPException(status_code=422, detail="notes too long")

    delivery_type = validate_delivery_type(payload.delivery_type)
    requested_time = validate_requested_time(payload.requested_time)
    source = validate_source(payload.source)

    orders_sh = open_orders_spreadsheet(gc, tenant)
    menu_idx = load_menu_index(orders_sh)

    items_list = [{"sku": it.sku.strip(), "qty": int(it.qty)} for it in payload.items]
    total_amount = calc_total_amount(items_list, menu_idx)

    order_id = gen_order_id()

    append_order_row(
        orders_sh=orders_sh,
        tenant_id=payload.tenant_id,
        order_id=order_id,
        customer_name=name,
        customer_contact=contact,
        items=items_list,
        notes=notes,
        delivery_type=delivery_type,
        requested_time=requested_time,
        status="PENDING_PAYMENT",
        source=source,
        total_amount=total_amount,
    )

    # ---- Telegram notify admin (si está configurado) ----
    bot_token = str(tenant.get("bot_token", "")).strip()
    admin_chat_id = str(tenant.get("admin_chat_id", "")).strip()
    if bot_token and admin_chat_id:
        try:
            items_txt = fmt_items(items_list, menu_idx)
            text = (
                f"<b>🧾 Nuevo pedido</b>\n"
                f"<b>Tenant:</b> {payload.tenant_id}\n"
                f"<b>Order ID:</b> <code>{order_id}</code>\n"
                f"<b>Total:</b> {total_amount:.2f} BOB\n\n"
                f"<b>Items:</b>\n{items_txt}\n\n"
                f"<b>Cliente:</b> {name}\n"
                f"<b>Contacto:</b> {contact}\n"
                f"<b>Tipo:</b> {delivery_type}\n"
                f"<b>Hora:</b> {requested_time}\n"
                f"<b>Notas:</b> {notes or '-'}"
            )

            cb = build_paid_button_cb(payload.tenant_id, order_id, admin_chat_id)
            reply_markup = {
                "inline_keyboard": [
                    [{"text": "✅ Pagado", "callback_data": cb}]
                ]
            }
            tg_send_message(bot_token, admin_chat_id, text, reply_markup=reply_markup)
            log_event("telegram_notified", tenant_id=payload.tenant_id, order_id=order_id)
        except Exception as e:
            # No rompemos el flujo de creación por un fallo de Telegram
            log_event("telegram_notify_failed", tenant_id=payload.tenant_id, order_id=order_id, error=str(e))

    log_event("order_created", tenant_id=payload.tenant_id, order_id=order_id, total_amount=total_amount)
    return OrderCreateOut(ok=True, order_id=order_id, total_amount=total_amount, currency="BOB")


@app.post("/orders/mark_paid", response_model=MarkPaidOut)
def mark_paid(payload: MarkPaidIn):
    validate_tenant_id(payload.tenant_id)
    validate_order_id(payload.order_id)
    _rate_limiter.hit(f"mark_paid:{payload.tenant_id}", RL_MARKPAID_PER_MIN)

    gc = get_gspread_client()
    tenant = get_tenant_or_404(gc, payload.tenant_id)

    if not tenant.get("orders_enabled", False):
        raise HTTPException(status_code=400, detail=f"Orders not enabled for tenant: {payload.tenant_id}")

    expected_admin_chat_id = str(tenant.get("admin_chat_id", "")).strip()
    if not expected_admin_chat_id:
        raise HTTPException(status_code=500, detail="admin_chat_id is not set for this tenant")

    if str(payload.admin_chat_id).strip() != expected_admin_chat_id:
        raise HTTPException(status_code=403, detail="Invalid admin_chat_id for this tenant")

    orders_sh = open_orders_spreadsheet(gc, tenant)

    result = update_order_status(orders_sh, payload.order_id, "PAID")
    if not result.get("found"):
        raise HTTPException(status_code=404, detail=f"Order not found: {payload.order_id}")

    old_status = str(result.get("old_status", "") or "")
    old_norm = normalize(old_status)

    if old_norm == "paid":
        log_event("order_mark_paid_idempotent", tenant_id=payload.tenant_id, order_id=payload.order_id)
        return MarkPaidOut(ok=True, order_id=payload.order_id, status="PAID", old_status=old_status, already_paid=True)

    if old_norm not in ("pending_payment", "pendingpayment", ""):
        raise HTTPException(status_code=409, detail=f"Cannot mark paid from status={old_status}")

    log_event("order_mark_paid", tenant_id=payload.tenant_id, order_id=payload.order_id, old_status=old_status)
    return MarkPaidOut(ok=True, order_id=payload.order_id, status="PAID", old_status=old_status, already_paid=False)


# =========================
# Telegram Admin Endpoints (Long Polling)
# =========================

@app.post("/admin/telegram/test_send")
def admin_telegram_test_send(payload: TelegramTestSendIn):
    require_admin_token(payload.token)

    validate_tenant_id(payload.tenant_id)
    gc = get_gspread_client()
    tenant = get_tenant_or_404(gc, payload.tenant_id)

    bot_token = str(tenant.get("bot_token", "")).strip()
    admin_chat_id = str(tenant.get("admin_chat_id", "")).strip()
    if not bot_token:
        raise HTTPException(status_code=500, detail="bot_token is not set for this tenant")
    if not admin_chat_id:
        raise HTTPException(status_code=500, detail="admin_chat_id is not set for this tenant")

    tg_send_message(bot_token, admin_chat_id, payload.text.strip() or "Test")
    return {"ok": True}


@app.post("/admin/telegram/poll_once")
def admin_telegram_poll_once(payload: TelegramPollIn):
    """
    Long polling manual (demo). Llamas esto desde Swagger cada vez que quieras procesar:
    - mensajes normales
    - callbacks de botones (✅ Pagado)
    """
    require_admin_token(payload.token)
    validate_tenant_id(payload.tenant_id)

    gc = get_gspread_client()
    tenant = get_tenant_or_404(gc, payload.tenant_id)

    bot_token = str(tenant.get("bot_token", "")).strip()
    if not bot_token:
        raise HTTPException(status_code=500, detail="bot_token is not set for this tenant")

    offset = _TG_OFFSETS.get(payload.tenant_id, 0)

    data = tg_call(bot_token, "getUpdates", {"timeout": 0, "offset": offset})
    updates = data.get("result", []) or []

    processed = 0
    actions = []

    for upd in updates:
        processed += 1
        update_id = int(upd.get("update_id", 0))
        # avanzamos offset
        if update_id >= offset:
            offset = update_id + 1

        # callback de botón
        if "callback_query" in upd:
            cq = upd["callback_query"]
            cq_id = cq.get("id", "")
            cb_data = (cq.get("data") or "").strip()
            msg = cq.get("message") or {}
            chat = msg.get("chat") or {}
            chat_id = str(chat.get("id", "")).strip()
            message_id = msg.get("message_id")

            parsed = parse_callback_data(cb_data)
            if not parsed:
                tg_answer_callback(bot_token, cq_id, "Callback inválido")
                actions.append({"type": "callback_invalid"})
                continue

            if parsed.get("action") != "PAID":
                tg_answer_callback(bot_token, cq_id, "Acción no soportada")
                actions.append({"type": "callback_unsupported"})
                continue

            tenant_id = parsed["tenant_id"]
            order_id = parsed["order_id"]
            admin_chat_id = parsed["admin_chat_id"]

            try:
                res = mark_paid_internal(gc, tenant_id, order_id, admin_chat_id)
                tg_answer_callback(bot_token, cq_id, "Marcado como pagado ✅")

                # Editar el mensaje para que quede claro
                if chat_id and isinstance(message_id, int):
                    suffix = "\n\n<b>✅ Estado:</b> PAID"
                    new_text = (msg.get("text") or "") + suffix
                    tg_edit_message(bot_token, chat_id, message_id, new_text)

                actions.append({"type": "paid_ok", "order_id": order_id, "already_paid": res.get("already_paid", False)})
            except Exception as e:
                tg_answer_callback(bot_token, cq_id, "Error al marcar pagado")
                actions.append({"type": "paid_error", "error": str(e)})

            continue

        # mensaje normal (texto)
        if "message" in upd:
            m = upd["message"]
            chat = m.get("chat") or {}
            chat_id = str(chat.get("id", "")).strip()
            text = (m.get("text") or "").strip()

            if chat_id and text:
                # Respuesta mínima demo
                tg_send_message(
                    bot_token,
                    chat_id,
                    "Hola 👋\nEste bot está en modo DEMO.\nPara probar pedidos, créalos desde Swagger (/orders/create)."
                )
                actions.append({"type": "msg_replied", "chat_id": chat_id})

    _TG_OFFSETS[payload.tenant_id] = offset
    return {"ok": True, "processed": processed, "actions": actions, "next_offset": offset}
