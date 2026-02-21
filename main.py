import os
import json
import re
import time
import urllib.request
from datetime import datetime, timezone, timedelta
from typing import Any, Dict, List, Optional, Tuple
from collections import deque

from zoneinfo import ZoneInfo

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
ORDER_ID_RE = re.compile(r"^[a-f0-9]{8}$")

ALLOWED_DELIVERY_TYPES = {"pickup"}
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
            "orders_enabled": to_bool(r.get("orders_enabled", "")),
            "admin_bot_token": admin_bot_token,
            "client_bot_token": client_bot_token,
            "webhook_secret_admin": webhook_secret_admin,
            "webhook_secret_client": webhook_secret_client,
            "admin_chat_id": str(r.get("admin_chat_id", "")).strip(),
            "timezone": (r.get("timezone", "") or "America/La_Paz").strip(),
            "active": to_bool(r.get("active", "")),
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
        idx[sku] = {"sku": sku, "name": r.get("name", ""), "price": price, "category": r.get("category", "") or "Otros"}
    return idx


def group_menu_by_category(menu_idx: Dict[str, Dict[str, Any]]) -> Dict[str, List[Dict[str, Any]]]:
    cats: Dict[str, List[Dict[str, Any]]] = {}
    for _, item in menu_idx.items():
        cat = item.get("category", "") or "Otros"
        cats.setdefault(cat, []).append({"sku": item["sku"], "name": item.get("name", ""), "price": item.get("price", 0), "category": cat})
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


