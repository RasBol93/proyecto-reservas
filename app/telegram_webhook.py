# app/telegram_webhook.py

import json
import re
import time
import urllib.request
import urllib.parse
from typing import Any, Dict, Optional, List, Tuple, Set

from fastapi import APIRouter, HTTPException

from app.config import TELEGRAM_API_BASE
from app.tenants import get_tenant_or_404, resolve_bot_by_secret
from app.sheets import get_gspread_client, open_spreadsheet_by_key, detect_header_row
from app.menu import (
    load_menu_index,
    group_menu_by_category,
    calc_total_amount,
    load_menu_admin_index,
    group_menu_admin_by_category,
    get_menu_product_or_404,
    set_menu_product_active,
    set_menu_product_price,
    invalidate_menu_cache,
)
from app.orders import (
    append_order_row,
    update_order_status,
    update_order_payment_proof,
    find_latest_pending_order_for_contact,
    get_order_by_id,
    gen_order_id,
    build_items_snapshot,
)
from app.telegram_keyboard import kb
from app.utils import normalize, log_event, now_iso_utc

from app.stats import build_periods, resolve_period, build_stats_report_text, log_event_to_sheet
from app.admin_settings import resolve_business_status

router = APIRouter()

# =========================================================
# Estado en memoria (DEMO)
# =========================================================
SESSIONS: Dict[Tuple[str, int], Dict[str, Any]] = {}

REMINDER_COOLDOWN_SECONDS = 5 * 60
CONTACT_AFTER_SECONDS = 10 * 60  # 5 min cooldown + 5 min extra

DAY_ORDER = ["lun", "mar", "mie", "jue", "vie", "sab", "dom"]
DAY_LABELS = {
    "lun": "Lun",
    "mar": "Mar",
    "mie": "Mié",
    "jue": "Jue",
    "vie": "Vie",
    "sab": "Sáb",
    "dom": "Dom",
}

ADMIN_SETTINGS_SHEET_NAME = "AdminSettings"

PRICE_STEP_OPTIONS: List[Tuple[str, float]] = [
    ("-10", -10.0),
    ("-5", -5.0),
    ("-1", -1.0),
    ("+1", 1.0),
    ("+5", 5.0),
    ("+10", 10.0),
]


def get_sess(tenant_id: str, chat_id: int) -> Dict[str, Any]:
    key = (tenant_id, chat_id)
    if key not in SESSIONS:
        SESSIONS[key] = {"cart": [], "stage": "idle", "tmp": {}}
    return SESSIONS[key]


def clear_sess(tenant_id: str, chat_id: int) -> None:
    key = (tenant_id, chat_id)
    SESSIONS[key] = {"cart": [], "stage": "idle", "tmp": {}}


# -------------------------
# Telegram API helpers
# -------------------------

def telegram_api_call(bot_token: str, method: str, payload: Dict[str, Any]) -> Dict[str, Any]:
    if not bot_token:
        raise RuntimeError("bot_token missing")

    url = f"{TELEGRAM_API_BASE}/bot{bot_token}/{method}"
    data = json.dumps(payload).encode("utf-8")

    req = urllib.request.Request(
        url,
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )

    with urllib.request.urlopen(req, timeout=20) as resp:
        raw = resp.read().decode("utf-8")
        return json.loads(raw)


def telegram_send_text(
    bot_token: str,
    chat_id: int,
    text: str,
    reply_markup: Optional[Dict[str, Any]] = None,
    parse_mode: Optional[str] = None,
) -> bool:
    payload: Dict[str, Any] = {"chat_id": chat_id, "text": text}
    if reply_markup:
        payload["reply_markup"] = reply_markup
    if parse_mode:
        payload["parse_mode"] = parse_mode

    try:
        res = telegram_api_call(bot_token, "sendMessage", payload)
        ok = bool(res.get("ok", False))
        if not ok:
            log_event("telegram_send_failed", chat_id=chat_id, error=res.get("description") or res)
        return ok
    except Exception as e:
        log_event("telegram_send_exception", chat_id=chat_id, error=str(e))
        return False


def telegram_send_photo(bot_token: str, chat_id: int, photo: str, caption: str = "") -> bool:
    payload: Dict[str, Any] = {"chat_id": chat_id, "photo": photo}
    if caption:
        payload["caption"] = caption
    try:
        res = telegram_api_call(bot_token, "sendPhoto", payload)
        ok = bool(res.get("ok", False))
        if not ok:
            log_event("telegram_send_photo_failed", chat_id=chat_id, error=res.get("description") or res)
        return ok
    except Exception as e:
        log_event("telegram_send_photo_exception", chat_id=chat_id, error=str(e))
        return False


def telegram_send_document(bot_token: str, chat_id: int, document: str, caption: str = "") -> bool:
    payload: Dict[str, Any] = {"chat_id": chat_id, "document": document}
    if caption:
        payload["caption"] = caption
    try:
        res = telegram_api_call(bot_token, "sendDocument", payload)
        ok = bool(res.get("ok", False))
        if not ok:
            log_event("telegram_send_document_failed", chat_id=chat_id, error=res.get("description") or res)
        return ok
    except Exception as e:
        log_event("telegram_send_document_exception", chat_id=chat_id, error=str(e))
        return False


def telegram_answer_callback(bot_token: str, callback_query_id: str, text: str = "OK") -> None:
    try:
        res = telegram_api_call(bot_token, "answerCallbackQuery", {"callback_query_id": callback_query_id, "text": text})
        if not res.get("ok", True):
            log_event("telegram_ack_failed", error=res.get("description") or res)
    except Exception as e:
        log_event("telegram_ack_exception", error=str(e))


def reply_kb(button_rows: List[List[str]], resize: bool = True, one_time: bool = False) -> Dict[str, Any]:
    keyboard = [[{"text": txt} for txt in row] for row in button_rows]
    return {
        "keyboard": keyboard,
        "resize_keyboard": bool(resize),
        "one_time_keyboard": bool(one_time),
        "selective": False,
    }


# -------------------------
# Tenant helpers
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


# -------------------------
# Multipart helpers
# -------------------------

def _multipart_encode(fields: Dict[str, str], file_field: str, filename: str, content_type: str, file_bytes: bytes) -> Tuple[bytes, str]:
    boundary = f"----tgBoundary{int(time.time() * 1000)}"
    parts: List[bytes] = []

    for k, v in fields.items():
        parts.append(f"--{boundary}\r\n".encode("utf-8"))
        parts.append(f'Content-Disposition: form-data; name="{k}"\r\n\r\n'.encode("utf-8"))
        parts.append((v or "").encode("utf-8"))
        parts.append(b"\r\n")

    parts.append(f"--{boundary}\r\n".encode("utf-8"))
    parts.append(f'Content-Disposition: form-data; name="{file_field}"; filename="{filename}"\r\n'.encode("utf-8"))
    parts.append(f"Content-Type: {content_type}\r\n\r\n".encode("utf-8"))
    parts.append(file_bytes)
    parts.append(b"\r\n")
    parts.append(f"--{boundary}--\r\n".encode("utf-8"))

    body = b"".join(parts)
    ctype = f"multipart/form-data; boundary={boundary}"
    return body, ctype


def _telegram_get_file_path(bot_token: str, file_id: str) -> str:
    res = telegram_api_call(bot_token, "getFile", {"file_id": file_id})
    if not res.get("ok"):
        raise RuntimeError(f"getFile failed: {res}")
    return res["result"]["file_path"]


def _telegram_download_file_bytes(bot_token: str, file_path: str) -> bytes:
    url = f"{TELEGRAM_API_BASE}/file/bot{bot_token}/{file_path}"
    with urllib.request.urlopen(url, timeout=30) as resp:
        return resp.read()


def _telegram_send_file_bytes_admin(
    admin_token: str,
    method: str,
    chat_id: int,
    file_field: str,
    filename: str,
    content_type: str,
    file_bytes: bytes,
    caption: str = "",
) -> bool:
    url = f"{TELEGRAM_API_BASE}/bot{admin_token}/{method}"
    fields = {"chat_id": str(chat_id)}
    if caption:
        fields["caption"] = caption

    body, ctype = _multipart_encode(fields, file_field, filename, content_type, file_bytes)
    req = urllib.request.Request(url, data=body, headers={"Content-Type": ctype}, method="POST")

    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            raw = resp.read().decode("utf-8")
            data = json.loads(raw)
            ok = bool(data.get("ok", False))
            if not ok:
                log_event("admin_upload_failed", error=data.get("description") or data)
            return ok
    except Exception as e:
        log_event("admin_upload_exception", error=str(e))
        return False


# -------------------------
# Formatting helpers
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


def _fmt_price_short(v: Any) -> str:
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


def _extract_first_number(text: str) -> Optional[float]:
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
# Estado del negocio
# -------------------------

def _get_business_status_safe(orders_sh, tenant_tz: str) -> Dict[str, Any]:
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


def _business_block_message(bs: Dict[str, Any]) -> str:
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


def _send_business_blocked(bot_token: str, chat_id: int, bs: Dict[str, Any]) -> None:
    msg = _business_block_message(bs)
    telegram_send_text(bot_token, chat_id, f"⛔ {msg}")


def _client_orders_allowed_or_notify(bot_token: str, chat_id: int, orders_sh, tenant_tz: str) -> bool:
    bs = _get_business_status_safe(orders_sh=orders_sh, tenant_tz=tenant_tz)
    if bool(bs.get("accepts_orders_now")):
        return True

    _send_business_blocked(bot_token, chat_id, bs)
    return False


# -------------------------
# AdminSettings direct helpers
# -------------------------

