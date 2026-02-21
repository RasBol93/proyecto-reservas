# app/telegram_webhook.py
from typing import Any, Dict, List, Optional, Tuple

from fastapi import APIRouter, HTTPException

from app.config import MAX_NAME_LEN
from app.utils import normalize, log_event
from app.sheets import get_gspread_client
from app.tenants import get_tenant_or_404
from app.sheets import open_orders_spreadsheet
from app.menu import load_menu_index, group_menu_by_category
from app.contentfaq import load_content_map, load_faq_list
from app.orders_repo import (
    gen_order_id,
    append_order_row,
    update_order_status,
    calc_total_amount,
    validate_tenant_id,
    validate_order_id,
    validate_contact,
    validate_requested_time,
)
from app.telegram_api import (
    telegram_send_text,
    telegram_answer_callback,
    send_order_to_admin_telegram,
)
from app.telegram_state import (
    get_client_state,
    cart_add,
    cart_clear,
    cart_text_and_total,
)
from app.tenants import resolve_bot_by_secret

router = APIRouter()


def kb(rows: List[List[Tuple[str, str]]]) -> Dict[str, Any]:
    return {"inline_keyboard": [[{"text": t, "callback_data": c} for (t, c) in row] for row in rows]}


def main_menu_kb() -> Dict[str, Any]:
    return kb([
        [("📋 Ver Menú", "menu")],
        [("📍 Ver Ubicación", "loc")],
        [("❓ Preguntas Frecuentes", "faq")],
        [("🛒 Carrito", "cart")],
    ])


