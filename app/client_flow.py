# app/client_flow.py

import time
from typing import Any, Dict, List

from fastapi import HTTPException

from app.menu import load_menu_index, group_menu_by_category
from app.orders import (
    append_order_row,
    update_order_payment_proof,
    find_latest_pending_order_for_contact,
    get_order_by_id,
    gen_order_id,
    build_items_snapshot,
)
from app.telegram_api import telegram_send_text, telegram_send_photo
from app.telegram_keyboard import kb
from app.utils import normalize, log_event
from app.stats import log_event_to_sheet
from app.webhook_helpers import (
    REMINDER_COOLDOWN_SECONDS,
    CONTACT_AFTER_SECONDS,
    clear_sess,
    get_sess,
    get_payment_qr_file_id,
    get_payment_qr_url,
    fmt_cart_lines,
    fmt_snapshot_lines,
    build_order_recap_text,
    get_business_status_safe,
    send_business_blocked_text,
    contact_link_for_admin,
    client_home_kb,
    cart_kb,
    i_paid_kb,
    paid_actions_kb,
    contact_admin_kb,
)
from app.payment_flow import notify_admin_payment_reported
from app.alerts import (
    alert_order_failed,
    alert_payment_proof_failed,
    alert_payment_failed,
    alert_menu_error,
    alert_tenant_error,
    alert_system_error,
)
from app.content import (
    build_start_text,
    build_location_text,
    build_faq_text,
    build_survey_text,
    load_content_map,
    has_location,
    has_faq,
    has_survey,
)


def build_dynamic_home_kb(content_map: Dict[str, str]):
    rows = [
        [("📋 Ver menú", "menu")],
        [("🛒 Ver carrito", "cart")],
    ]

    if has_location(content_map):
        rows.append([("📍 Ubicación", "location")])

    rows.append([("⏰ Horarios", "hours")])

    if has_faq(content_map):
        rows.append([("❓ FAQ", "faq")])

    if has_survey(content_map):
        rows.append([("📝 Encuesta", "survey")])

    return kb(rows)


def _send_home(bot_token: str, chat_id: int, orders_sh) -> bool:
    content_map = load_content_map(orders_sh)
    return telegram_send_text(
        bot_token,
        chat_id,
        build_start_text(orders_sh),
        build_dynamic_home_kb(content_map),
    )


def client_orders_allowed_or_notify(bot_token: str, chat_id: int, orders_sh, tenant_tz: str) -> bool:
    try:
        bs = get_business_status_safe(orders_sh=orders_sh, tenant_tz=tenant_tz)
        if bool(bs.get("accepts_orders_now")):
            return True
        telegram_send_text(bot_token, chat_id, send_business_blocked_text(bs))
        return False
    except Exception as e:
        log_event(
            "client_orders_allowed_check_error",
            chat_id=chat_id,
            error_type=type(e).__name__,
            error=str(e),
        )
        alert_system_error(error=str(e), module="client_orders_allowed_or_notify")
        telegram_send_text(bot_token, chat_id, "⚠️ Ocurrió un error verificando el horario del negocio.")
        return False


