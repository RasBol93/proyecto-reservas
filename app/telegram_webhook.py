# app/telegram_webhook.py
#
# Cambios aplicados por tus 3 issues:
# 1) Mantener el botón "✅ Ya pagué" (se queda).
# 2) Enviar comprobante al admin SIEMPRE:
#    - El file_id del cliente NO suele funcionar en el bot admin (bots distintos).
#    - Solución: getFile con BOT CLIENTE -> construir URL -> enviar URL con BOT ADMIN.
# 3) Recordatorio: título al admin en negrita + emoji 🔔 al inicio.

import json
import re
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

# key: (tenant_id, chat_id) -> {"cart":[{"sku":..., "qty":...}], "stage": "...", "tmp": {...}}
SESSIONS: Dict[Tuple[str, int], Dict[str, Any]] = {}


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

    with urllib.request.urlopen(req, timeout=12) as resp:
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


def telegram_get_file_path(source_bot_token: str, file_id: str) -> Optional[str]:
    """
    Devuelve file_path usando getFile del bot que RECIBIÓ el archivo.
    (Importante: file_id suele ser válido solo para ese bot.)
    """
    if not source_bot_token or not file_id:
        return None
    try:
        res = telegram_api_call(source_bot_token, "getFile", {"file_id": file_id})
        if not res.get("ok"):
            log_event("telegram_getfile_failed", error=res.get("description") or res)
            return None
        result = res.get("result") or {}
        return (result.get("file_path") or "").strip() or None
    except Exception as e:
        log_event("telegram_getfile_exception", error=str(e))
        return None


def telegram_build_file_url(source_bot_token: str, file_id: str) -> Optional[str]:
    """
    Construye URL descargable:
      https://api.telegram.org/file/bot<TOKEN>/<file_path>
    """
    fp = telegram_get_file_path(source_bot_token, file_id)
    if not fp:
        return None
    return f"https://api.telegram.org/file/bot{source_bot_token}/{fp}"


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
    tenant_id: str,
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
# ADMIN notify
# -------------------------

def notify_admin_payment_reported(
    tenant: Dict[str, Any],
    tenant_id: str,
    orders_sh,
    order_id: str,
    is_reminder: bool = False,
) -> None:
    """
    Envía al admin:
    - Texto con botón ✅ Confirmar pago
    - Y SIEMPRE intenta mandar el comprobante (foto/PDF):
        usando URL construida vía BOT CLIENTE (getFile -> file_path -> file URL)
    """
    admin_token = get_admin_bot_token(tenant)
    admin_chat_id = get_admin_chat_id(tenant)
    client_token = get_client_bot_token(tenant)  # importante para construir URL de file

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

    # Título especial para recordatorio
    if is_reminder:
        title = "<b>🔔 RECORDATORIO — COMPROBANTE YA ENVIADO</b>\n\n"
    else:
        title = "<b>💳 COMPROBANTE RECIBIDO</b>\n\n"

    # Usamos HTML para negrita del título (y evitamos Markdown frágil)
    txt = (
        title
        f"Tenant: {tenant_id}\n"
        f"ID: {order_id}\n"
        f"Cliente: {order.get('customer_name','')}\n"
        f"Contacto(chat_id): {order.get('customer_contact','')}\n"
        f"Hora recogida: {order.get('requested_time','pendiente')}\n"
        f"Cantidad total: {total_qty}\n"
        f"Total: {total:.2f} BOB\n\n"
        f"Detalle:\n{lines_txt}\n\n"
        "Verifica el comprobante y presiona ✅ Confirmar pago."
    )

    telegram_send_text(admin_token, admin_chat_id, txt, reply_markup=confirm_btn, parse_mode="HTML")

    # ----
    # Enviar el comprobante REAL al admin
    # ----
    if not proof_file_id:
        log_event("admin_missing_proof_file_id", tenant_id=tenant_id, order_id=order_id)
        return

    # Construir URL usando el BOT CLIENTE (porque ese bot recibió el archivo)
    proof_url = telegram_build_file_url(client_token, proof_file_id) if client_token else None
    if not proof_url:
        log_event("admin_proof_url_build_failed", tenant_id=tenant_id, order_id=order_id)
        # fallback: intentar con file_id (puede funcionar si fuera mismo bot)
        if proof_type == "photo":
            telegram_send_photo(admin_token, admin_chat_id, proof_file_id, caption=proof_caption or "Comprobante (foto)")
        elif proof_type == "document":
            telegram_send_document(admin_token, admin_chat_id, proof_file_id, caption=proof_caption or "Comprobante (archivo)")
        return

    # Enviar por URL (esto sí funciona aunque sean bots distintos)
    if proof_type == "photo":
        telegram_send_photo(admin_token, admin_chat_id, proof_url, caption=proof_caption or "Comprobante (foto)")
    elif proof_type == "document":
        telegram_send_document(admin_token, admin_chat_id, proof_url, caption=proof_caption or "Comprobante (archivo)")
    else:
        # si no sabemos, mandarlo como documento
        telegram_send_document(admin_token, admin_chat_id, proof_url, caption=proof_caption or "Comprobante")


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
    # ✅ Se mantiene exactamente como tú quieres
    return kb([
        [("✅ Ya pagué", f"i_paid|{tenant_id}|{order_id}")],
        [("🏠 Inicio", "home")],
    ])


