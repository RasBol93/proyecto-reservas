# app/telegram_webhook.py
#
# Features (todo en uno, sin recortar):
# ✅ Menú: categorías -> productos -> cantidad -> carrito
# ✅ Pedido: confirmar -> pedir nombre -> (NUEVO) elegir hora pickup -> crear pedido en Sheets
# ✅ Pago: enviar QR -> cliente sube comprobante (foto/PDF) -> se guarda en Sheets
# ✅ Botón "✅ Ya pagué": recién ahí se avisa al admin
# ✅ Admin recibe notificación + comprobante REENVIADO (descargando bytes con bot cliente y re-subiendo con bot admin)
# ✅ Recordatorio con cooldown:
#    - antes de 5 min: mensaje amable "espera un momento"
#    - >=5 min: botón 🔔 Recordar al administrador (manda recordatorio al admin con título 🔔)
#    - >=10 min: botón 💬 Contactar al administrador (abre link directo a chat admin por tg://user?id=)
# ✅ Seguridad: solo admin_chat_id puede confirmar pago
# ✅ Idempotencia: PAID doble no rompe

import json
import re
import time
import urllib.request
import uuid
from datetime import datetime, timedelta, time as dtime
from typing import Any, Dict, Optional, List, Tuple

from fastapi import APIRouter, HTTPException

from app.config import TELEGRAM_API_BASE
from app.tenants import get_tenant_or_404, resolve_bot_by_secret
from app.sheets import get_gspread_client, open_spreadsheet_by_key, open_config_spreadsheet
from app.menu import load_menu_index, group_menu_by_category, calc_total_amount
from app.orders import (
    append_order_row,
    update_order_status,
    update_order_payment_proof,
    find_latest_pending_order_for_contact,
    get_order_by_id,
    gen_order_id,
)
from app.telegram_keyboard import kb
from app.utils import normalize, log_event

try:
    from zoneinfo import ZoneInfo
except Exception:
    ZoneInfo = None  # py<3.9 fallback

router = APIRouter()

# =========================================================
# Estado en memoria (DEMO)
# =========================================================
# key: (tenant_id, chat_id) -> {"cart":[{"sku":..., "qty":...}], "stage": "...", "tmp": {...}}
SESSIONS: Dict[Tuple[str, int], Dict[str, Any]] = {}

# cooldowns (segundos)
REMIND_AFTER_SECONDS = 5 * 60
CONTACT_AFTER_SECONDS = 10 * 60

# =========================================================
# BookingRules cache
# =========================================================
_BOOKING_RULES_CACHE: Dict[str, Tuple[float, Dict[str, str]]] = {}
BOOKING_RULES_TTL_SECONDS = 60


def get_sess(tenant_id: str, chat_id: int) -> Dict[str, Any]:
    key = (tenant_id, chat_id)
    if key not in SESSIONS:
        SESSIONS[key] = {"cart": [], "stage": "idle", "tmp": {}}
    return SESSIONS[key]


def clear_sess(tenant_id: str, chat_id: int) -> None:
    key = (tenant_id, chat_id)
    SESSIONS[key] = {"cart": [], "stage": "idle", "tmp": {}}


# -------------------------
# Telegram API helpers (JSON)
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
) -> None:
    payload: Dict[str, Any] = {"chat_id": chat_id, "text": text}
    if reply_markup:
        payload["reply_markup"] = reply_markup
    if parse_mode:
        payload["parse_mode"] = parse_mode

    try:
        res = telegram_api_call(bot_token, "sendMessage", payload)
        if not res.get("ok", True):
            log_event("telegram_send_failed", chat_id=chat_id, error=res.get("description") or res)
    except Exception as e:
        log_event("telegram_send_exception", chat_id=chat_id, error=str(e))


def telegram_send_photo(
    bot_token: str,
    chat_id: int,
    photo: str,
    caption: str = "",
) -> None:
    payload: Dict[str, Any] = {"chat_id": chat_id, "photo": photo}
    if caption:
        payload["caption"] = caption
    try:
        res = telegram_api_call(bot_token, "sendPhoto", payload)
        if not res.get("ok", True):
            log_event("telegram_send_photo_failed", chat_id=chat_id, error=res.get("description") or res)
    except Exception as e:
        log_event("telegram_send_photo_exception", chat_id=chat_id, error=str(e))


