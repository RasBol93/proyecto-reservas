# app/admin_messages.py

from typing import Any, Dict

from app.menu import (
    load_menu_admin_index,
    get_menu_product_or_404,
    set_menu_product_price,
    set_menu_product_name,
    set_menu_product_category,
    create_menu_product,
    invalidate_menu_cache,
)
from app.orders import (
    append_order_row,
    gen_order_id,
    build_items_snapshot,
)
from app.telegram_api import telegram_send_text, telegram_get_file_path, telegram_download_file_bytes
from app.utils import normalize, log_event
from app.stats import build_periods
from app.image_storage import upload_product_photo_for_tenant
from app.webhook_helpers import (
    get_sess,
    assert_admin_authorized,
    set_menu_photo_url,
    admin_periods_inline_kb,
    fmt_price_short,
    extract_first_number,
    fmt_snapshot_lines,
    build_order_recap_text,
)
from app.admin_hours import send_admin_hours_menu
from app.admin_menu import (
    send_admin_menu_home,
    send_admin_menu_product_detail,
)
from app.alerts import (
    alert_order_failed,
    alert_menu_error,
    alert_photo_upload_failed,
    alert_system_error,
)
from app.admin_consumers import _send_consumers_menu
from app.admin_manual_order import (
    _admin_order_reset,
    _send_admin_order_home,
    _admin_order_time_choice_kb,
)
from app.admin_nav import (
    admin_panel_kb,
)


def _finalize_admin_manual_order(
    tenant_id: str,
    bot_token: str,
    chat_id: int,
    orders_sh,
    tmp: Dict[str, Any],
) -> Dict[str, Any]:
    requested_time = str(tmp.get("admin_order_requested_time") or "").strip() or "ahora"
    cart = tmp.get("admin_order_cart") or []
    customer_name = str(tmp.get("admin_order_name") or "").strip()
    customer_contact = str(tmp.get("admin_order_contact") or "").strip()

    if not cart or not customer_name or not customer_contact:
        _admin_order_reset(tmp)
        telegram_send_text(
            bot_token,
            chat_id,
            "⚠️ Faltaban datos del pedido manual. Empecemos de nuevo.",
        )
        return {"ok": True}

    menu_idx = load_menu_admin_index(orders_sh, force=False)
    items_snapshot = build_items_snapshot(cart, menu_idx)
    lines_txt, total_amount, total_qty = fmt_snapshot_lines(items_snapshot)

    order_id = gen_order_id()

    result = append_order_row(
        orders_sh=orders_sh,
        tenant_id=tenant_id,
        order_id=order_id,
        customer_name=customer_name,
        customer_contact=customer_contact,
        customer_telegram_chat_id="",
        items=cart,
        items_snapshot=items_snapshot,
        currency="BOB",
        pricing_version="v1",
        notes="",
        delivery_type="pickup",
        requested_time=requested_time,
        status="PAID",
        source="admin_manual",
        total_amount=total_amount,
    )

    if not result.get("ok"):
        alert_order_failed(
            tenant_id=tenant_id,
            order_id=order_id,
            error=result.get("error") or "append_order_row failed",
        )
        telegram_send_text(
            bot_token,
            chat_id,
            "⚠️ Error guardando el pedido manual.",
        )
        return {"ok": True}

    _admin_order_reset(tmp)

    recap = build_order_recap_text(
        order_id=order_id,
        customer_name=customer_name,
        customer_contact=customer_contact,
        requested_time=requested_time,
        detail_lines=lines_txt,
        total_qty=total_qty,
        total=total_amount,
    )

    telegram_send_text(
        bot_token,
        chat_id,
        recap,
        parse_mode="Markdown",
    )
    telegram_send_text(
        bot_token,
        chat_id,
        "✅ *Pedido manual registrado como pagado.*\nYa cuenta para estadísticas y base de consumidores.",
        parse_mode="Markdown",
    )
    return {"ok": True}


def _reset_admin_menu_edit_state(tmp: Dict[str, Any]) -> None:
    tmp.pop("admin_menu_input_mode", None)
    tmp.pop("admin_menu_target_sku", None)
    tmp.pop("admin_menu_price_work", None)


