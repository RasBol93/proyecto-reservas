# app/telegram_webhook.py
#
# UX con cooldown:
# - Cliente sube comprobante -> NO notificar admin todavía. Pedir presionar "✅ Ya pagué".
# - Cliente presiona "✅ Ya pagué" -> notificar admin + reenviar comprobante (download con bot cliente + upload con bot admin).
# - Botón "🔔 Recordar al administrador":
#     * antes de 5 min: mensaje amable "espera un momento"
#     * después de 5 min: envía recordatorio al admin (título en negrita + campana)
# - 5 min DESPUÉS de enviar recordatorio: mostrar opción "📞 Contactar al administrador" (abre chat directo).
#
# Nota Telegram:
# - file_id NO es portable entre bots. Hay que descargar con bot cliente y re-subir con bot admin.

import json
import re
import time
import urllib.request
from typing import Any, Dict, Optional, List, Tuple

from fastapi import APIRouter, HTTPException

from app.config import TELEGRAM_API_BASE
from app.tenants import get_tenant_or_404, resolve_bot_by_secret
from app.sheets import get_gspread_client, open_spreadsheet_by_key
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

router = APIRouter()

# =========================================================
# Estado en memoria (DEMO)
# key: (tenant_id, chat_id) -> {"cart":[...], "stage": "...", "tmp": {...}}
# =========================================================
SESSIONS: Dict[Tuple[str, int], Dict[str, Any]] = {}

# Cooldowns
REMIND_AFTER_SECONDS = 300          # 5 min desde "✅ Ya pagué"
CONTACT_AFTER_REMIND_SECONDS = 300  # 5 min después de haber enviado recordatorio


def get_sess(tenant_id: str, chat_id: int) -> Dict[str, Any]:
    key = (tenant_id, chat_id)
    if key not in SESSIONS:
        SESSIONS[key] = {"cart": [], "stage": "idle", "tmp": {}}
    return SESSIONS[key]


def clear_sess(tenant_id: str, chat_id: int) -> None:
    key = (tenant_id, chat_id)
    SESSIONS[key] = {"cart": [], "stage": "idle", "tmp": {}}


# -------------------------
# Helpers básicos
# -------------------------

def _safe_int(v: Any) -> Optional[int]:
    try:
        return int(v)
    except Exception:
        return None


# -------------------------
# Telegram API helpers (JSON calls)
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


def telegram_answer_callback(bot_token: str, callback_query_id: str, text: str = "OK") -> None:
    try:
        res = telegram_api_call(bot_token, "answerCallbackQuery", {"callback_query_id": callback_query_id, "text": text})
        if not res.get("ok", True):
            log_event("telegram_ack_failed", error=res.get("description") or res)
    except Exception as e:
        log_event("telegram_ack_exception", error=str(e))


# -------------------------
# Telegram API helpers (multipart for bytes upload)
# -------------------------

def _multipart_form_data(fields: Dict[str, Any], files: Dict[str, Tuple[str, bytes, str]]) -> Tuple[bytes, str]:
    boundary = "----tgFormBoundary7MA4YWxkTrZu0gW"
    crlf = "\r\n"
    lines: List[bytes] = []

    for name, value in fields.items():
        lines.append(f"--{boundary}{crlf}".encode())
        lines.append(f'Content-Disposition: form-data; name="{name}"{crlf}{crlf}'.encode())
        lines.append(f"{value}{crlf}".encode())

    for field_name, (filename, file_bytes, mime) in files.items():
        lines.append(f"--{boundary}{crlf}".encode())
        lines.append(f'Content-Disposition: form-data; name="{field_name}"; filename="{filename}"{crlf}'.encode())
        lines.append(f"Content-Type: {mime}{crlf}{crlf}".encode())
        lines.append(file_bytes)
        lines.append(crlf.encode())

    lines.append(f"--{boundary}--{crlf}".encode())

    body = b"".join(lines)
    content_type = f"multipart/form-data; boundary={boundary}"
    return body, content_type


def telegram_send_photo_bytes(bot_token: str, chat_id: int, photo_bytes: bytes, filename: str = "proof.jpg", caption: str = "") -> Dict[str, Any]:
    url = f"{TELEGRAM_API_BASE}/bot{bot_token}/sendPhoto"
    fields = {"chat_id": str(chat_id)}
    if caption:
        fields["caption"] = caption
    body, content_type = _multipart_form_data(fields, {"photo": (filename, photo_bytes, "image/jpeg")})

    req = urllib.request.Request(url, data=body, headers={"Content-Type": content_type}, method="POST")
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode("utf-8"))


