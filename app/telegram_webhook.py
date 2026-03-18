# app/telegram_webhook.py

import io
import json
import re
import time
import urllib.request
import urllib.parse
from typing import Any, Dict, Optional, List, Tuple, Set

from fastapi import APIRouter, HTTPException
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseUpload

from app.config import TELEGRAM_API_BASE
from app.tenants import get_tenant_or_404, resolve_bot_by_secret
from app.sheets import (
    get_gspread_client,
    open_spreadsheet_by_key,
    detect_header_row,
    get_google_credentials,
)
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
# Drive helpers
# -------------------------

def get_drive_service():
    creds = get_google_credentials()
    return build("drive", "v3", credentials=creds)


def upload_product_photo_to_drive(
    tenant_id: str,
    sku: str,
    file_bytes: bytes,
    mime_type: str = "image/jpeg",
) -> str:
    service = get_drive_service()

    file_name = f"{tenant_id}_{sku}_{int(time.time())}.jpg"
    media = MediaIoBaseUpload(io.BytesIO(file_bytes), mimetype=mime_type, resumable=False)

    created = service.files().create(
        body={
            "name": file_name,
        },
        media_body=media,
        fields="id",
    ).execute()

    file_id = created["id"]

    service.permissions().create(
        fileId=file_id,
        body={"type": "anyone", "role": "reader"},
    ).execute()

    return f"https://drive.google.com/uc?export=view&id={file_id}"


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
        # =========================================================
# ADMIN SETTINGS
# =========================================================

def _load_admin_settings(ws) -> Dict[str, str]:
    values = ws.get_all_values()
    if not values:
        return {}

    header_idx = detect_header_row(values, required_headers=["key", "value"])
    headers = [normalize(h) for h in values[header_idx]]

    try:
        k_idx = headers.index("key")
        v_idx = headers.index("value")
    except ValueError:
        return {}

    out: Dict[str, str] = {}

    for row in values[header_idx + 1:]:
        if k_idx >= len(row):
            continue
        key = normalize(row[k_idx])
        if not key:
            continue

        value = ""
        if v_idx < len(row):
            value = (row[v_idx] or "").strip()

        out[key] = value

    return out


def _save_admin_settings(ws, data: Dict[str, str]) -> None:
    rows = [["key", "value"]]
    for k, v in data.items():
        rows.append([k, v])

    ws.clear()
    ws.update("A1", rows)


def _get_or_create_admin_settings_ws(spreadsheet):
    try:
        return spreadsheet.worksheet(ADMIN_SETTINGS_SHEET_NAME)
    except Exception:
        ws = spreadsheet.add_worksheet(title=ADMIN_SETTINGS_SHEET_NAME, rows=50, cols=2)
        ws.update("A1", [["key", "value"]])
        return ws


# =========================================================
# HORARIOS
# =========================================================

def _load_hours(spreadsheet) -> Dict[str, str]:
    ws = _get_or_create_admin_settings_ws(spreadsheet)
    data = _load_admin_settings(ws)

    hours = {}
    for d in DAY_ORDER:
        hours[d] = data.get(f"hours_{d}", "")

    return hours


def _save_hours(spreadsheet, hours: Dict[str, str]) -> None:
    ws = _get_or_create_admin_settings_ws(spreadsheet)
    data = _load_admin_settings(ws)

    for d in DAY_ORDER:
        data[f"hours_{d}"] = hours.get(d, "")

    _save_admin_settings(ws, data)


# =========================================================
# MENU CLIENTE
# =========================================================

def send_menu_categories(bot_token: str, chat_id: int, tenant_id: str, tenant: Dict[str, Any]) -> None:
    menu_idx = load_menu_index(tenant_id)
    grouped = group_menu_by_category(menu_idx)

    if not grouped:
        telegram_send_text(bot_token, chat_id, "El menú está vacío.")
        return

    rows = []
    for cat in grouped.keys():
        rows.append([(cat, f"cat|{cat}")])

    rows.append([("🛒 Ver carrito", "cart")])
    rows.append([("🏠 Inicio", "home")])

    telegram_send_text(
        bot_token,
        chat_id,
        "📋 *Menú*\nSelecciona una categoría:",
        kb(rows),
        parse_mode="Markdown",
    )


def send_menu_products(bot_token: str, chat_id: int, tenant_id: str, category: str) -> None:
    menu_idx = load_menu_index(tenant_id)
    grouped = group_menu_by_category(menu_idx)

    products = grouped.get(category)
    if not products:
        telegram_send_text(bot_token, chat_id, "No hay productos en esta categoría.")
        return

    for p in products:
        caption = f"*{p['name']}*\n{_fmt_price_short(p['price'])} BOB"

        if p.get("photo_url"):
            telegram_send_photo(bot_token, chat_id, p["photo_url"], caption)
        else:
            telegram_send_text(bot_token, chat_id, caption, parse_mode="Markdown")

        telegram_send_text(
            bot_token,
            chat_id,
            "Cantidad:",
            kb([
                [("1", f"qty|{p['sku']}|1"), ("2", f"qty|{p['sku']}|2"), ("3", f"qty|{p['sku']}|3")],
                [("4", f"qty|{p['sku']}|4")],
            ]),
        )

    telegram_send_text(
        bot_token,
        chat_id,
        "⬅️ Volver",
        kb([[("⬅️ Categorías", "menu")]]),
    )


# =========================================================
# CARRITO
# =========================================================

def send_cart(bot_token: str, chat_id: int, tenant_id: str) -> None:
    sess = get_sess(tenant_id, chat_id)

    menu_idx = load_menu_index(tenant_id)
    lines, total, total_qty = fmt_cart_lines(sess["cart"], menu_idx)

    text = (
        f"🛒 *Tu carrito*\n\n"
        f"{lines}\n\n"
        f"Cantidad: *{total_qty}*\n"
        f"Total: *{total:.2f} BOB*"
    )

    telegram_send_text(
        bot_token,
        chat_id,
        text,
        kb([
            [("➕ Seguir comprando", "menu")],
            [("🗑 Vaciar carrito", "clear_cart")],
            [("✅ Confirmar pedido", "checkout")],
        ]),
        parse_mode="Markdown",
    )
    # =========================================================
