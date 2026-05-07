from typing import Any, Dict, Optional

from fastapi import HTTPException

from app.menu import (
    load_menu_admin_index,
    group_menu_admin_by_category,
    get_menu_product_or_404,
    set_menu_product_active,
    set_menu_product_price,
    set_menu_product_category,
    get_menu_categories,
    invalidate_menu_cache,
)
from app.telegram_api import telegram_send_text
from app.telegram_keyboard import kb
from app.utils import normalize
from app.webhook_helpers import (
    get_sess,
    assert_admin_authorized,
    fmt_price_short,
)
from app.admin_menu import (
    send_admin_menu_home,
    send_admin_menu_category,
    send_admin_menu_product_detail,
    send_admin_menu_price_editor,
    apply_price_delta,
)
from app.admin_nav import admin_panel_kb


def _clear_admin_menu_session_cache(tmp: Dict[str, Any]) -> None:
    tmp.pop("admin_menu_cache", None)


def handle_admin_menu_callback(
    tenant: Dict[str, Any],
    tenant_id: str,
    bot_token: str,
    chat_id: int,
    data: str,
    orders_sh,
    get_effective_admin_role,
) -> Optional[Dict[str, Any]]:
    if not data.startswith("admmenu|"):
        return None

    assert_admin_authorized(tenant, chat_id, tenant_id)

    parts = data.split("|")
    if len(parts) < 3:
        return {"ok": True}

    cb_tenant_id = parts[1].strip()
    if cb_tenant_id != tenant_id:
        raise HTTPException(status_code=400, detail="Tenant mismatch in admin menu callback")

    action = parts[2].strip()
    sess = get_sess(tenant_id, chat_id)
    tmp = sess.setdefault("tmp", {})

    if action == "panel":
        tmp.pop("admin_menu_categories", None)
        tmp.pop("admin_menu_current_category", None)
        tmp.pop("admin_menu_last_sku", None)
        tmp.pop("admin_menu_price_sku", None)
        tmp.pop("admin_menu_price_work", None)
        tmp.pop("admin_menu_input_mode", None)
        tmp.pop("admin_menu_category_options", None)
        tmp.pop("admin_menu_create_step", None)
        tmp.pop("admin_menu_create_name", None)
        tmp.pop("admin_menu_create_category", None)
        tmp.pop("admin_menu_create_price", None)
        _clear_admin_menu_session_cache(tmp)

        user_role = get_effective_admin_role(tenant, chat_id)
        telegram_send_text(
            bot_token,
            chat_id,
            "🧭 PANEL ADMIN\n\nElige una opción:",
            reply_markup=admin_panel_kb(user_role=user_role),
        )
        return {"ok": True}

    if action == "home":
        _clear_admin_menu_session_cache(tmp)
        return {"ok": send_admin_menu_home(bot_token, chat_id, tenant_id, orders_sh, sess)}

    if action == "refresh":
        invalidate_menu_cache(orders_sh)
        _clear_admin_menu_session_cache(tmp)
        telegram_send_text(
            bot_token,
            chat_id,
            "✅ Menú refrescado.",
        )
        return {"ok": send_admin_menu_home(bot_token, chat_id, tenant_id, orders_sh, sess)}

    if action == "catrefresh":
        invalidate_menu_cache(orders_sh)
        _clear_admin_menu_session_cache(tmp)
        current_category = str(tmp.get("admin_menu_current_category") or "").strip()
        if not current_category:
            return {"ok": send_admin_menu_home(bot_token, chat_id, tenant_id, orders_sh, sess)}

        telegram_send_text(
            bot_token,
            chat_id,
            "✅ Categoría refrescada.",
        )
        return {"ok": send_admin_menu_category(bot_token, chat_id, tenant_id, orders_sh, sess, current_category)}

    if action == "cat" and len(parts) == 4:
        try:
            idx = int(parts[3].strip())
        except Exception:
            idx = -1

        _clear_admin_menu_session_cache(tmp)
        menu_idx = load_menu_admin_index(orders_sh, force=True)
        cats = group_menu_admin_by_category(menu_idx, orders_sh=orders_sh)
        cat_names = list(cats.keys())

        if idx < 0 or idx >= len(cat_names):
            return {"ok": send_admin_menu_home(bot_token, chat_id, tenant_id, orders_sh, sess)}

        category = cat_names[idx]
        return {"ok": send_admin_menu_category(bot_token, chat_id, tenant_id, orders_sh, sess, category)}

    if action == "catback":
        current_category = str(tmp.get("admin_menu_current_category") or "").strip()
        if not current_category:
            _clear_admin_menu_session_cache(tmp)
            return {"ok": send_admin_menu_home(bot_token, chat_id, tenant_id, orders_sh, sess)}
        _clear_admin_menu_session_cache(tmp)
        return {"ok": send_admin_menu_category(bot_token, chat_id, tenant_id, orders_sh, sess, current_category)}

    if action == "prd" and len(parts) == 4:
        sku = parts[3].strip()
        return {"ok": send_admin_menu_product_detail(bot_token, chat_id, tenant_id, orders_sh, sess, sku)}

    if action == "toggle" and len(parts) == 4:
        sku = parts[3].strip()
        item_before = get_menu_product_or_404(orders_sh, sku)
        new_active = not bool(item_before.get("active", False))

        set_menu_product_active(orders_sh, sku, new_active)

        # Limpieza total de caches: global + sesión admin
        invalidate_menu_cache(orders_sh)
        _clear_admin_menu_session_cache(tmp)

        item_after = get_menu_product_or_404(orders_sh, sku)

        telegram_send_text(
            bot_token,
            chat_id,
            f"✅ Estado actualizado.\nProducto: {item_after.get('name', '')}\nActivo: {'Sí' if item_after.get('active') else 'No'}",
        )
        return {"ok": send_admin_menu_product_detail(bot_token, chat_id, tenant_id, orders_sh, sess, sku)}

    if action == "price" and len(parts) == 4:
        sku = parts[3].strip()
        item = get_menu_product_or_404(orders_sh, sku)

        tmp["admin_menu_input_mode"] = "price_final"
        tmp["admin_menu_price_sku"] = sku
        tmp["admin_menu_price_work"] = float(item.get("price", 0.0))

        telegram_send_text(
            bot_token,
            chat_id,
            (
                "💲 MODIFICAR PRECIO\n\n"
                f"Producto: {item.get('name', '')}\n"
                f"Precio actual: Bs {fmt_price_short(item.get('price', 0))}\n\n"
                "Escribe el nuevo precio final.\n"
                "Ejemplos válidos:\n"
                "- 25\n"
                "- 25 bs\n"
                "- 25 bolivianos"
            ),
        )
        return {"ok": True}

    if action == "padj" and len(parts) == 5:
        sku = parts[3].strip()
        token = parts[4].strip().lower()

        item = get_menu_product_or_404(orders_sh, sku)
        current_sku = str(tmp.get("admin_menu_price_sku") or "").strip()
        if current_sku != sku:
            tmp["admin_menu_price_sku"] = sku
            tmp["admin_menu_price_work"] = float(item.get("price", 0.0))

        work_price = float(tmp.get("admin_menu_price_work") or 0.0)
        work_price = apply_price_delta(work_price, token)
        tmp["admin_menu_price_work"] = work_price

        return {"ok": send_admin_menu_price_editor(bot_token, chat_id, tenant_id, orders_sh, sess, sku)}

    if action == "psave" and len(parts) == 4:
        sku = parts[3].strip()
        current_sku = str(tmp.get("admin_menu_price_sku") or "").strip()

        if current_sku != sku:
            item = get_menu_product_or_404(orders_sh, sku)
            tmp["admin_menu_price_sku"] = sku
            tmp["admin_menu_price_work"] = float(item.get("price", 0.0))

        new_price = float(tmp.get("admin_menu_price_work") or 0.0)
        result = set_menu_product_price(orders_sh, sku, new_price)

        invalidate_menu_cache(orders_sh)
        _clear_admin_menu_session_cache(tmp)

        tmp.pop("admin_menu_price_sku", None)
        tmp.pop("admin_menu_price_work", None)
        tmp.pop("admin_menu_input_mode", None)

        telegram_send_text(
            bot_token,
            chat_id,
            f"✅ Precio actualizado.\nSKU: {sku}\nNuevo precio: Bs {fmt_price_short(result.get('price', 0))}",
        )
        return {"ok": send_admin_menu_product_detail(bot_token, chat_id, tenant_id, orders_sh, sess, sku)}

    if action == "pback" and len(parts) == 4:
        sku = parts[3].strip()
        tmp.pop("admin_menu_price_sku", None)
        tmp.pop("admin_menu_price_work", None)
        tmp.pop("admin_menu_input_mode", None)
        return {"ok": send_admin_menu_product_detail(bot_token, chat_id, tenant_id, orders_sh, sess, sku)}

    if action == "pricewrite" and len(parts) == 4:
        sku = parts[3].strip()
        item = get_menu_product_or_404(orders_sh, sku)
        tmp["admin_menu_input_mode"] = "price_final"
        tmp["admin_menu_price_sku"] = sku
        tmp["admin_menu_price_work"] = float(item.get("price", 0.0))

        telegram_send_text(
            bot_token,
            chat_id,
            (
                "✍️ ESCRIBIR PRECIO FINAL\n\n"
                f"Producto: {item.get('name', '')}\n"
                f"Precio actual: Bs {fmt_price_short(item.get('price', 0))}\n\n"
                "Escribe el nuevo precio final.\n"
                "Ejemplos válidos:\n"
                "- 25\n"
                "- 25 bs\n"
                "- 25 bolivianos"
            ),
        )
        return {"ok": True}

    if action == "discount" and len(parts) == 4:
        sku = parts[3].strip()
        item = get_menu_product_or_404(orders_sh, sku)
        tmp["admin_menu_input_mode"] = "discount_pct"
        tmp["admin_menu_price_sku"] = sku
        tmp["admin_menu_price_work"] = float(item.get("price", 0.0))

        telegram_send_text(
            bot_token,
            chat_id,
            (
                "🏷️ APLICAR DESCUENTO %\n\n"
                f"Producto: {item.get('name', '')}\n"
                f"Precio actual: Bs {fmt_price_short(item.get('price', 0))}\n\n"
                "Escribe el porcentaje de descuento.\n"
                "Ejemplos válidos:\n"
                "- 10\n"
                "- 15%\n"
                "- 20 por ciento"
            ),
        )
        return {"ok": True}

    if action == "photo" and len(parts) == 4:
        sku = parts[3].strip()
        tmp["admin_menu_input_mode"] = "awaiting_photo"
        tmp["admin_menu_price_sku"] = sku

        item = get_menu_product_or_404(orders_sh, sku)

        telegram_send_text(
            bot_token,
            chat_id,
            f"📷 Envía ahora la foto para:\n{item.get('name', '')}",
        )
        return {"ok": True}

    if action == "edit_name" and len(parts) == 4:
        sku = parts[3].strip()
        item = get_menu_product_or_404(orders_sh, sku)

        tmp["admin_menu_input_mode"] = "edit_name"
        tmp["admin_menu_price_sku"] = sku

        telegram_send_text(
            bot_token,
            chat_id,
            (
                "✏️ EDITAR NOMBRE\n\n"
                f"Producto actual: {item.get('name', '')}\n\n"
                "Escribe el nuevo nombre del producto."
            ),
        )
        return {"ok": True}

    if action == "edit_category" and len(parts) == 4:
        sku = parts[3].strip()
        item = get_menu_product_or_404(orders_sh, sku)

        categories = get_menu_categories(orders_sh)
        tmp["admin_menu_category_options"] = categories

        rows = []
        for i, cat in enumerate(categories[:20]):
            rows.append([(f"📂 {cat}", f"admmenu|{tenant_id}|setcat|{sku}|{i}")])

        rows.append([("➕ Nueva categoría", f"admmenu|{tenant_id}|newcat|{sku}")])
        rows.append([("⬅️ Volver al producto", f"admmenu|{tenant_id}|prd|{sku}")])

        telegram_send_text(
            bot_token,
            chat_id,
            (
                "📂 CAMBIAR CATEGORÍA\n\n"
                f"Producto: {item.get('name', '')}\n"
                f"Categoría actual: {item.get('category', '')}\n\n"
                "Elige una categoría o crea una nueva:"
            ),
            reply_markup=kb(rows),
        )
        return {"ok": True}

    if action == "setcat" and len(parts) == 5:
        sku = parts[3].strip()
        try:
            idx = int(parts[4].strip())
        except Exception:
            idx = -1

        categories = tmp.get("admin_menu_category_options") or []
        if idx < 0 or idx >= len(categories):
            telegram_send_text(
                bot_token,
                chat_id,
                "⚠️ No pude identificar esa categoría. Intenta otra vez.",
            )
            return {"ok": True}

        selected_category = str(categories[idx]).strip()
        result = set_menu_product_category(orders_sh, sku, selected_category)

        invalidate_menu_cache(orders_sh)
        _clear_admin_menu_session_cache(tmp)

        tmp.pop("admin_menu_category_options", None)
        tmp.pop("admin_menu_input_mode", None)
        tmp.pop("admin_menu_price_sku", None)

        telegram_send_text(
            bot_token,
            chat_id,
            f"✅ Categoría actualizada.\nNueva categoría: {result.get('category', '')}",
        )
        return {"ok": send_admin_menu_product_detail(bot_token, chat_id, tenant_id, orders_sh, sess, sku)}

    if action == "newcat" and len(parts) == 4:
        sku = parts[3].strip()

        tmp["admin_menu_input_mode"] = "new_category"
        tmp["admin_menu_price_sku"] = sku

        telegram_send_text(
            bot_token,
            chat_id,
            "Escribe la nueva categoría:",
        )
        return {"ok": True}

    if action == "create_product":
        tmp.pop("admin_menu_input_mode", None)
        tmp.pop("admin_menu_price_sku", None)
        tmp.pop("admin_menu_price_work", None)
        tmp.pop("admin_menu_category_options", None)

        tmp["admin_menu_create_step"] = "name"
        tmp.pop("admin_menu_create_name", None)
        tmp.pop("admin_menu_create_category", None)
        tmp.pop("admin_menu_create_price", None)

        telegram_send_text(
            bot_token,
            chat_id,
            "➕ CREAR PRODUCTO\n\nEscribe el nombre del nuevo producto:",
        )
        return {"ok": True}

    if action == "create_setcat" and len(parts) == 4:
        try:
            idx = int(parts[3].strip())
        except Exception:
            idx = -1

        categories = tmp.get("admin_menu_category_options") or []
        if idx < 0 or idx >= len(categories):
            telegram_send_text(
                bot_token,
                chat_id,
                "⚠️ No pude identificar esa categoría. Intenta otra vez.",
            )
            return {"ok": True}

        selected_category = str(categories[idx]).strip()
        tmp["admin_menu_create_category"] = selected_category
        tmp["admin_menu_create_step"] = "price"
        tmp.pop("admin_menu_category_options", None)

        telegram_send_text(
            bot_token,
            chat_id,
            (
                "💲 PRECIO DEL NUEVO PRODUCTO\n\n"
                f"Categoría elegida: {selected_category}\n\n"
                "Escribe el precio del producto.\n"
                "Ejemplos válidos:\n"
                "- 25\n"
                "- 25 bs\n"
                "- 25 bolivianos"
            ),
        )
        return {"ok": True}

    if action == "create_newcat":
        tmp["admin_menu_create_step"] = "new_category_for_create"
        telegram_send_text(
            bot_token,
            chat_id,
            "Escribe la nueva categoría para el producto:",
        )
        return {"ok": True}

    return {"ok": True}
