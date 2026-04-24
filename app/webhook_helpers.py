# app/webhook_helpers.py

import json
import re
import time
import urllib.parse
from typing import Any, Dict, Optional, List, Tuple

from fastapi import HTTPException

from app.menu import calc_total_amount
from app.sheets import detect_header_row
from app.telegram_api import reply_kb
from app.telegram_keyboard import kb
from app.utils import normalize, log_event
from app.admin_settings import resolve_business_status


SESSIONS: Dict[Tuple[str, int], Dict[str, Any]] = {}

REMINDER_COOLDOWN_SECONDS = 5 * 60
CONTACT_AFTER_SECONDS = 10 * 60  # 5 min cooldown + 5 min extra

# housekeeping simple para evitar crecimiento infinito
_SESSION_LAST_TOUCH: Dict[Tuple[str, int], float] = {}
_SESSION_TTL_SECONDS = 6 * 60 * 60  # 6 horas
_SESSION_CLEANUP_EVERY = 200
_SESSION_OPS = 0

# rate limit simple en memoria para hardening futuro
_RATE_LIMIT_STATE: Dict[Tuple[str, int, str], List[float]] = {}
_RATE_LIMIT_TTL_SECONDS = 15 * 60
_RATE_LIMIT_CLEANUP_EVERY = 200
_RATE_LIMIT_OPS = 0


# -------------------------
# Session internals
# -------------------------

def _safe_session_key(tenant_id: str, chat_id: int) -> Tuple[str, int]:
    try:
        return (str(tenant_id or ""), int(chat_id))
    except Exception:
        return (str(tenant_id or ""), 0)


def _touch_session_key(key: Tuple[str, int]) -> None:
    _SESSION_LAST_TOUCH[key] = time.time()


def _cleanup_sessions_if_needed() -> None:
    global _SESSION_OPS
    _SESSION_OPS += 1

    if _SESSION_OPS % _SESSION_CLEANUP_EVERY != 0:
        return

    now = time.time()
    stale_before = now - _SESSION_TTL_SECONDS

    stale_keys = [k for k, ts in _SESSION_LAST_TOUCH.items() if ts < stale_before]
    for k in stale_keys:
        SESSIONS.pop(k, None)
        _SESSION_LAST_TOUCH.pop(k, None)


def _ensure_session_shape(sess: Dict[str, Any]) -> Dict[str, Any]:
    if not isinstance(sess, dict):
        sess = {}

    if not isinstance(sess.get("cart"), list):
        sess["cart"] = []

    if not isinstance(sess.get("stage"), str) or not str(sess.get("stage") or "").strip():
        sess["stage"] = "idle"

    if not isinstance(sess.get("tmp"), dict):
        sess["tmp"] = {}

    return sess


# -------------------------
# Public session helpers
# -------------------------

def get_sess(tenant_id: str, chat_id: int) -> Dict[str, Any]:
    key = _safe_session_key(tenant_id, chat_id)

    if key not in SESSIONS:
        SESSIONS[key] = {"cart": [], "stage": "idle", "tmp": {}}

    sess = _ensure_session_shape(SESSIONS.get(key) or {})
    SESSIONS[key] = sess

    _touch_session_key(key)
    _cleanup_sessions_if_needed()
    _cleanup_rate_limit_if_needed()
    return sess


def clear_sess(tenant_id: str, chat_id: int) -> None:
    key = _safe_session_key(tenant_id, chat_id)
    SESSIONS[key] = {"cart": [], "stage": "idle", "tmp": {}}
    _touch_session_key(key)
    _cleanup_sessions_if_needed()
    _cleanup_rate_limit_if_needed()


def delete_sess(tenant_id: str, chat_id: int) -> None:
    key = _safe_session_key(tenant_id, chat_id)
    SESSIONS.pop(key, None)
    _SESSION_LAST_TOUCH.pop(key, None)


def session_cache_info() -> Dict[str, Any]:
    return {
        "sessions_count": len(SESSIONS),
        "ttl_seconds": _SESSION_TTL_SECONDS,
        "cleanup_every_ops": _SESSION_CLEANUP_EVERY,
        "last_touch_count": len(_SESSION_LAST_TOUCH),
    }