# CHECKOUT / PEDIDO
# =========================================================

def _tenant_sheet(gc, tenant: Dict[str, Any]):
    sheet_id = (tenant.get("orders_sheet_id") or "").strip()
    if not sheet_id:
        raise HTTPException(status_code=500, detail="orders_sheet_id missing")
    return open_spreadsheet_by_key(gc, sheet_id)


def _find_ws_by_name(spreadsheet, name: str):
    try:
        return spreadsheet.worksheet(name)
    except Exception:
        return None


def _ensure_orders_ws(spreadsheet):
    ws = _find_ws_by_name(spreadsheet, "ORDERS")
    if ws:
        return ws

    ws = spreadsheet.add_worksheet(title="ORDERS", rows=2000, cols=20)
    ws.update(
        "A1",
        [[
            "order_id",
            "tenant_id",
            "customer_name",
            "customer_contact",
            "items",
            "items_snapshot",
            "currency",
            "pricing_version",
            "delivery_type",
            "requested_time",
            "status",
            "source",
            "total_amount",
            "payment_proof_file_id",
            "payment_proof_type",
            "payment_proof_caption",
            "created_at",
        ]]
    )
    return ws


def _append_order_row_direct(
    spreadsheet,
    tenant_id: str,
    order_id: str,
    customer_name: str,
    customer_contact: str,
    items: List[Dict[str, Any]],
    items_snapshot: List[Dict[str, Any]],
    currency: str,
    pricing_version: str,
    delivery_type: str,
    requested_time: str,
    status: str,
    source: str,
    total_amount: float,
) -> None:
    ws = _ensure_orders_ws(spreadsheet)
    ws.append_row(
        [
            order_id,
            tenant_id,
            customer_name,
            customer_contact,
            json.dumps(items, ensure_ascii=False),
            json.dumps(items_snapshot, ensure_ascii=False),
            currency,
            pricing_version,
            delivery_type,
            requested_time,
            status,
            source,
            total_amount,
            "",
            "",
            "",
            now_iso_utc(),
        ],
        value_input_option="USER_ENTERED",
    )


def _set_session_stage(tenant_id: str, chat_id: int, stage: str) -> None:
    sess = get_sess(tenant_id, chat_id)
    sess["stage"] = stage


def _set_session_tmp(tenant_id: str, chat_id: int, **kwargs) -> None:
    sess = get_sess(tenant_id, chat_id)
    sess.setdefault("tmp", {})
    for k, v in kwargs.items():
        sess["tmp"][k] = v


def _clear_session_tmp_keys(tenant_id: str, chat_id: int, keys: List[str]) -> None:
    sess = get_sess(tenant_id, chat_id)
    sess.setdefault("tmp", {})
    for k in keys:
        sess["tmp"].pop(k, None)


def start_checkout(bot_token: str, chat_id: int, tenant_id: str) -> None:
    sess = get_sess(tenant_id, chat_id)
    if not sess["cart"]:
        telegram_send_text(bot_token, chat_id, "Tu carrito está vacío.")
        return

    _set_session_stage(tenant_id, chat_id, "awaiting_name")
    telegram_send_text(
        bot_token,
        chat_id,
        "Perfecto. Envíame tu *nombre* para el pedido.",
        parse_mode="Markdown",
    )


def handle_customer_name(
    bot_token: str,
    chat_id: int,
    tenant_id: str,
    tenant: Dict[str, Any],
    customer_name: str,
) -> None:
    sess = get_sess(tenant_id, chat_id)
    menu_idx = load_menu_index(tenant_id)

    items_list: List[Dict[str, Any]] = []
    for it in sess["cart"]:
        sku = str(it.get("sku") or "").strip()
        qty = int(it.get("qty") or 1)
        if sku in menu_idx:
            items_list.append({"sku": sku, "qty": qty})

    if not items_list:
        telegram_send_text(bot_token, chat_id, "Tu carrito está vacío.")
        _set_session_stage(tenant_id, chat_id, "idle")
        return

    items_snapshot = build_items_snapshot(items_list, menu_idx)
    detail_lines, total_real, total_qty_real = fmt_snapshot_lines(items_snapshot)

    order_id = gen_order_id()
    requested_time = "pendiente"

    gc = get_gspread_client()
    spreadsheet = _tenant_sheet(gc, tenant)

    _append_order_row_direct(
        spreadsheet=spreadsheet,
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

    _set_session_stage(tenant_id, chat_id, "awaiting_proof")
    _set_session_tmp(
        tenant_id,
        chat_id,
        pending_order_id=order_id,
        customer_name=customer_name,
    )

    recap = build_order_recap_text(
        order_id=order_id,
        customer_name=customer_name,
        customer_contact=str(chat_id),
        requested_time=requested_time,
        detail_lines=detail_lines,
        total_qty=total_qty_real,
        total=total_real,
    )

    telegram_send_text(
        bot_token,
        chat_id,
        recap + "\n💳 *Ahora realiza el pago.*",
        parse_mode="Markdown",
    )

    qr_file_id = get_payment_qr_file_id(tenant)
    qr_url = get_payment_qr_url(tenant)

    if qr_file_id:
        telegram_send_photo(bot_token, chat_id, qr_file_id, caption="QR de pago")
    elif qr_url:
        telegram_send_photo(bot_token, chat_id, qr_url, caption="QR de pago")
    else:
        telegram_send_text(bot_token, chat_id, "No hay QR configurado para este restaurante.")

    telegram_send_text(
        bot_token,
        chat_id,
        "📎 Cuando pagues, envía aquí tu *comprobante* (foto o PDF).",
        parse_mode="Markdown",
    )


# =========================================================
# DRIVE / FOTOS DE PRODUCTOS
# =========================================================

def _extract_drive_folder_id(url_or_id: str) -> str:
    s = (url_or_id or "").strip()
    if not s:
        return ""

    m = re.search(r"/folders/([a-zA-Z0-9_-]+)", s)
    if m:
        return m.group(1)

    if re.fullmatch(r"[a-zA-Z0-9_-]{10,}", s):
        return s

    return ""


def _load_drive_service_account_info() -> Dict[str, Any]:
    raw = os.getenv("GCP_CREDENTIALS_JSON", "").strip()
    if not raw:
        raise RuntimeError("GCP_CREDENTIALS_JSON missing")
    return json.loads(raw)


def _drive_access_token() -> str:
    from google.oauth2 import service_account
    from google.auth.transport.requests import Request

    info = _load_drive_service_account_info()
    creds = service_account.Credentials.from_service_account_info(
        info,
        scopes=["https://www.googleapis.com/auth/drive"],
    )
    creds.refresh(Request())
    return creds.token


def _drive_create_public_permission(file_id: str) -> None:
    token = _drive_access_token()
    url = f"https://www.googleapis.com/drive/v3/files/{file_id}/permissions"

    payload = json.dumps({
        "role": "reader",
        "type": "anyone",
    }).encode("utf-8")

    req = urllib.request.Request(
        url,
        data=payload,
        method="POST",
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        },
    )

    with urllib.request.urlopen(req, timeout=30) as resp:
        resp.read()


