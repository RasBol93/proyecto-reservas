import os
import json
import re
import time
import urllib.request
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple
from collections import deque

import gspread
from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel, Field

# =========================
# Config & helpers
# =========================

APP_NAME = "proyecto-reservas"

ENV_CONFIG_SPREADSHEET_ID = "RESERVACIONES_CONFIG"
ENV_GCP_CREDS_JSON = "GCP_CREDENTIALS_JSON"
ENV_ADMIN_TOKEN = "ADMIN_TOKEN"

MAX_ITEMS_PER_ORDER = 30
MAX_NAME_LEN = 80
MAX_CONTACT_LEN = 30
MAX_REQUESTED_TIME_LEN = 60
MAX_SOURCE_LEN = 20

TENANT_ID_RE = re.compile(r"^[a-z0-9_]{2,40}$")
ORDER_ID_RE = re.compile(r"^[a-f0-9]{8}$")  # secrets.token_hex(4) => 8 chars hex

ALLOWED_DELIVERY_TYPES = {"pickup"}  # SOLO PICKUP (como pediste)
ALLOWED_SOURCES = {"api", "swagger", "telegram", "whatsapp"}

# Rate limits (por tenant, por minuto)
RL_MENU_PER_MIN = 120
RL_CREATE_PER_MIN = 60
RL_MARKPAID_PER_MIN = 60

# Telegram
TELEGRAM_API_BASE = "https://api.telegram.org"


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
    safe = {k: v for k, v in fields.items() if k not in ("creds", "token", "GCP_CREDENTIALS_JSON", "ADMIN_TOKEN")}
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
    # Para Telegram, esto es chat_id (número). Para otros canales puede variar más adelante.
    if not re.match(r"^\+?\d{3,20}$", c):
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
    info = json.loads(creds)

    scopes = [
        "https://www.googleapis.com/auth/spreadsheets",
        "https://www.googleapis.com/auth/drive",
    ]
    return gspread.service_account_from_dict(info, scopes=scopes)


def get_config_spreadsheet(gc: gspread.Client) -> gspread.Spreadsheet:
    sid = os.getenv(ENV_CONFIG_SPREADSHEET_ID, "").strip()
    if not sid:
        raise RuntimeError(f"Missing env var: {ENV_CONFIG_SPREADSHEET_ID}")
    return gc.open_by_key(sid)


def get_ws(spreadsheet: gspread.Spreadsheet, title: str) -> gspread.Worksheet:
    return spreadsheet.worksheet(title)


# Cache simple en memoria
_TENANTS_CACHE: Dict[str, Dict[str, Any]] = {}
_TENANTS_CACHE_AT: Optional[str] = None


def _pick_first_nonempty(*vals: Any) -> str:
    for v in vals:
        s = (v or "")
        s = str(s).strip()
        if s:
            return s
    return ""