def handle_client_callback(
    tenant: Dict[str, Any],
    tenant_id: str,
    bot_token: str,
    chat_id: int,
    data: str,
    orders_sh,
    tenant_tz: str,
) -> Dict[str, Any]:
    try:
        sess = get_sess(tenant_id, chat_id)
        tmp = sess.get("tmp") or {}
        sess["tmp"] = tmp

        log_event("client_callback", tenant_id=tenant_id, chat_id=chat_id, data=data)

        if data == "home":
            _send_home(bot_token, chat_id, orders_sh)
            return {"ok": True}

        if data == "location":
            telegram_send_text(bot_token, chat_id, build_location_text(orders_sh))
            return {"ok": True}

        if data == "faq":
            telegram_send_text(bot_token, chat_id, build_faq_text(orders_sh))
            return {"ok": True}

        if data == "survey":
            telegram_send_text(bot_token, chat_id, build_survey_text(orders_sh))
            return {"ok": True}

        if data == "hours":
            bs = get_business_status_safe(orders_sh=orders_sh, tenant_tz=tenant_tz)
            open_now = "🟢 Hoy estamos abiertos" if bs.get("accepts_orders_now") else "🔴 Hoy estamos cerrados"

            open_time = str(bs.get("open_time") or "").strip()
            close_time = str(bs.get("close_time") or "").strip()
            last_order_time = str(bs.get("last_order_time") or "").strip()

            parts = [open_now]
            if open_time and close_time:
                parts.append(f"Horario de hoy: {open_time} - {close_time}")
            if last_order_time:
                parts.append(f"Última hora de pedido: {last_order_time}")

            telegram_send_text(bot_token, chat_id, "\n\n".join(parts))
            return {"ok": True}

        if data == "menu":
            if not client_orders_allowed_or_notify(bot_token, chat_id, orders_sh, tenant_tz):
                return {"ok": True}

            try:
                menu_idx = load_menu_index(orders_sh)
                cats = group_menu_by_category(menu_idx)
            except Exception as e:
                log_event(
                    "client_menu_load_error",
                    tenant_id=tenant_id,
                    chat_id=chat_id,
                    error_type=type(e).__name__,
                    error=str(e),
                )
                alert_menu_error(tenant_id=tenant_id, error=str(e))
                telegram_send_text(bot_token, chat_id, "⚠️ No pude cargar el menú en este momento.")
                return {"ok": True}

            if not cats:
                _send_home(bot_token, chat_id, orders_sh)
                telegram_send_text(bot_token, chat_id, "No hay menú activo.")
                return {"ok": True}

            rows = []
            for c in sorted(cats.keys(), key=lambda x: normalize(x)):
                rows.append([(c, f"cat|{normalize(c)}")])
            rows.append([("🛒 Carrito", "cart")])
            rows.append([("🏠 Inicio", "home")])

            telegram_send_text(bot_token, chat_id, "📋 Elige una categoría:", kb(rows))
            return {"ok": True}

        if data.startswith("cat|"):
            if not client_orders_allowed_or_notify(bot_token, chat_id, orders_sh, tenant_tz):
                return {"ok": True}

            cat_norm = data.split("|", 1)[1].strip()

            try:
                menu_idx = load_menu_index(orders_sh)
                cats = group_menu_by_category(menu_idx)
            except Exception as e:
                log_event(
                    "client_category_load_error",
                    tenant_id=tenant_id,
                    chat_id=chat_id,
                    category=cat_norm,
                    error_type=type(e).__name__,
                    error=str(e),
                )
                alert_menu_error(tenant_id=tenant_id, error=str(e))
                telegram_send_text(bot_token, chat_id, "⚠️ No pude cargar esa categoría.")
                return {"ok": True}

            real_cat = None
            for c in cats.keys():
                if normalize(c) == cat_norm:
                    real_cat = c
                    break

            if not real_cat:
                _send_home(bot_token, chat_id, orders_sh)
                telegram_send_text(bot_token, chat_id, "Categoría no encontrada.")
                return {"ok": True}

            items = cats.get(real_cat, [])
            if not items:
                _send_home(bot_token, chat_id, orders_sh)
                telegram_send_text(bot_token, chat_id, "No hay productos activos.")
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
            if not client_orders_allowed_or_notify(bot_token, chat_id, orders_sh, tenant_tz):
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
            if not client_orders_allowed_or_notify(bot_token, chat_id, orders_sh, tenant_tz):
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

            try:
                menu_idx = load_menu_index(orders_sh)
            except Exception as e:
                log_event(
                    "client_qty_menu_load_error",
                    tenant_id=tenant_id,
                    chat_id=chat_id,
                    sku=sku,
                    error_type=type(e).__name__,
                    error=str(e),
                )
                alert_menu_error(tenant_id=tenant_id, sku=sku, error=str(e))
                telegram_send_text(bot_token, chat_id, "⚠️ No pude validar el producto en este momento.")
                return {"ok": True}

            if sku not in menu_idx:
                _send_home(bot_token, chat_id, orders_sh)
                telegram_send_text(bot_token, chat_id, "Producto no disponible.")
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
            try:
                menu_idx = load_menu_index(orders_sh)
            except Exception as e:
                log_event(
                    "client_cart_menu_load_error",
                    tenant_id=tenant_id,
                    chat_id=chat_id,
                    error_type=type(e).__name__,
                    error=str(e),
                )
                alert_menu_error(tenant_id=tenant_id, error=str(e))
                telegram_send_text(bot_token, chat_id, "⚠️ No pude cargar tu carrito.")
                return {"ok": True}

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
            _send_home(bot_token, chat_id, orders_sh)
            telegram_send_text(bot_token, chat_id, "🧹 Carrito vaciado.")
            return {"ok": True}

        if data == "cart_confirm":
            if not client_orders_allowed_or_notify(bot_token, chat_id, orders_sh, tenant_tz):
                return {"ok": True}

            cart = sess.get("cart") or []
            if not cart:
                _send_home(bot_token, chat_id, orders_sh)
                telegram_send_text(bot_token, chat_id, "Tu carrito está vacío.")
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
                _send_home(bot_token, chat_id, orders_sh)
                telegram_send_text(bot_token, chat_id, "No encontré tu pedido. Vuelve a /start.")
                return {"ok": True}

            proof_file_id = (order.get("payment_proof_file_id") or "").strip()
            if not proof_file_id:
                telegram_send_text(bot_token, chat_id, "Aún no recibí tu comprobante.\nEnvía una foto o PDF del pago primero.")
                return {"ok": True}

            try:
                ok_sent = notify_admin_payment_reported(tenant, tenant_id, orders_sh, order_id, is_reminder=False)
            except Exception as e:
                log_event(
                    "notify_admin_payment_reported_error",
                    tenant_id=tenant_id,
                    chat_id=chat_id,
                    order_id=order_id,
                    error_type=type(e).__name__,
                    error=str(e),
                )
                alert_payment_failed(tenant_id=tenant_id, order_id=order_id, error=str(e))
                telegram_send_text(bot_token, chat_id, "⚠️ No pude avisar al administrador en este momento. Intenta de nuevo.")
                return {"ok": True}

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

            try:
                ok_sent = notify_admin_payment_reported(tenant, tenant_id, orders_sh, order_id, is_reminder=True)
            except Exception as e:
                log_event(
                    "notify_admin_payment_reminder_error",
                    tenant_id=tenant_id,
                    chat_id=chat_id,
                    order_id=order_id,
                    error_type=type(e).__name__,
                    error=str(e),
                )
                alert_payment_failed(tenant_id=tenant_id, order_id=order_id, error=str(e))
                telegram_send_text(bot_token, chat_id, "⚠️ No pude enviar el recordatorio en este momento.")
                return {"ok": True}

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

            link = contact_link_for_admin(tenant)
            if not link:
                alert_tenant_error(tenant_id=tenant_id, error="contact_link_for_admin missing")
                _send_home(bot_token, chat_id, orders_sh)
                telegram_send_text(bot_token, chat_id, "No tengo configurado el contacto directo del administrador.")
                return {"ok": True}

            telegram_send_text(bot_token, chat_id, "💬 Contacto directo habilitado.\nToca el enlace para escribirle al administrador:")
            telegram_send_text(bot_token, chat_id, link)
            return {"ok": True}

        return {"ok": True}

    except Exception as e:
        log_event(
            "client_callback_error",
            tenant_id=tenant_id,
            chat_id=chat_id,
            data=data,
            error_type=type(e).__name__,
            error=str(e),
        )
        alert_system_error(error=str(e), module="client_callback")
        telegram_send_text(bot_token, chat_id, "⚠️ Ocurrió un error. Intenta nuevamente.")
        return {"ok": True}