def _drive_upload_bytes(folder_id: str, filename: str, content_type: str, file_bytes: bytes) -> Dict[str, Any]:
    token = _drive_access_token()
    boundary = f"====driveBoundary{int(time.time() * 1000)}"

    metadata = {
        "name": filename,
        "parents": [folder_id] if folder_id else [],
    }

    parts: List[bytes] = []
    parts.append(f"--{boundary}\r\n".encode())
    parts.append(b'Content-Type: application/json; charset=UTF-8\r\n\r\n')
    parts.append(json.dumps(metadata).encode("utf-8"))
    parts.append(b"\r\n")

    parts.append(f"--{boundary}\r\n".encode())
    parts.append(f"Content-Type: {content_type}\r\n\r\n".encode())
    parts.append(file_bytes)
    parts.append(b"\r\n")
    parts.append(f"--{boundary}--\r\n".encode())

    body = b"".join(parts)

    req = urllib.request.Request(
        "https://www.googleapis.com/upload/drive/v3/files?uploadType=multipart&fields=id,name,webViewLink,webContentLink",
        data=body,
        method="POST",
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": f'multipart/related; boundary="{boundary}"',
        },
    )

    with urllib.request.urlopen(req, timeout=60) as resp:
        raw = resp.read().decode("utf-8")
        data = json.loads(raw)

    file_id = data["id"]
    _drive_create_public_permission(file_id)

    return {
        "id": file_id,
        "name": data.get("name", filename),
        "url": f"https://drive.google.com/uc?export=view&id={file_id}",
        "webViewLink": data.get("webViewLink", ""),
        "webContentLink": data.get("webContentLink", ""),
    }


def _menu_sheet(spreadsheet):
    ws = _find_ws_by_name(spreadsheet, "Menu")
    if not ws:
        raise HTTPException(status_code=500, detail="Worksheet 'Menu' not found")
    return ws


def _set_menu_photo_url(spreadsheet, sku: str, photo_url: str) -> bool:
    ws = _menu_sheet(spreadsheet)
    values = ws.get_all_values()
    if not values:
        return False

    header_row_1based = detect_header_row(
        values,
        required_headers=["sku", "name", "price", "active", "category"],
        max_scan=10,
    )
    header = [h.strip() for h in values[header_row_1based - 1]]

    if "photo_url" not in header:
        ws.update_cell(header_row_1based, len(header) + 1, "photo_url")
        header.append("photo_url")

    sku_col = header.index("sku") + 1
    photo_col = header.index("photo_url") + 1

    found = False
    for i in range(header_row_1based + 1, len(values) + 1):
        row = values[i - 1]
        sku_val = row[sku_col - 1] if len(row) >= sku_col else ""
        if str(sku_val).strip() == sku:
            ws.update_cell(i, photo_col, photo_url)
            found = True
            break

    return found


def _upload_product_photo_to_drive(
    tenant: Dict[str, Any],
    bot_token: str,
    telegram_file_id: str,
    sku: str,
) -> Dict[str, Any]:
    folder_id = _extract_drive_folder_id(tenant.get("product_photos_drive_folder_id") or "")
    if not folder_id:
        raise RuntimeError("product_photos_drive_folder_id missing")

    file_path = _telegram_get_file_path(bot_token, telegram_file_id)
    file_bytes = _telegram_download_file_bytes(bot_token, file_path)

    ext = "jpg"
    content_type = "image/jpeg"
    if "." in file_path:
        ext = file_path.rsplit(".", 1)[-1].lower()
        if ext == "png":
            content_type = "image/png"
        elif ext == "webp":
            content_type = "image/webp"

    filename = f"{sku}_{int(time.time())}.{ext}"
    return _drive_upload_bytes(folder_id, filename, content_type, file_bytes)
    # =========================================================
# COMPROBANTES / PAGOS
# =========================================================

def _orders_ws(spreadsheet):
    ws = _find_ws_by_name(spreadsheet, "ORDERS")
    if not ws:
        raise HTTPException(status_code=500, detail="Worksheet 'ORDERS' not found")
    return ws


def _orders_headers(ws) -> List[str]:
    values = ws.get_all_values()
    if not values:
        return []
    return [str(x).strip() for x in values[0]]