def _get_admin_settings_ws(orders_sh):
    try:
        return orders_sh.worksheet(ADMIN_SETTINGS_SHEET_NAME)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Missing worksheet '{ADMIN_SETTINGS_SHEET_NAME}': {e}")


def _admin_headers_map(ws) -> Dict[str, int]:
    values = ws.get_all_values()
    if not values:
        raise HTTPException(status_code=500, detail="AdminSettings is empty")

    header = values[0]
    out: Dict[str, int] = {}
    for i, h in enumerate(header):
        k = normalize(h)
        if k and k not in out:
            out[k] = i + 1
    return out


def _admin_find_row_by_key(ws, key: str) -> Optional[int]:
    values = ws.get_all_values()
    if not values:
        return None

    key_norm = normalize(key).replace(" ", "_")
    for ridx in range(2, len(values) + 1):
        row = values[ridx - 1]
        cell = row[0] if len(row) >= 1 else ""
        if normalize(cell).replace(" ", "_") == key_norm:
            return ridx
    return None


def _admin_upsert_setting(
    orders_sh,
    key: str,
    value: str,
    scope: str,
    updated_by: str,
    notes: str = "",
    active: str = "TRUE",
) -> None:
    ws = _get_admin_settings_ws(orders_sh)
    headers = _admin_headers_map(ws)
    ridx = _admin_find_row_by_key(ws, key)

    now_ts = now_iso_utc()
    key_val = str(key or "").strip()
    value_val = str(value or "").strip()
    scope_val = str(scope or "").strip()
    updated_by_val = str(updated_by or "").strip()
    notes_val = str(notes or "").strip()
    active_val = str(active or "TRUE").strip()

    if ridx is None:
        header_len = max(headers.values()) if headers else 7
        row = [""] * header_len

        def put(col_name: str, val: str) -> None:
            c = headers.get(normalize(col_name))
            if c:
                row[c - 1] = val

        put("key", key_val)
        put("value", value_val)
        put("active", active_val)
        put("scope", scope_val)
        put("updated_at", now_ts)
        put("updated_by", updated_by_val)
        put("notes", notes_val)

        ws.append_row(row, value_input_option="RAW")
        return

    def update_if_exists(col_name: str, val: str) -> None:
        c = headers.get(normalize(col_name))
        if c:
            ws.update_cell(ridx, c, val)

    update_if_exists("key", key_val)
    update_if_exists("value", value_val)
    update_if_exists("active", active_val)
    update_if_exists("scope", scope_val)
    update_if_exists("updated_at", now_ts)
    update_if_exists("updated_by", updated_by_val)
    update_if_exists("notes", notes_val)


def _admin_set_weekly_open_days(orders_sh, days: List[str], updated_by: str) -> None:
    safe_days = [d for d in DAY_ORDER if d in set(days or [])]
    csv = ",".join(safe_days)
    _admin_upsert_setting(
        orders_sh=orders_sh,
        key="weekly_open_days",
        value=csv,
        scope="global",
        updated_by=updated_by,
        notes="dias normales de apertura",
    )


def _admin_set_weekly_normal_hours(
    orders_sh,
    open_time: str,
    close_time: str,
    last_order_time: str,
    updated_by: str,
) -> None:
    _admin_upsert_setting(
        orders_sh=orders_sh,
        key="weekly_open_time",
        value=open_time,
        scope="global",
        updated_by=updated_by,
        notes="hora de apertura normal",
    )
    _admin_upsert_setting(
        orders_sh=orders_sh,
        key="weekly_close_time",
        value=close_time,
        scope="global",
        updated_by=updated_by,
        notes="hora de cierre normal",
    )
    _admin_upsert_setting(
        orders_sh=orders_sh,
        key="weekly_last_order_time",
        value=last_order_time,
        scope="global",
        updated_by=updated_by,
        notes="ultima hora normal de pedido",
    )


def _admin_set_today_closed(orders_sh, enabled: bool, updated_by: str) -> None:
    _admin_upsert_setting(
        orders_sh=orders_sh,
        key="today_closed",
        value="TRUE" if enabled else "FALSE",
        scope="today",
        updated_by=updated_by,
        notes="negocio cerrado hoy",
    )


def _admin_set_today_open_force(orders_sh, enabled: bool, updated_by: str) -> None:
    _admin_upsert_setting(
        orders_sh=orders_sh,
        key="today_open_force",
        value="TRUE" if enabled else "FALSE",
        scope="today",
        updated_by=updated_by,
        notes="abrir excepcionalmente hoy",
    )


def _admin_set_today_open_override(orders_sh, open_time: str, updated_by: str) -> None:
    _admin_upsert_setting(
        orders_sh=orders_sh,
        key="today_open_time_override",
        value=open_time,
        scope="today",
        updated_by=updated_by,
        notes="apertura especial hoy",
    )


def _admin_set_today_close_override(orders_sh, close_time: str, updated_by: str) -> None:
    _admin_upsert_setting(
        orders_sh=orders_sh,
        key="today_close_time_override",
        value=close_time,
        scope="today",
        updated_by=updated_by,
        notes="cierre especial hoy",
    )


def _admin_set_today_last_order_override(orders_sh, last_order_time: str, updated_by: str) -> None:
    _admin_upsert_setting(
        orders_sh=orders_sh,
        key="today_last_order_time_override",
        value=last_order_time,
        scope="today",
        updated_by=updated_by,
        notes="ultima hora especial hoy",
    )


def _admin_restore_habitual(orders_sh, updated_by: str) -> None:
    _admin_set_today_closed(orders_sh, enabled=False, updated_by=updated_by)
    _admin_set_today_open_force(orders_sh, enabled=False, updated_by=updated_by)
    _admin_set_today_open_override(orders_sh, open_time="", updated_by=updated_by)
    _admin_set_today_close_override(orders_sh, close_time="", updated_by=updated_by)
    _admin_set_today_last_order_override(orders_sh, last_order_time="", updated_by=updated_by)


# -------------------------
# Admin config horarios helpers
# -------------------------

def _hhmm_to_compact(hhmm: str) -> str:
    return str(hhmm or "").replace(":", "")


def _compact_to_hhmm(v: str) -> str:
    v = str(v or "").strip()
    if len(v) != 4 or not v.isdigit():
        raise HTTPException(status_code=400, detail=f"Invalid compact time: {v}")
    return f"{v[:2]}:{v[2:]}"


def _build_halfhour_slots() -> List[str]:
    slots: List[str] = []
    hour = 6
    minute = 0
    while True:
        slots.append(f"{hour:02d}:{minute:02d}")
        if hour == 23 and minute == 30:
            break
        minute += 30
        if minute >= 60:
            minute = 0
            hour += 1
    return slots


TIME_SLOTS = _build_halfhour_slots()


def _admin_hours_menu_kb(tenant_id: str) -> Dict[str, Any]:
    return kb([
        [("📅 Días normales", f"admhrs|{tenant_id}|days"), ("🕒 Horario normal", f"admhrs|{tenant_id}|norm")],
        [("🌙 Cerrar más temprano hoy", f"admhrs|{tenant_id}|early"), ("🌅 Abrir más tarde hoy", f"admhrs|{tenant_id}|late")],
        [("🔴 No abrir hoy", f"admhrs|{tenant_id}|closed"), ("✨ Abrir excepcionalmente hoy", f"admhrs|{tenant_id}|openforce")],
        [("🔄 Volver a lo habitual", f"admhrs|{tenant_id}|habitual")],
    ])


def _admin_hours_status_text(bs: Dict[str, Any]) -> str:
    status_txt = "ABIERTO" if bs.get("accepts_orders_now") else "CERRADO / NO DISPONIBLE"
    today_code = str(bs.get("today_weekday_code") or "").strip()
    weekly_days = bs.get("weekly_open_days") or []
    days_txt = ", ".join(weekly_days) or "-"
    today_in_weekly = today_code in set(weekly_days)

    msg = (
        "⚙️ CONFIG DÍAS Y HORARIOS\n\n"
        f"Estado actual: {status_txt}\n"
        f"Día de hoy: {today_code or '-'}\n"
        f"Hoy está dentro de días normales: {'Sí' if today_in_weekly else 'No'}\n"
        f"Abre hoy: {'Sí' if bs.get('is_open_today') else 'No'}\n"
        f"Acepta pedidos ahora: {'Sí' if bs.get('accepts_orders_now') else 'No'}\n"
        f"Hora apertura: {bs.get('open_time') or '-'}\n"
        f"Hora cierre: {bs.get('close_time') or '-'}\n"
        f"Última hora de pedido: {bs.get('last_order_time') or '-'}\n"
        f"Días normales: {days_txt}\n"
        f"No abrir hoy: {'Sí' if bs.get('today_closed') else 'No'}\n"
        f"Abrir excepcionalmente hoy: {'Sí' if bs.get('today_open_force') else 'No'}\n"
        f"Override apertura hoy: {'Sí' if bs.get('has_open_override') else 'No'}\n"
        f"Override cierre hoy: {'Sí' if bs.get('has_close_override') else 'No'}\n"
        f"Override última hora hoy: {'Sí' if bs.get('has_last_order_override') else 'No'}\n"
    )

    public_message = str(bs.get("public_message") or "").strip()
    if public_message:
        msg += f"\nMensaje público actual:\n{public_message}\n"

    msg += "\nElige una opción:"
    return msg


def _send_admin_hours_menu(bot_token: str, chat_id: int, tenant_id: str, orders_sh, tenant_tz: str) -> bool:
    bs = _get_business_status_safe(orders_sh=orders_sh, tenant_tz=tenant_tz)
    return telegram_send_text(
        bot_token,
        chat_id,
        _admin_hours_status_text(bs),
        reply_markup=_admin_hours_menu_kb(tenant_id),
    )