# -------------------------
# Helpers de seguridad / parsing
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


def _assert_order_belongs_to_chat(order: Dict[str, Any], chat_id: int, tenant_id: str, order_id: str) -> None:
    owner = str(order.get("customer_contact") or "").strip()
    if not owner:
        log_event("order_missing_owner_contact", tenant_id=tenant_id, order_id=order_id)
        return
    if owner != str(chat_id):
        log_event("order_not_owned_by_chat", tenant_id=tenant_id, order_id=order_id, chat_id=chat_id, owner=owner)
        raise HTTPException(status_code=403, detail="Not authorized for this order")


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

        # ADMIN: confirmar pago
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

            telegram_send_text(
                bot_token,
                chat_id,
                (f"ℹ️ Pedido {order_id} ya estaba en PAID." if res.get("already") else f"✅ Pedido {order_id} marcado como PAID"),
            )

            # avisar al cliente con el BOT CLIENTE
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

        # CLIENT: botón "✅ Ya pagué" => recordatorio (con título 🔔)
        if mode == "client" and data.startswith("i_paid|"):
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

            _assert_order_belongs_to_chat(order, chat_id, tenant_id, order_id)

            proof_file_id = (order.get("payment_proof_file_id") or "").strip()
            if not proof_file_id:
                telegram_send_text(
                    bot_token,
                    chat_id,
                    "Aún no recibí tu comprobante.\nPor favor envía una foto o PDF del pago primero.",
                )
                return {"ok": True}

            notify_admin_payment_reported(tenant, tenant_id, orders_sh, order_id, is_reminder=True)

            telegram_send_text(
                bot_token,
                chat_id,
                "🔔 Listo. Le envié un recordatorio al administrador con tu comprobante.",
                reply_markup=client_home_kb(),
            )
            return {"ok": True}

        # resto callbacks client (menú/carrito/etc.)
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
                msg_txt = (
                    f"🛒 *Tu carrito*\n"
                    f"Cantidad: *{total_qty}*\n"
                    f"Total: *{total:.2f}* BOB\n\n"
                    f"{lines_txt}"
                )
                telegram_send_text(bot_token, chat_id, msg_txt, reply_markup=cart_kb(has_items), parse_mode="Markdown")
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

            # Al subir comprobante: guardar en Sheets + notificar admin con el archivo
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

                # Notificar admin inmediatamente
                notify_admin_payment_reported(tenant, tenant_id, orders_sh, order_id, is_reminder=False)

                telegram_send_text(
                    bot_token,
                    chat_id,
                    "✅ Comprobante recibido.\n📨 Ya se lo envié al administrador para verificación.\n"
                    "Si deseas, también puedes presionar “✅ Ya pagué” para enviar un recordatorio.",
                    reply_markup=i_paid_kb(tenant_id, order_id),
                )
                return {"ok": True}

            if normalize(text) in ("start", "/start", "hola"):
                clear_sess(tenant_id, chat_id)
                telegram_send_text(bot_token, chat_id, "Bienvenido 👋\nElige una opción:", client_home_kb())
                return {"ok": True}

            if sess.get("stage") == "awaiting_name":
                customer_name = text.strip()
                if not customer_name:
                    telegram_send_text(bot_token, chat_id, "Dime tu nombre, por favor.")
                    return {"ok": True}

                menu_idx = load_menu_index(orders_sh)
                cart = sess.get("cart") or []
                _, total, total_qty = fmt_cart_lines(cart, menu_idx)

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
                    tenant_id=tenant_id,
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
                    telegram_send_text(
                        bot_token,
                        chat_id,
                        "⚠️ No tengo QR configurado para este tenant (payment_qr_file_id / payment_qr_url).",
                    )
                    log_event("missing_qr_config", tenant_id=tenant_id)

                telegram_send_text(
                    bot_token,
                    chat_id,
                    "📎 Cuando pagues, envía aquí tu *comprobante* (foto o PDF).",
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