def load_tenants(gc: gspread.Client, force: bool = False) -> Dict[str, Dict[str, Any]]:
    """
    Soporta nombres nuevos y compatibilidad con los viejos:
      Nuevos:
        admin_bot_token, webhook_secret_admin,
        client_bot_token, webhook_secret_client
      Viejos (fallback):
        bot_token, webhook_secret
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

        admin_bot_token = _pick_first_nonempty(r.get("admin_bot_token"), r.get("bot_token"))
        client_bot_token = _pick_first_nonempty(r.get("client_bot_token"))

        webhook_secret_admin = _pick_first_nonempty(r.get("webhook_secret_admin"), r.get("webhook_secret"))
        webhook_secret_client = _pick_first_nonempty(r.get("webhook_secret_client"))

        tenants[tid] = {
            "tenant_id": tid,
            "name": r.get("name", ""),
            "business_type": r.get("business_type", ""),
            "orders_sheet_id": str(r.get("orders_sheet_id", "")).strip(),
            "bookings_enabled": to_bool(r.get("bookings_enabled", "")),
            "orders_enabled": to_bool(r.get("orders_enabled", "")),

            "admin_bot_token": admin_bot_token,
            "client_bot_token": client_bot_token,
            "webhook_secret_admin": webhook_secret_admin,
            "webhook_secret_client": webhook_secret_client,

            # Compat
            "bot_token": admin_bot_token,
            "webhook_secret": webhook_secret_admin,

            "admin_chat_id": str(r.get("admin_chat_id", "")).strip(),
            "timezone": r.get("timezone", "America/La_Paz"),
            "active": to_bool(r.get("active", "")),
            "admin_whatsapp": (r.get("admin_whatsapp", "") or "").strip(),
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
    return gc.open_by_key(sid)


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
            "category": r.get("category", "") or "Otros",
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
        if sku not in menu_idx:
            raise HTTPException(status_code=422, detail=f"Unknown sku in items: {sku}")
        qty_i = int(qty)
        if qty_i <= 0:
            raise HTTPException(status_code=422, detail=f"qty must be >= 1 for sku={sku}")
        total += float(menu_idx[sku]["price"]) * qty_i
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
        "notes": "",  # NO NOTAS (como pediste)
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


def find_order_fields(orders_sh: gspread.Spreadsheet, order_id: str) -> Dict[str, Any]:
    """
    Devuelve campos útiles del pedido buscándolo en la hoja Orders.
    Necesario para notificar al cliente al marcar PAID.
    """
    ws = get_ws(orders_sh, "Orders")
    values = ws.get_all_values()
    if not values:
        return {"found": False}

    headers = values[0]
    headers_norm = [normalize(h) for h in headers]

    def get_col(name: str) -> int:
        if name in headers_norm:
            return headers_norm.index(name)
        return -1

    i_order = get_col("order_id")
    if i_order < 0:
        return {"found": False}

    # campos opcionales
    i_contact = get_col("customer_contact")
    i_name = get_col("customer_name")
    i_total = get_col("total_amount")

    for row in values[1:]:
        if i_order < len(row) and str(row[i_order]).strip().lower() == order_id.lower():
            out = {"found": True, "order_id": order_id}
            if i_contact >= 0 and i_contact < len(row):
                out["customer_contact"] = str(row[i_contact]).strip()
            if i_name >= 0 and i_name < len(row):
                out["customer_name"] = str(row[i_name]).strip()
            if i_total >= 0 and i_total < len(row):
                out["total_amount"] = str(row[i_total]).strip()
            return out

    return {"found": False}


def gen_order_id() -> str:
    import secrets
    return secrets.token_hex(4)


# ---------- Content / FAQ (desde Sheets) ----------

def load_content_map(orders_sh: gspread.Spreadsheet) -> Dict[str, str]:
    """
    Lee tab Content: key,value,active
    """
    ws = get_ws(orders_sh, "Content")
    rows = read_records_manual(ws, required_headers=["key", "value", "active"])
    out: Dict[str, str] = {}
    for r in rows:
        if not to_bool(r.get("active", "")):
            continue
        k = normalize(r.get("key", ""))
        v = str(r.get("value", "")).strip()
        if k and v:
            out[k] = v
    return out


def load_faq_list(orders_sh: gspread.Spreadsheet) -> List[Dict[str, Any]]:
    """
    Lee tab FAQ: id,question,answer,active,priority
    """
    ws = get_ws(orders_sh, "FAQ")
    rows = read_records_manual(ws, required_headers=["id", "question", "answer", "active", "priority"])
    out: List[Dict[str, Any]] = []
    for r in rows:
        if not to_bool(r.get("active", "")):
            continue
        fid = str(r.get("id", "")).strip()
        q = str(r.get("question", "")).strip()
        a = str(r.get("answer", "")).strip()
        try:
            p = int(str(r.get("priority", "")).strip() or "999")
        except Exception:
            p = 999
        if fid and q and a:
            out.append({"id": fid, "question": q, "answer": a, "priority": p})
    out.sort(key=lambda x: x["priority"])
    return out


def load_pickup_slots(orders_sh: gspread.Spreadsheet) -> List[str]:
    """
    Desde Content:
      pickup_time_mode = slots
      pickup_time_slots = "12:00,12:30,13:00"
    """
    try:
        content = load_content_map(orders_sh)
    except Exception:
        return []

    mode = normalize(content.get("pickup_time_mode", ""))
    if mode != "slots":
        return []

    raw = content.get("pickup_time_slots", "").strip()
    if not raw:
        return []

    slots = []
    for part in raw.split(","):
        s = part.strip()
        if not s:
            continue
        # validación liviana (HH:MM o "ahora")
        if s.lower() == "ahora":
            slots.append("ahora")
            continue
        if re.match(r"^\d{1,2}:\d{2}$", s):
            slots.append(s)
    return slots[:24]  # límite razonable


# =========================
# Telegram helpers
# =========================

def telegram_api_call(bot_token: str, method: str, payload: Dict[str, Any]) -> Dict[str, Any]:
    if not bot_token:
        raise RuntimeError("bot_token missing")

    url = f"{TELEGRAM_API_BASE}/bot{bot_token}/{method}"
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"}, method="POST")

    try:
        with urllib.request.urlopen(req, timeout=12) as resp:
            raw = resp.read().decode("utf-8")
            return json.loads(raw)
    except Exception as e:
        log_event("telegram_api_error", method=method, error=str(e))
        return {"ok": False, "error": str(e)}


def telegram_send_text(bot_token: str, chat_id: int, text: str, reply_markup: Optional[Dict[str, Any]] = None) -> None:
    payload: Dict[str, Any] = {"chat_id": chat_id, "text": text}
    if reply_markup:
        payload["reply_markup"] = reply_markup
    res = telegram_api_call(bot_token, "sendMessage", payload)
    log_event("telegram_send_text", ok=res.get("ok", False))


def telegram_answer_callback(bot_token: str, callback_query_id: str, text: str) -> None:
    res = telegram_api_call(bot_token, "answerCallbackQuery", {"callback_query_id": callback_query_id, "text": text})
    log_event("telegram_answer_callback", ok=res.get("ok", False))


def format_order_message(
    tenant: Dict[str, Any],
    order_id: str,
    customer_name: str,
    customer_contact: str,
    items_list: List[Dict[str, Any]],
    total_amount: float,
    requested_time: str,
) -> str:
    lines = []
    lines.append("🧾 *Nuevo pedido*")
    lines.append(f"🏷️ Tenant: `{tenant.get('tenant_id','')}`")
    lines.append(f"🆔 Order ID: `{order_id}`")
    lines.append(f"👤 Cliente: {customer_name}")
    lines.append(f"📞 Contacto: {customer_contact}")
    lines.append("🚚 Tipo: pickup")
    lines.append(f"⏰ Hora: {requested_time}")
    lines.append("")
    lines.append("*Items:*")
    for it in items_list:
        lines.append(f"• `{it['sku']}` x{it['qty']}")
    lines.append("")
    lines.append(f"💰 Total: *{total_amount} BOB*")
    return "\n".join(lines)


def send_order_to_admin_telegram(
    tenant: Dict[str, Any],
    order_id: str,
    customer_name: str,
    customer_contact: str,
    items_list: List[Dict[str, Any]],
    total_amount: float,
    requested_time: str,
) -> None:
    bot_token = (tenant.get("admin_bot_token", "") or "").strip()
    admin_chat_id = str(tenant.get("admin_chat_id", "")).strip()
    if not bot_token or not admin_chat_id:
        log_event("telegram_skip_missing_config", tenant_id=tenant.get("tenant_id"))
        return

    text = format_order_message(
        tenant=tenant,
        order_id=order_id,
        customer_name=customer_name,
        customer_contact=customer_contact,
        items_list=items_list,
        total_amount=total_amount,
        requested_time=requested_time,
    )

    callback_data = f"paid|{tenant['tenant_id']}|{order_id}"

    payload = {
        "chat_id": int(admin_chat_id),
        "text": text,
        "parse_mode": "Markdown",
        "reply_markup": {"inline_keyboard": [[{"text": "✅ Pagado", "callback_data": callback_data}]]},
    }

    res = telegram_api_call(bot_token, "sendMessage", payload)
    log_event("telegram_send_order", tenant_id=tenant.get("tenant_id"), order_id=order_id, ok=res.get("ok", False))


def notify_client_paid(tenant: Dict[str, Any], orders_sh: gspread.Spreadsheet, order_id: str) -> None:
    """
    Al marcar PAID, avisa al cliente por el bot CLIENT, si customer_contact es chat_id numérico.
    """
    client_token = (tenant.get("client_bot_token", "") or "").strip()
    if not client_token:
        log_event("client_notify_skip_missing_token", tenant_id=tenant.get("tenant_id"), order_id=order_id)
        return

    info = find_order_fields(orders_sh, order_id)
    if not info.get("found"):
        log_event("client_notify_skip_order_not_found", tenant_id=tenant.get("tenant_id"), order_id=order_id)
        return

    contact = str(info.get("customer_contact", "")).strip()
    if not contact or not re.match(r"^\d{3,20}$", contact):
        # si no es chat_id numérico, no podemos mandar por Telegram
        log_event("client_notify_skip_invalid_contact", tenant_id=tenant.get("tenant_id"), order_id=order_id, contact=contact)
        return

    chat_id = int(contact)
    name = info.get("customer_name", "") or "cliente"
    total = info.get("total_amount", "")

    msg = f"✅ ¡Listo, {name}! Tu pedido *{order_id}* fue marcado como *PAGADO*."
    if total:
        msg += f"\n💰 Total: {total} BOB"
    msg += "\n\nGracias. 🙌"

    telegram_send_text(client_token, chat_id, msg)


def resolve_bot_by_secret(tenant: Dict[str, Any], secret: str) -> Tuple[str, str]:
    s = (secret or "").strip()
    admin_secret = (tenant.get("webhook_secret_admin", "") or "").strip()
    client_secret = (tenant.get("webhook_secret_client", "") or "").strip()

    if admin_secret and s == admin_secret:
        return ("admin", (tenant.get("admin_bot_token", "") or "").strip())
    if client_secret and s == client_secret:
        return ("client", (tenant.get("client_bot_token", "") or "").strip())

    raise HTTPException(status_code=403, detail="Invalid webhook secret")


# =========================
# Client bot state (in-memory)
# =========================

CLIENT_STATE: Dict[str, Dict[str, Any]] = {}

def _state_key(tenant_id: str, chat_id: int) -> str:
    return f"{tenant_id}:{chat_id}"

def get_client_state(tenant_id: str, chat_id: int) -> Dict[str, Any]:
    key = _state_key(tenant_id, chat_id)
    if key not in CLIENT_STATE:
        CLIENT_STATE[key] = {
            "step": "HOME",          # HOME | PICK_CAT | PICK_PROD | PICK_QTY | ASK_NAME | ASK_TIME
            "cart": [],              # list of {"sku":..., "qty":...}
            "pending_sku": None,
            "selected_cat": None,
            "customer_name": "",
            "requested_time": "",
        }
    return CLIENT_STATE[key]

def cart_add(state: Dict[str, Any], sku: str, qty: int) -> None:
    sku = str(sku).strip()
    qty = int(qty)
    for it in state["cart"]:
        if it["sku"] == sku:
            it["qty"] += qty
            return
    state["cart"].append({"sku": sku, "qty": qty})

def cart_clear(state: Dict[str, Any]) -> None:
    state["cart"] = []
    state["pending_sku"] = None

def cart_text_and_total(state: Dict[str, Any], menu_idx: Dict[str, Dict[str, Any]]) -> Tuple[str, float]:
    if not state["cart"]:
        return ("Tu carrito está vacío.", 0.0)
    lines = ["🛒 Carrito:"]
    total = 0.0
    for it in state["cart"]:
        sku = it["sku"]
        qty = it["qty"]
        name = menu_idx.get(sku, {}).get("name", sku)
        price = float(menu_idx.get(sku, {}).get("price", 0))
        subtotal = price * qty
        total += subtotal
        lines.append(f"- {name} ({sku}) x{qty} = {subtotal:.2f} BOB")
    lines.append(f"\nTotal: {total:.2f} BOB")
    return ("\n".join(lines), round(total, 2))


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
    admin_chat_id: str

class MarkPaidOut(BaseModel):
    ok: bool
    order_id: str
    status: str
    old_status: Optional[str] = None
    already_paid: Optional[bool] = None


# =========================
# FastAPI App
# =========================

app = FastAPI(title=APP_NAME, version="2.0.1-client-pro")


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
        delivery_type=delivery_type,
        requested_time=requested_time,
        status="PENDING_PAYMENT",
        source=source,
        total_amount=total_amount,
    )

    try:
        send_order_to_admin_telegram(
            tenant=tenant,
            order_id=order_id,
            customer_name=name,
            customer_contact=contact,
            items_list=items_list,
            total_amount=total_amount,
            requested_time=requested_time,
        )
    except Exception as e:
        log_event("telegram_send_exception", tenant_id=payload.tenant_id, order_id=order_id, error=str(e))

    log_event("order_created", tenant_id=payload.tenant_id, order_id=order_id, total_amount=total_amount, source=source)
    return OrderCreateOut(ok=True, order_id=order_id, total_amount=total_amount, currency="BOB")


@app.post("/orders/mark_paid", response_model=MarkPaidOut)
def mark_paid(payload: MarkPaidIn):
    validate_tenant_id(payload.tenant_id)
    validate_order_id(payload.order_id)
    _rate_limiter.hit(f"mark_paid:{payload.tenant_id}", RL_MARKPAID_PER_MIN)

    gc = get_gspread_client()
    tenant = get_tenant_or_404(gc, payload.tenant_id)

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
    if normalize(old_status) == "paid":
        return MarkPaidOut(ok=True, order_id=payload.order_id, status="PAID", old_status=old_status, already_paid=True)

    if normalize(old_status) not in ("pending_payment", "pendingpayment", ""):
        raise HTTPException(status_code=409, detail=f"Cannot mark paid from status={old_status}")

    # ✅ NEW: notificar cliente si es pedido Telegram (chat_id numérico)
    try:
        notify_client_paid(tenant, orders_sh, payload.order_id)
    except Exception as e:
        log_event("client_notify_exception", tenant_id=payload.tenant_id, order_id=payload.order_id, error=str(e))

    return MarkPaidOut(ok=True, order_id=payload.order_id, status="PAID", old_status=old_status, already_paid=False)


# =========================
# Telegram webhook (admin + client)
# =========================

def kb(rows: List[List[Tuple[str, str]]]) -> Dict[str, Any]:
    return {"inline_keyboard": [[{"text": t, "callback_data": c} for (t, c) in row] for row in rows]}

def main_menu_kb() -> Dict[str, Any]:
    return kb([
        [("📋 Ver Menú", "menu")],
        [("📍 Ver Ubicación", "loc")],
        [("❓ Preguntas Frecuentes", "faq")],
        [("🛒 Carrito", "cart")],
    ])

def polite_use_buttons(bot_token: str, chat_id: int) -> None:
    telegram_send_text(bot_token, chat_id, "Por favor usa los botones para continuar 👇", main_menu_kb())

def pickup_time_kb(slots: List[str]) -> Dict[str, Any]:
    # 2 botones por fila
    rows = []
    row = []
    for s in slots[:24]:
        row.append((s, f"time|{s}"))
        if len(row) == 2:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    rows.append([("⬅️ Volver", "cart")])
    return kb(rows)

@app.post("/telegram/webhook/{tenant_id}/{secret}")
async def telegram_webhook(tenant_id: str, secret: str, update: Dict[str, Any]):
    validate_tenant_id(tenant_id)

    gc = get_gspread_client()
    tenant = get_tenant_or_404(gc, tenant_id)
    mode, bot_token = resolve_bot_by_secret(tenant, secret)
    if not bot_token:
        log_event("telegram_missing_bot_token", tenant_id=tenant_id, mode=mode)
        return {"ok": True}

    orders_sh = open_orders_spreadsheet(gc, tenant)

    # 1) Callback query
    cb = update.get("callback_query")
    if cb:
        data = (cb.get("data") or "").strip()
        chat_id = int(cb["message"]["chat"]["id"])
        cb_id = cb.get("id")

        # ACK rápido
        if cb_id:
            telegram_answer_callback(bot_token, cb_id, "OK")

        # ADMIN: paid|tenant|order_id
        if mode == "admin":
            from_user = cb.get("from") or {}
            from_id = str(from_user.get("id", "")).strip()
            expected_admin_chat_id = str(tenant.get("admin_chat_id", "")).strip()
            if expected_admin_chat_id and from_id != expected_admin_chat_id:
                raise HTTPException(status_code=403, detail="Not allowed")

            parts = data.split("|")
            if len(parts) == 3 and parts[0] == "paid":
                cb_tenant_id = parts[1].strip()
                order_id = parts[2].strip().lower()
                if cb_tenant_id != tenant_id:
                    raise HTTPException(status_code=400, detail="Tenant mismatch in callback data")
                validate_order_id(order_id)

                result = update_order_status(orders_sh, order_id, "PAID")
                if not result.get("found"):
                    if cb_id:
                        telegram_answer_callback(bot_token, cb_id, "⚠️ No encontré ese pedido")
                    return {"ok": True}

                old_status = str(result.get("old_status", "") or "")
                already_paid = normalize(old_status) == "paid"
                if cb_id:
                    telegram_answer_callback(bot_token, cb_id, "✅ Marcado como PAID" if not already_paid else "✅ Ya estaba PAID")

                # ✅ NEW: notificar cliente
                try:
                    notify_client_paid(tenant, orders_sh, order_id)
                except Exception as e:
                    log_event("client_notify_exception", tenant_id=tenant_id, order_id=order_id, error=str(e))

                return {"ok": True}

            return {"ok": True}

        # CLIENT callbacks
        if mode == "client":
            state = get_client_state(tenant_id, chat_id)
            menu_idx = load_menu_index(orders_sh)
            cats = group_menu_by_category(menu_idx)

            # HOME shortcuts
            if data in ("home", "menu", "loc", "faq", "cart"):
                if data == "home":
                    state["step"] = "HOME"
                    telegram_send_text(bot_token, chat_id, "Elige una opción:", main_menu_kb())
                    return {"ok": True}

                if data == "menu":
                    if not cats:
                        telegram_send_text(bot_token, chat_id, "No hay menú activo.", main_menu_kb())
                        return {"ok": True}
                    rows = []
                    for c in sorted(cats.keys(), key=lambda x: normalize(x)):
                        rows.append([(c, f"cat|{normalize(c)}")])
                    rows.append([("⬅️ Volver", "home"), ("🛒 Carrito", "cart")])
                    state["step"] = "PICK_CAT"
                    telegram_send_text(bot_token, chat_id, "📋 Elige una categoría:", kb(rows))
                    return {"ok": True}

                if data == "loc":
                    try:
                        content = load_content_map(orders_sh)
                        text = content.get("location_text", "Ubicación no configurada.")
                        maps = content.get("location_maps_url", "")
                        if maps:
                            text = f"{text}\n\n🗺 {maps}"
                        telegram_send_text(bot_token, chat_id, text, main_menu_kb())
                    except Exception:
                        telegram_send_text(bot_token, chat_id, "Ubicación no disponible (Content no configurado).", main_menu_kb())
                    return {"ok": True}

                if data == "faq":
                    try:
                        faqs = load_faq_list(orders_sh)
                        if not faqs:
                            telegram_send_text(bot_token, chat_id, "No hay FAQs activas.", main_menu_kb())
                            return {"ok": True}
                        rows = []
                        for f in faqs[:10]:
                            rows.append([(f["question"], f"faq|{f['id']}")])
                        rows.append([("⬅️ Volver", "home")])
                        telegram_send_text(bot_token, chat_id, "❓ Preguntas frecuentes:", kb(rows))
                    except Exception:
                        telegram_send_text(bot_token, chat_id, "FAQs no disponibles (FAQ no configurado).", main_menu_kb())
                    return {"ok": True}

                if data == "cart":
                    text, _ = cart_text_and_total(state, menu_idx)
                    rows = []
                    if state["cart"]:
                        rows.append([("✅ Confirmar pedido", "confirm"), ("🗑 Vaciar", "clear")])
                        rows.append([("➕ Seguir comprando", "menu")])
                    rows.append([("⬅️ Volver", "home")])
                    telegram_send_text(bot_token, chat_id, text, kb(rows))
                    return {"ok": True}

            # FAQ answer
            if data.startswith("faq|"):
                fid = data.split("|", 1)[1].strip()
                faqs = load_faq_list(orders_sh)
                found = next((f for f in faqs if f["id"] == fid), None)
                if not found:
                    telegram_send_text(bot_token, chat_id, "FAQ no encontrada.", main_menu_kb())
                    return {"ok": True}
                telegram_send_text(bot_token, chat_id, f"❓ {found['question']}\n\n{found['answer']}", main_menu_kb())
                return {"ok": True}

            # category -> products
            if data.startswith("cat|"):
                cat_norm = data.split("|", 1)[1].strip()
                real_cat = None
                for c in cats.keys():
                    if normalize(c) == cat_norm:
                        real_cat = c
                        break
                if not real_cat:
                    telegram_send_text(bot_token, chat_id, "Categoría no encontrada.", main_menu_kb())
                    return {"ok": True}

                items = cats.get(real_cat, [])
                if not items:
                    telegram_send_text(bot_token, chat_id, "No hay productos activos.", main_menu_kb())
                    return {"ok": True}

                rows = []
                for it in items[:20]:
                    label = f"{it['name']} ({it['price']:.0f})"
                    rows.append([(label, f"prd|{it['sku']}")])
                rows.append([("⬅️ Categorías", "menu"), ("🛒 Carrito", "cart")])
                state["step"] = "PICK_PROD"
                state["selected_cat"] = real_cat
                telegram_send_text(bot_token, chat_id, f"🍽 {real_cat} — elige un producto:", kb(rows))
                return {"ok": True}

            # product -> qty
            if data.startswith("prd|"):
                sku = data.split("|", 1)[1].strip()
                if sku not in menu_idx:
                    telegram_send_text(bot_token, chat_id, "Producto no disponible.", main_menu_kb())
                    return {"ok": True}
                state["pending_sku"] = sku
                p = menu_idx[sku]
                rows = [
                    [("1", f"qty|{sku}|1"), ("2", f"qty|{sku}|2"), ("3", f"qty|{sku}|3"), ("4", f"qty|{sku}|4")],
                    [("⬅️ Volver", "menu"), ("🛒 Carrito", "cart")],
                ]
                state["step"] = "PICK_QTY"
                telegram_send_text(bot_token, chat_id, f"🧮 Cantidad para: {p['name']} ({p['price']:.0f} BOB)", kb(rows))
                return {"ok": True}

            # qty -> add cart
            if data.startswith("qty|"):
                parts = data.split("|")
                if len(parts) != 3:
                    return {"ok": True}
                sku = parts[1].strip()
                qty = int(parts[2])
                if sku not in menu_idx:
                    telegram_send_text(bot_token, chat_id, "Producto no disponible.", main_menu_kb())
                    return {"ok": True}
                cart_add(state, sku, qty)
                p = menu_idx[sku]
                rows = [
                    [("➕ Seguir comprando", "menu")],
                    [("🛒 Ver carrito", "cart")],
                    [("⬅️ Inicio", "home")],
                ]
                telegram_send_text(bot_token, chat_id, f"✅ Agregado: {p['name']} x{qty}", kb(rows))
                return {"ok": True}

            # clear cart
            if data == "clear":
                cart_clear(state)
                telegram_send_text(bot_token, chat_id, "Carrito vaciado.", main_menu_kb())
                return {"ok": True}

            # confirm -> ask name
            if data == "confirm":
                if not state["cart"]:
                    telegram_send_text(bot_token, chat_id, "Tu carrito está vacío.", main_menu_kb())
                    return {"ok": True}
                state["step"] = "ASK_NAME"
                telegram_send_text(bot_token, chat_id, "Por favor escribe tu *nombre* (solo texto):")
                return {"ok": True}

            # time selected from slots
            if data.startswith("time|"):
                if state.get("step") != "ASK_TIME":
                    # si llega fuera de lugar, volvemos al home
                    polite_use_buttons(bot_token, chat_id)
                    return {"ok": True}
                selected = data.split("|", 1)[1].strip()
                selected = validate_requested_time(selected)
                state["requested_time"] = selected
                # Forzamos creación como si hubiera escrito texto
                # Simulamos entrada en la misma lógica de mensajes (abajo)
                # (La creación final ocurre en el bloque ASK_TIME de mensajes)
                # Aquí solo pedimos que escriba cualquier cosa? No.
                # Mejor: creamos pedido aquí.
                menu_idx2 = load_menu_index(orders_sh)
                if not state["cart"]:
                    state["step"] = "HOME"
                    telegram_send_text(bot_token, chat_id, "Tu carrito está vacío.", main_menu_kb())
                    return {"ok": True}

                total_amount = calc_total_amount(state["cart"], menu_idx2)
                order_id = gen_order_id()
                customer_contact = str(chat_id)
                validate_contact(customer_contact)

                append_order_row(
                    orders_sh=orders_sh,
                    tenant_id=tenant_id,
                    order_id=order_id,
                    customer_name=state["customer_name"],
                    customer_contact=customer_contact,
                    items=state["cart"],
                    delivery_type="pickup",
                    requested_time=state["requested_time"],
                    status="PENDING_PAYMENT",
                    source="telegram",
                    total_amount=total_amount,
                )

                try:
                    send_order_to_admin_telegram(
                        tenant=tenant,
                        order_id=order_id,
                        customer_name=state["customer_name"],
                        customer_contact=customer_contact,
                        items_list=state["cart"],
                        total_amount=total_amount,
                        requested_time=state["requested_time"],
                    )
                except Exception as e:
                    log_event("telegram_send_exception", tenant_id=tenant_id, order_id=order_id, error=str(e))

                cart_clear(state)
                state["customer_name"] = ""
                state["requested_time"] = ""
                state["step"] = "HOME"

                telegram_send_text(
                    bot_token,
                    chat_id,
                    f"✅ Pedido creado\nID: {order_id}\nTotal: {total_amount:.2f} BOB\nEstado: PENDING_PAYMENT",
                    main_menu_kb(),
                )
                return {"ok": True}

            return {"ok": True}

        return {"ok": True}

    # 2) Mensaje normal (admin/client)
    msg = update.get("message") or update.get("edited_message")
    if not msg:
        return {"ok": True}

    chat = msg.get("chat") or {}
    chat_id = chat.get("id")
    text_in = (msg.get("text") or "").strip()
    if chat_id is None:
        return {"ok": True}
    chat_id_int = int(chat_id)

    # Admin bot: debug mínimo
    if mode == "admin":
        expected_admin_chat_id = str(tenant.get("admin_chat_id", "")).strip()
        if expected_admin_chat_id and str(chat_id_int) != expected_admin_chat_id:
            return {"ok": True}
        if text_in:
            telegram_send_text(bot_token, chat_id_int, "OK admin ✅")
        return {"ok": True}

    # Client bot
    state = get_client_state(tenant_id, chat_id_int)

    # /start
    if normalize(text_in) in ("start", "/start"):
        state["step"] = "HOME"
        telegram_send_text(bot_token, chat_id_int, "Bienvenido. Elige una opción:", main_menu_kb())
        return {"ok": True}

    # Si escribe "hola" u otra cosa en cualquier estado donde deben usarse botones:
    if state["step"] in ("HOME", "PICK_CAT", "PICK_PROD", "PICK_QTY"):
        # ✅ NEW: instrucción clara
        polite_use_buttons(bot_token, chat_id_int)
        return {"ok": True}

    # Captura nombre
    if state["step"] == "ASK_NAME":
        name = text_in.strip()
        if not name or len(name) > MAX_NAME_LEN:
            telegram_send_text(bot_token, chat_id_int, "Nombre inválido. Intenta nuevamente:")
            return {"ok": True}
        state["customer_name"] = name

        # ✅ NEW: si hay slots configurados, mostramos botones
        slots = load_pickup_slots(orders_sh)
        state["step"] = "ASK_TIME"
        if slots:
            telegram_send_text(bot_token, chat_id_int, "Elige la hora de recojo:", pickup_time_kb(slots))
        else:
            telegram_send_text(bot_token, chat_id_int, "¿A qué hora deseas recoger? (ej: ahora, 19:30)")
        return {"ok": True}

    # Captura hora (modo libre) y crea orden
    if state["step"] == "ASK_TIME":
        slots = load_pickup_slots(orders_sh)
        if slots:
            # Si hay slots, no aceptamos texto libre: debe usar botones
            polite_use_buttons(bot_token, chat_id_int)
            return {"ok": True}

        requested = validate_requested_time(text_in)
        state["requested_time"] = requested

        menu_idx = load_menu_index(orders_sh)
        if not state["cart"]:
            state["step"] = "HOME"
            telegram_send_text(bot_token, chat_id_int, "Tu carrito está vacío.", main_menu_kb())
            return {"ok": True}

        total_amount = calc_total_amount(state["cart"], menu_idx)
        order_id = gen_order_id()

        customer_contact = str(chat_id_int)
        validate_contact(customer_contact)

        append_order_row(
            orders_sh=orders_sh,
            tenant_id=tenant_id,
            order_id=order_id,
            customer_name=state["customer_name"],
            customer_contact=customer_contact,
            items=state["cart"],
            delivery_type="pickup",
            requested_time=state["requested_time"],
            status="PENDING_PAYMENT",
            source="telegram",
            total_amount=total_amount,
        )

        try:
            send_order_to_admin_telegram(
                tenant=tenant,
                order_id=order_id,
                customer_name=state["customer_name"],
                customer_contact=customer_contact,
                items_list=state["cart"],
                total_amount=total_amount,
                requested_time=state["requested_time"],
            )
        except Exception as e:
            log_event("telegram_send_exception", tenant_id=tenant_id, order_id=order_id, error=str(e))

        cart_clear(state)
        state["customer_name"] = ""
        state["requested_time"] = ""
        state["step"] = "HOME"

        telegram_send_text(
            bot_token,
            chat_id_int,
            f"✅ Pedido creado\nID: {order_id}\nTotal: {total_amount:.2f} BOB\nEstado: PENDING_PAYMENT",
            main_menu_kb(),
        )
        return {"ok": True}

    # fallback final
    polite_use_buttons(bot_token, chat_id_int)
    return {"ok": True}