def _find_order_row_by_id(ws, order_id: str) -> Optional[int]:
    values = ws.get_all_values()
    if not values or len(values) < 2:
        return None

    header = [str(x).strip() for x in values[0]]
    if "order_id" not in header:
        return None

    col = header.index("order_id")

    for i in range(2, len(values) + 1):
        row = values[i - 1]
        val = row[col] if len(row) > col else ""
        if str(val).strip() == str(order_id).strip():
            return i
    return None


def _set_order_payment_proof_direct(
    spreadsheet,
    order_id: str,
    proof_file_id: str,
    proof_type: str,
    proof_caption: str,
) -> bool:
    ws = _orders_ws(spreadsheet)
    values = ws.get_all_values()
    if not values:
        return False

    header = [str(x).strip() for x in values[0]]
    needed = ["payment_proof_file_id", "payment_proof_type", "payment_proof_caption"]

    for col_name in needed:
        if col_name not in header:
            ws.update_cell(1, len(header) + 1, col_name)
            header.append(col_name)

    row_idx = _find_order_row_by_id(ws, order_id)
    if not row_idx:
        return False

    ws.update_cell(row_idx, header.index("payment_proof_file_id") + 1, proof_file_id)
    ws.update_cell(row_idx, header.index("payment_proof_type") + 1, proof_type)
    ws.update_cell(row_idx, header.index("payment_proof_caption") + 1, proof_caption or "")
    return True


# =========================================================
# MANEJO MENSAJES CLIENTE
# =========================================================

def _handle_client_message(
    bot_token: str,
    tenant_id: str,
    tenant: Dict[str, Any],
    msg: Dict[str, Any],
) -> Dict[str, Any]:
    chat_id = _safe_int((msg.get("chat") or {}).get("id"))
    if chat_id is None:
        return {"ok": True}

    text = (msg.get("text") or "").strip()
    text_norm = normalize(text)
    sess = get_sess(tenant_id, chat_id)

    gc = get_gspread_client()
    spreadsheet = _tenant_sheet(gc, tenant)

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
                orders_sh=spreadsheet,
                customer_contact=str(chat_id),
                status="PENDING_PAYMENT",
            )

        if not order_id:
            telegram_send_text(bot_token, chat_id, "No encontré un pedido pendiente. Usa /start.")
            return {"ok": True}

        ok = _set_order_payment_proof_direct(
            spreadsheet=spreadsheet,
            order_id=order_id,
            proof_file_id=proof_file_id,
            proof_type=proof_type,
            proof_caption=proof_caption,
        )
        if not ok:
            telegram_send_text(bot_token, chat_id, "No pude guardar el comprobante.")
            return {"ok": True}

        telegram_send_text(
            bot_token,
            chat_id,
            "✅ Comprobante recibido.\nAhora presiona “✅ Ya pagué” para avisar al administrador.",
            reply_markup=i_paid_kb(tenant_id, order_id),
        )
        return {"ok": True}

    if text_norm in ("/id", "id"):
        telegram_send_text(bot_token, chat_id, f"chat_id = {chat_id}")
        return {"ok": True}

    if text_norm in ("/start", "start", "hola"):
        clear_sess(tenant_id, chat_id)
        telegram_send_text(bot_token, chat_id, "Bienvenido 👋\nElige una opción:", client_home_kb())
        return {"ok": True}

    if sess.get("stage") == "awaiting_name":
        customer_name = text.strip()
        if not customer_name:
            telegram_send_text(bot_token, chat_id, "Dime tu nombre, por favor.")
            return {"ok": True}

        handle_customer_name(
            bot_token=bot_token,
            chat_id=chat_id,
            tenant_id=tenant_id,
            tenant=tenant,
            customer_name=customer_name,
        )
        return {"ok": True}

    telegram_send_text(bot_token, chat_id, "Usa /start para ver el menú.", reply_markup=client_home_kb())
    return {"ok": True}


# =========================================================
# MANEJO MENSAJES ADMIN
# =========================================================