# -------------------------
# Rate limiting helpers
# -------------------------

def _safe_rate_limit_key(tenant_id: str, chat_id: int, bucket: str) -> Tuple[str, int, str]:
    try:
        return (str(tenant_id or ""), int(chat_id), str(bucket or "").strip())
    except Exception:
        return (str(tenant_id or ""), 0, str(bucket or "").strip())


def _cleanup_rate_limit_if_needed() -> None:
    global _RATE_LIMIT_OPS
    _RATE_LIMIT_OPS += 1

    if _RATE_LIMIT_OPS % _RATE_LIMIT_CLEANUP_EVERY != 0:
        return

    now = time.time()
    stale_before = now - _RATE_LIMIT_TTL_SECONDS

    stale_keys = []
    for k, values in _RATE_LIMIT_STATE.items():
        if not values:
            stale_keys.append(k)
            continue
        if max(values) < stale_before:
            stale_keys.append(k)

    for k in stale_keys:
        _RATE_LIMIT_STATE.pop(k, None)


def rate_limit_allow(
    tenant_id: str,
    chat_id: int,
    bucket: str,
    limit: int,
    window_seconds: int,
) -> bool:
    key = _safe_rate_limit_key(tenant_id, chat_id, bucket)
    now = time.time()
    window_start = now - max(1, int(window_seconds))

    values = _RATE_LIMIT_STATE.get(key) or []
    values = [ts for ts in values if ts >= window_start]

    allowed = len(values) < max(1, int(limit))
    if allowed:
        values.append(now)

    _RATE_LIMIT_STATE[key] = values
    _cleanup_rate_limit_if_needed()
    return allowed


def rate_limit_reset(tenant_id: str, chat_id: int, bucket: str) -> None:
    key = _safe_rate_limit_key(tenant_id, chat_id, bucket)
    _RATE_LIMIT_STATE.pop(key, None)


def rate_limit_cache_info() -> Dict[str, Any]:
    return {
        "entries_count": len(_RATE_LIMIT_STATE),
        "ttl_seconds": _RATE_LIMIT_TTL_SECONDS,
        "cleanup_every_ops": _RATE_LIMIT_CLEANUP_EVERY,
    }


# -------------------------
# Bot / chat helpers
# -------------------------

def get_admin_bot_token(tenant: Dict[str, Any]) -> str:
    return (tenant.get("admin_bot_token") or tenant.get("bot_token_admin") or "").strip()


def get_client_bot_token(tenant: Dict[str, Any]) -> str:
    return (tenant.get("client_bot_token") or tenant.get("bot_token_client") or "").strip()


def get_admin_chat_id(tenant: Dict[str, Any]) -> Optional[int]:
    raw = (tenant.get("admin_chat_id") or "").strip()
    if not raw:
        return None
    try:
        return int(raw)
    except Exception:
        return None


# =========================
# OWNER SUPPORT
# =========================

def get_owner_bot_token(tenant: Dict[str, Any]) -> str:
    return (tenant.get("owner_bot_token") or "").strip()


def get_owner_chat_id(tenant: Dict[str, Any]) -> Optional[int]:
    raw = (tenant.get("owner_chat_id") or "").strip()
    if not raw:
        return None
    try:
        return int(raw)
    except Exception:
        return None


def is_owner_enabled(tenant: Dict[str, Any]) -> bool:
    v = str(tenant.get("owner_enabled") or "").strip().lower()
    return v in ("true", "1", "yes")


def get_user_role(tenant: Dict[str, Any], chat_id: int) -> str:
    admin_chat_id = get_admin_chat_id(tenant)
    owner_chat_id = get_owner_chat_id(tenant)

    if admin_chat_id and chat_id == admin_chat_id:
        return "admin"

    if is_owner_enabled(tenant) and owner_chat_id and chat_id == owner_chat_id:
        return "owner"

    return "unknown"