def _reset_admin_menu_create_state(tmp: Dict[str, Any]) -> None:
    tmp.pop("admin_menu_create_step", None)
    tmp.pop("admin_menu_create_name", None)
    tmp.pop("admin_menu_create_category", None)
    tmp.pop("admin_menu_create_price", None)
    tmp.pop("admin_menu_create_sku", None)


def handle_admin_message_impl(
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
        txt_norm = normalize(text)
        sess = get_sess(tenant_id, chat_id)
        tmp = sess.setdefault("tmp", {})

        if txt_norm == "panel":
            assert_admin_authorized(tenant, chat_id, tenant_id)
            telegram_send_text(
                bot_token,
                chat_id,
                "🧭 PANEL ADMIN\n\nElige una opción:",
                reply_markup=admin_panel_kb(),
            )
            return {"ok": True}

        admin_order_step = str(tmp.get("admin_order_step") or "").strip()

        if admin_order_step:
            assert_admin_authorized(tenant, chat_id, tenant_id)

            if admin_order_step == "awaiting_name":
                customer_name = text.strip()
                if not customer_name:
                    telegram_send_text(
                        bot_token,
                        chat_id,
                        "El nombre no puede estar vacío. Escribe el nombre del cliente:",
                    )
                    return {"ok": True}

                tmp["admin_order_name"] = customer_name
                tmp["admin_order_step"] = "awaiting_contact"
                telegram_send_text(
                    bot_token,
                    chat_id,
                    "Escribe el contacto del cliente (teléfono o referencia):",
                )
                return {"ok": True}

            if admin_order_step == "awaiting_contact":
                customer_contact = text.strip()
                if not customer_contact:
                    telegram_send_text(
                        bot_token,
                        chat_id,
                        "El contacto no puede estar vacío. Escribe el contacto del cliente:",
                    )
                    return {"ok": True}

                tmp["admin_order_contact"] = customer_contact
                tmp["admin_order_step"] = "awaiting_time_choice"
                telegram_send_text(
                    bot_token,
                    chat_id,
                    "Elige cuándo se preparará el pedido:",
                    reply_markup=_admin_order_time_choice_kb(tenant_id),
                )
                return {"ok": True}

            if admin_order_step == "awaiting_time_manual":
                requested_time = text.strip()
                if not requested_time:
                    telegram_send_text(
                        bot_token,
                        chat_id,
                        "Escribe una hora válida.\nEjemplos: 19:30, 20h",
                    )
                    return {"ok": True}

                tmp["admin_order_requested_time"] = requested_time
                tmp["admin_order_step"] = "finalize_manual_order"
                return _finalize_admin_manual_order(
                    tenant_id=tenant_id,
                    bot_token=bot_token,
                    chat_id=chat_id,
                    orders_sh=orders_sh,
                    tmp=tmp,
                )

            if admin_order_step == "finalize_manual_order":
                return _finalize_admin_manual_order(
                    tenant_id=tenant_id,
                    bot_token=bot_token,
                    chat_id=chat_id,
                    orders_sh=orders_sh,
                    tmp=tmp,
                )

        input_mode = str(tmp.get("admin_menu_input_mode") or "").strip()
        target_sku = str(tmp.get("admin_menu_target_sku") or "").strip()
        create_step = str(tmp.get("admin_menu_create_step") or "").strip()

        # -------------------------
        # CREAR PRODUCTO
        # -------------------------
        if create_step:
            assert_admin_authorized(tenant, chat_id, tenant_id)

            if create_step == "name":
                product_name = text.strip()
                if not product_name:
                    telegram_send_text(
                        bot_token,
                        chat_id,
                        "El nombre no puede estar vacío. Escribe el nombre del producto:",
                    )
                    return {"ok": True}

                tmp["admin_menu_create_name"] = product_name
                tmp["admin_menu_create_step"] = "category"

                telegram_send_text(
                    bot_token,
                    chat_id,
                    "Escribe la categoría del producto.\nSi quieres usar una nueva, solo escríbela.",
                )
                return {"ok": True}

            if create_step == "category":
                category = text.strip()
                if not category:
                    telegram_send_text(
                        bot_token,
                        chat_id,
                        "La categoría no puede estar vacía. Escríbela:",
                    )
                    return {"ok": True}

                tmp["admin_menu_create_category"] = category
                tmp["admin_menu_create_step"] = "price"

                telegram_send_text(
                    bot_token,
                    chat_id,
                    "Escribe el precio del producto.\nEjemplos: 25, 25 bs",
                )
                return {"ok": True}

            if create_step == "price":
                n = extract_first_number(text)
                if n is None:
                    telegram_send_text(
                        bot_token,
                        chat_id,
                        "No pude leer un precio válido.\nEscribe algo como: 25 o 25 bs",
                    )
                    return {"ok": True}

                if n < 0:
                    telegram_send_text(
                        bot_token,
                        chat_id,
                        "El precio no puede ser negativo.",
                    )
                    return {"ok": True}

                created = create_menu_product(
                    orders_sh=orders_sh,
                    name=str(tmp.get("admin_menu_create_name") or "").strip(),
                    category=str(tmp.get("admin_menu_create_category") or "").strip(),
                    price=float(n),
                    active=True,
                    photo_url="",
                )

                sku = str(created.get("sku") or "").strip()

                tmp["admin_menu_create_sku"] = sku
                tmp["admin_menu_target_sku"] = sku
                tmp["admin_menu_create_step"] = "photo_optional"

                telegram_send_text(
                    bot_token,
                    chat_id,
                    (
                        "✅ Producto creado.\n\n"
                        f"Nombre: {created.get('name', '')}\n"
                        f"Categoría: {created.get('category', '')}\n"
                        f"Precio: Bs {fmt_price_short(created.get('price', 0))}\n\n"
                        "Ahora puedes enviar una foto del producto.\n"
                        "Si no quieres cargar foto ahora, escribe: omitir"
                    ),
                )
                return {"ok": True}

            if create_step == "photo_optional":
                if txt_norm in ("omitir", "saltar", "skip", "no"):
                    sku = str(tmp.get("admin_menu_create_sku") or "").strip()
                    _reset_admin_menu_create_state(tmp)
                    return {
                        "ok": send_admin_menu_product_detail(
                            bot_token, chat_id, tenant_id, orders_sh, sess, sku
                        )
                    }

                if msg.get("photo"):
                    sku = str(tmp.get("admin_menu_create_sku") or "").strip()
                    if not sku:
                        _reset_admin_menu_create_state(tmp)
                        telegram_send_text(
                            bot_token,
                            chat_id,
                            "⚠️ No encontré el producto recién creado. Volvamos al menú.",
                        )
                        return {"ok": send_admin_menu_home(bot_token, chat_id, tenant_id, orders_sh, sess)}

                    admin_file_id = msg["photo"][-1]["file_id"]

                    try:
                        admin_file_path = telegram_get_file_path(bot_token, admin_file_id)
                        file_bytes = telegram_download_file_bytes(bot_token, admin_file_path)

                        content_type = "image/jpeg"
                        low_path = admin_file_path.lower()
                        if low_path.endswith(".png"):
                            content_type = "image/png"
                        elif low_path.endswith(".webp"):
                            content_type = "image/webp"

                        photo_url = upload_product_photo_for_tenant(
                            tenant=tenant,
                            tenant_id=tenant_id,
                            sku=sku,
                            file_bytes=file_bytes,
                            mime_type=content_type,
                        )
                    except Exception as e:
                        telegram_send_text(
                            bot_token,
                            chat_id,
                            "No pude subir la foto al storage configurado.",
                        )
                        log_event("admin_product_create_photo_storage_upload_failed", tenant_id=tenant_id, sku=sku, error=str(e))
                        alert_photo_upload_failed(tenant_id=tenant_id, sku=sku, error=str(e))
                        return {"ok": True}

                    found = set_menu_photo_url(orders_sh, sku, photo_url)

                    if not found:
                        alert_menu_error(tenant_id=tenant_id, sku=sku, error="SKU not found in Menu for created product photo update")
                        telegram_send_text(
                            bot_token,
                            chat_id,
                            f"No encontré el producto SKU {sku} en la hoja Menu.",
                        )
                        return {"ok": True}

                    invalidate_menu_cache(orders_sh)
                    _reset_admin_menu_create_state(tmp)

                    telegram_send_text(
                        bot_token,
                        chat_id,
                        "✅ Foto guardada correctamente.",
                    )

                    return {
                        "ok": send_admin_menu_product_detail(
                            bot_token, chat_id, tenant_id, orders_sh, sess, sku
                        )
                    }

                telegram_send_text(
                    bot_token,
                    chat_id,
                    "Estoy esperando una foto del producto o que escribas 'omitir'.",
                )
                return {"ok": True}

        # -------------------------
        # FOTO PRODUCTO EXISTENTE
        # -------------------------
        if input_mode == "awaiting_photo" and target_sku:
            assert_admin_authorized(tenant, chat_id, tenant_id)

            if msg.get("photo"):
                admin_file_id = msg["photo"][-1]["file_id"]

                try:
                    admin_file_path = telegram_get_file_path(bot_token, admin_file_id)
                    file_bytes = telegram_download_file_bytes(bot_token, admin_file_path)

                    content_type = "image/jpeg"
                    low_path = admin_file_path.lower()
                    if low_path.endswith(".png"):
                        content_type = "image/png"
                    elif low_path.endswith(".webp"):
                        content_type = "image/webp"

                    photo_url = upload_product_photo_for_tenant(
                        tenant=tenant,
                        tenant_id=tenant_id,
                        sku=target_sku,
                        file_bytes=file_bytes,
                        mime_type=content_type,
                    )
                except Exception as e:
                    telegram_send_text(
                        bot_token,
                        chat_id,
                        "No pude subir la foto al storage configurado.",
                    )
                    log_event("admin_product_photo_storage_upload_failed", tenant_id=tenant_id, sku=target_sku, error=str(e))
                    alert_photo_upload_failed(tenant_id=tenant_id, sku=target_sku, error=str(e))
                    return {"ok": True}

                found = set_menu_photo_url(orders_sh, target_sku, photo_url)

                if not found:
                    alert_menu_error(tenant_id=tenant_id, sku=target_sku, error="SKU not found in Menu for photo update")
                    telegram_send_text(
                        bot_token,
                        chat_id,
                        f"No encontré el producto SKU {target_sku} en la hoja Menu.",
                    )
                    return {"ok": True}

                invalidate_menu_cache(orders_sh)

                _reset_admin_menu_edit_state(tmp)

                telegram_send_text(
                    bot_token,
                    chat_id,
                    "✅ Foto guardada correctamente y vinculada al producto.",
                )

                return {
                    "ok": send_admin_menu_product_detail(
                        bot_token, chat_id, tenant_id, orders_sh, sess, target_sku
                    )
                }

            telegram_send_text(
                bot_token,
                chat_id,
                "📷 Estoy esperando una foto del producto. Envíala como imagen de Telegram.",
            )
            return {"ok": True}

        # -------------------------
        # EDICIONES DE PRODUCTO EXISTENTE
        # -------------------------
        if input_mode and target_sku:
            assert_admin_authorized(tenant, chat_id, tenant_id)

            if input_mode == "price_final":
                item = get_menu_product_or_404(orders_sh, target_sku)
                n = extract_first_number(text)

                if n is None:
                    telegram_send_text(
                        bot_token,
                        chat_id,
                        "No pude leer un número válido.\nEscribe solo el precio o algo como: 25 bs",
                    )
                    return {"ok": True}

                if n < 0:
                    telegram_send_text(
                        bot_token,
                        chat_id,
                        "El precio no puede ser negativo. Intenta otra vez.",
                    )
                    return {"ok": True}

                result = set_menu_product_price(orders_sh, target_sku, float(n))
                _reset_admin_menu_edit_state(tmp)

                telegram_send_text(
                    bot_token,
                    chat_id,
                    (
                        "✅ Precio actualizado.\n"
                        f"Producto: {item.get('name', '')}\n"
                        f"Nuevo precio: Bs {fmt_price_short(result.get('price', 0))}"
                    ),
                )
                return {"ok": send_admin_menu_product_detail(bot_token, chat_id, tenant_id, orders_sh, sess, target_sku)}

            if input_mode == "edit_name":
                new_name = text.strip()
                if not new_name:
                    telegram_send_text(
                        bot_token,
                        chat_id,
                        "El nombre no puede estar vacío. Escríbelo otra vez:",
                    )
                    return {"ok": True}

                result = set_menu_product_name(orders_sh, target_sku, new_name)
                _reset_admin_menu_edit_state(tmp)

                telegram_send_text(
                    bot_token,
                    chat_id,
                    f"✅ Nombre actualizado.\nNuevo nombre: {result.get('name', '')}",
                )
                return {"ok": send_admin_menu_product_detail(bot_token, chat_id, tenant_id, orders_sh, sess, target_sku)}

            if input_mode == "new_category":
                new_category = text.strip()
                if not new_category:
                    telegram_send_text(
                        bot_token,
                        chat_id,
                        "La categoría no puede estar vacía. Escríbela otra vez:",
                    )
                    return {"ok": True}

                result = set_menu_product_category(orders_sh, target_sku, new_category)
                _reset_admin_menu_edit_state(tmp)

                telegram_send_text(
                    bot_token,
                    chat_id,
                    f"✅ Categoría actualizada.\nNueva categoría: {result.get('category', '')}",
                )
                return {"ok": send_admin_menu_product_detail(bot_token, chat_id, tenant_id, orders_sh, sess, target_sku)}

        if txt_norm in ("estadisticas", "/stats", "stats"):
            assert_admin_authorized(tenant, chat_id, tenant_id)
            periods = build_periods(tenant_tz)
            telegram_send_text(
                bot_token,
                chat_id,
                "📊 Elige el período:",
                reply_markup=admin_periods_inline_kb(tenant_id, periods),
            )
            telegram_send_text(bot_token, chat_id, "Escribe 'panel' cuando quieras volver al panel admin.")
            return {"ok": True}

        if (
            txt_norm in ("crear pedido", "crear pedido manual", "pedido manual", "nuevo pedido")
            or "crear pedido" in txt_norm
        ):
            assert_admin_authorized(tenant, chat_id, tenant_id)
            _admin_order_reset(tmp)
            tmp["admin_order_cart"] = []
            return {"ok": _send_admin_order_home(bot_token, chat_id, tenant_id, orders_sh, sess)}

        if txt_norm in (
            "base de consumidores",
            "consumidores",
            "clientes",
            "base consumidores",
        ):
            assert_admin_authorized(tenant, chat_id, tenant_id)
            return {"ok": _send_consumers_menu(bot_token, chat_id, tenant_id)}

        if txt_norm in (
            "config dias y horarios",
            "dias y horarios",
            "configuracion dias y horarios",
            "configuracion de dias y horarios",
        ):
            assert_admin_authorized(tenant, chat_id, tenant_id)
            return {"ok": send_admin_hours_menu(bot_token, chat_id, tenant_id, orders_sh, tenant_tz)}

        if txt_norm in (
            "config menu y precios",
            "menu y precios",
            "configuracion menu y precios",
            "configuracion de menu y precios",
        ):
            assert_admin_authorized(tenant, chat_id, tenant_id)
            return {"ok": send_admin_menu_home(bot_token, chat_id, tenant_id, orders_sh, sess)}

        if txt_norm in ("start", "/start", "hola"):
            telegram_send_text(
                bot_token,
                chat_id,
                "Admin bot listo ✅\n\nEscribe 'panel' para abrir el panel admin.",
            )
            return {"ok": True}

        telegram_send_text(
            bot_token,
            chat_id,
            "OK admin ✅\n\nEscribe 'panel' para abrir el panel admin.",
        )
        return {"ok": True}

    except Exception as e:
        log_event(
            "admin_message_error",
            tenant_id=tenant_id,
            chat_id=chat_id,
            error_type=type(e).__name__,
            error=str(e),
        )
        alert_system_error(error=str(e), module="admin_message")
        telegram_send_text(
            bot_token,
            chat_id,
            "⚠️ Ocurrió un error en el panel admin.",
        )
        return {"ok": True}