def telegram_send_document_bytes(bot_token: str, chat_id: int, doc_bytes: bytes, filename: str = "proof.bin", caption: str = "") -> Dict[str, Any]:
    url = f"{TELEGRAM_API_BASE}/bot{bot_token}/sendDocument"
    fields = {"chat_id": str(chat_id)}
    if caption:
        fields["caption"] = caption
    body, content_type = _multipart_form_data(fields, {"document": (filename, doc_bytes, "application/octet-stream")})

    req = urllib.request.Request(url, data=body, headers={"Content-Type": content_type}, method="POST")
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode("utf-8"))


def telegram_get_file_path(bot_token: str, file_id: str) -> str:
    res = telegram_api_call(bot_token, "getFile", {"file_id": file_id})
    if not res.get("ok"):
        raise RuntimeError(f"getFile failed: {res}")
    return res["result"]["file_path"]


def telegram_download_file_bytes(bot_token: str, file_path: str) -> bytes:
    url = f"{TELEGRAM_API_BASE}/file/bot{bot_token}/{file_path}"
    with urllib.request.urlopen(url, timeout=30) as resp:
        return resp.read()


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


def _assert_admin_authorized(tenant: Dict[str, Any], chat_id: int, tenant_id: str) -> None:
    admin_chat_id = get_admin_chat_id(tenant)
    if admin_chat_id is None:
        log_event("admin_chat_id_missing_security_warning", tenant_id=tenant_id, chat_id=chat_id)
        return
    if chat_id != admin_chat_id:
        log_event("admin_paid_unauthorized", tenant_id=tenant_id, chat_id=chat_id, expected_admin_chat_id=admin_chat_id)
        raise HTTPException(status_code=403, detail="Not authorized")


# -------------------------
# QR helpers (opcional)
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
# Keyboards cliente
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


def remind_kb(tenant_id: str, order_id: str) -> Dict[str, Any]:
    return kb([
        [("🔔 Recordar al administrador", f"remind|{tenant_id}|{order_id}")],
        [("🏠 Inicio", "home")],
    ])


def contact_admin_kb(admin_chat_id: int) -> Dict[str, Any]:
    """
    Inline keyboard con botón URL que abre chat directo.
    tg://user?id=... funciona muy bien en móvil.
    """
    return {
        "inline_keyboard": [
            [{"text": "📞 Contactar al administrador", "url": f"tg://user?id={admin_chat_id}"}],
            [{"text": "🏠 Inicio", "callback_data": "home"}],
        ]
    }


# -------------------------
# Envío de notificación al admin + reenvío comprobante (bytes)
# -------------------------

def _forward_proof_to_admin(
    tenant: Dict[str, Any],
    tenant_id: str,
    proof_file_id: str,
    proof_type: str,
    proof_caption: str,
) -> None:
    admin_token = get_admin_bot_token(tenant)
    admin_chat_id = get_admin_chat_id(tenant)
    client_token = get_client_bot_token(tenant)

    if not admin_token or not admin_chat_id or not client_token:
        log_event(
            "forward_proof_missing_config",
            tenant_id=tenant_id,
            has_admin_token=bool(admin_token),
            has_admin_chat=bool(admin_chat_id),
            has_client_token=bool(client_token),
        )
        return

    try:
        file_path = telegram_get_file_path(client_token, proof_file_id)
        blob = telegram_download_file_bytes(client_token, file_path)
    except Exception as e:
        log_event("forward_proof_download_failed", tenant_id=tenant_id, error=str(e))
        return

    try:
        if proof_type == "photo":
            res = telegram_send_photo_bytes(
                admin_token, admin_chat_id, blob,
                filename="proof.jpg",
                caption=proof_caption or "Comprobante (foto)",
            )
        else:
            res = telegram_send_document_bytes(
                admin_token, admin_chat_id, blob,
                filename="proof.bin",
                caption=proof_caption or "Comprobante (archivo)",
            )
        if not res.get("ok", True):
            log_event("forward_proof_upload_failed", tenant_id=tenant_id, error=res.get("description") or res)
    except Exception as e:
        log_event("forward_proof_upload_exception", tenant_id=tenant_id, error=str(e))


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

    if is_reminder:
        title = "<b>🔔 RECORDATORIO — PAGO REPORTADO</b>"
        parse_mode = "HTML"
    else:
        title = "💳 PAGO REPORTADO"
        parse_mode = None

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

    telegram_send_text(admin_token, admin_chat_id, txt, reply_markup=confirm_btn, parse_mode=parse_mode)

    if proof_file_id and proof_type:
        _forward_proof_to_admin(
            tenant=tenant,
            tenant_id=tenant_id,
            proof_file_id=proof_file_id,
            proof_type=proof_type,
            proof_caption=proof_caption,
        )
    else:
        log_event("admin_notify_missing_proof", tenant_id=tenant_id, order_id=order_id)