# =========================
# AUTH
# =========================

def assert_admin_authorized(tenant: Dict[str, Any], chat_id: int, tenant_id: str) -> None:
    role = get_user_role(tenant, chat_id)

    if role in ("admin", "owner"):
        return

    admin_chat_id = get_admin_chat_id(tenant)
    if admin_chat_id is None:
        log_event("admin_chat_id_missing_security_warning", tenant_id=tenant_id, chat_id=chat_id)
        return

    log_event(
        "admin_paid_unauthorized",
        tenant_id=tenant_id,
        chat_id=chat_id,
        expected_admin_chat_id=admin_chat_id,
    )
    raise HTTPException(status_code=403, detail="Not authorized")


def get_admin_username(tenant: Dict[str, Any]) -> str:
    return (tenant.get("admin_username") or "").strip().lstrip("@")


# -------------------------
# Payment QR helpers
# -------------------------

def get_payment_qr_file_id(tenant: Dict[str, Any]) -> str:
    return (tenant.get("payment_qr_file_id") or "").strip()


def _drive_file_id_from_url(url: str) -> Optional[str]:
    if not url:
        return None
    m = re.search(r"/file/d/([a-zA-Z0-9_-]+)", url)
    if m:
        return m.group(1)
    m = re.search(r"[?&]id=([a-zA-Z0-9_-]+)", url)
    if m:
        return m.group(1)
    return None


def _normalize_public_qr_url(url: str) -> str:
    url = (url or "").strip()
    if not url:
        return ""
    file_id = _drive_file_id_from_url(url)
    if file_id:
        return f"https://drive.google.com/uc?export=download&id={file_id}"
    return url


def get_payment_qr_url(tenant: Dict[str, Any]) -> str:
    raw = (tenant.get("payment_qr_url") or tenant.get("payment_qr_link") or "").strip()
    return _normalize_public_qr_url(raw)


# -------------------------
# Order / cart formatting
# -------------------------

def parse_items_field(items_field: Any) -> List[Dict[str, Any]]:
    if isinstance(items_field, list):
        return items_field

    if isinstance(items_field, str) and items_field.strip():
        try:
            v = json.loads(items_field)
            return v if isinstance(v, list) else []
        except Exception:
            return []

    return []


def fmt_cart_lines(cart: List[Dict[str, Any]], menu_idx: Dict[str, Any]) -> Tuple[str, float, int]:
    total_qty = 0
    lines = []
    items_for_total = []

    for it in cart:
        sku = (it.get("sku") or "").strip()
        try:
            qty = int(it.get("qty") or 1)
        except Exception:
            qty = 1
        qty = max(1, qty)

        if sku not in menu_idx:
            continue

        total_qty += qty
        name = str(menu_idx[sku]["name"] or "").strip()
        price = float(menu_idx[sku]["price"])
        line_total = price * qty
        lines.append(f"• {qty} x {name} — Bs {line_total:.2f}")
        items_for_total.append({"sku": sku, "qty": qty})

    total = calc_total_amount(items_for_total, menu_idx) if items_for_total else 0.0
    return ("\n".join(lines) if lines else "(vacío)"), total, total_qty


def fmt_snapshot_lines(items_snapshot: List[Dict[str, Any]]) -> Tuple[str, float, int]:
    total_qty = 0
    total = 0.0
    lines = []

    for it in items_snapshot or []:
        name = str(it.get("name") or it.get("sku") or "").strip()
        try:
            qty = int(it.get("qty") or 1)
        except Exception:
            qty = 1
        qty = max(1, qty)

        try:
            unit_price = float(it.get("unit_price") or 0)
        except Exception:
            unit_price = 0.0

        try:
            line_total = float(it.get("line_total") or (unit_price * qty))
        except Exception:
            line_total = unit_price * qty

        total_qty += qty
        total += line_total
        lines.append(f"• {qty} x {name} — Bs {line_total:.2f}")

    return ("\n".join(lines) if lines else "(vacío)"), float(total), int(total_qty)


