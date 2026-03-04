# app/telegram_webhook.py

import json
import re
import time
import urllib.request
import urllib.parse
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

# ✅ NUEVO: stats
from app.stats import build_periods, resolve_period, build_stats_report_text, log_event_to_sheet

router = APIRouter()

# =========================================================
# Estado en memoria (DEMO)
# =========================================================
# key: (tenant_id, chat_id) -> {"cart":[{"sku":..., "qty":...}], "stage":"...", "tmp": {...}}
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
# Multipart helpers (reenviar bytes admin)
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


def _telegram_send_file_bytes_admin(admin_token: str, method: str, chat_id: int, file_field: str, filename: str, content_type: str, file_bytes: bytes, caption: str = "") -> bool:
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
# Forward proof to admin (clave)
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

    log_event("admin_notify_result", tenant_id=tenant_id, order_id=order_id, ok_txt=bool(ok_txt), ok_proof=bool(ok_proof), is_reminder=bool(is_reminder))
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


# ✅ NUEVO: Admin keyboards (mínimos)
def admin_home_kb() -> Dict[str, Any]:
    return kb([
        [("📊 Estadísticas", "admin_stats")],
    ])


def admin_periods_kb(tenant_id: str, periods: List[Tuple[str, str]]) -> Dict[str, Any]:
    rows = []
    for label, key in periods:
        rows.append([(f"📊 {label}", f"admin_stats_period|{tenant_id}|{key}")])
    rows.append([("⬅️ Volver", "admin_home")])
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
        # ADMIN: confirmar pago
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
                telegram_send_text(bot_token, chat_id, f"⚠️ Pedido {order_id} no encontrado en Sheets.")
                return {"ok": True}

            telegram_send_text(bot_token, chat_id, f"✅ Pedido {order_id} marcado como PAID")

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

        # ✅ NUEVO: ADMIN stats callbacks
        if mode == "admin":
            if data == "admin_home":
                telegram_send_text(bot_token, chat_id, "Panel admin:", reply_markup=admin_home_kb())
                return {"ok": True}

            if data == "admin_stats":
                # mostrar períodos
                periods = build_periods(tenant_tz)
                telegram_send_text(
                    bot_token,
                    chat_id,
                    "📊 Elige el período:",
                    reply_markup=admin_periods_kb(tenant_id, periods),
                )
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

                # (opcional) seguridad: solo admin_chat_id puede ver stats
                _assert_admin_authorized(tenant, chat_id, tenant_id)

                period = resolve_period(tenant_tz, period_key)
                txt = build_stats_report_text(orders_sh, tenant_id=tenant_id, tenant_tz=tenant_tz, period=period)

                telegram_send_text(bot_token, chat_id, txt)
                # dejar botón para volver a períodos (sin spamear muchos botones)
                telegram_send_text(bot_token, chat_id, "⬅️ Volver:", reply_markup=admin_periods_kb(tenant_id, build_periods(tenant_tz)))
                return {"ok": True}

        # -------------------------
        # CLIENT callbacks (MENÚ COMPLETO + PAGO)
        # -------------------------
        if mode == "client":
            sess = get_sess(tenant_id, chat_id)
            tmp = sess.get("tmp") or {}
            sess["tmp"] = tmp

            # ---- Home
            if data == "home":
                telegram_send_text(bot_token, chat_id, "Elige una opción:", client_home_kb())
                return {"ok": True}

            # ---- Menu (categorías)
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

            # ---- Categoría -> productos
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
                for it in items[:25]:
                    rows.append([(f"{it['name']} ({it['price']:.0f})", f"prd|{it['sku']}")])
                rows.append([("🛒 Carrito", "cart")])
                rows.append([("⬅️ Categorías", "menu")])
                rows.append([("🏠 Inicio", "home")])

                telegram_send_text(bot_token, chat_id, f"🍽 {real_cat} — elige un producto:", kb(rows))
                return {"ok": True}

            # ---- Producto -> elegir cantidad
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

            # ---- Qty -> agregar al carrito
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

            # ---- Cart
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

            # ---- Cliente presiona "Ya pagué" (NOTIFICA ADMIN + PRUEBA)
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

            # ---- Recordatorio con cooldown 5 min
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

            # ---- Contactar admin (solo después de 10 min desde “Ya pagué”)
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

            # fallback
            return {"ok": True}

        return {"ok": True}

    # =========================================================
    # 2) MENSAJE NORMAL (texto + media)
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

                # ✅ NUEVO: registrar conversación iniciada (no rompe si falla)
                log_event_to_sheet(
                    orders_sh=orders_sh,
                    tenant_id=tenant_id,
                    chat_id=str(chat_id),
                    event_type="client_start",
                    meta={"source": "telegram", "text": text[:50]},
                )

                telegram_send_text(bot_token, chat_id, "Bienvenido 👋\nElige una opción:", client_home_kb())
                return {"ok": True}

            # Captura nombre (confirmación carrito)
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

        # ADMIN
        if mode == "admin":
            # comando alternativo
            if normalize(text) in ("/stats", "stats"):
                telegram_send_text(bot_token, chat_id, "Panel admin:", reply_markup=admin_home_kb())
                telegram_send_text(bot_token, chat_id, "📊 Elige el período:", reply_markup=admin_periods_kb(tenant_id, build_periods(tenant_tz)))
                return {"ok": True}

            if normalize(text) in ("start", "/start", "hola"):
                telegram_send_text(bot_token, chat_id, "Admin bot listo ✅", reply_markup=admin_home_kb())
                return {"ok": True}

            telegram_send_text(bot_token, chat_id, "OK admin ✅", reply_markup=admin_home_kb())
            return {"ok": True}

        return {"ok": True}

    return {"ok": True}
