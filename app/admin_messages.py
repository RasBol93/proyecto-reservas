# app/admin_messages.py

from typing import Any, Dict

from app.menu import (
    load_menu_admin_index,
    get_menu_product_or_404,
    set_menu_product_price,
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
    admin_fixed_kb,
    admin_periods_inline_kb,
    fmt_price_short,
    extract_first_number,
    fmt_snapshot_lines,
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
)


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

        admin_order_step = str(tmp.get("admin_order_step") or "").strip()

        if admin_order_step:
            assert_admin_authorized(tenant, chat_id, tenant_id)

            if admin_order_step == "awaiting_name":
                customer_name = text.strip()
                if not customer_name:
                    telegram_send_text(bot_token, chat_id, "El nombre no puede estar vacío. Escribe el nombre del cliente:")
                    return {"ok": True}

                tmp["admin_order_name"] = customer_name
                tmp["admin_order_step"] = "awaiting_contact"
                telegram_send_text(bot_token, chat_id, "Escribe el contacto del cliente (teléfono o referencia):")
                return {"ok": True}

            if admin_order_step == "awaiting_contact":
                customer_contact = text.strip()
                if not customer_contact:
                    telegram_send_text(bot_token, chat_id, "El contacto no puede estar vacío. Escribe el contacto del cliente:")
                    return {"ok": True}

                tmp["admin_order_contact"] = customer_contact
                tmp["admin_order_step"] = "awaiting_time"
                telegram_send_text(
                    bot_token,
                    chat_id,
                    "Escribe la hora solicitada.\nEjemplos: ahora, 19:30, 20h",
                )
                return {"ok": True}

            if admin_order_step == "awaiting_time":
                requested_time = text.strip()
                if not requested_time:
                    requested_time = "ahora"

                cart = tmp.get("admin_order_cart") or []
                customer_name = str(tmp.get("admin_order_name") or "").strip()
                customer_contact = str(tmp.get("admin_order_contact") or "").strip()

                if not cart or not customer_name or not customer_contact:
                    _admin_order_reset(tmp)
                    telegram_send_text(
                        bot_token,
                        chat_id,
                        "⚠️ Faltaban datos del pedido manual. Empecemos de nuevo.",
                        reply_markup=admin_fixed_kb(),
                    )
                    return {"ok": True}

                menu_idx = load_menu_admin_index(orders_sh, force=False)
                items_snapshot = build_items_snapshot(cart, menu_idx)
                _, total_amount, _total_qty = fmt_snapshot_lines(items_snapshot)

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
                        reply_markup=admin_fixed_kb(),
                    )
                    return {"ok": True}

                _admin_order_reset(tmp)

                telegram_send_text(
                    bot_token,
                    chat_id,
                    (
                        "✅ PEDIDO MANUAL REGISTRADO\n\n"
                        f"Código de pedido: {order_id}\n"
                        f"Cliente: {customer_name}\n"
                        f"Teléfono: {customer_contact}\n"
                        f"Hora: {requested_time}\n"
                        f"Total: Bs {total_amount:.2f}\n\n"
                        "Se guardó como confirmado y ya cuenta para estadísticas."
                    ),
                    reply_markup=admin_fixed_kb(),
                )
                return {"ok": True}

        input_mode = str(tmp.get("admin_menu_input_mode") or "").strip()
        input_sku = str(tmp.get("admin_menu_price_sku") or "").strip()

        if input_mode == "awaiting_photo" and input_sku:
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
                        sku=input_sku,
                        file_bytes=file_bytes,
                        mime_type=content_type,
                    )
                except Exception as e:
                    telegram_send_text(bot_token, chat_id, "No pude subir la foto al storage configurado.")
                    log_event("admin_product_photo_storage_upload_failed", tenant_id=tenant_id, sku=input_sku, error=str(e))
                    alert_photo_upload_failed(tenant_id=tenant_id, sku=input_sku, error=str(e))
                    return {"ok": True}

                found = set_menu_photo_url(orders_sh, input_sku, photo_url)

                if not found:
                    alert_menu_error(tenant_id=tenant_id, sku=input_sku, error="SKU not found in Menu for photo update")
                    telegram_send_text(bot_token, chat_id, f"No encontré el producto SKU {input_sku} en la hoja Menu.")
                    return {"ok": True}

                invalidate_menu_cache(orders_sh)

                tmp.pop("admin_menu_input_mode", None)
                tmp.pop("admin_menu_price_sku", None)

                telegram_send_text(
                    bot_token,
                    chat_id,
                    "✅ Foto guardada correctamente y vinculada al producto.",
                )

                return {
                    "ok": send_admin_menu_product_detail(
                        bot_token, chat_id, tenant_id, orders_sh, sess, input_sku
                    )
                }

            telegram_send_text(
                bot_token,
                chat_id,
                "📷 Estoy esperando una foto del producto. Envíala como imagen de Telegram.",
            )
            return {"ok": True}

        if input_mode and input_sku:
            assert_admin_authorized(tenant, chat_id, tenant_id)

            item = get_menu_product_or_404(orders_sh, input_sku)
            current_price = float(item.get("price", 0.0))
            n = extract_first_number(text)

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
                    f"✅ Precio actualizado.\nSKU: {input_sku}\nNuevo precio: Bs {fmt_price_short(result.get('price', 0))}",
                )
                return {"ok": send_admin_menu_product_detail(bot_token, chat_id, tenant_id, orders_sh, sess, input_sku)}

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
                        f"Precio anterior: Bs {fmt_price_short(current_price)}\n"
                        f"Nuevo precio: Bs {fmt_price_short(result.get('price', 0))}"
                    ),
                )
                return {"ok": send_admin_menu_product_detail(bot_token, chat_id, tenant_id, orders_sh, sess, input_sku)}

        if txt_norm in ("estadisticas", "/stats", "stats"):
            assert_admin_authorized(tenant, chat_id, tenant_id)
            periods = build_periods(tenant_tz)
            telegram_send_text(
                bot_token,
                chat_id,
                "📊 Elige el período:",
                reply_markup=admin_periods_inline_kb(tenant_id, periods),
            )
            telegram_send_text(bot_token, chat_id, "Panel admin:", reply_markup=admin_fixed_kb())
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
            telegram_send_text(bot_token, chat_id, "Admin bot listo ✅", reply_markup=admin_fixed_kb())
            return {"ok": True}

        telegram_send_text(bot_token, chat_id, "OK admin ✅", reply_markup=admin_fixed_kb())
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
        telegram_send_text(bot_token, chat_id, "⚠️ Ocurrió un error en el panel admin.", reply_markup=admin_fixed_kb())
        return {"ok": True}