def _handle_admin_message(
    bot_token: str,
    tenant_id: str,
    tenant: Dict[str, Any],
    msg: Dict[str, Any],
) -> Dict[str, Any]:
    chat_id = _safe_int((msg.get("chat") or {}).get("id"))
    if chat_id is None:
        return {"ok": True}

    _assert_admin_authorized(tenant, chat_id, tenant_id)

    text = (msg.get("text") or "").strip()
    txt_norm = normalize(text)

    sess = get_sess(tenant_id, chat_id)
    tmp = sess.setdefault("tmp", {})

    input_mode = str(tmp.get("admin_menu_input_mode") or "").strip()
    input_sku = str(tmp.get("admin_menu_price_sku") or "").strip()

    gc = get_gspread_client()
    spreadsheet = _tenant_sheet(gc, tenant)

    # ===============================
    # ADMIN subida de foto producto
    # ===============================
    if input_mode == "awaiting_photo" and input_sku:
        if msg.get("photo"):
            admin_file_id = msg["photo"][-1]["file_id"]

            try:
                upload = _upload_product_photo_to_drive(
                    tenant=tenant,
                    bot_token=bot_token,
                    telegram_file_id=admin_file_id,
                    sku=input_sku,
                )
            except Exception as e:
                telegram_send_text(bot_token, chat_id, "No pude guardar la foto del producto.")
                log_event(
                    "admin_product_photo_drive_upload_failed",
                    tenant_id=tenant_id,
                    sku=input_sku,
                    error=str(e),
                )
                return {"ok": True}

            photo_url = upload["url"]
            ok = _set_menu_photo_url(spreadsheet, input_sku, photo_url)
            if not ok:
                telegram_send_text(bot_token, chat_id, f"No encontré el producto SKU {input_sku} en la hoja Menu.")
                return {"ok": True}

            invalidate_menu_cache(tenant_id)

            tmp.pop("admin_menu_input_mode", None)
            tmp.pop("admin_menu_price_sku", None)

            telegram_send_text(
                bot_token,
                chat_id,
                "✅ Foto guardada correctamente.\nNo se envió nada al cliente.",
            )

            return {
                "ok": _send_admin_menu_product_detail(
                    bot_token,
                    chat_id,
                    tenant_id,
                    spreadsheet,
                    sess,
                    input_sku,
                )
            }

        telegram_send_text(
            bot_token,
            chat_id,
            "📷 Estoy esperando una foto del producto. Envíala como imagen de Telegram.",
        )
        return {"ok": True}

    if input_mode and input_sku:
        item = get_menu_product_or_404(spreadsheet, input_sku)
        current_price = float(item.get("price", 0.0))
        n = _extract_first_number(text)

        if n is None:
            if input_mode == "price_final":
                telegram_send_text(bot_token, chat_id, "No pude leer un número válido.")
            elif input_mode == "discount_pct":
                telegram_send_text(bot_token, chat_id, "No pude leer un porcentaje válido.")
            return {"ok": True}

        if input_mode == "price_final":
            if n < 0:
                telegram_send_text(bot_token, chat_id, "El precio no puede ser negativo.")
                return {"ok": True}

            result = set_menu_product_price(spreadsheet, input_sku, float(n))
            tmp.pop("admin_menu_input_mode", None)
            tmp.pop("admin_menu_price_sku", None)
            tmp.pop("admin_menu_price_work", None)

            telegram_send_text(
                bot_token,
                chat_id,
                f"✅ Precio actualizado.\nSKU: {input_sku}\nNuevo precio: Bs {_fmt_price_short(result.get('price', 0))}",
            )
            return {"ok": _send_admin_menu_product_detail(bot_token, chat_id, tenant_id, spreadsheet, sess, input_sku)}

        if input_mode == "discount_pct":
            if n < 0 or n > 100:
                telegram_send_text(bot_token, chat_id, "El descuento debe estar entre 0 y 100.")
                return {"ok": True}

            new_price = round(current_price * (1.0 - (float(n) / 100.0)), 2)
            result = set_menu_product_price(spreadsheet, input_sku, new_price)

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
            return {"ok": _send_admin_menu_product_detail(bot_token, chat_id, tenant_id, spreadsheet, sess, input_sku)}

    if txt_norm in ("estadisticas", "/stats", "stats"):
        periods = build_periods("America/La_Paz")
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
        return {"ok": _send_admin_hours_menu(bot_token, chat_id, tenant_id, spreadsheet, "America/La_Paz")}

    if txt_norm in (
        "config menu y precios",
        "menu y precios",
        "configuracion menu y precios",
        "configuracion de menu y precios",
    ):
        return {"ok": _send_admin_menu_home(bot_token, chat_id, tenant_id, spreadsheet, sess)}

    if txt_norm in ("start", "/start", "hola"):
        telegram_send_text(bot_token, chat_id, "Admin bot listo ✅", reply_markup=admin_fixed_kb())
        return {"ok": True}

    telegram_send_text(bot_token, chat_id, "OK admin ✅", reply_markup=admin_fixed_kb())
    return {"ok": True}


# =========================================================
# CALLBACKS CLIENTE
# =========================================================