def telegram_send_document(
    bot_token: str,
    chat_id: int,
    document: str,
    caption: str = "",
) -> None:
    payload: Dict[str, Any] = {"chat_id": chat_id, "document": document}
    if caption:
        payload["caption"] = caption
    try:
        res = telegram_api_call(bot_token, "sendDocument", payload)
        if not res.get("ok", True):
            log_event("telegram_send_document_failed", chat_id=chat_id, error=res.get("description") or res)
    except Exception as e:
        log_event("telegram_send_document_exception", chat_id=chat_id, error=str(e))


def telegram_answer_callback(bot_token: str, callback_query_id: str, text: str = "OK") -> None:
    try:
        res = telegram_api_call(bot_token, "answerCallbackQuery", {"callback_query_id": callback_query_id, "text": text})
        if not res.get("ok", True):
            log_event("telegram_ack_failed", error=res.get("description") or res)
    except Exception as e:
        log_event("telegram_ack_exception", error=str(e))


# -------------------------
# Telegram API helpers (multipart bytes)
#   -> necesario para reenviar comprobante entre bots
# -------------------------

def _multipart_post(url: str, fields: Dict[str, str], file_field: str, filename: str, content_type: str, file_bytes: bytes) -> Dict[str, Any]:
    boundary = "----WebKitFormBoundary" + uuid.uuid4().hex
    body = bytearray()

    def add_field(name: str, value: str):
        body.extend(f"--{boundary}\r\n".encode("utf-8"))
        body.extend(f'Content-Disposition: form-data; name="{name}"\r\n\r\n'.encode("utf-8"))
        body.extend(str(value).encode("utf-8"))
        body.extend(b"\r\n")

    for k, v in fields.items():
        add_field(k, v)

    body.extend(f"--{boundary}\r\n".encode("utf-8"))
    body.extend(f'Content-Disposition: form-data; name="{file_field}"; filename="{filename}"\r\n'.encode("utf-8"))
    body.extend(f"Content-Type: {content_type}\r\n\r\n".encode("utf-8"))
    body.extend(file_bytes)
    body.extend(b"\r\n")
    body.extend(f"--{boundary}--\r\n".encode("utf-8"))

    req = urllib.request.Request(
        url,
        data=bytes(body),
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
        method="POST",
    )

    with urllib.request.urlopen(req, timeout=30) as resp:
        raw = resp.read().decode("utf-8")
        try:
            return json.loads(raw)
        except Exception:
            return {"ok": False, "raw": raw}


def telegram_send_photo_bytes(bot_token: str, chat_id: int, photo_bytes: bytes, caption: str = "Comprobante (foto)") -> None:
    url = f"{TELEGRAM_API_BASE}/bot{bot_token}/sendPhoto"
    res = _multipart_post(
        url=url,
        fields={"chat_id": str(chat_id), "caption": caption},
        file_field="photo",
        filename="proof.jpg",
        content_type="image/jpeg",
        file_bytes=photo_bytes,
    )
    if not res.get("ok", False):
        log_event("telegram_send_photo_bytes_failed", chat_id=chat_id, error=res.get("description") or res)


def telegram_send_document_bytes(bot_token: str, chat_id: int, doc_bytes: bytes, filename: str = "proof.pdf", caption: str = "Comprobante (archivo)") -> None:
    url = f"{TELEGRAM_API_BASE}/bot{bot_token}/sendDocument"
    res = _multipart_post(
        url=url,
        fields={"chat_id": str(chat_id), "caption": caption},
        file_field="document",
        filename=filename,
        content_type="application/octet-stream",
        file_bytes=doc_bytes,
    )
    if not res.get("ok", False):
        log_event("telegram_send_document_bytes_failed", chat_id=chat_id, error=res.get("description") or res)


def telegram_get_file_path(bot_token: str, file_id: str) -> Optional[str]:
    try:
        res = telegram_api_call(bot_token, "getFile", {"file_id": file_id})
        if not res.get("ok"):
            return None
        return res["result"]["file_path"]
    except Exception as e:
        log_event("telegram_get_file_path_failed", error=str(e))
        return None


def telegram_download_file_bytes(bot_token: str, file_path: str) -> Optional[bytes]:
    try:
        url = f"{TELEGRAM_API_BASE}/file/bot{bot_token}/{file_path}"
        with urllib.request.urlopen(url, timeout=30) as resp:
            return resp.read()
    except Exception as e:
        log_event("telegram_download_file_failed", error=str(e))
        return None


