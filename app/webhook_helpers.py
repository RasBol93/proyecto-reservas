import json
import re
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


def get_sess(tenant_id: str, chat_id: int) -> Dict[str, Any]:
    key = (tenant_id, chat_id)
    if key not in SESSIONS:
        SESSIONS[key] = {"cart": [], "stage": "idle", "tmp": {}}
    return SESSIONS[key]


def clear_sess(tenant_id: str, chat_id: int) -> None:
    key = (tenant_id, chat_id)
    SESSIONS[key] = {"cart": [], "stage": "idle", "tmp": {}}


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


def get_admin_username(tenant: Dict[str, Any]) -> str:
    return (tenant.get("admin_username") or "").strip().lstrip("@")


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
        name = menu_idx[sku]["name"]
        price = float(menu_idx[sku]["price"])
        lines.append(f"- {qty} x {name} ({price:.0f})")
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
        lines.append(f"- {qty} x {name} ({unit_price:.0f}) = {line_total:.0f}")

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
        f"🧾 *Resumen de tu pedido*\n"
        f"ID: `{order_id}`\n"
        f"Cliente: *{customer_name}*\n"
        f"Contacto: `{customer_contact}`\n"
        f"Hora recogida: *{requested_time}*\n"
        f"Cantidad total: *{total_qty}*\n"
        f"Total: *{total:.2f}* BOB\n\n"
        f"*Detalle:*\n{detail_lines}\n"
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


def get_business_status_safe(orders_sh, tenant_tz: str) -> Dict[str, Any]:
    try:
        return resolve_business_status(orders_sh=orders_sh, tenant_tz=tenant_tz).__dict__
    except Exception as e:
        log_event("business_status_resolve_failed", error=str(e), tenant_tz=tenant_tz)
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


def safe_int(v: Any) -> Optional[int]:
    try:
        return int(v)
    except Exception:
        return None


def assert_admin_authorized(tenant: Dict[str, Any], chat_id: int, tenant_id: str) -> None:
    admin_chat_id = get_admin_chat_id(tenant)
    if admin_chat_id is None:
        log_event("admin_chat_id_missing_security_warning", tenant_id=tenant_id, chat_id=chat_id)
        return
    if chat_id != admin_chat_id:
        log_event("admin_paid_unauthorized", tenant_id=tenant_id, chat_id=chat_id, expected_admin_chat_id=admin_chat_id)
        raise HTTPException(status_code=403, detail="Not authorized")


def contact_link_for_admin(tenant: Dict[str, Any]) -> Optional[str]:
    u = get_admin_username(tenant)
    if u:
        return f"https://t.me/{urllib.parse.quote(u)}"
    admin_chat_id = get_admin_chat_id(tenant)
    if admin_chat_id:
        return f"tg://user?id={admin_chat_id}"
    return None


def set_menu_photo_url(orders_sh, sku: str, photo_url: str) -> bool:
    ws = orders_sh.worksheet("Menu")
    values = ws.get_all_values()
    if not values:
        return False

    header_row_1based = detect_header_row(
        values,
        required_headers=["sku", "name", "price", "active", "category"],
        max_scan=10,
    )
    header = values[header_row_1based - 1]

    try:
        sku_col = header.index("sku") + 1
    except ValueError:
        return False

    try:
        photo_col = header.index("photo_url") + 1
    except ValueError:
        photo_col = len(header) + 1
        ws.update_cell(header_row_1based, photo_col, "photo_url")
        header.append("photo_url")

    found = False
    for i in range(header_row_1based + 1, len(values) + 1):
        row = values[i - 1]
        sku_val = row[sku_col - 1] if len(row) >= sku_col else ""
        if str(sku_val).strip() == sku:
            ws.update_cell(i, photo_col, photo_url)
            found = True
            break

    return found


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
        ["📊 Estadísticas"],
        ["⚙️ Config días y horarios"],
        ["⚙️ Config menú y precios"],
    ], resize=True, one_time=False)


def admin_periods_inline_kb(tenant_id: str, periods: List[Tuple[str, str]]) -> Dict[str, Any]:
    rows = []
    for label, key in periods:
        rows.append([(f"📊 {label}", f"admin_stats_period|{tenant_id}|{key}")])
    return kb(rows)