@router.post("/telegram/webhook/{tenant_id}/{secret}")
async def telegram_webhook(tenant_id: str, secret: str, update: Dict[str, Any]):
    validate_tenant_id(tenant_id)

    gc = get_gspread_client()
    tenant = get_tenant_or_404(gc, tenant_id)
    mode, bot_token = resolve_bot_by_secret(tenant, secret)
    if not bot_token:
        log_event("telegram_missing_bot_token", tenant_id=tenant_id, mode=mode)
        return {"ok": True}

    orders_sh = open_orders_spreadsheet(gc, tenant)

    # -------------------------
    # 1) Callback query
    # -------------------------
    cb = update.get("callback_query")
    if cb:
        data = (cb.get("data") or "").strip()
        chat_id = int(cb["message"]["chat"]["id"])
        cb_id = cb.get("id")

        # ACK rápido
        if cb_id:
            telegram_answer_callback(bot_token, cb_id, "OK")

        # ADMIN: paid|tenant|order_id
        if mode == "admin":
            from_user = cb.get("from") or {}
            from_id = str(from_user.get("id", "")).strip()
            expected_admin_chat_id = str(tenant.get("admin_chat_id", "")).strip()
            if expected_admin_chat_id and from_id != expected_admin_chat_id:
                raise HTTPException(status_code=403, detail="Not allowed")

            parts = data.split("|")
            if len(parts) == 3 and parts[0] == "paid":
                cb_tenant_id = parts[1].strip()
                order_id = parts[2].strip().lower()
                if cb_tenant_id != tenant_id:
                    raise HTTPException(status_code=400, detail="Tenant mismatch in callback data")
                validate_order_id(order_id)

                result = update_order_status(orders_sh, order_id, "PAID")
                if not result.get("found"):
                    if cb_id:
                        telegram_answer_callback(bot_token, cb_id, "⚠️ No encontré ese pedido")
                    return {"ok": True}

                old_status = str(result.get("old_status", "") or "")
                already_paid = normalize(old_status) == "paid"
                if cb_id:
                    telegram_answer_callback(
                        bot_token,
                        cb_id,
                        "✅ Marcado como PAID" if not already_paid else "✅ Ya estaba PAID",
                    )
                return {"ok": True}

            return {"ok": True}

        # CLIENT callbacks
        if mode == "client":
            state = get_client_state(tenant_id, chat_id)
            menu_idx = load_menu_index(orders_sh)
            cats = group_menu_by_category(menu_idx)

            # HOME shortcuts
            if data in ("home", "menu", "loc", "faq", "cart"):
                if data == "home":
                    state["step"] = "HOME"
                    telegram_send_text(bot_token, chat_id, "Elige una opción:", main_menu_kb())
                    return {"ok": True}

                if data == "menu":
                    if not cats:
                        telegram_send_text(bot_token, chat_id, "No hay menú activo.", main_menu_kb())
                        return {"ok": True}

                    rows = []
                    for c in sorted(cats.keys(), key=lambda x: normalize(x)):
                        rows.append([(c, f"cat|{normalize(c)}")])
                    rows.append([("⬅️ Volver", "home"), ("🛒 Carrito", "cart")])

                    state["step"] = "PICK_CAT"
                    telegram_send_text(bot_token, chat_id, "📋 Elige una categoría:", kb(rows))
                    return {"ok": True}

                if data == "loc":
                    try:
                        content = load_content_map(orders_sh)
                        text = content.get("location_text", "Ubicación no configurada.")
                        maps = content.get("location_maps_url", "")
                        if maps:
                            text = f"{text}\n\n🗺 {maps}"
                        telegram_send_text(bot_token, chat_id, text, main_menu_kb())
                    except Exception:
                        telegram_send_text(bot_token, chat_id, "Ubicación no disponible (Content no configurado).", main_menu_kb())
                    return {"ok": True}

                if data == "faq":
                    try:
                        faqs = load_faq_list(orders_sh)
                        if not faqs:
                            telegram_send_text(bot_token, chat_id, "No hay FAQs activas.", main_menu_kb())
                            return {"ok": True}

                        rows = []
                        for f in faqs[:10]:
                            rows.append([(f["question"], f"faq|{f['id']}")])
                        rows.append([("⬅️ Volver", "home")])
                        telegram_send_text(bot_token, chat_id, "❓ Preguntas frecuentes:", kb(rows))
                    except Exception:
                        telegram_send_text(bot_token, chat_id, "FAQs no disponibles (FAQ no configurado).", main_menu_kb())
                    return {"ok": True}

                if data == "cart":
                    text, _ = cart_text_and_total(state, menu_idx)
                    rows = []
                    if state["cart"]:
                        rows.append([("✅ Confirmar pedido", "confirm"), ("🗑 Vaciar", "clear")])
                        rows.append([("➕ Seguir comprando", "menu")])
                    rows.append([("⬅️ Volver", "home")])
                    telegram_send_text(bot_token, chat_id, text, kb(rows))
                    return {"ok": True}

            # FAQ answer
            if data.startswith("faq|"):
                fid = data.split("|", 1)[1].strip()
                faqs = load_faq_list(orders_sh)
                found = next((f for f in faqs if f["id"] == fid), None)
                if not found:
                    telegram_send_text(bot_token, chat_id, "FAQ no encontrada.", main_menu_kb())
                    return {"ok": True}
                telegram_send_text(bot_token, chat_id, f"❓ {found['question']}\n\n{found['answer']}", main_menu_kb())
                return {"ok": True}

            # category -> products
            if data.startswith("cat|"):
                cat_norm = data.split("|", 1)[1].strip()
                real_cat = None
                for c in cats.keys():
                    if normalize(c) == cat_norm:
                        real_cat = c
                        break
                if not real_cat:
                    telegram_send_text(bot_token, chat_id, "Categoría no encontrada.", main_menu_kb())
                    return {"ok": True}

                items = cats.get(real_cat, [])
                if not items:
                    telegram_send_text(bot_token, chat_id, "No hay productos activos.", main_menu_kb())
                    return {"ok": True}

                rows = []
                for it in items[:20]:
                    label = f"{it['name']} ({it['price']:.0f})"
                    rows.append([(label, f"prd|{it['sku']}")])
                rows.append([("⬅️ Categorías", "menu"), ("🛒 Carrito", "cart")])

                state["step"] = "PICK_PROD"
                state["selected_cat"] = real_cat
                telegram_send_text(bot_token, chat_id, f"🍽 {real_cat} — elige un producto:", kb(rows))
                return {"ok": True}

            # product -> qty
            if data.startswith("prd|"):
                sku = data.split("|", 1)[1].strip()
                if sku not in menu_idx:
                    telegram_send_text(bot_token, chat_id, "Producto no disponible.", main_menu_kb())
                    return {"ok": True}

                state["pending_sku"] = sku
                p = menu_idx[sku]
                rows = [
                    [("1", f"qty|{sku}|1"), ("2", f"qty|{sku}|2"), ("3", f"qty|{sku}|3"), ("4", f"qty|{sku}|4")],
                    [("⬅️ Volver", "menu"), ("🛒 Carrito", "cart")],
                ]
                state["step"] = "PICK_QTY"
                telegram_send_text(bot_token, chat_id, f"🧮 Cantidad para: {p['name']} ({p['price']:.0f} BOB)", kb(rows))
                return {"ok": True}

            # qty -> add cart
            if data.startswith("qty|"):
                parts = data.split("|")
                if len(parts) != 3:
                    return {"ok": True}
                sku = parts[1].strip()
                qty = int(parts[2])

                if sku not in menu_idx:
                    telegram_send_text(bot_token, chat_id, "Producto no disponible.", main_menu_kb())
                    return {"ok": True}

                cart_add(state, sku, qty)
                p = menu_idx[sku]
                rows = [
                    [("➕ Seguir comprando", "menu")],
                    [("🛒 Ver carrito", "cart")],
                    [("⬅️ Inicio", "home")],
                ]
                telegram_send_text(bot_token, chat_id, f"✅ Agregado: {p['name']} x{qty}", kb(rows))
                return {"ok": True}

            # clear cart
            if data == "clear":
                cart_clear(state)
                telegram_send_text(bot_token, chat_id, "Carrito vaciado.", main_menu_kb())
                return {"ok": True}

            # confirm -> ask name
            if data == "confirm":
                if not state["cart"]:
                    telegram_send_text(bot_token, chat_id, "Tu carrito está vacío.", main_menu_kb())
                    return {"ok": True}
                state["step"] = "ASK_NAME"
                telegram_send_text(bot_token, chat_id, "Por favor escribe tu *nombre* (solo texto):")
                return {"ok": True}

            return {"ok": True}

        return {"ok": True}

    # -------------------------
    # 2) Mensaje normal (admin/client)
    # -------------------------
    msg = update.get("message") or update.get("edited_message")
    if not msg:
        return {"ok": True}

    chat = msg.get("chat") or {}
    chat_id = chat.get("id")
    text_in = (msg.get("text") or "").strip()
    if chat_id is None:
        return {"ok": True}
    chat_id_int = int(chat_id)

    # Admin bot: debug mínimo
    if mode == "admin":
        expected_admin_chat_id = str(tenant.get("admin_chat_id", "")).strip()
        if expected_admin_chat_id and str(chat_id_int) != expected_admin_chat_id:
            return {"ok": True}
        if text_in:
            telegram_send_text(bot_token, chat_id_int, "OK admin ✅")
        return {"ok": True}

    # Client bot: /start o flujo de captura (nombre/hora)
    state = get_client_state(tenant_id, chat_id_int)

    if normalize(text_in) in ("start", "/start", "hola"):
        state["step"] = "HOME"
        try:
            content = load_content_map(orders_sh)
            welcome = content.get("welcome_text", "Bienvenido. Elige una opción:")
        except Exception:
            welcome = "Bienvenido. Elige una opción:"
        telegram_send_text(bot_token, chat_id_int, welcome, main_menu_kb())
        return {"ok": True}

    # Captura nombre
    if state["step"] == "ASK_NAME":
        name = text_in.strip()
        if not name or len(name) > MAX_NAME_LEN:
            telegram_send_text(bot_token, chat_id_int, "Nombre inválido. Intenta nuevamente:")
            return {"ok": True}
        state["customer_name"] = name
        state["step"] = "ASK_TIME"
        telegram_send_text(bot_token, chat_id_int, "¿A qué hora deseas recoger? (ej: ahora, 19:30)")
        return {"ok": True}

    # Captura hora y crea orden
    if state["step"] == "ASK_TIME":
        requested = validate_requested_time(text_in)
        state["requested_time"] = requested

        menu_idx = load_menu_index(orders_sh)
        if not state["cart"]:
            state["step"] = "HOME"
            telegram_send_text(bot_token, chat_id_int, "Tu carrito está vacío.", main_menu_kb())
            return {"ok": True}

        total_amount = calc_total_amount(state["cart"], menu_idx)
        order_id = gen_order_id()

        customer_contact = str(chat_id_int)  # por ahora
        validate_contact(customer_contact)

        append_order_row(
            orders_sh=orders_sh,
            tenant_id=tenant_id,
            order_id=order_id,
            customer_name=state["customer_name"],
            customer_contact=customer_contact,
            items=state["cart"],
            delivery_type="pickup",
            requested_time=state["requested_time"],
            status="PENDING_PAYMENT",
            source="telegram",
            total_amount=total_amount,
        )

        try:
            send_order_to_admin_telegram(
                tenant=tenant,
                order_id=order_id,
                customer_name=state["customer_name"],
                customer_contact=customer_contact,
                items_list=state["cart"],
                total_amount=total_amount,
                requested_time=state["requested_time"],
            )
        except Exception as e:
            log_event("telegram_send_exception", tenant_id=tenant_id, order_id=order_id, error=str(e))

        # reset
        cart_clear(state)
        state["customer_name"] = ""
        state["requested_time"] = ""
        state["step"] = "HOME"

        telegram_send_text(
            bot_token,
            chat_id_int,
            f"✅ Pedido creado\nID: {order_id}\nTotal: {total_amount:.2f} BOB\nEstado: PENDING_PAYMENT",
            main_menu_kb(),
        )
        return {"ok": True}

    # fallback mínimo
    telegram_send_text(bot_token, chat_id_int, "Usa /start para ver el menú.", main_menu_kb())
    return {"ok": True}