def handle_client_message(
    tenant: Dict[str, Any],
    tenant_id: str,
    bot_token: str,
    chat_id: int,
    msg: Dict[str, Any],
    orders_sh,
    tenant_tz: str,
) -> Dict[str, Any]:
    try:
        text = (msg.get("text") or "").strip()
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
                _send_home(bot_token, chat_id, orders_sh)
                telegram_send_text(bot_token, chat_id, "No encontré un pedido pendiente. Crea uno nuevo con /start.")
                return {"ok": True}

            result = update_order_payment_proof(
                orders_sh=orders_sh,
                order_id=order_id,
                proof_file_id=proof_file_id,
                proof_type=proof_type,
                proof_caption=proof_caption,
            )

            if not result.get("ok"):
                alert_payment_proof_failed(
                    tenant_id=tenant_id,
                    order_id=order_id,
                    chat_id=chat_id,
                    error=result.get("error") or "update_order_payment_proof failed",
                )
                telegram_send_text(bot_token, chat_id, "⚠️ Ocurrió un error guardando tu comprobante. Intenta nuevamente.")
                return {"ok": True}

            telegram_send_text(
                bot_token,
                chat_id,
                "✅ Comprobante recibido.\nAhora presiona “✅ Ya pagué” para avisar al administrador.",
                reply_markup=i_paid_kb(tenant_id, order_id),
            )
            return {"ok": True}

        if normalize(text) in ("start", "/start", "hola"):
            clear_sess(tenant_id, chat_id)

            try:
                log_event_to_sheet(
                    orders_sh=orders_sh,
                    tenant_id=tenant_id,
                    chat_id=str(chat_id),
                    event_type="client_start",
                    meta={"source": "telegram", "text": text[:50]},
                )
            except Exception as e:
                log_event(
                    "client_start_log_event_to_sheet_error",
                    tenant_id=tenant_id,
                    chat_id=chat_id,
                    error_type=type(e).__name__,
                    error=str(e),
                )

            _send_home(bot_token, chat_id, orders_sh)
            return {"ok": True}

        if sess.get("stage") == "awaiting_name":
            if not client_orders_allowed_or_notify(bot_token, chat_id, orders_sh, tenant_tz):
                sess["stage"] = "idle"
                return {"ok": True}

            customer_name = text.strip()
            if not customer_name:
                telegram_send_text(bot_token, chat_id, "Dime tu nombre, por favor.")
                return {"ok": True}

            try:
                menu_idx = load_menu_index(orders_sh)
            except Exception as e:
                log_event(
                    "client_awaiting_name_menu_load_error",
                    tenant_id=tenant_id,
                    chat_id=chat_id,
                    error_type=type(e).__name__,
                    error=str(e),
                )
                alert_menu_error(tenant_id=tenant_id, error=str(e))
                telegram_send_text(bot_token, chat_id, "⚠️ No pude cargar el menú para crear tu pedido.")
                return {"ok": True}

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
                _send_home(bot_token, chat_id, orders_sh)
                telegram_send_text(bot_token, chat_id, "Tu carrito está vacío.")
                sess["stage"] = "idle"
                return {"ok": True}

            items_snapshot = build_items_snapshot(items_list, menu_idx)
            lines_real, total_real, total_qty_real = fmt_snapshot_lines(items_snapshot)

            order_id = gen_order_id()
            requested_time = "pendiente"

            result = append_order_row(
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

            if not result.get("ok"):
                alert_order_failed(
                    tenant_id=tenant_id,
                    order_id=order_id,
                    chat_id=chat_id,
                    error=result.get("error") or "append_order_row failed",
                )
                telegram_send_text(bot_token, chat_id, "⚠️ No pude crear tu pedido en este momento. Intenta nuevamente.")
                return {"ok": True}

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
                alert_tenant_error(tenant_id=tenant_id, error="missing QR config")

            telegram_send_text(
                bot_token,
                chat_id,
                "📎 Cuando pagues, envía aquí tu *comprobante* (foto o PDF).\n"
                "Después de enviarlo, podrás presionar “✅ Ya pagué”.",
                parse_mode="Markdown",
            )
            return {"ok": True}

        telegram_send_text(bot_token, chat_id, "Usa /start para ver el menú.")
        return {"ok": True}

    except Exception as e:
        log_event(
            "client_message_error",
            tenant_id=tenant_id,
            chat_id=chat_id,
            error_type=type(e).__name__,
            error=str(e),
        )
        alert_system_error(error=str(e), module="client_message")
        telegram_send_text(bot_token, chat_id, "⚠️ Ocurrió un error. Intenta nuevamente.")
        return {"ok": True}