def build_order_recap_text(
    order_id: str,
    customer_name: str,
    customer_contact: str,
    requested_time: str,
    detail_lines: str,
    total_qty: int,
    total: float,
) -> str:
    return (
        "🧾 *Resumen de tu pedido*\n"
        f"Código de pedido: `{order_id}`\n"
        f"Cliente: *{customer_name}*\n"
        f"Contacto: `{customer_contact}`\n"
        f"Hora de recojo: *{requested_time}*\n"
        f"Resumen: *{total_qty}*\n"
        f"Total: *Bs {total:.2f}*\n\n"
        f"*Detalle:*\n{detail_lines}"
    )


def fmt_price_short(v: Any) -> str:
    try:
        n = round(float(v), 2)
    except Exception:
        return "0"
    s = f"{n:.2f}"
    if s.endswith("00"):
        return str(int(round(n)))
    if s.endswith("0"):
        return s[:-1]
    return s


def extract_first_number(text: str) -> Optional[float]:
    s = str(text or "").strip().lower()
    if not s:
        return None

    s = s.replace(",", ".")
    m = re.search(r"(-?\d+(?:\.\d+)?)", s)
    if not m:
        return None

    try:
        return float(m.group(1))
    except Exception:
        return None


# -------------------------
# Business status helpers
# -------------------------

def _default_business_status(tenant_tz: str) -> Dict[str, Any]:
    return {
        "tenant_tz": tenant_tz,
        "now_local_iso": "",
        "today_weekday_code": "",
        "is_open_today": True,
        "accepts_orders_now": True,
        "open_time": "",
        "close_time": "",
        "last_order_time": "",
        "weekly_open_days": [],
        "today_closed": False,
        "today_open_force": False,
        "has_open_override": False,
        "has_close_override": False,
        "has_last_order_override": False,
        "public_message": "",
    }


def get_business_status_safe(orders_sh, tenant_tz: str, settings_map=None) -> Dict[str, Any]:
    try:
        res = resolve_business_status(orders_sh=orders_sh, tenant_tz=tenant_tz, settings_map=settings_map)
        if hasattr(res, "__dict__"):
            return res.__dict__
        if isinstance(res, dict):
            return res
        return _default_business_status(tenant_tz)
    except Exception as e:
        log_event("business_status_resolve_failed", error=str(e), tenant_tz=tenant_tz)
        return _default_business_status(tenant_tz)


def business_block_message(bs: Dict[str, Any]) -> str:
    public_message = str(bs.get("public_message") or "").strip()
    if public_message:
        return public_message

    if bs.get("today_closed"):
        return "Hoy no estamos atendiendo."
    if bs.get("is_open_today") and not bs.get("accepts_orders_now"):
        last_order_time = str(bs.get("last_order_time") or "").strip()
        if last_order_time:
            return f"Por hoy ya no estamos tomando pedidos. La última hora de pedido era {last_order_time}."
        return "Por hoy ya no estamos tomando pedidos."

    return "En este momento no estamos aceptando pedidos."


def send_business_blocked_text(bs: Dict[str, Any]) -> str:
    msg = business_block_message(bs)
    return f"⛔ {msg}"


# -------------------------
# Misc helpers
# -------------------------

def safe_int(v: Any) -> Optional[int]:
    try:
        return int(v)
    except Exception:
        return None


def contact_link_for_admin(tenant: Dict[str, Any]) -> Optional[str]:
    u = get_admin_username(tenant)
    if u:
        return f"https://t.me/{urllib.parse.quote(u)}"
    admin_chat_id = get_admin_chat_id(tenant)
    if admin_chat_id:
        return f"tg://user?id={admin_chat_id}"
    return None