def _handle_client_callback(
    bot_token: str,
    tenant_id: str,
    tenant: Dict[str, Any],
    chat_id: int,
    data: str,
) -> Dict[str, Any]:
    sess = get_sess(tenant_id, chat_id)
    tmp = sess.setdefault("tmp", {})

    gc = get_gspread_client()
    spreadsheet = _tenant_sheet(gc, tenant)

    if data == "home":
        telegram_send_text(bot_token, chat_id, "Elige una opción:", client_home_kb())
        return {"ok": True}

    if data == "menu":
        menu_idx = load_menu_index(tenant_id)
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
        cat_norm = data.split("|", 1)[1].strip()

        menu_idx = load_menu_index(tenant_id)
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
            photo_url = str(it.get("photo_url") or "").strip()
            photo_file_id = str(it.get("photo_file_id") or "").strip()

            if photo_url:
                telegram_send_photo(
                    bot_token,
                    chat_id,
                    photo_url,
                    caption=f"{it['name']}\nBs {it['price']}",
                )
            elif photo_file_id:
                telegram_send_photo(
                    bot_token,
                    chat_id,
                    photo_file_id,
                    caption=f"{it['name']}\nBs {it['price']}",
                )

        return {"ok": True}

    if data.startswith("prd|"):
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
        parts = data.split("|")
        if len(parts) != 3:
            return {"ok": True}

        _, sku, qty_s = parts
        try:
            qty = int(qty_s)
        except Exception:
            qty = 1
        qty = max(1, qty)

        menu_idx = load_menu_index(tenant_id)
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

        _, total, total_qty = fmt_cart_lines(cart, menu_idx)
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
        menu_idx = load_menu_index(tenant_id)
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
        start_checkout(bot_token, chat_id, tenant_id)
        return {"ok": True}

    if data.startswith("i_paid|"):
        parts = data.split("|")
        if len(parts) != 3:
            return {"ok": True}

        _, cb_tenant_id, order_id = parts
        if cb_tenant_id.strip() != tenant_id:
            raise HTTPException(status_code=400, detail="Tenant mismatch in i_paid callback")

        order = get_order_by_id(spreadsheet, order_id)
        if not order:
            telegram_send_text(bot_token, chat_id, "No encontré tu pedido. Vuelve a /start.", reply_markup=client_home_kb())
            return {"ok": True}

        proof_file_id = (order.get("payment_proof_file_id") or "").strip()
        if not proof_file_id:
            telegram_send_text(bot_token, chat_id, "Aún no recibí tu comprobante.\nEnvía una foto o PDF del pago primero.")
            return {"ok": True}

        ok_sent = notify_admin_payment_reported(tenant, tenant_id, spreadsheet, order_id, is_reminder=False)
        tmp["paid_pressed_at_ts"] = int(time.time())
        tmp["last_notified_order_id"] = order_id
        tmp["last_admin_notify_ok"] = bool(ok_sent)

        telegram_send_text(
            bot_token,
            chat_id,
            "✅ Recibido. Espera unos minutos mientras verificamos tu pago.\nSi no hay respuesta, podrás enviar un recordatorio.",
            reply_markup=paid_actions_kb(tenant_id, order_id),
        )
        return {"ok": True}

    if data.startswith("remind|"):
        parts = data.split("|")
        if len(parts) != 3:
            return {"ok": True}

        _, cb_tenant_id, order_id = parts
        if cb_tenant_id.strip() != tenant_id:
            raise HTTPException(status_code=400, detail="Tenant mismatch in remind callback")

        paid_at = int(tmp.get("paid_pressed_at_ts") or 0)
        now_ts = int(time.time())

        if not paid_at:
            telegram_send_text(bot_token, chat_id, "Primero presiona “✅ Ya pagué”.", reply_markup=paid_actions_kb(tenant_id, order_id))
            return {"ok": True}

        if (now_ts - paid_at) < REMINDER_COOLDOWN_SECONDS:
            left = REMINDER_COOLDOWN_SECONDS - (now_ts - paid_at)
            mins = max(1, int((left + 59) / 60))
            telegram_send_text(
                bot_token,
                chat_id,
                f"🙏 Espera un momento.\nPodrás enviar un recordatorio en aproximadamente *{mins} minuto(s)*.",
                reply_markup=paid_actions_kb(tenant_id, order_id),
                parse_mode="Markdown",
            )
            return {"ok": True}

        ok_sent = notify_admin_payment_reported(tenant, tenant_id, spreadsheet, order_id, is_reminder=True)
        tmp["reminder_sent_at_ts"] = now_ts
        tmp["last_admin_reminder_ok"] = bool(ok_sent)

        if ok_sent:
            telegram_send_text(
                bot_token,
                chat_id,
                "🔔 Listo. Enviamos un *recordatorio* al administrador.",
                reply_markup=contact_admin_kb(tenant_id, order_id),
                parse_mode="Markdown",
            )
        else:
            telegram_send_text(
                bot_token,
                chat_id,
                "😕 Intenté enviar el recordatorio, pero falló.",
                reply_markup=paid_actions_kb(tenant_id, order_id),
            )
        return {"ok": True}

    if data.startswith("contact|"):
        parts = data.split("|")
        if len(parts) != 3:
            return {"ok": True}

        _, cb_tenant_id, order_id = parts
        if cb_tenant_id.strip() != tenant_id:
            raise HTTPException(status_code=400, detail="Tenant mismatch in contact callback")

        paid_at = int(tmp.get("paid_pressed_at_ts") or 0)
        now_ts = int(time.time())

        if not paid_at:
            telegram_send_text(bot_token, chat_id, "Primero presiona “✅ Ya pagué”.", reply_markup=paid_actions_kb(tenant_id, order_id))
            return {"ok": True}

        if (now_ts - paid_at) < CONTACT_AFTER_SECONDS:
            left = CONTACT_AFTER_SECONDS - (now_ts - paid_at)
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


# =========================================================
# CALLBACKS ADMIN
# =========================================================