def _admin_days_state(sess: Dict[str, Any], bs: Dict[str, Any]) -> Set[str]:
    tmp = sess.setdefault("tmp", {})
    raw = tmp.get("admin_days_selected")
    if isinstance(raw, list):
        return set(raw)
    return set(bs.get("weekly_open_days") or [])


def _admin_days_kb(tenant_id: str, selected_days: Set[str]) -> Dict[str, Any]:
    row1 = []
    row2 = []
    for code in DAY_ORDER[:4]:
        prefix = "✅" if code in selected_days else "⬜"
        row1.append((f"{prefix} {DAY_LABELS[code]}", f"admhrs|{tenant_id}|dayt|{code}"))
    for code in DAY_ORDER[4:]:
        prefix = "✅" if code in selected_days else "⬜"
        row2.append((f"{prefix} {DAY_LABELS[code]}", f"admhrs|{tenant_id}|dayt|{code}"))

    return kb([
        row1,
        row2,
        [("💾 Guardar días", f"admhrs|{tenant_id}|dayssave")],
        [("⬅️ Volver", f"admhrs|{tenant_id}|menu")],
    ])


def _send_admin_days_menu(bot_token: str, chat_id: int, tenant_id: str, sess: Dict[str, Any], bs: Dict[str, Any]) -> bool:
    selected_days = _admin_days_state(sess, bs)
    sess.setdefault("tmp", {})["admin_days_selected"] = list(selected_days)
    selected_txt = ", ".join([DAY_LABELS[d] for d in DAY_ORDER if d in selected_days]) or "Ninguno"

    msg = (
        "📅 DÍAS NORMALES DE APERTURA\n\n"
        f"Seleccionados: {selected_txt}\n\n"
        "Toca los días para marcar o desmarcar.\n"
        "Luego presiona “Guardar días”."
    )
    return telegram_send_text(
        bot_token,
        chat_id,
        msg,
        reply_markup=_admin_days_kb(tenant_id, selected_days),
    )


def _time_grid_kb(prefix: str, tenant_id: str, back_action: str) -> Dict[str, Any]:
    rows: List[List[Tuple[str, str]]] = []
    current: List[Tuple[str, str]] = []
    for hhmm in TIME_SLOTS:
        current.append((hhmm, f"admhrs|{tenant_id}|{prefix}|{_hhmm_to_compact(hhmm)}"))
        if len(current) == 4:
            rows.append(current)
            current = []
    if current:
        rows.append(current)
    rows.append([("⬅️ Volver", f"admhrs|{tenant_id}|{back_action}")])
    return kb(rows)


def _send_admin_norm_open_menu(bot_token: str, chat_id: int, tenant_id: str) -> bool:
    msg = (
        "🕒 HORARIO NORMAL\n\n"
        "Paso 1 de 3:\n"
        "Elige la hora normal de apertura."
    )
    return telegram_send_text(
        bot_token,
        chat_id,
        msg,
        reply_markup=_time_grid_kb(prefix="normopen", tenant_id=tenant_id, back_action="menu"),
    )


def _send_admin_norm_close_menu(bot_token: str, chat_id: int, tenant_id: str, open_time: str) -> bool:
    msg = (
        "🕒 HORARIO NORMAL\n\n"
        f"Apertura elegida: {open_time}\n\n"
        "Paso 2 de 3:\n"
        "Elige la hora normal de cierre."
    )
    return telegram_send_text(
        bot_token,
        chat_id,
        msg,
        reply_markup=_time_grid_kb(prefix="normclose", tenant_id=tenant_id, back_action="norm"),
    )


def _send_admin_norm_last_menu(bot_token: str, chat_id: int, tenant_id: str, open_time: str, close_time: str) -> bool:
    msg = (
        "🕒 HORARIO NORMAL\n\n"
        f"Apertura: {open_time}\n"
        f"Cierre: {close_time}\n\n"
        "Paso 3 de 3:\n"
        "Elige la última hora normal de pedido."
    )
    return telegram_send_text(
        bot_token,
        chat_id,
        msg,
        reply_markup=_time_grid_kb(prefix="normlast", tenant_id=tenant_id, back_action="norm"),
    )


def _send_admin_early_close_menu(bot_token: str, chat_id: int, tenant_id: str) -> bool:
    msg = (
        "🌙 CERRAR MÁS TEMPRANO HOY\n\n"
        "Paso 1 de 2:\n"
        "Elige la nueva hora de cierre de hoy."
    )
    return telegram_send_text(
        bot_token,
        chat_id,
        msg,
        reply_markup=_time_grid_kb(prefix="earlyclose", tenant_id=tenant_id, back_action="menu"),
    )


def _send_admin_early_last_menu(bot_token: str, chat_id: int, tenant_id: str, close_time: str) -> bool:
    msg = (
        "🌙 CERRAR MÁS TEMPRANO HOY\n\n"
        f"Cierre de hoy elegido: {close_time}\n\n"
        "Paso 2 de 2:\n"
        "Elige la nueva última hora de pedido de hoy."
    )
    return telegram_send_text(
        bot_token,
        chat_id,
        msg,
        reply_markup=_time_grid_kb(prefix="earlylast", tenant_id=tenant_id, back_action="early"),
    )


def _send_admin_late_open_menu(bot_token: str, chat_id: int, tenant_id: str) -> bool:
    msg = (
        "🌅 ABRIR MÁS TARDE HOY\n\n"
        "Elige la nueva hora de apertura de hoy."
    )
    return telegram_send_text(
        bot_token,
        chat_id,
        msg,
        reply_markup=_time_grid_kb(prefix="lateopen", tenant_id=tenant_id, back_action="menu"),
    )


# -------------------------
# Forward proof to admin
# -------------------------

def _forward_proof_to_admin(
    tenant: Dict[str, Any],
    tenant_id: str,
    proof_file_id: str,
    proof_type: str,
    proof_caption: str,
) -> bool:
    client_token = get_client_bot_token(tenant)
    admin_token = get_admin_bot_token(tenant)
    admin_chat_id = get_admin_chat_id(tenant)

    if not client_token or not admin_token or not admin_chat_id:
        log_event(
            "forward_proof_missing_config",
            tenant_id=tenant_id,
            has_client=bool(client_token),
            has_admin=bool(admin_token),
            has_admin_chat=bool(admin_chat_id),
        )
        return False

    try:
        file_path = _telegram_get_file_path(client_token, proof_file_id)
        file_bytes = _telegram_download_file_bytes(client_token, file_path)
        filename = file_path.split("/")[-1] if file_path else "proof"
        caption = proof_caption or ("Comprobante (foto)" if proof_type == "photo" else "Comprobante (archivo)")

        if proof_type == "photo":
            return _telegram_send_file_bytes_admin(
                admin_token=admin_token,
                method="sendPhoto",
                chat_id=admin_chat_id,
                file_field="photo",
                filename=filename or "proof.jpg",
                content_type="image/jpeg",
                file_bytes=file_bytes,
                caption=caption,
            )

        return _telegram_send_file_bytes_admin(
            admin_token=admin_token,
            method="sendDocument",
            chat_id=admin_chat_id,
            file_field="document",
            filename=filename or "proof.pdf",
            content_type="application/octet-stream",
            file_bytes=file_bytes,
            caption=caption,
        )

    except Exception as e:
        log_event("forward_proof_failed", tenant_id=tenant_id, error=str(e))
        return False


# -------------------------
# Admin notify
# -------------------------

def notify_admin_payment_reported(
    tenant: Dict[str, Any],
    tenant_id: str,
    orders_sh,
    order_id: str,
    is_reminder: bool = False,
) -> bool:
    admin_token = get_admin_bot_token(tenant)
    admin_chat_id = get_admin_chat_id(tenant)

    if not admin_token or not admin_chat_id:
        log_event("admin_notify_failed", tenant_id=tenant_id, reason="missing_admin_token_or_chat")
        return False

    order = get_order_by_id(orders_sh, order_id)
    if not order:
        telegram_send_text(admin_token, admin_chat_id, f"⚠️ Pedido {order_id} no encontrado en Sheets.")
        return False

    items_snapshot = parse_items_field(order.get("items_snapshot"))
    if items_snapshot:
        lines_txt, snapshot_total, total_qty = fmt_snapshot_lines(items_snapshot)
        total = snapshot_total
    else:
        try:
            menu_idx = load_menu_index(orders_sh)
        except Exception as e:
            log_event("admin_menu_load_error", tenant_id=tenant_id, error=str(e))
            menu_idx = {}
        cart = parse_items_field(order.get("items"))
        lines_txt, _, total_qty = fmt_cart_lines(cart, menu_idx)
        try:
            total = float(order.get("total_amount") or 0)
        except Exception:
            total = 0.0

    proof_file_id = (order.get("payment_proof_file_id") or "").strip()
    proof_type = (order.get("payment_proof_type") or "").strip()
    proof_caption = (order.get("payment_proof_caption") or "").strip()

    confirm_btn = kb([[("✅ Confirmar pago", f"paid|{tenant_id}|{order_id}")]])

    title = "🔔 RECORDATORIO — PAGO REPORTADO" if is_reminder else "💳 PAGO REPORTADO"
    txt = (
        f"{title}\n\n"
        f"Tenant: {tenant_id}\n"
        f"ID: {order_id}\n"
        f"Cliente: {order.get('customer_name','')}\n"
        f"Contacto(chat_id): {order.get('customer_contact','')}\n"
        f"Hora recogida: {order.get('requested_time','pendiente')}\n"
        f"Cantidad total: {total_qty}\n"
        f"Total: {total:.2f} BOB\n\n"
        f"Detalle:\n{lines_txt}\n\n"
        "Presiona ✅ Confirmar pago cuando verifiques."
    )

    ok_txt = telegram_send_text(admin_token, admin_chat_id, txt, reply_markup=confirm_btn)

    ok_proof = False
    if proof_file_id and proof_type:
        ok_proof = _forward_proof_to_admin(tenant, tenant_id, proof_file_id, proof_type, proof_caption)
    else:
        log_event("admin_missing_proof", tenant_id=tenant_id, order_id=order_id)

    log_event(
        "admin_notify_result",
        tenant_id=tenant_id,
        order_id=order_id,
        ok_txt=bool(ok_txt),
        ok_proof=bool(ok_proof),
        is_reminder=bool(is_reminder),
    )
    return bool(ok_txt)