# =========================================================
# Webhook endpoint
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

            # avisar al cliente
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
            tmp = sess.get("tmp") or {}
            sess["tmp"] = tmp

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

            # ✅ Cliente presiona "Ya pagué" -> notificar admin (primera notificación)
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
                proof_type = (order.get("payment_proof_type") or "").strip()
                if not proof_file_id or not proof_type:
                    telegram_send_text(
                        bot_token,
                        chat_id,
                        "Aún no recibí tu comprobante.\nPor favor envía una foto o PDF del pago primero.",
                        reply_markup=client_home_kb(),
                    )
                    return {"ok": True}

                notify_admin_payment_reported(tenant, tenant_id, orders_sh, order_id, is_reminder=False)

                now = time.time()
                tmp["paid_notified_order_id"] = order_id
                tmp["paid_notified_at_ts"] = now
                tmp["reminder_sent_at_ts"] = None

                telegram_send_text(
                    bot_token,
                    chat_id,
                    "✅ Recibido. Ya avisamos al administrador.\n"
                    "Si no responde en unos minutos, podrás enviar un recordatorio.",
                    reply_markup=remind_kb(tenant_id, order_id),
                )
                return {"ok": True}

            # 🔔 Recordatorio con cooldown + escalamiento a contacto
            if data.startswith("remind|"):
                parts = data.split("|")
                if len(parts) != 3:
                    return {"ok": True}

                _, cb_tenant_id, order_id = parts
                cb_tenant_id = cb_tenant_id.strip()
                order_id = order_id.strip()

                if cb_tenant_id != tenant_id:
                    raise HTTPException(status_code=400, detail="Tenant mismatch in remind callback")

                order = get_order_by_id(orders_sh, order_id)
                if not order:
                    telegram_send_text(bot_token, chat_id, "No encontré tu pedido. Vuelve a /start.", reply_markup=client_home_kb())
                    return {"ok": True}

                proof_file_id = (order.get("payment_proof_file_id") or "").strip()
                proof_type = (order.get("payment_proof_type") or "").strip()
                if not proof_file_id or not proof_type:
                    telegram_send_text(
                        bot_token,
                        chat_id,
                        "Aún no veo tu comprobante guardado. Envíalo primero (foto o PDF).",
                        reply_markup=client_home_kb(),
                    )
                    return {"ok": True}

                now = time.time()
                paid_ts = tmp.get("paid_notified_at_ts")
                rem_ts = tmp.get("reminder_sent_at_ts")

                # Si por algún motivo no está el paid_ts, lo seteamos y pedimos esperar
                if not isinstance(paid_ts, (int, float)):
                    tmp["paid_notified_at_ts"] = now
                    telegram_send_text(
                        bot_token,
                        chat_id,
                        "😊 Perfecto. Ya avisamos al administrador.\n"
                        "Por favor espera *unos minutos* antes de enviar un recordatorio.",
                        reply_markup=remind_kb(tenant_id, order_id),
                        parse_mode="Markdown",
                    )
                    return {"ok": True}

                # 1) Aún no han pasado 5 min desde "Ya pagué"
                elapsed_from_paid = now - float(paid_ts)
                if elapsed_from_paid < REMIND_AFTER_SECONDS and not rem_ts:
                    wait = int(REMIND_AFTER_SECONDS - elapsed_from_paid)
                    mins = max(1, (wait + 59) // 60)
                    telegram_send_text(
                        bot_token,
                        chat_id,
                        f"😊 Gracias. Ya le avisamos al administrador.\n"
                        f"Para no saturarlo, espera *{mins} min* antes de enviar un recordatorio.",
                        reply_markup=remind_kb(tenant_id, order_id),
                        parse_mode="Markdown",
                    )
                    return {"ok": True}

                # 2) Ya se envió recordatorio anteriormente:
                if isinstance(rem_ts, (int, float)):
                    elapsed_from_rem = now - float(rem_ts)
                    if elapsed_from_rem < CONTACT_AFTER_REMIND_SECONDS:
                        wait = int(CONTACT_AFTER_REMIND_SECONDS - elapsed_from_rem)
                        mins = max(1, (wait + 59) // 60)
                        telegram_send_text(
                            bot_token,
                            chat_id,
                            f"🙏 Ya enviamos un recordatorio.\n"
                            f"Por favor espera *{mins} min* más. Si no responde, te habilitaremos contacto directo.",
                            reply_markup=remind_kb(tenant_id, order_id),
                            parse_mode="Markdown",
                        )
                        return {"ok": True}

                    # ✅ Después de 5 min del recordatorio: habilitar contacto directo
                    admin_chat_id = get_admin_chat_id(tenant)
                    if not admin_chat_id:
                        telegram_send_text(
                            bot_token,
                            chat_id,
                            "⚠️ No tengo configurado el contacto del administrador todavía.",
                            reply_markup=client_home_kb(),
                        )
                        return {"ok": True}

                    telegram_send_text(
                        bot_token,
                        chat_id,
                        "📞 Si el administrador aún no responde, puedes escribirle directamente aquí:",
                        reply_markup=contact_admin_kb(admin_chat_id),
                    )
                    return {"ok": True}

                # 3) Ya pasaron 5 min desde "Ya pagué" y todavía NO se envió recordatorio: enviar recordatorio
                notify_admin_payment_reported(tenant, tenant_id, orders_sh, order_id, is_reminder=True)
                tmp["reminder_sent_at_ts"] = now
                log_event("client_sent_reminder", tenant_id=tenant_id, order_id=order_id, chat_id=chat_id)

                telegram_send_text(
                    bot_token,
                    chat_id,
                    "🔔 Listo. Enviamos un *recordatorio* al administrador.\n"
                    "Si no responde, en unos minutos te habilitaremos contacto directo.",
                    reply_markup=remind_kb(tenant_id, order_id),
                    parse_mode="Markdown",
                )
                return {"ok": True}

            # Categorías / productos / qty
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

        # CLIENT: upload proof (foto o PDF)
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

            # ✅ Al subir proof: SOLO guardar + pedir "Ya pagué"
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

                telegram_send_text(
                    bot_token,
                    chat_id,
                    "✅ Comprobante recibido.\nAhora presiona “✅ Ya pagué” para avisar al administrador.",
                    reply_markup=i_paid_kb(tenant_id, order_id),
                )
                return {"ok": True}

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

                order_id = gen_order_id()
                requested_time = "pendiente"

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

                log_event("order_created", tenant_id=tenant_id, order_id=order_id, chat_id=chat_id, total=total)

                sess["stage"] = "awaiting_proof"
                sess["tmp"] = sess.get("tmp") or {}
                sess["tmp"]["pending_order_id"] = order_id
                sess["tmp"]["customer_name"] = customer_name

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
                    try:
                        telegram_api_call(bot_token, "sendPhoto", {"chat_id": chat_id, "photo": qr_file_id, "caption": "QR de pago"})
                    except Exception as e:
                        log_event("send_qr_fileid_failed", tenant_id=tenant_id, error=str(e))
                elif qr_url:
                    try:
                        telegram_api_call(bot_token, "sendPhoto", {"chat_id": chat_id, "photo": qr_url, "caption": "QR de pago"})
                    except Exception as e:
                        log_event("send_qr_url_failed", tenant_id=tenant_id, error=str(e))
                else:
                    telegram_send_text(
                        bot_token,
                        chat_id,
                        "⚠️ No tengo QR configurado para este tenant (payment_qr_file_id / payment_qr_url).",
                    )
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

        # ADMIN
        if normalize(text) in ("start", "/start", "hola"):
            telegram_send_text(bot_token, chat_id, "Admin bot listo. Escribe /id para ver tu chat_id ✅")
            return {"ok": True}

        telegram_send_text(bot_token, chat_id, "OK admin ✅ (escribe /id para ver tu chat_id)")
        return {"ok": True}

    return {"ok": True}