def ensure_orders_headers(ws: gspread.Worksheet, required: List[str]) -> None:
    values = ws.get_all_values()
    if not values or not values[0]:
        raise HTTPException(status_code=500, detail="Orders sheet is empty or missing headers in row 1")
    headers = values[0]
    headers_norm = [normalize(h) for h in headers]
    missing = [h for h in required if normalize(h) not in headers_norm]
    if missing:
        raise HTTPException(status_code=500, detail=f"Orders sheet missing required headers in row 1: {missing}")


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
    ensure_orders_headers(ws, required=[
        "order_id", "created_at", "tenant_id", "customer_name", "customer_contact",
        "items", "notes", "delivery_type", "requested_time", "status", "source", "total_amount"
    ])

    payload_map: Dict[str, Any] = {
        "order_id": order_id,
        "created_at": now_iso_utc(),
        "tenant_id": tenant_id,
        "customer_name": customer_name,
        "customer_contact": customer_contact,
        "items": json.dumps(items, ensure_ascii=False),
        "notes": "",
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

    headers_norm = [normalize(h) for h in values[0]]
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
# Content / Schedule
# =========================

def load_content_map(orders_sh: gspread.Spreadsheet) -> Dict[str, str]:
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


def _parse_hhmm(s: str) -> Optional[Tuple[int, int]]:
    s = (s or "").strip()
    m = re.match(r"^(\d{1,2}):(\d{2})$", s)
    if not m:
        return None
    hh = int(m.group(1))
    mm = int(m.group(2))
    if hh < 0 or hh > 23 or mm < 0 or mm > 59:
        return None
    return (hh, mm)


def _round_up_to_slot(dt: datetime, slot_minutes: int) -> datetime:
    # Redondeo hacia arriba al próximo múltiplo de slot_minutes
    if slot_minutes <= 0:
        return dt
    minute = dt.minute
    mod = minute % slot_minutes
    if mod == 0 and dt.second == 0:
        return dt.replace(second=0, microsecond=0)
    add = slot_minutes - mod
    rounded = dt + timedelta(minutes=add)
    return rounded.replace(second=0, microsecond=0)


def _weekday_token(dt: datetime) -> str:
    return ["mon","tue","wed","thu","fri","sat","sun"][dt.weekday()]


def compute_pickup_slots(tenant: Dict[str, Any], content: Dict[str, str]) -> List[str]:
    """
    computed:
      pickup_open_time=11:00
      pickup_last_time=21:30
      pickup_slot_minutes=30
      pickup_lead_minutes=20
      pickup_days=mon,tue,wed,thu,fri,sat,sun (opcional)
    manual:
      pickup_time_slots=...
    """
    mode = normalize(content.get("pickup_schedule_mode", ""))

    tz = ZoneInfo((tenant.get("timezone") or "America/La_Paz").strip())
    now_local = datetime.now(tz)

    days_raw = normalize(content.get("pickup_days", "mon,tue,wed,thu,fri,sat,sun"))
    allowed_days = set([d.strip() for d in days_raw.split(",") if d.strip()])

    if mode == "manual":
        raw = content.get("pickup_time_slots", "").strip()
        if not raw:
            return []
        slots = []
        for part in raw.split(","):
            t = part.strip()
            if not t:
                continue
            if t.lower() == "ahora":
                slots.append("ahora")
                continue
            if _parse_hhmm(t):
                slots.append(t)
        # filtramos futuros (en manual, también los filtramos)
        return filter_future_slots_for_today_or_tomorrow(now_local, slots)

    # default computed
    open_t = _parse_hhmm(content.get("pickup_open_time", ""))
    last_t = _parse_hhmm(content.get("pickup_last_time", ""))
    try:
        slot_minutes = int(str(content.get("pickup_slot_minutes", "30")).strip() or "30")
    except Exception:
        slot_minutes = 30
    try:
        lead_minutes = int(str(content.get("pickup_lead_minutes", "0")).strip() or "0")
    except Exception:
        lead_minutes = 0

    if not open_t or not last_t:
        return []

    def build_for_date(base_date: datetime) -> List[datetime]:
        hh1, mm1 = open_t
        hh2, mm2 = last_t
        start = base_date.replace(hour=hh1, minute=mm1, second=0, microsecond=0)
        end = base_date.replace(hour=hh2, minute=mm2, second=0, microsecond=0)
        if end < start:
            return []
        out = []
        cur = start
        while cur <= end:
            out.append(cur)
            cur = cur + timedelta(minutes=slot_minutes)
        return out

    # elegimos hoy si está permitido; sino mañana (buscamos hasta 7 días)
    candidates: List[datetime] = []
    for add_days in range(0, 7):
        day = now_local + timedelta(days=add_days)
        if _weekday_token(day) not in allowed_days:
            continue
        candidates = build_for_date(day)
        if candidates:
            # filtramos futuros con lead time
            min_dt = now_local + timedelta(minutes=lead_minutes)
            min_dt = _round_up_to_slot(min_dt, slot_minutes)
            future = [d for d in candidates if d >= min_dt]
            if future:
                return [d.strftime("%H:%M") for d in future[:24]]
            # si hoy ya no da, seguimos buscando mañana
            continue

    return []


def filter_future_slots_for_today_or_tomorrow(now_local: datetime, slots: List[str]) -> List[str]:
    """
    Para manual slots tipo "HH:MM": devuelve solo los que sean >= ahora (hoy),
    y si no queda ninguno, devuelve los de mañana (los mismos slots).
    """
    hhmm = [s for s in slots if _parse_hhmm(s)]
    if not hhmm:
        return slots

    today_ok = []
    for s in hhmm:
        hh, mm = _parse_hhmm(s)
        dt = now_local.replace(hour=hh, minute=mm, second=0, microsecond=0)
        if dt >= now_local:
            today_ok.append(s)

    if today_ok:
        return today_ok[:24]

    # si hoy ya pasó todo, mostramos "mañana": mismos HH:MM, no filtramos por ahora
    return hhmm[:24]


def normalize_user_time_to_slot(tenant: Dict[str, Any], content: Dict[str, str], user_text: str) -> Tuple[Optional[str], str]:
    """
    Convierte texto libre a un slot válido:
    - acepta "19" => 19:00
    - acepta "19:10" => redondea al próximo slot
    - valida que esté dentro de apertura/cierre y que sea futuro.
    Retorna: (slot_elegido_o_None, mensaje_error_o_vacio)
    """
    tz = ZoneInfo((tenant.get("timezone") or "America/La_Paz").strip())
    now_local = datetime.now(tz)

    mode = normalize(content.get("pickup_schedule_mode", "computed"))

    # Si hay slots calculables, usamos esos como fuente de verdad
    slots = compute_pickup_slots(tenant, content)
    if not slots:
        # fallback: solo aceptar texto (no podemos validar bien)
        t = user_text.strip()
        if not t:
            return (None, "Hora inválida.")
        return (t, "")

    txt = (user_text or "").strip().lower()

    # casos simples
    if re.match(r"^\d{1,2}$", txt):
        hh = int(txt)
        if 0 <= hh <= 23:
            cand = f"{hh:02d}:00"
            if cand in slots:
                return (cand, "")
            # si no está exacto, buscamos el primer slot >= cand
            for s in slots:
                if s >= cand:
                    return (s, "")
            return (None, "Ese horario ya no está disponible hoy. Elige uno de los sugeridos.")

    hhmm = _parse_hhmm(txt)
    if hhmm:
        cand = f"{hhmm[0]:02d}:{hhmm[1]:02d}"
        if cand in slots:
            return (cand, "")
        # buscamos el siguiente slot >= cand
        for s in slots:
            if s >= cand:
                return (s, "")
        return (None, "Ese horario ya no está disponible. Elige uno de los sugeridos.")

    # si escribe "ahora"
    if txt == "ahora":
        return (slots[0], "")  # primer slot futuro

    return (None, "No entendí la hora. Escribe por ejemplo 19 o 19:30, o elige un botón.")


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


def send_order_to_admin_telegram(tenant: Dict[str, Any], order_id: str, customer_name: str, customer_contact: str, items_list: List[Dict[str, Any]], total_amount: float, requested_time: str) -> None:
    bot_token = (tenant.get("admin_bot_token", "") or "").strip()
    admin_chat_id = str(tenant.get("admin_chat_id", "")).strip()
    if not bot_token or not admin_chat_id:
        return
    text = (
        "🧾 *Nuevo pedido*\n"
        f"🏷️ Tenant: `{tenant.get('tenant_id','')}`\n"
        f"🆔 Order ID: `{order_id}`\n"
        f"👤 Cliente: {customer_name}\n"
        f"📞 Contacto: {customer_contact}\n"
        "🚚 Tipo: pickup\n"
        f"⏰ Hora: {requested_time}\n\n"
        "*Items:*\n" + "\n".join([f"• `{it['sku']}` x{it['qty']}" for it in items_list]) +
        f"\n\n💰 Total: *{total_amount} BOB*"
    )
    callback_data = f"paid|{tenant['tenant_id']}|{order_id}"
    payload = {
        "chat_id": int(admin_chat_id),
        "text": text,
        "parse_mode": "Markdown",
        "reply_markup": {"inline_keyboard": [[{"text": "✅ Pagado", "callback_data": callback_data}]]},
    }
    telegram_api_call(bot_token, "sendMessage", payload)


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
# Client bot state
# =========================

CLIENT_STATE: Dict[str, Dict[str, Any]] = {}

def _state_key(tenant_id: str, chat_id: int) -> str:
    return f"{tenant_id}:{chat_id}"

def get_client_state(tenant_id: str, chat_id: int) -> Dict[str, Any]:
    key = _state_key(tenant_id, chat_id)
    if key not in CLIENT_STATE:
        CLIENT_STATE[key] = {
            "step": "HOME",
            "cart": [],
            "customer_name": "",
            "requested_time": "",
        }
    return CLIENT_STATE[key]


def cart_add(state: Dict[str, Any], sku: str, qty: int) -> None:
    for it in state["cart"]:
        if it["sku"] == sku:
            it["qty"] += qty
            return
    state["cart"].append({"sku": sku, "qty": qty})

def cart_clear(state: Dict[str, Any]) -> None:
    state["cart"] = []

def cart_text_and_total(state: Dict[str, Any], menu_idx: Dict[str, Dict[str, Any]]) -> Tuple[str, float]:
    if not state["cart"]:
        return ("Tu carrito está vacío.", 0.0)
    total = 0.0
    lines = ["🛒 Carrito:"]
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


# =========================
# FastAPI App
# =========================

app = FastAPI(title=APP_NAME, version="2.1.0-schedule")


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
def get_menu(tenant_id: str = Query(...)):
    validate_tenant_id(tenant_id)
    _rate_limiter.hit(f"menu:{tenant_id}", RL_MENU_PER_MIN)
    gc = get_gspread_client()
    tenant = get_tenant_or_404(gc, tenant_id)
    if not tenant.get("orders_enabled", False):
        raise HTTPException(status_code=400, detail=f"Orders not enabled for tenant: {tenant_id}")
    orders_sh = open_orders_spreadsheet(gc, tenant)
    menu_idx = load_menu_index(orders_sh)
    return {"ok": True, "tenant_id": tenant_id, "categories": group_menu_by_category(menu_idx)}


@app.get("/pickup/slots")
def get_pickup_slots(tenant_id: str = Query(...)):
    """
    ✅ Para WhatsApp/ManyChat también: un endpoint que devuelve slots futuros
    """
    validate_tenant_id(tenant_id)
    gc = get_gspread_client()
    tenant = get_tenant_or_404(gc, tenant_id)
    orders_sh = open_orders_spreadsheet(gc, tenant)
    content = load_content_map(orders_sh)
    slots = compute_pickup_slots(tenant, content)
    return {"ok": True, "tenant_id": tenant_id, "slots": slots}


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
    source = validate_source(payload.source)
    requested_time_raw = validate_requested_time(payload.requested_time)

    orders_sh = open_orders_spreadsheet(gc, tenant)
    content = load_content_map(orders_sh)

    # ✅ NEW: validar/normalizar requested_time contra schedule si está configurado
    slot, err = normalize_user_time_to_slot(tenant, content, requested_time_raw)
    if err:
        raise HTTPException(status_code=422, detail=err)
    requested_time = slot or requested_time_raw

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

    # Notificar admin (si es un tenant con admin bot)
    try:
        send_order_to_admin_telegram(tenant, order_id, name, contact, items_list, total_amount, requested_time)
    except Exception as e:
        log_event("telegram_send_exception", tenant_id=payload.tenant_id, order_id=order_id, error=str(e))

    return OrderCreateOut(ok=True, order_id=order_id, total_amount=total_amount, currency="BOB")