# -------------------------
# helpers tenant fields
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
# Formatting helpers
# -------------------------

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


def build_order_recap_text(
    order_id: str,
    customer_name: str,
    customer_contact: str,
    requested_time: str,
    cart: List[Dict[str, Any]],
    menu_idx: Dict[str, Any],
    total: float,
) -> str:
    lines_txt, _, total_qty = fmt_cart_lines(cart, menu_idx)
    return (
        f"🧾 *Resumen de tu pedido*\n"
        f"ID: `{order_id}`\n"
        f"Cliente: *{customer_name}*\n"
        f"Contacto: `{customer_contact}`\n"
        f"Hora recogida: *{requested_time}*\n"
        f"Cantidad total: *{total_qty}*\n"
        f"Total: *{total:.2f}* BOB\n\n"
        f"*Detalle:*\n{lines_txt}\n"
    )


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


# -------------------------
# Seguridad / parsing
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


# -------------------------
# Inline keyboard URL (para "contactar admin")
# -------------------------

def kb_url(buttons: List[List[Tuple[str, str]]]) -> Dict[str, Any]:
    """
    buttons: [[(text, url)], ...]
    """
    inline = []
    for row in buttons:
        inline_row = []
        for text, url in row:
            inline_row.append({"text": text, "url": url})
        inline.append(inline_row)
    return {"inline_keyboard": inline}


# -------------------------
# BookingRules / Pickup time helpers
# -------------------------

def _rules_cache_key(tenant_id: str) -> str:
    return normalize(tenant_id).replace(" ", "")


def _load_booking_rules_for_tenant(gc, tenant_id: str) -> Dict[str, str]:
    ck = _rules_cache_key(tenant_id)
    now_ts = time.time()

    if ck in _BOOKING_RULES_CACHE:
        ts, data = _BOOKING_RULES_CACHE[ck]
        if (now_ts - ts) <= BOOKING_RULES_TTL_SECONDS:
            return data

    try:
        sh = open_config_spreadsheet(gc)
        ws = sh.worksheet("BookingRules")
        values = ws.get_all_values()
    except Exception as e:
        log_event("bookingrules_read_failed", tenant_id=tenant_id, error=str(e))
        data = {}
        _BOOKING_RULES_CACHE[ck] = (now_ts, data)
        return data

    if not values:
        data = {}
        _BOOKING_RULES_CACHE[ck] = (now_ts, data)
        return data

    header = [normalize(x) for x in values[0]]

    def col(name: str) -> Optional[int]:
        n = normalize(name)
        return header.index(n) if n in header else None

    c_tenant = col("tenant_id")
    c_key = col("rule_key")
    c_val = col("value")
    if c_tenant is None or c_key is None or c_val is None:
        data = {}
        _BOOKING_RULES_CACHE[ck] = (now_ts, data)
        return data

    tid_norm = _rules_cache_key(tenant_id)
    out: Dict[str, str] = {}

    for row in values[1:]:
        tid = (row[c_tenant] if c_tenant < len(row) else "").strip()
        if _rules_cache_key(tid) != tid_norm:
            continue
        rk = (row[c_key] if c_key < len(row) else "").strip()
        rv = (row[c_val] if c_val < len(row) else "").strip()
        if rk:
            out[normalize(rk)] = rv

    _BOOKING_RULES_CACHE[ck] = (now_ts, out)
    return out


def _parse_hhmm(s: str) -> Optional[dtime]:
    s = (s or "").strip()
    m = re.match(r"^(\d{1,2}):(\d{2})$", s)
    if not m:
        return None
    hh = int(m.group(1))
    mm = int(m.group(2))
    if hh < 0 or hh > 23 or mm < 0 or mm > 59:
        return None
    return dtime(hour=hh, minute=mm)


def _ceil_to_next_half_hour(dt: datetime) -> datetime:
    dt = dt.replace(second=0, microsecond=0)
    m = dt.minute
    if m in (0, 30):
        return dt
    if m < 30:
        return dt.replace(minute=30)
    return (dt.replace(minute=0) + timedelta(hours=1))