# -------------------------
# Client keyboards
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


# -------------------------
# Helpers seguridad/parsing
# -------------------------

def _safe_int(v: Any) -> Optional[int]:
    try:
        return int(v)
    except Exception:
        return None


def _assert_admin_authorized(tenant: Dict[str, Any], chat_id: int, tenant_id: str) -> None:
    admin_chat_id = get_admin_chat_id(tenant)
    if admin_chat_id is None:
        log_event("admin_chat_id_missing_security_warning", tenant_id=tenant_id, chat_id=chat_id)
        return
    if chat_id != admin_chat_id:
        log_event("admin_paid_unauthorized", tenant_id=tenant_id, chat_id=chat_id, expected_admin_chat_id=admin_chat_id)
        raise HTTPException(status_code=403, detail="Not authorized")


def _contact_link_for_admin(tenant: Dict[str, Any]) -> Optional[str]:
    u = get_admin_username(tenant)
    if u:
        return f"https://t.me/{urllib.parse.quote(u)}"
    admin_chat_id = get_admin_chat_id(tenant)
    if admin_chat_id:
        return f"tg://user?id={admin_chat_id}"
    return None


# -------------------------
# Webhook endpoint
# -------------------------

@router.post("/telegram/webhook/{tenant_id}/{secret}")
async def telegram_webhook(tenant_id: str, secret: str, update: Dict[str, Any]):
    tenant_id = (tenant_id or "").strip()
    if not tenant_id:
        return {"ok": True}

    gc = get_gspread_client()
    tenant = get_tenant_or_404(tenant_id, gc=gc)

    mode, bot_token = resolve_bot_by_secret(tenant, secret)
    if not bot_token:
        return {"ok": True}

    orders_sheet_id = (tenant.get("orders_sheet_id") or "").strip()
    if not orders_sheet_id:
        raise HTTPException(status_code=500, detail=f"orders_sheet_id missing for tenant: {tenant_id}")

    orders_sh = open_spreadsheet_by_key(gc, orders_sheet_id)
    tenant_tz = (tenant.get("timezone") or "America/La_Paz").strip()

    # =========================================================
    # 1) CALLBACK QUERY
    # =========================================================
    cb = update.get("callback_query")
    if cb:
        data = (cb.get("data") or "").strip()
        cb_id = cb.get("id")

        msg_obj = cb.get("message") or {}
        chat_obj = msg_obj.get("chat") or {}
        chat_id = _safe_int(chat_obj.get("id"))
        if chat_id is None:
            log_event("callback_missing_chat_id", tenant_id=tenant_id, data=data)
            return {"ok": True}

        if cb_id:
            telegram_answer_callback(bot_token, cb_id, "OK")

        # -------------------------
        # ADMIN callbacks
        # -------------------------
        if mode == "admin" and data.startswith("paid|"):
            parts = data.split("|")
            if len(parts) != 3:
                return {"ok": True}

            cb_tenant_id = parts[1].strip()
            order_id = parts[2].strip()

            if cb_tenant_id != tenant_id:
                raise HTTPException(status_code=400, detail="Tenant mismatch in callback")

            _assert_admin_authorized(tenant, chat_id, tenant_id)

            res = update_order_status(orders_sh, order_id, "PAID")
            if not res.get("found"):
                telegram_send_text(bot_token, chat_id, f"⚠️ Pedido {order_id} no encontrado en Sheets.", reply_markup=admin_fixed_kb())
                return {"ok": True}

            telegram_send_text(bot_token, chat_id, f"✅ Pedido {order_id} marcado como PAID", reply_markup=admin_fixed_kb())

            order = get_order_by_id(orders_sh, order_id)
            if order:
                client_token = get_client_bot_token(tenant)
                client_chat = (order.get("customer_contact") or "").strip()
                if client_token and client_chat:
                    try:
                        telegram_send_text(client_token, int(client_chat), f"✅ Pago validado. Tu pedido {order_id} fue confirmado. ¡Gracias!")
                    except Exception as e:
                        log_event("notify_client_paid_failed", tenant_id=tenant_id, order_id=order_id, error=str(e))

            return {"ok": True}

        if mode == "admin" and data.startswith("admin_stats_period|"):
            parts = data.split("|")
            if len(parts) != 3:
                return {"ok": True}
            _, cb_tenant_id, period_key = parts
            cb_tenant_id = cb_tenant_id.strip()
            period_key = period_key.strip()

            if cb_tenant_id != tenant_id:
                raise HTTPException(status_code=400, detail="Tenant mismatch in stats callback")

            _assert_admin_authorized(tenant, chat_id, tenant_id)

            period = resolve_period(tenant_tz, period_key)
            txt = build_stats_report_text(orders_sh, tenant_id=tenant_id, tenant_tz=tenant_tz, period=period)

            telegram_send_text(bot_token, chat_id, txt, reply_markup=admin_fixed_kb())
            return {"ok": True}

        if mode == "admin" and data.startswith("admhrs|"):
            _assert_admin_authorized(tenant, chat_id, tenant_id)

            parts = data.split("|")
            if len(parts) < 3:
                return {"ok": True}

            cb_tenant_id = parts[1].strip()
            if cb_tenant_id != tenant_id:
                raise HTTPException(status_code=400, detail="Tenant mismatch in admin hours callback")

            action = parts[2].strip()
            sess = get_sess(tenant_id, chat_id)
            tmp = sess.setdefault("tmp", {})
            updated_by = f"admin_bot:{chat_id}"

            if action == "menu":
                tmp.pop("admin_days_selected", None)
                tmp.pop("admin_norm_open", None)
                tmp.pop("admin_norm_close", None)
                tmp.pop("admin_early_close", None)
                return {"ok": _send_admin_hours_menu(bot_token, chat_id, tenant_id, orders_sh, tenant_tz)}

            if action == "habitual":
                tmp.pop("admin_days_selected", None)
                tmp.pop("admin_norm_open", None)
                tmp.pop("admin_norm_close", None)
                tmp.pop("admin_early_close", None)
                _admin_restore_habitual(orders_sh=orders_sh, updated_by=updated_by)
                telegram_send_text(bot_token, chat_id, "✅ Se restauró la configuración habitual de hoy.")
                return {"ok": _send_admin_hours_menu(bot_token, chat_id, tenant_id, orders_sh, tenant_tz)}

            if action == "days":
                bs = _get_business_status_safe(orders_sh=orders_sh, tenant_tz=tenant_tz)
                tmp["admin_days_selected"] = list(bs.get("weekly_open_days") or [])
                return {"ok": _send_admin_days_menu(bot_token, chat_id, tenant_id, sess, bs)}

            if action == "dayt" and len(parts) == 4:
                code = parts[3].strip()
                if code not in DAY_ORDER:
                    return {"ok": True}
                current = set(tmp.get("admin_days_selected") or [])
                if code in current:
                    current.remove(code)
                else:
                    current.add(code)
                tmp["admin_days_selected"] = list(current)
                bs = _get_business_status_safe(orders_sh=orders_sh, tenant_tz=tenant_tz)
                return {"ok": _send_admin_days_menu(bot_token, chat_id, tenant_id, sess, bs)}

            if action == "dayssave":
                selected = [d for d in DAY_ORDER if d in set(tmp.get("admin_days_selected") or [])]
                _admin_set_weekly_open_days(
                    orders_sh=orders_sh,
                    days=selected,
                    updated_by=updated_by,
                )
                tmp.pop("admin_days_selected", None)

                bs_after = _get_business_status_safe(orders_sh=orders_sh, tenant_tz=tenant_tz)
                today_code = str(bs_after.get("today_weekday_code") or "").strip()
                today_in = today_code in set(bs_after.get("weekly_open_days") or [])
                force_open = bool(bs_after.get("today_open_force"))
                today_closed = bool(bs_after.get("today_closed"))

                msg = "✅ Días normales actualizados."
                if today_code and (not today_in) and (not force_open) and (not today_closed):
                    msg += f"\n⚠️ Ojo: hoy ({today_code}) quedó fuera de los días normales."

                telegram_send_text(bot_token, chat_id, msg)
                return {"ok": _send_admin_hours_menu(bot_token, chat_id, tenant_id, orders_sh, tenant_tz)}

            if action == "norm":
                tmp.pop("admin_norm_open", None)
                tmp.pop("admin_norm_close", None)
                return {"ok": _send_admin_norm_open_menu(bot_token, chat_id, tenant_id)}

            if action == "normopen" and len(parts) == 4:
                open_time = _compact_to_hhmm(parts[3].strip())
                tmp["admin_norm_open"] = open_time
                return {"ok": _send_admin_norm_close_menu(bot_token, chat_id, tenant_id, open_time)}

            if action == "normclose" and len(parts) == 4:
                close_time = _compact_to_hhmm(parts[3].strip())
                open_time = str(tmp.get("admin_norm_open") or "").strip()
                if not open_time:
                    return {"ok": _send_admin_norm_open_menu(bot_token, chat_id, tenant_id)}
                tmp["admin_norm_close"] = close_time
                return {"ok": _send_admin_norm_last_menu(bot_token, chat_id, tenant_id, open_time, close_time)}

            if action == "normlast" and len(parts) == 4:
                last_time = _compact_to_hhmm(parts[3].strip())
                open_time = str(tmp.get("admin_norm_open") or "").strip()
                close_time = str(tmp.get("admin_norm_close") or "").strip()
                if not open_time or not close_time:
                    return {"ok": _send_admin_norm_open_menu(bot_token, chat_id, tenant_id)}

                _admin_set_weekly_normal_hours(
                    orders_sh=orders_sh,
                    open_time=open_time,
                    close_time=close_time,
                    last_order_time=last_time,
                    updated_by=updated_by,
                )

                tmp.pop("admin_norm_open", None)
                tmp.pop("admin_norm_close", None)

                telegram_send_text(
                    bot_token,
                    chat_id,
                    f"✅ Horario normal actualizado.\nApertura: {open_time}\nCierre: {close_time}\nÚltima hora de pedido: {last_time}",
                )
                return {"ok": _send_admin_hours_menu(bot_token, chat_id, tenant_id, orders_sh, tenant_tz)}

            if action == "early":
                tmp.pop("admin_early_close", None)
                return {"ok": _send_admin_early_close_menu(bot_token, chat_id, tenant_id)}

            if action == "earlyclose" and len(parts) == 4:
                close_time = _compact_to_hhmm(parts[3].strip())
                tmp["admin_early_close"] = close_time
                return {"ok": _send_admin_early_last_menu(bot_token, chat_id, tenant_id, close_time)}

            if action == "earlylast" and len(parts) == 4:
                last_time = _compact_to_hhmm(parts[3].strip())
                close_time = str(tmp.get("admin_early_close") or "").strip()
                if not close_time:
                    return {"ok": _send_admin_early_close_menu(bot_token, chat_id, tenant_id)}

                _admin_set_today_closed(orders_sh=orders_sh, enabled=False, updated_by=updated_by)
                _admin_set_today_open_force(orders_sh=orders_sh, enabled=False, updated_by=updated_by)
                _admin_set_today_close_override(orders_sh=orders_sh, close_time=close_time, updated_by=updated_by)
                _admin_set_today_last_order_override(orders_sh=orders_sh, last_order_time=last_time, updated_by=updated_by)

                tmp.pop("admin_early_close", None)
                telegram_send_text(
                    bot_token,
                    chat_id,
                    f"✅ Cierre temprano configurado para hoy.\nCierre: {close_time}\nÚltima hora de pedido: {last_time}",
                )
                return {"ok": _send_admin_hours_menu(bot_token, chat_id, tenant_id, orders_sh, tenant_tz)}

            if action == "late":
                return {"ok": _send_admin_late_open_menu(bot_token, chat_id, tenant_id)}

            if action == "lateopen" and len(parts) == 4:
                open_time = _compact_to_hhmm(parts[3].strip())
                _admin_set_today_closed(orders_sh=orders_sh, enabled=False, updated_by=updated_by)
                _admin_set_today_open_force(orders_sh=orders_sh, enabled=False, updated_by=updated_by)
                _admin_set_today_open_override(orders_sh=orders_sh, open_time=open_time, updated_by=updated_by)
                telegram_send_text(
                    bot_token,
                    chat_id,
                    f"✅ Apertura tardía configurada para hoy.\nNueva apertura: {open_time}",
                )
                return {"ok": _send_admin_hours_menu(bot_token, chat_id, tenant_id, orders_sh, tenant_tz)}

            if action == "closed":
                _admin_set_today_closed(
                    orders_sh=orders_sh,
                    enabled=True,
                    updated_by=updated_by,
                )
                _admin_set_today_open_FORCE = _admin_set_today_open_force
                _admin_set_today_open_FORCE(
                    orders_sh=orders_sh,
                    enabled=False,
                    updated_by=updated_by,
                )
                _admin_set_today_open_override(orders_sh=orders_sh, open_time="", updated_by=updated_by)
                _admin_set_today_close_override(orders_sh=orders_sh, close_time="", updated_by=updated_by)
                _admin_set_today_last_order_override(orders_sh=orders_sh, last_order_time="", updated_by=updated_by)
                telegram_send_text(bot_token, chat_id, "✅ Hoy quedó marcado como NO abrir.")
                return {"ok": _send_admin_hours_menu(bot_token, chat_id, tenant_id, orders_sh, tenant_tz)}

            if action == "openforce":
                _admin_set_today_open_force(
                    orders_sh=orders_sh,
                    enabled=True,
                    updated_by=updated_by,
                )
                _admin_set_today_closed(
                    orders_sh=orders_sh,
                    enabled=False,
                    updated_by=updated_by,
                )
                _admin_set_today_open_override(orders_sh=orders_sh, open_time="", updated_by=updated_by)
                _admin_set_today_close_override(orders_sh=orders_sh, close_time="", updated_by=updated_by)
                _admin_set_today_last_order_override(orders_sh=orders_sh, last_order_time="", updated_by=updated_by)
                telegram_send_text(bot_token, chat_id, "✅ Hoy quedó marcado como abrir excepcionalmente.")
                return {"ok": _send_admin_hours_menu(bot_token, chat_id, tenant_id, orders_sh, tenant_tz)}

            return {"ok": True}

        if mode == "admin" and data.startswith("admmenu|"):
            _assert_admin_authorized(tenant, chat_id, tenant_id)

            parts = data.split("|")
            if len(parts) < 3:
                return {"ok": True}

            cb_tenant_id = parts[1].strip()
            if cb_tenant_id != tenant_id:
                raise HTTPException(status_code=400, detail="Tenant mismatch in admin menu callback")

            action = parts[2].strip()
            sess = get_sess(tenant_id, chat_id)
            tmp = sess.setdefault("tmp", {})

            if action == "panel":
                tmp.pop("admin_menu_categories", None)
                tmp.pop("admin_menu_current_category", None)
                tmp.pop("admin_menu_last_sku", None)
                tmp.pop("admin_menu_price_sku", None)
                tmp.pop("admin_menu_price_work", None)
                tmp.pop("admin_menu_input_mode", None)
                telegram_send_text(bot_token, chat_id, "Panel admin:", reply_markup=admin_fixed_kb())
                return {"ok": True}

            if action == "home":
                return {"ok": _send_admin_menu_home(bot_token, chat_id, tenant_id, orders_sh, sess)}

            if action == "refresh":
                invalidate_menu_cache(orders_sh)
                telegram_send_text(bot_token, chat_id, "✅ Menú refrescado.")
                return {"ok": _send_admin_menu_home(bot_token, chat_id, tenant_id, orders_sh, sess)}

            if action == "catrefresh":
                invalidate_menu_cache(orders_sh)
                current_category = str(tmp.get("admin_menu_current_category") or "").strip()
                if not current_category:
                    return {"ok": _send_admin_menu_home(bot_token, chat_id, tenant_id, orders_sh, sess)}
                telegram_send_text(bot_token, chat_id, "✅ Categoría refrescada.")
                return {"ok": _send_admin_menu_category(bot_token, chat_id, tenant_id, orders_sh, sess, current_category)}

            if action == "cat" and len(parts) == 4:
                try:
                    idx = int(parts[3].strip())
                except Exception:
                    idx = -1

                menu_idx = load_menu_admin_index(orders_sh, force=False)
                cats = group_menu_admin_by_category(menu_idx)
                cat_names = sorted(cats.keys(), key=lambda x: normalize(x))

                if idx < 0 or idx >= len(cat_names):
                    return {"ok": _send_admin_menu_home(bot_token, chat_id, tenant_id, orders_sh, sess)}

                category = cat_names[idx]
                return {"ok": _send_admin_menu_category(bot_token, chat_id, tenant_id, orders_sh, sess, category)}

            if action == "catback":
                current_category = str(tmp.get("admin_menu_current_category") or "").strip()
                if not current_category:
                    return {"ok": _send_admin_menu_home(bot_token, chat_id, tenant_id, orders_sh, sess)}
                return {"ok": _send_admin_menu_category(bot_token, chat_id, tenant_id, orders_sh, sess, current_category)}

            if action == "prd" and len(parts) == 4:
                sku = parts[3].strip()
                return {"ok": _send_admin_menu_product_detail(bot_token, chat_id, tenant_id, orders_sh, sess, sku)}

            if action == "toggle" and len(parts) == 4:
                sku = parts[3].strip()
                item_before = get_menu_product_or_404(orders_sh, sku)
                new_active = not bool(item_before.get("active", False))
                set_menu_product_active(orders_sh, sku, new_active)
                item_after = get_menu_product_or_404(orders_sh, sku)

                telegram_send_text(
                    bot_token,
                    chat_id,
                    f"✅ Estado actualizado.\nProducto: {item_after.get('name','')}\nActivo: {'Sí' if item_after.get('active') else 'No'}",
                )
                return {"ok": _send_admin_menu_product_detail(bot_token, chat_id, tenant_id, orders_sh, sess, sku)}

            if action == "price" and len(parts) == 4:
                sku = parts[3].strip()
                return {"ok": _send_admin_menu_price_editor(bot_token, chat_id, tenant_id, orders_sh, sess, sku)}

            if action == "padj" and len(parts) == 5:
                sku = parts[3].strip()
                token = parts[4].strip().lower()

                item = get_menu_product_or_404(orders_sh, sku)
                current_sku = str(tmp.get("admin_menu_price_sku") or "").strip()
                if current_sku != sku:
                    tmp["admin_menu_price_sku"] = sku
                    tmp["admin_menu_price_work"] = float(item.get("price", 0.0))

                work_price = float(tmp.get("admin_menu_price_work") or 0.0)
                work_price = _apply_price_delta(work_price, token)
                tmp["admin_menu_price_work"] = work_price

                return {"ok": _send_admin_menu_price_editor(bot_token, chat_id, tenant_id, orders_sh, sess, sku)}

            if action == "psave" and len(parts) == 4:
                sku = parts[3].strip()
                current_sku = str(tmp.get("admin_menu_price_sku") or "").strip()

                if current_sku != sku:
                    item = get_menu_product_or_404(orders_sh, sku)
                    tmp["admin_menu_price_sku"] = sku
                    tmp["admin_menu_price_work"] = float(item.get("price", 0.0))

                new_price = float(tmp.get("admin_menu_price_work") or 0.0)
                result = set_menu_product_price(orders_sh, sku, new_price)

                tmp.pop("admin_menu_price_sku", None)
                tmp.pop("admin_menu_price_work", None)
                tmp.pop("admin_menu_input_mode", None)

                telegram_send_text(
                    bot_token,
                    chat_id,
                    f"✅ Precio actualizado.\nSKU: {sku}\nNuevo precio: Bs {_fmt_price_short(result.get('price', 0))}",
                )
                return {"ok": _send_admin_menu_product_detail(bot_token, chat_id, tenant_id, orders_sh, sess, sku)}

            if action == "pback" and len(parts) == 4:
                sku = parts[3].strip()
                tmp.pop("admin_menu_price_sku", None)
                tmp.pop("admin_menu_price_work", None)
                tmp.pop("admin_menu_input_mode", None)
                return {"ok": _send_admin_menu_product_detail(bot_token, chat_id, tenant_id, orders_sh, sess, sku)}

            if action == "pricewrite" and len(parts) == 4:
                sku = parts[3].strip()
                item = get_menu_product_or_404(orders_sh, sku)
                tmp["admin_menu_input_mode"] = "price_final"
                tmp["admin_menu_price_sku"] = sku
                tmp["admin_menu_price_work"] = float(item.get("price", 0.0))

                telegram_send_text(
                    bot_token,
                    chat_id,
                    (
                        "✍️ ESCRIBIR PRECIO FINAL\n\n"
                        f"Producto: {item.get('name','')}\n"
                        f"Precio actual: Bs {_fmt_price_short(item.get('price', 0))}\n\n"
                        "Escribe el nuevo precio final.\n"
                        "Ejemplos válidos:\n"
                        "- 25\n"
                        "- 25 bs\n"
                        "- 25 bolivianos"
                    ),
                )
                return {"ok": True}

            if action == "discount" and len(parts) == 4:
                sku = parts[3].strip()
                item = get_menu_product_or_404(orders_sh, sku)
                tmp["admin_menu_input_mode"] = "discount_pct"
                tmp["admin_menu_price_sku"] = sku
                tmp["admin_menu_price_work"] = float(item.get("price", 0.0))

                telegram_send_text(
                    bot_token,
                    chat_id,
                    (
                        "🏷️ APLICAR DESCUENTO %\n\n"
                        f"Producto: {item.get('name','')}\n"
                        f"Precio actual: Bs {_fmt_price_short(item.get('price', 0))}\n\n"
                        "Escribe el porcentaje de descuento.\n"
                        "Ejemplos válidos:\n"
                        "- 10\n"
                        "- 15%\n"
                        "- 20 por ciento"
                    ),
                )
                return {"ok": True}

            if action == "photo" and len(parts) == 4:
                sku = parts[3].strip()
                tmp["admin_menu_input_mode"] = "awaiting_photo"
                tmp["admin_menu_price_sku"] = sku

                item = get_menu_product_or_404(orders_sh, sku)

                telegram_send_text(
                    bot_token,
                    chat_id,
                    f"📷 Envía ahora la foto para:\n{item.get('name','')}"
                )
                return {"ok": True}

            return {"ok": True}

        # -------------------------
        # CLIENT callbacks
        # -------------------------
        if mode == "client":
            sess = get_sess(tenant_id, chat_id)
            tmp = sess.get("tmp") or {}
            sess["tmp"] = tmp

            if data == "home":
                telegram_send_text(bot_token, chat_id, "Elige una opción:", client_home_kb())
                return {"ok": True}

            if data == "menu":
                if not _client_orders_allowed_or_notify(bot_token, chat_id, orders_sh, tenant_tz):
                    return {"ok": True}

                menu_idx = load_menu_index(orders_sh)
                cats = group_menu_by_category(menu_idx)

                if not cats:
                    telegram_send_text(bot_token, chat_id, "No hay menú activo.", client_home_kb())
                    return {"ok": True}

                rows = []
                for c in sorted(cats.keys(), key=lambda x: normalize(x)):
                    rows.append([(c, f"cat|{normalize(c)}")])
                rows.append([("🛒 Carrito", "cart")])
                rows.append([("🏠 Inicio", "home")])

                telegram_send_text(bot_token, chat_id, "📋 Elige una categoría:", kb(rows))
                return {"ok": True}

            if data.startswith("cat|"):
                if not _client_orders_allowed_or_notify(bot_token, chat_id, orders_sh, tenant_tz):
                    return {"ok": True}

                cat_norm = data.split("|", 1)[1].strip()

                menu_idx = load_menu_index(orders_sh)
                cats = group_menu_by_category(menu_idx)

                real_cat = None
                for c in cats.keys():
                    if normalize(c) == cat_norm:
                        real_cat = c
                        break

                if not real_cat:
                    telegram_send_text(bot_token, chat_id, "Categoría no encontrada.", reply_markup=client_home_kb())
                    return {"ok": True}

                items = cats.get(real_cat, [])
                if not items:
                    telegram_send_text(bot_token, chat_id, "No hay productos activos.", reply_markup=client_home_kb())
                    return {"ok": True}

                rows = []
                for it in items[:25]:
                    rows.append([(f"{it['name']} ({it['price']:.0f})", f"prd|{it['sku']}")])
                rows.append([("🛒 Carrito", "cart")])
                rows.append([("⬅️ Categorías", "menu")])
                rows.append([("🏠 Inicio", "home")])

                telegram_send_text(bot_token, chat_id, f"🍽 {real_cat} — elige un producto:", kb(rows))

                for it in items:
                    photo_file_id = str(it.get("photo_file_id") or "").strip()
                    if photo_file_id:
                        telegram_send_photo(
                            bot_token,
                            chat_id,
                            photo_file_id,
                            caption=f"{it['name']}\nBs {it['price']}",
                        )

                return {"ok": True}

            if data.startswith("prd|"):
                if not _client_orders_allowed_or_notify(bot_token, chat_id, orders_sh, tenant_tz):
                    return {"ok": True}

                sku = data.split("|", 1)[1].strip()
                rows = [
                    [("1", f"qty|{sku}|1"), ("2", f"qty|{sku}|2"), ("3", f"qty|{sku}|3"), ("4", f"qty|{sku}|4")],
                    [("🛒 Carrito", "cart")],
                    [("⬅️ Volver", "menu")],
                    [("🏠 Inicio", "home")],
                ]
                telegram_send_text(bot_token, chat_id, "Selecciona cantidad:", kb(rows))
                return {"ok": True}

            if data.startswith("qty|"):
                if not _client_orders_allowed_or_notify(bot_token, chat_id, orders_sh, tenant_tz):
                    return {"ok": True}

                parts = data.split("|")
                if len(parts) != 3:
                    return {"ok": True}

                _, sku, qty_s = parts
                try:
                    qty = int(qty_s)
                except Exception:
                    qty = 1
                qty = max(1, qty)

                menu_idx = load_menu_index(orders_sh)
                if sku not in menu_idx:
                    telegram_send_text(bot_token, chat_id, "Producto no disponible.", reply_markup=client_home_kb())
                    return {"ok": True}

                cart = sess.get("cart") or []
                found = False
                for it in cart:
                    if it.get("sku") == sku:
                        it["qty"] = int(it.get("qty") or 0) + qty
                        found = True
                        break
                if not found:
                    cart.append({"sku": sku, "qty": qty})
                sess["cart"] = cart

                lines_txt, total, total_qty = fmt_cart_lines(cart, menu_idx)
                name = menu_idx[sku]["name"]

                telegram_send_text(
                    bot_token,
                    chat_id,
                    f"✅ Agregado al carrito: {qty} x {name}\n\nCantidad: {total_qty}\nTotal: {total:.2f} BOB",
                    reply_markup=kb([
                        [("🛒 Ver carrito", "cart")],
                        [("⬅️ Seguir comprando", "menu")],
                        [("🏠 Inicio", "home")],
                    ]),
                )
                return {"ok": True}

            if data == "cart":
                menu_idx = load_menu_index(orders_sh)
                cart = sess.get("cart") or []
                lines_txt, total, total_qty = fmt_cart_lines(cart, menu_idx)

                has_items = total_qty > 0
                msg = (
                    f"🛒 *Tu carrito*\n"
                    f"Cantidad: *{total_qty}*\n"
                    f"Total: *{total:.2f}* BOB\n\n"
                    f"{lines_txt}"
                )
                telegram_send_text(bot_token, chat_id, msg, reply_markup=cart_kb(has_items), parse_mode="Markdown")
                return {"ok": True}

            if data == "cart_clear":
                sess["cart"] = []
                sess["stage"] = "idle"
                sess["tmp"] = {}
                telegram_send_text(bot_token, chat_id, "🧹 Carrito vaciado.", reply_markup=client_home_kb())
                return {"ok": True}

            if data == "cart_confirm":
                if not _client_orders_allowed_or_notify(bot_token, chat_id, orders_sh, tenant_tz):
                    return {"ok": True}

                cart = sess.get("cart") or []
                if not cart:
                    telegram_send_text(bot_token, chat_id, "Tu carrito está vacío.", reply_markup=client_home_kb())
                    return {"ok": True}

                sess["stage"] = "awaiting_name"
                telegram_send_text(bot_token, chat_id, "Perfecto. ¿Cuál es tu *nombre* para el pedido?", parse_mode="Markdown")
                return {"ok": True}

            if data.startswith("i_paid|"):
                parts = data.split("|")
                if len(parts) != 3:
                    return {"ok": True}

                _, cb_tenant_id, order_id = parts
                cb_tenant_id = cb_tenant_id.strip()
                order_id = order_id.strip()

                if cb_tenant_id != tenant_id:
                    raise HTTPException(status_code=400, detail="Tenant mismatch in i_paid callback")

                order = get_order_by_id(orders_sh, order_id)
                if not order:
                    telegram_send_text(bot_token, chat_id, "No encontré tu pedido. Vuelve a /start.", reply_markup=client_home_kb())
                    return {"ok": True}

                proof_file_id = (order.get("payment_proof_file_id") or "").strip()
                if not proof_file_id:
                    telegram_send_text(bot_token, chat_id, "Aún no recibí tu comprobante.\nEnvía una foto o PDF del pago primero.")
                    return {"ok": True}

                ok_sent = notify_admin_payment_reported(tenant, tenant_id, orders_sh, order_id, is_reminder=False)

                tmp["paid_pressed_at_ts"] = int(time.time())
                tmp["last_notified_order_id"] = order_id
                tmp["last_admin_notify_ok"] = bool(ok_sent)

                telegram_send_text(
                    bot_token,
                    chat_id,
                    "✅ Recibido. Espera unos minutos mientras verificamos tu pago.\n"
                    "Si no hay respuesta, podrás enviar un recordatorio.",
                    reply_markup=paid_actions_kb(tenant_id, order_id),
                )
                return {"ok": True}

            if data.startswith("remind|"):
                parts = data.split("|")
                if len(parts) != 3:
                    return {"ok": True}

                _, cb_tenant_id, order_id = parts
                cb_tenant_id = cb_tenant_id.strip()
                order_id = order_id.strip()

                if cb_tenant_id != tenant_id:
                    raise HTTPException(status_code=400, detail="Tenant mismatch in remind callback")

                paid_at = int(tmp.get("paid_pressed_at_ts") or 0)
                now = int(time.time())

                if not paid_at:
                    telegram_send_text(bot_token, chat_id, "Primero presiona “✅ Ya pagué”.", reply_markup=paid_actions_kb(tenant_id, order_id))
                    return {"ok": True}

                if (now - paid_at) < REMINDER_COOLDOWN_SECONDS:
                    left = REMINDER_COOLDOWN_SECONDS - (now - paid_at)
                    mins = max(1, int((left + 59) / 60))
                    telegram_send_text(
                        bot_token,
                        chat_id,
                        f"🙏 Gracias. Por favor espera un momento.\nPodrás enviar un recordatorio en aproximadamente *{mins} minuto(s)*.",
                        reply_markup=paid_actions_kb(tenant_id, order_id),
                        parse_mode="Markdown",
                    )
                    return {"ok": True}

                ok_sent = notify_admin_payment_reported(tenant, tenant_id, orders_sh, order_id, is_reminder=True)
                tmp["reminder_sent_at_ts"] = now
                tmp["last_admin_reminder_ok"] = bool(ok_sent)

                if ok_sent:
                    telegram_send_text(
                        bot_token,
                        chat_id,
                        "🔔 Listo. Enviamos un *recordatorio* al administrador.\n"
                        "Si no responde, en unos minutos podrás contactarlo directamente.",
                        reply_markup=contact_admin_kb(tenant_id, order_id),
                        parse_mode="Markdown",
                    )
                else:
                    telegram_send_text(
                        bot_token,
                        chat_id,
                        "😕 Intenté enviar el recordatorio, pero falló.\nIntenta nuevamente en unos segundos.",
                        reply_markup=paid_actions_kb(tenant_id, order_id),
                    )
                return {"ok": True}

            if data.startswith("contact|"):
                parts = data.split("|")
                if len(parts) != 3:
                    return {"ok": True}

                _, cb_tenant_id, order_id = parts
                cb_tenant_id = cb_tenant_id.strip()
                order_id = order_id.strip()

                if cb_tenant_id != tenant_id:
                    raise HTTPException(status_code=400, detail="Tenant mismatch in contact callback")

                paid_at = int(tmp.get("paid_pressed_at_ts") or 0)
                now = int(time.time())

                if not paid_at:
                    telegram_send_text(bot_token, chat_id, "Primero presiona “✅ Ya pagué”.", reply_markup=paid_actions_kb(tenant_id, order_id))
                    return {"ok": True}

                if (now - paid_at) < CONTACT_AFTER_SECONDS:
                    left = CONTACT_AFTER_SECONDS - (now - paid_at)
                    mins = max(1, int((left + 59) / 60))
                    telegram_send_text(
                        bot_token,
                        chat_id,
                        f"🙏 Aún es pronto.\nPodrás contactar al administrador en aproximadamente *{mins} minuto(s)*.",
                        reply_markup=contact_admin_kb(tenant_id, order_id),
                        parse_mode="Markdown",
                    )
                    return {"ok": True}

                link = _contact_link_for_admin(tenant)
                if not link:
                    telegram_send_text(bot_token, chat_id, "No tengo configurado el contacto directo del administrador.", reply_markup=client_home_kb())
                    return {"ok": True}

                telegram_send_text(bot_token, chat_id, "💬 Contacto directo habilitado.\nToca el enlace para escribirle al administrador:")
                telegram_send_text(bot_token, chat_id, link)
                return {"ok": True}

            return {"ok": True}

        return {"ok": True}

    # =========================================================
    # 2) MENSAJE NORMAL
    # =========================================================
    msg = update.get("message") or update.get("edited_message")
    if msg:
        chat_id = _safe_int((msg.get("chat") or {}).get("id"))
        if chat_id is None:
            return {"ok": True}

        text = (msg.get("text") or "").strip()

        if normalize(text) in ("/id", "id"):
            telegram_send_text(bot_token, chat_id, f"chat_id = {chat_id}")
            return {"ok": True}

        if mode == "client":
            sess = get_sess(tenant_id, chat_id)

            proof_file_id = None
            proof_type = None
            proof_caption = (msg.get("caption") or "").strip()

            if msg.get("photo"):
                proof_file_id = msg["photo"][-1].get("file_id")
                proof_type = "photo"
            elif msg.get("document"):
                proof_file_id = (msg.get("document") or {}).get("file_id")
                proof_type = "document"
                if not proof_caption:
                    proof_caption = ((msg.get("document") or {}).get("file_name") or "").strip()

            if proof_file_id and proof_type:
                order_id = (sess.get("tmp") or {}).get("pending_order_id")
                if not order_id:
                    order_id = find_latest_pending_order_for_contact(
                        orders_sh=orders_sh,
                        customer_contact=str(chat_id),
                        status="PENDING_PAYMENT",
                    )

                if not order_id:
                    telegram_send_text(bot_token, chat_id, "No encontré un pedido pendiente. Crea uno nuevo con /start.", reply_markup=client_home_kb())
                    return {"ok": True}

                update_order_payment_proof(
                    orders_sh=orders_sh,
                    order_id=order_id,
                    proof_file_id=proof_file_id,
                    proof_type=proof_type,
                    proof_caption=proof_caption,
                )

                telegram_send_text(
                    bot_token,
                    chat_id,
                    "✅ Comprobante recibido.\nAhora presiona “✅ Ya pagué” para avisar al administrador.",
                    reply_markup=i_paid_kb(tenant_id, order_id),
                )
                return {"ok": True}

            if normalize(text) in ("start", "/start", "hola"):
                clear_sess(tenant_id, chat_id)

                log_event_to_sheet(
                    orders_sh=orders_sh,
                    tenant_id=tenant_id,
                    chat_id=str(chat_id),
                    event_type="client_start",
                    meta={"source": "telegram", "text": text[:50]},
                )

                bs = _get_business_status_safe(orders_sh=orders_sh, tenant_tz=tenant_tz)
                if not bool(bs.get("accepts_orders_now")):
                    _send_business_blocked(bot_token, chat_id, bs)
                    return {"ok": True}

                telegram_send_text(bot_token, chat_id, "Bienvenido 👋\nElige una opción:", client_home_kb())
                return {"ok": True}

            if sess.get("stage") == "awaiting_name":
                if not _client_orders_allowed_or_notify(bot_token, chat_id, orders_sh, tenant_tz):
                    sess["stage"] = "idle"
                    return {"ok": True}

                customer_name = text.strip()
                if not customer_name:
                    telegram_send_text(bot_token, chat_id, "Dime tu nombre, por favor.")
                    return {"ok": True}

                menu_idx = load_menu_index(orders_sh)
                cart = sess.get("cart") or []

                items_list: List[Dict[str, Any]] = []
                for it in cart:
                    sku = str(it.get("sku") or "").strip()
                    if not sku:
                        continue
                    try:
                        qty = int(it.get("qty") or 1)
                    except Exception:
                        qty = 1
                    qty = max(1, qty)
                    if sku in menu_idx:
                        items_list.append({"sku": sku, "qty": qty})

                if not items_list:
                    telegram_send_text(bot_token, chat_id, "Tu carrito está vacío.", reply_markup=client_home_kb())
                    sess["stage"] = "idle"
                    return {"ok": True}

                items_snapshot = build_items_snapshot(items_list, menu_idx)
                lines_real, total_real, total_qty_real = fmt_snapshot_lines(items_snapshot)

                order_id = gen_order_id()
                requested_time = "pendiente"

                append_order_row(
                    orders_sh=orders_sh,
                    tenant_id=tenant_id,
                    order_id=order_id,
                    customer_name=customer_name,
                    customer_contact=str(chat_id),
                    items=items_list,
                    items_snapshot=items_snapshot,
                    currency="BOB",
                    pricing_version="v1",
                    delivery_type="pickup",
                    requested_time=requested_time,
                    status="PENDING_PAYMENT",
                    source="telegram",
                    total_amount=total_real,
                )

                sess["stage"] = "awaiting_proof"
                sess["tmp"] = sess.get("tmp") or {}
                sess["tmp"]["pending_order_id"] = order_id
                sess["tmp"]["customer_name"] = customer_name

                recap = build_order_recap_text(
                    order_id=order_id,
                    customer_name=customer_name,
                    customer_contact=str(chat_id),
                    requested_time=requested_time,
                    detail_lines=lines_real,
                    total_qty=total_qty_real,
                    total=total_real,
                )

                telegram_send_text(
                    bot_token,
                    chat_id,
                    recap + "\n💳 *Ahora realiza el pago.*\nTe enviamos el QR a continuación.",
                    parse_mode="Markdown",
                )

                qr_file_id = get_payment_qr_file_id(tenant)
                qr_url = get_payment_qr_url(tenant)

                if qr_file_id:
                    telegram_send_photo(bot_token, chat_id, qr_file_id, caption="QR de pago")
                elif qr_url:
                    telegram_send_photo(bot_token, chat_id, qr_url, caption="QR de pago")
                else:
                    telegram_send_text(bot_token, chat_id, "⚠️ No tengo QR configurado para este tenant (payment_qr_file_id / payment_qr_url).")
                    log_event("missing_qr_config", tenant_id=tenant_id)

                telegram_send_text(
                    bot_token,
                    chat_id,
                    "📎 Cuando pagues, envía aquí tu *comprobante* (foto o PDF).\n"
                    "Después de enviarlo, podrás presionar “✅ Ya pagué”.",
                    parse_mode="Markdown",
                )
                return {"ok": True}

            telegram_send_text(bot_token, chat_id, "Usa /start para ver el menú.", reply_markup=client_home_kb())
            return {"ok": True}

        if mode == "admin":
            txt_norm = normalize(text)
            sess = get_sess(tenant_id, chat_id)
            tmp = sess.setdefault("tmp", {})

            input_mode = str(tmp.get("admin_menu_input_mode") or "").strip()
            input_sku = str(tmp.get("admin_menu_price_sku") or "").strip()

            # subida de foto producto (estado estable que ya te funcionaba)
            if input_mode == "awaiting_photo" and input_sku:
                _assert_admin_authorized(tenant, chat_id, tenant_id)

                if msg.get("photo"):
                    file_id = msg["photo"][-1]["file_id"]

                    ws = orders_sh.worksheet("Menu")
                    values = ws.get_all_values()
                    if not values:
                        telegram_send_text(bot_token, chat_id, "No pude leer la hoja Menu.")
                        return {"ok": True}

                    header_row_1based = detect_header_row(
                        values,
                        required_headers=["sku", "name", "price", "active", "category"],
                        max_scan=10,
                    )
                    header = values[header_row_1based - 1]

                    try:
                        sku_col = header.index("sku") + 1
                        photo_col = header.index("photo_file_id") + 1
                    except ValueError:
                        telegram_send_text(bot_token, chat_id, "Falta la columna 'photo_file_id' en la hoja Menu.")
                        return {"ok": True}

                    found = False
                    for i in range(header_row_1based + 1, len(values) + 1):
                        row = values[i - 1]
                        sku_val = row[sku_col - 1] if len(row) >= sku_col else ""
                        if str(sku_val).strip() == input_sku:
                            ws.update_cell(i, photo_col, file_id)
                            found = True
                            break

                    if not found:
                        telegram_send_text(bot_token, chat_id, f"No encontré el producto SKU {input_sku} en la hoja Menu.")
                        return {"ok": True}

                    invalidate_menu_cache(orders_sh)

                    tmp.pop("admin_menu_input_mode", None)
                    tmp.pop("admin_menu_price_sku", None)

                    telegram_send_text(
                        bot_token,
                        chat_id,
                        "✅ Foto guardada correctamente",
                    )

                    return {"ok": _send_admin_menu_product_detail(
                        bot_token, chat_id, tenant_id, orders_sh, sess, input_sku
                    )}

                telegram_send_text(
                    bot_token,
                    chat_id,
                    "📷 Estoy esperando una foto del producto. Envíala como imagen de Telegram.",
                )
                return {"ok": True}

            if input_mode and input_sku:
                _assert_admin_authorized(tenant, chat_id, tenant_id)

                item = get_menu_product_or_404(orders_sh, input_sku)
                current_price = float(item.get("price", 0.0))
                n = _extract_first_number(text)

                if n is None:
                    if input_mode == "price_final":
                        telegram_send_text(
                            bot_token,
                            chat_id,
                            "No pude leer un número válido.\nEscribe solo el precio o algo como: 25 bs",
                        )
                    elif input_mode == "discount_pct":
                        telegram_send_text(
                            bot_token,
                            chat_id,
                            "No pude leer un porcentaje válido.\nEscribe algo como: 10 o 15%",
                        )
                    return {"ok": True}

                if input_mode == "price_final":
                    if n < 0:
                        telegram_send_text(bot_token, chat_id, "El precio no puede ser negativo. Intenta otra vez.")
                        return {"ok": True}

                    result = set_menu_product_price(orders_sh, input_sku, float(n))
                    tmp.pop("admin_menu_input_mode", None)
                    tmp.pop("admin_menu_price_sku", None)
                    tmp.pop("admin_menu_price_work", None)

                    telegram_send_text(
                        bot_token,
                        chat_id,
                        f"✅ Precio actualizado.\nSKU: {input_sku}\nNuevo precio: Bs {_fmt_price_short(result.get('price', 0))}",
                    )
                    return {"ok": _send_admin_menu_product_detail(bot_token, chat_id, tenant_id, orders_sh, sess, input_sku)}

                if input_mode == "discount_pct":
                    if n < 0:
                        telegram_send_text(bot_token, chat_id, "El descuento no puede ser negativo. Intenta otra vez.")
                        return {"ok": True}
                    if n > 100:
                        telegram_send_text(bot_token, chat_id, "El descuento no puede ser mayor a 100%. Intenta otra vez.")
                        return {"ok": True}

                    new_price = round(current_price * (1.0 - (float(n) / 100.0)), 2)
                    if new_price < 0:
                        new_price = 0.0

                    result = set_menu_product_price(orders_sh, input_sku, new_price)
                    tmp.pop("admin_menu_input_mode", None)
                    tmp.pop("admin_menu_price_sku", None)
                    tmp.pop("admin_menu_price_work", None)

                    telegram_send_text(
                        bot_token,
                        chat_id,
                        (
                            f"✅ Descuento aplicado.\n"
                            f"SKU: {input_sku}\n"
                            f"Descuento: {n}%\n"
                            f"Precio anterior: Bs {_fmt_price_short(current_price)}\n"
                            f"Nuevo precio: Bs {_fmt_price_short(result.get('price', 0))}"
                        ),
                    )
                    return {"ok": _send_admin_menu_product_detail(bot_token, chat_id, tenant_id, orders_sh, sess, input_sku)}

            if txt_norm in ("estadisticas", "/stats", "stats"):
                _assert_admin_authorized(tenant, chat_id, tenant_id)
                periods = build_periods(tenant_tz)
                telegram_send_text(
                    bot_token,
                    chat_id,
                    "📊 Elige el período:",
                    reply_markup=admin_periods_inline_kb(tenant_id, periods),
                )
                telegram_send_text(bot_token, chat_id, "Panel admin:", reply_markup=admin_fixed_kb())
                return {"ok": True}

            if txt_norm in (
                "config dias y horarios",
                "dias y horarios",
                "configuracion dias y horarios",
                "configuracion de dias y horarios",
            ):
                _assert_admin_authorized(tenant, chat_id, tenant_id)
                return {"ok": _send_admin_hours_menu(bot_token, chat_id, tenant_id, orders_sh, tenant_tz)}

            if txt_norm in (
                "config menu y precios",
                "menu y precios",
                "configuracion menu y precios",
                "configuracion de menu y precios",
            ):
                _assert_admin_authorized(tenant, chat_id, tenant_id)
                return {"ok": _send_admin_menu_home(bot_token, chat_id, tenant_id, orders_sh, sess)}

            if txt_norm in ("start", "/start", "hola"):
                telegram_send_text(bot_token, chat_id, "Admin bot listo ✅", reply_markup=admin_fixed_kb())
                return {"ok": True}

            telegram_send_text(bot_token, chat_id, "OK admin ✅", reply_markup=admin_fixed_kb())
            return {"ok": True}

        return {"ok": True}

    return {"ok": True}
