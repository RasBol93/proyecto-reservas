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


def client_orders_allowed_or_notify(bot_token: str, chat_id: int, orders_sh, tenant_tz: str) -> bool:
    bs = get_business_status_safe(orders_sh=orders_sh, tenant_tz=tenant_tz)
    if bool(bs.get("accepts_orders_now")):
        return True
    telegram_send_text(bot_token, chat_id, send_business_blocked_text(bs))
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
    sess = get_sess(tenant_id, chat_id)
    tmp = sess.get("tmp") or {}
    sess["tmp"] = tmp

    if data == "home":
        telegram_send_text(bot_token, chat_id, "Elige una opción:", client_home_kb())
        return {"ok": True}

    if data == "menu":
        if not client_orders_allowed_or_notify(bot_token, chat_id, orders_sh, tenant_tz):
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
        if not client_orders_allowed_or_notify(bot_token, chat_id, orders_sh, tenant_tz):
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
        if not client_orders_allowed_or_notify(bot_token, chat_id, orders_sh, tenant_tz):
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

        link = contact_link_for_admin(tenant)
        if not link:
            telegram_send_text(bot_token, chat_id, "No tengo configurado el contacto directo del administrador.", reply_markup=client_home_kb())
            return {"ok": True}

        telegram_send_text(bot_token, chat_id, "💬 Contacto directo habilitado.\nToca el enlace para escribirle al administrador:")
        telegram_send_text(bot_token, chat_id, link)
        return {"ok": True}

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

        bs = get_business_status_safe(orders_sh=orders_sh, tenant_tz=tenant_tz)
        if not bool(bs.get("accepts_orders_now")):
            telegram_send_text(bot_token, chat_id, send_business_blocked_text(bs))
            return {"ok": True}

        telegram_send_text(bot_token, chat_id, "Bienvenido 👋\nElige una opción:", client_home_kb())
        return {"ok": True}

    if sess.get("stage") == "awaiting_name":
        if not client_orders_allowed_or_notify(bot_token, chat_id, orders_sh, tenant_tz):
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