def set_menu_photo_url(orders_sh, sku: str, photo_url: str) -> bool:
    try:
        ws = orders_sh.worksheet("Menu")
    except Exception:
        return False

    try:
        values = ws.get_all_values()
    except Exception:
        return False

    if not values:
        return False

    header_row_1based = detect_header_row(
        values,
        required_headers=["sku", "name", "price", "active", "category"],
        max_scan=10,
    )
    if header_row_1based < 1 or header_row_1based > len(values):
        return False

    header = values[header_row_1based - 1]
    header_norm = [normalize(h) for h in header]

    def _find_col(header_norm_list: List[str], key: str) -> Optional[int]:
        nk = normalize(key)
        for idx, h in enumerate(header_norm_list):
            if h == nk:
                return idx + 1  # 1-based
        return None

    sku_col = _find_col(header_norm, "sku")
    if sku_col is None:
        return False

    photo_col = _find_col(header_norm, "photo_url")
    if photo_col is None:
        photo_col = len(header) + 1
        try:
            ws.update_cell(header_row_1based, photo_col, "photo_url")
        except Exception:
            return False
        header.append("photo_url")
        header_norm.append("photo_url")

    found = False
    for i in range(header_row_1based + 1, len(values) + 1):
        row = values[i - 1]
        sku_val = row[sku_col - 1] if len(row) >= sku_col else ""
        if str(sku_val).strip() == sku:
            try:
                ws.update_cell(i, photo_col, str(photo_url or "").strip())
            except Exception:
                return False
            found = True
            break

    return found


# -------------------------
# Keyboard builders
# -------------------------

def client_home_kb() -> Dict[str, Any]:
    return kb([
        [("📋 Ver menú", "menu")],
        [("🛒 Ver carrito", "cart")],
    ])


def cart_kb(has_items: bool) -> Dict[str, Any]:
    rows = []
    if has_items:
        rows.append([("✅ Confirmar pedido", "cart_confirm")])
        rows.append([("🧹 Vaciar carrito", "cart_clear")])
    rows.append([("⬅️ Seguir comprando", "menu")])
    rows.append([("🏠 Inicio", "home")])
    return kb(rows)


def build_client_cart_manage_kb(cart: List[Dict[str, Any]], menu_idx: Dict[str, Any]) -> Dict[str, Any]:
    rows: List[List[Tuple[str, str]]] = []

    for it in cart:
        sku = str(it.get("sku") or "").strip()
        if not sku or sku not in menu_idx:
            continue

        name = str(menu_idx[sku].get("name") or sku).strip()
        try:
            qty = int(it.get("qty") or 1)
        except Exception:
            qty = 1
        qty = max(1, qty)

        rows.append([(f"{name} x{qty}", "home")])
        rows.append([
            ("➖", f"cdec|{sku}"),
            ("➕", f"cinc|{sku}"),
            ("🗑", f"crem|{sku}"),
        ])

    has_items = bool(cart)
    if has_items:
        rows.append([("✅ Confirmar pedido", "cart_confirm")])
        rows.append([("🧹 Vaciar carrito", "cart_clear")])

    rows.append([("⬅️ Seguir comprando", "menu")])
    rows.append([("🏠 Inicio", "home")])
    return kb(rows)


def i_paid_kb(tenant_id: str, order_id: str) -> Dict[str, Any]:
    return kb([
        [("✅ Ya pagué", f"i_paid|{tenant_id}|{order_id}")],
        [("🏠 Inicio", "home")],
    ])


def paid_actions_kb(tenant_id: str, order_id: str) -> Dict[str, Any]:
    return kb([
        [("🔔 Recordar al administrador", f"remind|{tenant_id}|{order_id}")],
        [("🏠 Inicio", "home")],
    ])


def contact_admin_kb(tenant_id: str, order_id: str) -> Dict[str, Any]:
    return kb([
        [("💬 Contactar al administrador", f"contact|{tenant_id}|{order_id}")],
        [("🏠 Inicio", "home")],
    ])


def admin_fixed_kb() -> Dict[str, Any]:
    return reply_kb([
        ["⚙️ Panel"],
    ], resize=True, one_time=False)


def admin_periods_inline_kb(tenant_id: str, periods: List[Tuple[str, str]]) -> Dict[str, Any]:
    rows = []
    for label, key in periods:
        rows.append([(f"📊 {label}", f"admin_stats_period|{tenant_id}|{key}")])
    return kb(rows)