def _build_pickup_time_options(now_local: datetime, open_t: dtime, close_t: dtime, prep_minutes: int = 30, max_buttons: int = 10) -> List[datetime]:
    today = now_local.date()
    open_dt = datetime.combine(today, open_t, tzinfo=now_local.tzinfo)
    close_dt = datetime.combine(today, close_t, tzinfo=now_local.tzinfo)

    # base: ahora + prep
    base = now_local + timedelta(minutes=prep_minutes)

    # si aún no abre, base = open + prep
    if now_local < open_dt:
        base = open_dt + timedelta(minutes=prep_minutes)

    # si ya pasó el cierre, no hay slots hoy
    if base > close_dt:
        return []

    first = base
    opts: List[datetime] = []

    # opción 1: exactamente base (lo más rápido posible)
    opts.append(first.replace(second=0, microsecond=0))

    # siguientes: cada 30 min (ceiling)
    cur = _ceil_to_next_half_hour(first)
    if cur < first:
        cur = first

    # para evitar duplicar si base ya cae justo en 00/30
    if cur == opts[0]:
        cur = cur + timedelta(minutes=30)

    while len(opts) < max_buttons and cur <= close_dt:
        opts.append(cur)
        cur += timedelta(minutes=30)

    return opts


def _pickup_time_kb(options: List[datetime]) -> Dict[str, Any]:
    rows = []
    for i, dt in enumerate(options):
        label = dt.strftime("%H:%M")
        if i == 0:
            text = f"⚡ Lo antes posible ({label})"
        else:
            text = label
        # callback: ptime|<unix_ts>
        rows.append([(text, f"ptime|{int(dt.timestamp())}")])
    rows.append([("🏠 Inicio", "home")])
    return kb(rows)


# -------------------------
# Recordatorio keyboards
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


def i_paid_kb(tenant_id: str, order_id: str, show_remind: bool, show_contact: bool, admin_chat_id: Optional[int]) -> Dict[str, Any]:
    rows: List[List[Tuple[str, str]]] = [
        [("✅ Ya pagué", f"i_paid|{tenant_id}|{order_id}")],
    ]
    if show_remind:
        rows.append([("🔔 Recordar al administrador", f"remind|{tenant_id}|{order_id}")])
    if show_contact and admin_chat_id:
        # URL button via kb_url (no callback)
        return {
            "inline_keyboard": [
                [{"text": "✅ Ya pagué", "callback_data": f"i_paid|{tenant_id}|{order_id}"}],
                [{"text": "🔔 Recordar al administrador", "callback_data": f"remind|{tenant_id}|{order_id}"}] if show_remind else [],
                [{"text": "💬 Contactar al administrador", "url": f"tg://user?id={admin_chat_id}"}],
                [{"text": "🏠 Inicio", "callback_data": "home"}],
            ]
        }
    rows.append([("🏠 Inicio", "home")])
    return kb(rows)


# -------------------------
# ADMIN notify (incluye reenviar comprobante)
# -------------------------

def _forward_payment_proof_to_admin(
    tenant: Dict[str, Any],
    admin_token: str,
    admin_chat_id: int,
    proof_type: str,
    proof_file_id: str,
    proof_caption: str,
) -> None:
    """
    Reenvío real entre bots:
    - getFile + download con BOT CLIENTE
    - sendPhoto/sendDocument con BOT ADMIN usando bytes (multipart)
    """
    client_token = get_client_bot_token(tenant)
    if not client_token:
        log_event("proof_forward_failed", reason="missing_client_bot_token")
        return

    file_path = telegram_get_file_path(client_token, proof_file_id)
    if not file_path:
        log_event("proof_forward_failed", reason="get_file_path_failed", proof_type=proof_type)
        return

    data = telegram_download_file_bytes(client_token, file_path)
    if not data:
        log_event("proof_forward_failed", reason="download_failed", proof_type=proof_type)
        return

    caption = proof_caption or ("Comprobante (foto)" if proof_type == "photo" else "Comprobante (archivo)")

    if proof_type == "photo":
        telegram_send_photo_bytes(admin_token, admin_chat_id, data, caption=caption)
    elif proof_type == "document":
        filename = "proof.bin"
        # intenta deducir extensión del file_path
        try:
            filename = file_path.split("/")[-1] or filename
        except Exception:
            pass
        telegram_send_document_bytes(admin_token, admin_chat_id, data, filename=filename, caption=caption)
    else:
        log_event("proof_forward_unknown_type", proof_type=proof_type)


