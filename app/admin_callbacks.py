# app/admin_callbacks.py

from typing import Any, Dict

from fastapi import HTTPException

from app.menu import (
    load_menu_admin_index,
    group_menu_admin_by_category,
    get_menu_product_or_404,
    set_menu_product_active,
    set_menu_product_price,
    get_menu_categories,
    invalidate_menu_cache,
)
from app.telegram_api import telegram_send_text
from app.utils import normalize, log_event
from app.webhook_helpers import get_sess, assert_admin_authorized, fmt_price_short
from app.admin_menu import (
    send_admin_menu_home,
    send_admin_menu_category,
    send_admin_menu_product_detail,
    send_admin_menu_price_editor,
    apply_price_delta,
)
from app.admin_nav import admin_panel_kb


def handle_admin_callback_impl(
    tenant: Dict[str, Any],
    tenant_id: str,
    bot_token: str,
    chat_id: int,
    data: str,
    orders_sh,
    tenant_tz: str,
) -> Dict[str, Any]:
    try:

        # -------------------------
        # PANEL
        # -------------------------

        if data == "admin_panel":
            telegram_send_text(
                bot_token,
                chat_id,
                "🧭 PANEL ADMIN\n\nElige una opción:",
                reply_markup=admin_panel_kb(),
            )
            return {"ok": True}

        # -------------------------
        # MENÚ ADMIN
        # -------------------------

        if data == "admin_menu":
            assert_admin_authorized(tenant, chat_id, tenant_id)
            sess = get_sess(tenant_id, chat_id)
            return {"ok": send_admin_menu_home(bot_token, chat_id, tenant_id, orders_sh, sess)}

        if data.startswith("admmenu|"):

            assert_admin_authorized(tenant, chat_id, tenant_id)

            parts = data.split("|")
            if len(parts) < 3:
                return {"ok": True}

            cb_tenant_id = parts[1].strip()
            if cb_tenant_id != tenant_id:
                raise HTTPException(status_code=400, detail="Tenant mismatch")

            action = parts[2].strip()

            sess = get_sess(tenant_id, chat_id)
            tmp = sess.setdefault("tmp", {})

            # -------------------------
            # NAV
            # -------------------------

            if action == "panel":
                tmp.clear()
                telegram_send_text(
                    bot_token,
                    chat_id,
                    "🧭 PANEL ADMIN\n\nElige una opción:",
                    reply_markup=admin_panel_kb(),
                )
                return {"ok": True}

            if action == "home":
                return {"ok": send_admin_menu_home(bot_token, chat_id, tenant_id, orders_sh, sess)}

            if action == "refresh":
                invalidate_menu_cache(orders_sh)
                telegram_send_text(bot_token, chat_id, "✅ Menú actualizado")
                return {"ok": send_admin_menu_home(bot_token, chat_id, tenant_id, orders_sh, sess)}

            # -------------------------
            # CATEGORÍAS
            # -------------------------

            if action == "cat" and len(parts) == 4:

                idx = int(parts[3])

                menu_idx = load_menu_admin_index(orders_sh)
                cats = group_menu_admin_by_category(menu_idx)
                cat_names = sorted(cats.keys(), key=lambda x: normalize(x))

                if idx >= len(cat_names):
                    return {"ok": True}

                category = cat_names[idx]

                return {"ok": send_admin_menu_category(bot_token, chat_id, tenant_id, orders_sh, sess, category)}

            if action == "catback":
                category = tmp.get("admin_menu_current_category")
                return {"ok": send_admin_menu_category(bot_token, chat_id, tenant_id, orders_sh, sess, category)}

            # -------------------------
            # PRODUCTO
            # -------------------------

            if action == "prd" and len(parts) == 4:
                sku = parts[3]

                tmp["admin_menu_target_sku"] = sku

                return {"ok": send_admin_menu_product_detail(bot_token, chat_id, tenant_id, orders_sh, sess, sku)}

            if action == "toggle" and len(parts) == 4:
                sku = parts[3]

                item = get_menu_product_or_404(orders_sh, sku)
                new_state = not item["active"]

                set_menu_product_active(orders_sh, sku, new_state)

                telegram_send_text(
                    bot_token,
                    chat_id,
                    f"Estado actualizado: {'Activo' if new_state else 'Inactivo'}",
                )

                return {"ok": send_admin_menu_product_detail(bot_token, chat_id, tenant_id, orders_sh, sess, sku)}

            # -------------------------
            # PRECIO (UNIFICADO)
            # -------------------------

            if action == "price" and len(parts) == 4:
                sku = parts[3]

                tmp["admin_menu_target_sku"] = sku

                return {"ok": send_admin_menu_price_editor(bot_token, chat_id, tenant_id, orders_sh, sess, sku)}

            if action == "padj" and len(parts) == 5:
                sku = parts[3]
                token = parts[4]

                item = get_menu_product_or_404(orders_sh, sku)

                if tmp.get("admin_menu_target_sku") != sku:
                    tmp["admin_menu_target_sku"] = sku
                    tmp["admin_menu_price_work"] = float(item["price"])

                work_price = tmp.get("admin_menu_price_work", item["price"])
                work_price = apply_price_delta(work_price, token)

                tmp["admin_menu_price_work"] = work_price

                return {"ok": send_admin_menu_price_editor(bot_token, chat_id, tenant_id, orders_sh, sess, sku)}

            if action == "psave" and len(parts) == 4:
                sku = parts[3]

                new_price = float(tmp.get("admin_menu_price_work", 0))

                set_menu_product_price(orders_sh, sku, new_price)

                tmp.pop("admin_menu_price_work", None)

                telegram_send_text(
                    bot_token,
                    chat_id,
                    f"Precio actualizado: Bs {fmt_price_short(new_price)}",
                )

                return {"ok": send_admin_menu_product_detail(bot_token, chat_id, tenant_id, orders_sh, sess, sku)}

            if action == "pback" and len(parts) == 4:
                sku = parts[3]
                tmp.pop("admin_menu_price_work", None)
                return {"ok": send_admin_menu_product_detail(bot_token, chat_id, tenant_id, orders_sh, sess, sku)}

            # -------------------------
            # EDITAR NOMBRE
            # -------------------------

            if action == "edit_name" and len(parts) == 4:
                sku = parts[3]

                tmp["admin_menu_target_sku"] = sku
                tmp["admin_menu_input_mode"] = "edit_name"

                item = get_menu_product_or_404(orders_sh, sku)

                telegram_send_text(
                    bot_token,
                    chat_id,
                    f"✏️ Nuevo nombre para:\n{item['name']}",
                )
                return {"ok": True}

            # -------------------------
            # CAMBIAR CATEGORÍA (OPCIÓN B)
            # -------------------------

            if action == "edit_category" and len(parts) == 4:
                sku = parts[3]

                tmp["admin_menu_target_sku"] = sku

                categories = get_menu_categories(orders_sh)

                rows = []
                for c in categories[:20]:
                    rows.append([(c, f"admmenu|{tenant_id}|setcat|{sku}|{c}")])

                rows.append([("➕ Nueva categoría", f"admmenu|{tenant_id}|newcat|{sku}")])

                telegram_send_text(
                    bot_token,
                    chat_id,
                    "Selecciona categoría:",
                    reply_markup={"inline_keyboard": rows},
                )

                return {"ok": True}

            if action == "setcat" and len(parts) == 5:
                sku = parts[3]
                category = parts[4]

                from app.menu import set_menu_product_category
                set_menu_product_category(orders_sh, sku, category)

                telegram_send_text(bot_token, chat_id, "Categoría actualizada")

                return {"ok": send_admin_menu_product_detail(bot_token, chat_id, tenant_id, orders_sh, sess, sku)}

            if action == "newcat" and len(parts) == 4:
                sku = parts[3]

                tmp["admin_menu_target_sku"] = sku
                tmp["admin_menu_input_mode"] = "new_category"

                telegram_send_text(bot_token, chat_id, "Escribe la nueva categoría:")
                return {"ok": True}

            # -------------------------
            # CREAR PRODUCTO
            # -------------------------

            if action == "create_product":
                tmp["admin_menu_create_step"] = "name"

                telegram_send_text(bot_token, chat_id, "Nombre del nuevo producto:")
                return {"ok": True}

        return {"ok": True}

    except Exception as e:
        log_event("admin_callback_error", error=str(e))
        telegram_send_text(bot_token, chat_id, "⚠️ Error en panel admin")
        return {"ok": True}
