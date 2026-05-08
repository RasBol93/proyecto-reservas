# app/admin_messages_menu.py

from typing import Any, Dict, Optional

from app.menu import (
    get_menu_product_or_404,
    set_menu_product_price,
    invalidate_menu_cache,
    set_menu_product_name,
    set_menu_product_category,
    create_menu_product,
    get_menu_categories,
)
from app.telegram_api import telegram_send_text, telegram_get_file_path, telegram_download_file_bytes
from app.telegram_keyboard import kb
from app.utils import log_event, normalize
from app.image_storage import upload_product_photo_for_tenant
from app.webhook_helpers import (
    assert_admin_authorized,
    set_menu_photo_url,
    fmt_price_short,
    extract_first_number,
)
from app.admin_menu import send_admin_menu_home, send_admin_menu_product_detail
from app.admin_callbacks_menu import clear_admin_menu_order_state
from app.alerts import (
    alert_menu_error,
    alert_photo_upload_failed,
)


def handle_admin_menu_message(
    tenant: Dict[str, Any],
    tenant_id: str,
    bot_token: str,
    chat_id: int,
    msg: Dict[str, Any],
    orders_sh,
    sess: Dict[str, Any],
) -> Optional[Dict[str, Any]]:
    tmp = sess.setdefault("tmp", {})
    text = (msg.get("text") or "").strip()
    txt_norm = normalize(text)

    if tmp.get("admin_menu_order_categories"):
        if txt_norm in ("volver", "cancelar"):
            clear_admin_menu_order_state(tmp)
            return {"ok": send_admin_menu_home(bot_token, chat_id, tenant_id, orders_sh, sess)}
        if txt_norm in ("panel", "âš™ï¸panel", "âš™ï¸ panel"):
            clear_admin_menu_order_state(tmp)
            return None

    input_mode = str(tmp.get("admin_menu_input_mode") or "").strip()
    input_sku = str(tmp.get("admin_menu_price_sku") or "").strip()
    create_step = str(tmp.get("admin_menu_create_step") or "").strip()

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
            tmp["admin_menu_create_step"] = "awaiting_category_selection"

            categories = get_menu_categories(orders_sh)
            tmp["admin_menu_category_options"] = categories

            rows = []
            for i, cat in enumerate(categories[:20]):
                rows.append([(f"📂 {cat}", f"admmenu|{tenant_id}|create_setcat|{i}")])

            rows.append([("➕ Nueva categoría", f"admmenu|{tenant_id}|create_newcat")])

            telegram_send_text(
                bot_token,
                chat_id,
                "Elige una categoría para el producto:",
                reply_markup=kb(rows),
            )
            return {"ok": True}

        if create_step == "awaiting_category_selection":
            telegram_send_text(
                bot_token,
                chat_id,
                "Selecciona una categoría usando los botones o toca 'Nueva categoría'.",
                reply_markup=kb([
                    [("➕ Nueva categoría", f"admmenu|{tenant_id}|create_newcat")],
                    [("🧭 Panel admin", "admin_panel")],
                ]),
            )
            return {"ok": True}

        if create_step == "new_category_for_create":
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
                    "El precio no puede ser negativo. Intenta otra vez.",
                )
                return {"ok": True}

            result = create_menu_product(
                orders_sh=orders_sh,
                name=str(tmp.get("admin_menu_create_name") or "").strip(),
                category=str(tmp.get("admin_menu_create_category") or "").strip(),
                price=float(n),
                active=True,
                photo_url="",
            )

            created_sku = str(result.get("sku") or "").strip()

            tmp.pop("admin_menu_create_step", None)
            tmp.pop("admin_menu_create_name", None)
            tmp.pop("admin_menu_create_category", None)
            tmp.pop("admin_menu_create_price", None)
            tmp.pop("admin_menu_category_options", None)

            tmp["admin_menu_input_mode"] = "awaiting_photo"
            tmp["admin_menu_price_sku"] = created_sku

            telegram_send_text(
                bot_token,
                chat_id,
                (
                    "✅ Producto creado correctamente.\n\n"
                    f"Nombre: {result.get('name', '')}\n"
                    f"Categoría: {result.get('category', '')}\n"
                    f"Precio: Bs {fmt_price_short(result.get('price', 0))}\n\n"
                    "Ahora puedes enviar una foto del producto."
                ),
            )
            return {"ok": True}

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
                telegram_send_text(
                    bot_token,
                    chat_id,
                    "No pude subir la foto al storage configurado.",
                )
                log_event("admin_product_photo_storage_upload_failed", tenant_id=tenant_id, sku=input_sku, error=str(e))
                alert_photo_upload_failed(tenant_id=tenant_id, sku=input_sku, error=str(e))
                return {"ok": True}

            found = set_menu_photo_url(orders_sh, input_sku, photo_url)

            if not found:
                alert_menu_error(tenant_id=tenant_id, sku=input_sku, error="SKU not found in Menu for photo update")
                telegram_send_text(
                    bot_token,
                    chat_id,
                    f"No encontré el producto SKU {input_sku} en la hoja Menu.",
                )
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

        if input_mode == "edit_name":
            new_name = text.strip()
            if not new_name:
                telegram_send_text(
                    bot_token,
                    chat_id,
                    "El nombre no puede estar vacío. Escribe el nuevo nombre del producto:",
                )
                return {"ok": True}

            result = set_menu_product_name(orders_sh, input_sku, new_name)
            tmp.pop("admin_menu_input_mode", None)
            tmp.pop("admin_menu_price_sku", None)
            tmp.pop("admin_menu_price_work", None)

            telegram_send_text(
                bot_token,
                chat_id,
                f"✅ Nombre actualizado.\nNuevo nombre: {result.get('name', '')}",
            )
            return {"ok": send_admin_menu_product_detail(bot_token, chat_id, tenant_id, orders_sh, sess, input_sku)}

        if input_mode == "new_category":
            new_category = text.strip()
            if not new_category:
                telegram_send_text(
                    bot_token,
                    chat_id,
                    "La categoría no puede estar vacía. Escríbela otra vez:",
                )
                return {"ok": True}

            result = set_menu_product_category(orders_sh, input_sku, new_category)
            tmp.pop("admin_menu_input_mode", None)
            tmp.pop("admin_menu_price_sku", None)
            tmp.pop("admin_menu_price_work", None)
            tmp.pop("admin_menu_category_options", None)

            telegram_send_text(
                bot_token,
                chat_id,
                f"✅ Categoría actualizada.\nNueva categoría: {result.get('category', '')}",
            )
            return {"ok": send_admin_menu_product_detail(bot_token, chat_id, tenant_id, orders_sh, sess, input_sku)}

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
                telegram_send_text(
                    bot_token,
                    chat_id,
                    "El precio no puede ser negativo. Intenta otra vez.",
                )
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
                telegram_send_text(
                    bot_token,
                    chat_id,
                    "El descuento no puede ser negativo. Intenta otra vez.",
                )
                return {"ok": True}
            if n > 100:
                telegram_send_text(
                    bot_token,
                    chat_id,
                    "El descuento no puede ser mayor a 100%. Intenta otra vez.",
                )
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

    return None