def notify_admin_payment_reported(
    tenant: Dict[str, Any],
    tenant_id: str,
    orders_sh,
    order_id: str,
    is_reminder: bool = False,
) -> None:
    admin_token = get_admin_bot_token(tenant)
    admin_chat_id = get_admin_chat_id(tenant)

    if not admin_token:
        log_event("admin_notify_failed", tenant_id=tenant_id, reason="missing_admin_bot_token")
        return
    if not admin_chat_id:
        log_event("admin_notify_failed", tenant_id=tenant_id, reason="missing_admin_chat_id")
        return

    order = get_order_by_id(orders_sh, order_id)
    if not order:
        telegram_send_text(admin_token, admin_chat_id, f"⚠️ Pedido {order_id} no encontrado en Sheets.")
        return

    # menu puede fallar
    try:
        menu_idx = load_menu_index(orders_sh)
    except Exception as e:
        log_event("admin_menu_load_error", tenant_id=tenant_id, error=str(e))
        menu_idx = {}

    cart = parse_items_field(order.get("items"))
    try:
        total = float(order.get("total_amount") or 0)
    except Exception:
        total = 0.0

    lines_txt, _, total_qty = fmt_cart_lines(cart, menu_idx)

    proof_file_id = (order.get("payment_proof_file_id") or "").strip()
    proof_type = (order.get("payment_proof_type") or "").strip()
    proof_caption = (order.get("payment_proof_caption") or "").strip()

    confirm_btn = kb([[("✅ Confirmar pago", f"paid|{tenant_id}|{order_id}")]])

    # título (HTML para negrita segura)
    if is_reminder:
        title = "<b>🔔 RECORDATORIO — PAGO REPORTADO</b>\n\n"
    else:
        title = "💳 PAGO REPORTADO\n\n"

    txt = (
        title
        + f"Tenant: {tenant_id}\n"
        + f"ID: {order_id}\n"
        + f"Cliente: {order.get('customer_name','')}\n"
        + f"Contacto(chat_id): {order.get('customer_contact','')}\n"
        + f"Hora recogida: {order.get('requested_time','pendiente')}\n"
        + f"Cantidad total: {total_qty}\n"
        + f"Total: {total:.2f} BOB\n\n"
        + f"Detalle:\n{lines_txt}\n\n"
        + "Presiona ✅ Confirmar pago cuando verifiques."
    )

    # usar HTML solo si es reminder (para negrita). si no, sin parse.
    telegram_send_text(admin_token, admin_chat_id, txt, reply_markup=confirm_btn, parse_mode=("HTML" if is_reminder else None))

    # reenviar comprobante (bytes)
    if proof_file_id and proof_type:
        _forward_payment_proof_to_admin(
            tenant=tenant,
            admin_token=admin_token,
            admin_chat_id=admin_chat_id,
            proof_type=proof_type,
            proof_file_id=proof_file_id,
            proof_caption=proof_caption,
        )
    else:
        log_event("admin_missing_proof", tenant_id=tenant_id, order_id=order_id)


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
        # ADMIN: confirmar pago
        # -------------------------
        if mode == "admin" and data.startswith("paid|"):
            parts = data.split("|")
            if len(parts) != 3:
                log_event("admin_paid_bad_callback_format", tenant_id=tenant_id, data=data)
                return {"ok": True}

            cb_tenant_id = parts[1].strip()
            order_id = parts[2].strip()

            if cb_tenant_id != tenant_id:
                raise HTTPException(status_code=400, detail="Tenant mismatch in callback")

            _assert_admin_authorized(tenant, chat_id, tenant_id)

            res = update_order_status(orders_sh, order_id, "PAID")
            if not res.get("found"):
                telegram_send_text(bot_token, chat_id, f"⚠️ Pedido {order_id} no encontrado en Sheets.")
                return {"ok": True}

            if res.get("already"):
                telegram_send_text(bot_token, chat_id, f"ℹ️ Pedido {order_id} ya estaba en PAID.")
            else:
                telegram_send_text(bot_token, chat_id, f"✅ Pedido {order_id} marcado como PAID")

            log_event("admin_mark_paid", tenant_id=tenant_id, order_id=order_id, admin_chat_id=chat_id, already=bool(res.get("already")))

            # avisar al cliente con BOT CLIENTE
            order = get_order_by_id(orders_sh, order_id)
            if order:
                client_token = get_client_bot_token(tenant)
                client_chat = (order.get("customer_contact") or "").strip()
                if client_token and client_chat:
                    try:
                        telegram_send_text(
                            client_token,
                            int(client_chat),
                            f"✅ Pago validado. Tu pedido {order_id} fue confirmado. ¡Gracias!",
                        )
                    except Exception as e:
                        log_event("notify_client_paid_failed", tenant_id=tenant_id, order_id=order_id, error=str(e))

            return {"ok": True}

        # -------------------------
        # CLIENT callbacks
        # -------------------------
        if mode == "client":
            sess = get_sess(tenant_id, chat_id)

            if data == "home":
                telegram_send_text(bot_token, chat_id, "Elige una opción:", client_home_kb())
                return {"ok": True}

            if data == "menu":
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
                cart = sess.get("cart") or []
                if not cart:
                    telegram_send_text(bot_token, chat_id, "Tu carrito está vacío.", reply_markup=client_home_kb())
                    return {"ok": True}

                sess["stage"] = "awaiting_name"
                telegram_send_text(bot_token, chat_id, "Perfecto. ¿Cuál es tu *nombre* para el pedido?", parse_mode="Markdown")
                return {"ok": True}

            # ---------
            # NUEVO: elección de hora pickup desde botones
            # ptime|<unix_ts>
            # ---------
            if data.startswith("ptime|"):
                parts = data.split("|", 1)
                if len(parts) != 2:
                    return {"ok": True}
                ts_s = parts[1].strip()
                try:
                    ts = int(ts_s)
                except Exception:
                    telegram_send_text(bot_token, chat_id, "Hora inválida. Vuelve a /start.", reply_markup=client_home_kb())
                    return {"ok": True}

                # necesitamos datos guardados en tmp
                tmp = sess.get("tmp") or {}
                customer_name = (tmp.get("customer_name") or "").strip()
                cart = tmp.get("cart_snapshot") or []
                total = float(tmp.get("total_amount") or 0.0)

                if not customer_name or not cart:
                    telegram_send_text(bot_token, chat_id, "No pude recuperar tu pedido. Vuelve a /start.", reply_markup=client_home_kb())
                    sess["stage"] = "idle"
                    sess["tmp"] = {}
                    return {"ok": True}

                # requested_time string
                tzname = (tenant.get("timezone") or "America/La_Paz").strip()
                tz = ZoneInfo(tzname) if ZoneInfo else None
                dt = datetime.fromtimestamp(ts, tz=tz)
                requested_time = dt.strftime("%H:%M")

                # crear orden
                order_id = gen_order_id()

                append_order_row(
                    orders_sh=orders_sh,
                    tenant_id=tenant_id,
                    order_id=order_id,
                    customer_name=customer_name,
                    customer_contact=str(chat_id),
                    items=cart,
                    delivery_type="pickup",
                    requested_time=requested_time,
                    status="PENDING_PAYMENT",
                    source="telegram",
                    total_amount=total,
                )

                log_event("order_created", tenant_id=tenant_id, order_id=order_id, chat_id=chat_id, total=total, requested_time=requested_time)

                sess["stage"] = "awaiting_proof"
                sess["tmp"] = sess.get("tmp") or {}
                sess["tmp"]["pending_order_id"] = order_id
                sess["tmp"]["paid_reported_at"] = None  # se setea al presionar i_paid
                sess["tmp"]["last_admin_notif_at"] = None

                menu_idx = load_menu_index(orders_sh)
                recap = build_order_recap_text(
                    order_id=order_id,
                    customer_name=customer_name,
                    customer_contact=str(chat_id),
                    requested_time=requested_time,
                    cart=cart,
                    menu_idx=menu_idx,
                    total=total,
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
                    telegram_send_text(bot_token, chat_id, "⚠️ No tengo QR configurado para este tenant.")
                    log_event("missing_qr_config", tenant_id=tenant_id)

                telegram_send_text(
                    bot_token,
                    chat_id,
                    "📎 Cuando pagues, envía aquí tu *comprobante* (foto o PDF).\n"
                    "Después de enviarlo, podrás presionar “✅ Ya pagué”.",
                    parse_mode="Markdown",
                )
                return {"ok": True}

            # Cliente presiona "Ya pagué" -> avisar admin
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
                    telegram_send_text(bot_token, chat_id, "Aún no recibí tu comprobante.\nPor favor envía una foto o PDF del pago primero.")
                    return {"ok": True}

                # notificar admin
                notify_admin_payment_reported(tenant, tenant_id, orders_sh, order_id, is_reminder=False)

                now_ts = int(time.time())
                sess["tmp"] = sess.get("tmp") or {}
                sess["tmp"]["paid_reported_at"] = now_ts
                sess["tmp"]["last_admin_notif_at"] = now_ts
                sess["tmp"]["last_order_id"] = order_id

                telegram_send_text(
                    bot_token,
                    chat_id,
                    "✅ Recibido. Espera unos minutos mientras verificamos tu pago.",
                    reply_markup=client_home_kb(),
                )
                return {"ok": True}

            # Recordatorio (con cooldown)
            if data.startswith("remind|"):
                parts = data.split("|")
                if len(parts) != 3:
                    return {"ok": True}

                _, cb_tenant_id, order_id = parts
                cb_tenant_id = cb_tenant_id.strip()
                order_id = order_id.strip()

                if cb_tenant_id != tenant_id:
                    raise HTTPException(status_code=400, detail="Tenant mismatch in remind callback")

                tmp = sess.get("tmp") or {}
                paid_reported_at = tmp.get("paid_reported_at")
                if not paid_reported_at:
                    telegram_send_text(bot_token, chat_id, "Primero presiona “✅ Ya pagué”.")
                    return {"ok": True}

                elapsed = int(time.time()) - int(paid_reported_at)

                if elapsed < REMIND_AFTER_SECONDS:
                    remaining = REMIND_AFTER_SECONDS - elapsed
                    mins = max(1, int((remaining + 59) / 60))
                    telegram_send_text(
                        bot_token,
                        chat_id,
                        f"🙏 Gracias. Ya avisamos al administrador.\nPor favor espera un momento antes de enviar un recordatorio (aprox. {mins} min).",
                    )
                    return {"ok": True}

                # OK reminder
                notify_admin_payment_reported(tenant, tenant_id, orders_sh, order_id, is_reminder=True)
                tmp["last_admin_notif_at"] = int(time.time())
                sess["tmp"] = tmp

                telegram_send_text(
                    bot_token,
                    chat_id,
                    "🔔 Listo. Envié un recordatorio al administrador.",
                )
                return {"ok": True}

            if data.startswith("cat|"):
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
                for it in items[:20]:
                    rows.append([(f"{it['name']} ({it['price']:.0f})", f"prd|{it['sku']}")])
                rows.append([("🛒 Carrito", "cart")])
                rows.append([("⬅️ Categorías", "menu")])

                telegram_send_text(bot_token, chat_id, f"🍽 {real_cat} — elige un producto:", kb(rows))
                return {"ok": True}

            if data.startswith("prd|"):
                sku = data.split("|", 1)[1].strip()

                rows = [
                    [("1", f"qty|{sku}|1"), ("2", f"qty|{sku}|2"), ("3", f"qty|{sku}|3"), ("4", f"qty|{sku}|4")],
                    [("🛒 Carrito", "cart")],
                    [("⬅️ Volver", "menu")],
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

        return {"ok": True}

    # =========================================================
    # 2) MENSAJE NORMAL (texto + media)
    # =========================================================
    msg = update.get("message") or update.get("edited_message")
    if msg:
        chat_id = _safe_int((msg.get("chat") or {}).get("id"))
        if chat_id is None:
            log_event("message_missing_chat_id", tenant_id=tenant_id)
            return {"ok": True}

        text = (msg.get("text") or "").strip()

        if normalize(text) in ("/id", "id"):
            telegram_send_text(bot_token, chat_id, f"chat_id = {chat_id}")
            return {"ok": True}

        # CLIENT
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

            # Proof upload: guardar en Sheets y mostrar botones (Ya pagué + remind gated + contactar gated)
            if proof_file_id and proof_type:
                order_id = (sess.get("tmp") or {}).get("pending_order_id")

                if not order_id:
                    order_id = find_latest_pending_order_for_contact(
                        orders_sh=orders_sh,
                        customer_contact=str(chat_id),
                        status="PENDING_PAYMENT",
                    )

                if not order_id:
                    telegram_send_text(
                        bot_token,
                        chat_id,
                        "No encontré un pedido pendiente. Crea un pedido nuevo con /start.",
                        reply_markup=client_home_kb(),
                    )
                    return {"ok": True}

                update_order_payment_proof(
                    orders_sh=orders_sh,
                    order_id=order_id,
                    proof_file_id=proof_file_id,
                    proof_type=proof_type,
                    proof_caption=proof_caption,
                )

                log_event("client_payment_proof_received", tenant_id=tenant_id, order_id=order_id, proof_type=proof_type, chat_id=chat_id)

                # después de subir comprobante: mostrar Ya pagué (y más adelante recordar/contactar)
                admin_chat_id = get_admin_chat_id(tenant)
                telegram_send_text(
                    bot_token,
                    chat_id,
                    "✅ Comprobante recibido.\nAhora presiona “✅ Ya pagué” para avisar al administrador.",
                    reply_markup=i_paid_kb(
                        tenant_id=tenant_id,
                        order_id=order_id,
                        show_remind=True,      # el botón aparece, pero el callback aplica cooldown
                        show_contact=True,     # aparece como URL si ya pasó el tiempo; pero lo gateamos igual con lógica del client (más abajo)
                        admin_chat_id=admin_chat_id
                    ),
                )
                # guardamos referencia
                sess["tmp"] = sess.get("tmp") or {}
                sess["tmp"]["pending_order_id"] = order_id
                sess["tmp"]["last_order_id"] = order_id
                return {"ok": True}

            # Start
            if normalize(text) in ("start", "/start", "hola"):
                clear_sess(tenant_id, chat_id)
                telegram_send_text(bot_token, chat_id, "Bienvenido 👋\nElige una opción:", client_home_kb())
                return {"ok": True}

            # Captura nombre
            if sess.get("stage") == "awaiting_name":
                customer_name = text.strip()
                if not customer_name:
                    telegram_send_text(bot_token, chat_id, "Dime tu nombre, por favor.")
                    return {"ok": True}

                menu_idx = load_menu_index(orders_sh)
                cart = sess.get("cart") or []
                lines_txt, total, total_qty = fmt_cart_lines(cart, menu_idx)

                if total_qty <= 0:
                    telegram_send_text(bot_token, chat_id, "Tu carrito está vacío.", reply_markup=client_home_kb())
                    sess["stage"] = "idle"
                    return {"ok": True}

                # NUEVO: pedir hora pickup con botones (antes de crear orden)
                tzname = (tenant.get("timezone") or "America/La_Paz").strip()
                tz = ZoneInfo(tzname) if ZoneInfo else None
                now_local = datetime.now(tz=tz)

                rules = _load_booking_rules_for_tenant(gc, tenant_id)
                open_s = rules.get("open_time") or rules.get("opentime") or "12:00"
                close_s = rules.get("close_time") or rules.get("closetime") or "23:00"
                open_t = _parse_hhmm(open_s) or dtime(hour=12, minute=0)
                close_t = _parse_hhmm(close_s) or dtime(hour=23, minute=0)

                options = _build_pickup_time_options(now_local, open_t, close_t, prep_minutes=30, max_buttons=10)
                if not options:
                    telegram_send_text(
                        bot_token,
                        chat_id,
                        f"Hoy ya no tenemos horarios disponibles.\nHorario: {open_s}–{close_s}.\nVuelve mañana o más temprano.",
                        reply_markup=client_home_kb(),
                    )
                    sess["stage"] = "idle"
                    return {"ok": True}

                # guardar snapshot para crear pedido cuando elija horario
                sess["tmp"] = sess.get("tmp") or {}
                sess["tmp"]["customer_name"] = customer_name
                sess["tmp"]["cart_snapshot"] = cart
                sess["tmp"]["total_amount"] = float(total)

                sess["stage"] = "awaiting_pickup_time"

                telegram_send_text(
                    bot_token,
                    chat_id,
                    "¿A qué hora será el recojo?\nElige una opción:",
                    reply_markup=_pickup_time_kb(options),
                )
                return {"ok": True}

            # si está esperando pickup time, no aceptamos texto libre (solo botones)
            if sess.get("stage") == "awaiting_pickup_time":
                telegram_send_text(bot_token, chat_id, "Por favor elige la hora usando los botones 👇")
                return {"ok": True}

            telegram_send_text(bot_token, chat_id, "Usa /start para ver el menú.", reply_markup=client_home_kb())
            return {"ok": True}

        # ADMIN
        if normalize(text) in ("start", "/start", "hola"):
            telegram_send_text(bot_token, chat_id, "Admin bot listo. Escribe /id para ver tu chat_id ✅")
            return {"ok": True}

        telegram_send_text(bot_token, chat_id, "OK admin ✅ (escribe /id para ver tu chat_id)")
        return {"ok": True}

    return {"ok": True}