def _handle_admin_callback(
    bot_token: str,
    tenant_id: str,
    tenant: Dict[str, Any],
    chat_id: int,
    data: str,
) -> Dict[str, Any]:
    _assert_admin_authorized(tenant, chat_id, tenant_id)

    gc = get_gspread_client()
    spreadsheet = _tenant_sheet(gc, tenant)
    tenant_tz = (tenant.get("timezone") or "America/La_Paz").strip()

    if data.startswith("paid|"):
        parts = data.split("|")
        if len(parts) != 3:
            return {"ok": True}

        cb_tenant_id = parts[1].strip()
        order_id = parts[2].strip()

        if cb_tenant_id != tenant_id:
            raise HTTPException(status_code=400, detail="Tenant mismatch in callback")

        res = update_order_status(spreadsheet, order_id, "PAID")
        if not res.get("found"):
            telegram_send_text(bot_token, chat_id, f"⚠️ Pedido {order_id} no encontrado en Sheets.", reply_markup=admin_fixed_kb())
            return {"ok": True}

        telegram_send_text(bot_token, chat_id, f"✅ Pedido {order_id} marcado como PAID", reply_markup=admin_fixed_kb())

        order = get_order_by_id(spreadsheet, order_id)
        if order:
            client_token = get_client_bot_token(tenant)
            client_chat = (order.get("customer_contact") or "").strip()
            if client_token and client_chat:
                try:
                    telegram_send_text(client_token, int(client_chat), f"✅ Pago validado. Tu pedido {order_id} fue confirmado. ¡Gracias!")
                except Exception as e:
                    log_event("notify_client_paid_failed", tenant_id=tenant_id, order_id=order_id, error=str(e))

        return {"ok": True}

    if data.startswith("admin_stats_period|"):
        parts = data.split("|")
        if len(parts) != 3:
            return {"ok": True}
        _, cb_tenant_id, period_key = parts
        cb_tenant_id = cb_tenant_id.strip()
        period_key = period_key.strip()

        if cb_tenant_id != tenant_id:
            raise HTTPException(status_code=400, detail="Tenant mismatch in stats callback")

        period = resolve_period(tenant_tz, period_key)
        txt = build_stats_report_text(spreadsheet, tenant_id=tenant_id, tenant_tz=tenant_tz, period=period)
        telegram_send_text(bot_token, chat_id, txt, reply_markup=admin_fixed_kb())
        return {"ok": True}

    if data.startswith("admhrs|"):
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
            return {"ok": _send_admin_hours_menu(bot_token, chat_id, tenant_id, spreadsheet, tenant_tz)}

        if action == "habitual":
            tmp.pop("admin_days_selected", None)
            tmp.pop("admin_norm_open", None)
            tmp.pop("admin_norm_close", None)
            tmp.pop("admin_early_close", None)
            _admin_restore_habitual(orders_sh=spreadsheet, updated_by=updated_by)
            telegram_send_text(bot_token, chat_id, "✅ Se restauró la configuración habitual de hoy.")
            return {"ok": _send_admin_hours_menu(bot_token, chat_id, tenant_id, spreadsheet, tenant_tz)}

        if action == "days":
            bs = _get_business_status_safe(orders_sh=spreadsheet, tenant_tz=tenant_tz)
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
            bs = _get_business_status_safe(orders_sh=spreadsheet, tenant_tz=tenant_tz)
            return {"ok": _send_admin_days_menu(bot_token, chat_id, tenant_id, sess, bs)}

        if action == "dayssave":
            selected = [d for d in DAY_ORDER if d in set(tmp.get("admin_days_selected") or [])]
            _admin_set_weekly_open_days(
                orders_sh=spreadsheet,
                days=selected,
                updated_by=updated_by,
            )
            tmp.pop("admin_days_selected", None)
            telegram_send_text(bot_token, chat_id, "✅ Días normales actualizados.")
            return {"ok": _send_admin_hours_menu(bot_token, chat_id, tenant_id, spreadsheet, tenant_tz)}

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
                orders_sh=spreadsheet,
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
            return {"ok": _send_admin_hours_menu(bot_token, chat_id, tenant_id, spreadsheet, tenant_tz)}

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

            _admin_set_today_closed(orders_sh=spreadsheet, enabled=False, updated_by=updated_by)
            _admin_set_today_open_force(orders_sh=spreadsheet, enabled=False, updated_by=updated_by)
            _admin_set_today_close_override(orders_sh=spreadsheet, close_time=close_time, updated_by=updated_by)
            _admin_set_today_last_order_override(orders_sh=spreadsheet, last_order_time=last_time, updated_by=updated_by)

            tmp.pop("admin_early_close", None)
            telegram_send_text(
                bot_token,
                chat_id,
                f"✅ Cierre temprano configurado para hoy.\nCierre: {close_time}\nÚltima hora de pedido: {last_time}",
            )
            return {"ok": _send_admin_hours_menu(bot_token, chat_id, tenant_id, spreadsheet, tenant_tz)}

        if action == "late":
            return {"ok": _send_admin_late_open_menu(bot_token, chat_id, tenant_id)}

        if action == "lateopen" and len(parts) == 4:
            open_time = _compact_to_hhmm(parts[3].strip())
            _admin_set_today_closed(orders_sh=spreadsheet, enabled=False, updated_by=updated_by)
            _admin_set_today_open_force(orders_sh=spreadsheet, enabled=False, updated_by=updated_by)
            _admin_set_today_open_override(orders_sh=spreadsheet, open_time=open_time, updated_by=updated_by)
            telegram_send_text(bot_token, chat_id, f"✅ Apertura tardía configurada para hoy.\nNueva apertura: {open_time}")
            return {"ok": _send_admin_hours_menu(bot_token, chat_id, tenant_id, spreadsheet, tenant_tz)}

        if action == "closed":
            _admin_set_today_closed(orders_sh=spreadsheet, enabled=True, updated_by=updated_by)
            _admin_set_today_open_force(orders_sh=spreadsheet, enabled=False, updated_by=updated_by)
            _admin_set_today_open_override(orders_sh=spreadsheet, open_time="", updated_by=updated_by)
            _admin_set_today_close_override(orders_sh=spreadsheet, close_time="", updated_by=updated_by)
            _admin_set_today_last_order_override(orders_sh=spreadsheet, last_order_time="", updated_by=updated_by)
            telegram_send_text(bot_token, chat_id, "✅ Hoy quedó marcado como NO abrir.")
            return {"ok": _send_admin_hours_menu(bot_token, chat_id, tenant_id, spreadsheet, tenant_tz)}

        if action == "openforce":
            _admin_set_today_open_force(orders_sh=spreadsheet, enabled=True, updated_by=updated_by)
            _admin_set_today_closed(orders_sh=spreadsheet, enabled=False, updated_by=updated_by)
            _admin_set_today_open_override(orders_sh=spreadsheet, open_time="", updated_by=updated_by)
            _admin_set_today_close_override(orders_sh=spreadsheet, close_time="", updated_by=updated_by)
            _admin_set_today_last_order_override(orders_sh=spreadsheet, last_order_time="", updated_by=updated_by)
            telegram_send_text(bot_token, chat_id, "✅ Hoy quedó marcado como abrir excepcionalmente.")
            return {"ok": _send_admin_hours_menu(bot_token, chat_id, tenant_id, spreadsheet, tenant_tz)}

        return {"ok": True}

    if data.startswith("admmenu|"):
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
            return {"ok": _send_admin_menu_home(bot_token, chat_id, tenant_id, spreadsheet, sess)}

        if action == "refresh":
            invalidate_menu_cache(tenant_id)
            telegram_send_text(bot_token, chat_id, "✅ Menú refrescado.")
            return {"ok": _send_admin_menu_home(bot_token, chat_id, tenant_id, spreadsheet, sess)}

        if action == "catrefresh":
            invalidate_menu_cache(tenant_id)
            current_category = str(tmp.get("admin_menu_current_category") or "").strip()
            if not current_category:
                return {"ok": _send_admin_menu_home(bot_token, chat_id, tenant_id, spreadsheet, sess)}
            telegram_send_text(bot_token, chat_id, "✅ Categoría refrescada.")
            return {"ok": _send_admin_menu_category(bot_token, chat_id, tenant_id, spreadsheet, sess, current_category)}

        if action == "cat" and len(parts) == 4:
            try:
                idx = int(parts[3].strip())
            except Exception:
                idx = -1

            menu_idx = load_menu_admin_index(spreadsheet, force=False)
            cats = group_menu_admin_by_category(menu_idx)
            cat_names = sorted(cats.keys(), key=lambda x: normalize(x))

            if idx < 0 or idx >= len(cat_names):
                return {"ok": _send_admin_menu_home(bot_token, chat_id, tenant_id, spreadsheet, sess)}

            category = cat_names[idx]
            return {"ok": _send_admin_menu_category(bot_token, chat_id, tenant_id, spreadsheet, sess, category)}

        if action == "catback":
            current_category = str(tmp.get("admin_menu_current_category") or "").strip()
            if not current_category:
                return {"ok": _send_admin_menu_home(bot_token, chat_id, tenant_id, spreadsheet, sess)}
            return {"ok": _send_admin_menu_category(bot_token, chat_id, tenant_id, spreadsheet, sess, current_category)}

        if action == "prd" and len(parts) == 4:
            sku = parts[3].strip()
            return {"ok": _send_admin_menu_product_detail(bot_token, chat_id, tenant_id, spreadsheet, sess, sku)}

        if action == "toggle" and len(parts) == 4:
            sku = parts[3].strip()
            item_before = get_menu_product_or_404(spreadsheet, sku)
            new_active = not bool(item_before.get("active", False))
            set_menu_product_active(spreadsheet, sku, new_active)
            item_after = get_menu_product_or_404(spreadsheet, sku)

            telegram_send_text(
                bot_token,
                chat_id,
                f"✅ Estado actualizado.\nProducto: {item_after.get('name','')}\nActivo: {'Sí' if item_after.get('active') else 'No'}",
            )
            return {"ok": _send_admin_menu_product_detail(bot_token, chat_id, tenant_id, spreadsheet, sess, sku)}

        if action == "price" and len(parts) == 4:
            sku = parts[3].strip()
            return {"ok": _send_admin_menu_price_editor(bot_token, chat_id, tenant_id, spreadsheet, sess, sku)}

        if action == "padj" and len(parts) == 5:
            sku = parts[3].strip()
            token = parts[4].strip().lower()

            item = get_menu_product_or_404(spreadsheet, sku)
            current_sku = str(tmp.get("admin_menu_price_sku") or "").strip()
            if current_sku != sku:
                tmp["admin_menu_price_sku"] = sku
                tmp["admin_menu_price_work"] = float(item.get("price", 0.0))

            work_price = float(tmp.get("admin_menu_price_work") or 0.0)
            work_price = _apply_price_delta(work_price, token)
            tmp["admin_menu_price_work"] = work_price

            return {"ok": _send_admin_menu_price_editor(bot_token, chat_id, tenant_id, spreadsheet, sess, sku)}

        if action == "psave" and len(parts) == 4:
            sku = parts[3].strip()
            current_sku = str(tmp.get("admin_menu_price_sku") or "").strip()

            if current_sku != sku:
                item = get_menu_product_or_404(spreadsheet, sku)
                tmp["admin_menu_price_sku"] = sku
                tmp["admin_menu_price_work"] = float(item.get("price", 0.0))

            new_price = float(tmp.get("admin_menu_price_work") or 0.0)
            result = set_menu_product_price(spreadsheet, sku, new_price)

            tmp.pop("admin_menu_price_sku", None)
            tmp.pop("admin_menu_price_work", None)
            tmp.pop("admin_menu_input_mode", None)

            telegram_send_text(
                bot_token,
                chat_id,
                f"✅ Precio actualizado.\nSKU: {sku}\nNuevo precio: Bs {_fmt_price_short(result.get('price', 0))}",
            )
            return {"ok": _send_admin_menu_product_detail(bot_token, chat_id, tenant_id, spreadsheet, sess, sku)}

        if action == "pback" and len(parts) == 4:
            sku = parts[3].strip()
            tmp.pop("admin_menu_price_sku", None)
            tmp.pop("admin_menu_price_work", None)
            tmp.pop("admin_menu_input_mode", None)
            return {"ok": _send_admin_menu_product_detail(bot_token, chat_id, tenant_id, spreadsheet, sess, sku)}

        if action == "pricewrite" and len(parts) == 4:
            sku = parts[3].strip()
            item = get_menu_product_or_404(spreadsheet, sku)
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
                    "Escribe el nuevo precio final."
                ),
            )
            return {"ok": True}

        if action == "discount" and len(parts) == 4:
            sku = parts[3].strip()
            item = get_menu_product_or_404(spreadsheet, sku)
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
                    "Escribe el porcentaje de descuento."
                ),
            )
            return {"ok": True}

        if action == "photo" and len(parts) == 4:
            sku = parts[3].strip()
            tmp["admin_menu_input_mode"] = "awaiting_photo"
            tmp["admin_menu_price_sku"] = sku

            item = get_menu_product_or_404(spreadsheet, sku)

            telegram_send_text(
                bot_token,
                chat_id,
                f"📷 Envía ahora la foto para:\n{item.get('name','')}",
            )
            return {"ok": True}

        return {"ok": True}

    return {"ok": True}


# =========================================================
# WEBHOOK PRINCIPAL
# =========================================================

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

        if mode == "admin":
            return _handle_admin_callback(bot_token, tenant_id, tenant, chat_id, data)

        if mode == "client":
            return _handle_client_callback(bot_token, tenant_id, tenant, chat_id, data)

        return {"ok": True}

    msg = update.get("message") or update.get("edited_message")
    if msg:
        if mode == "admin":
            return _handle_admin_message(bot_token, tenant_id, tenant, msg)

        if mode == "client":
            return _handle_client_message(bot_token, tenant_id, tenant, msg)

        return {"ok": True}

    return {"ok": True}
