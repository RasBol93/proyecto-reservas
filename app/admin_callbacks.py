# app/admin_callbacks.py — callbacks admin sin teclado persistente inferior

from typing import Any, Dict

from fastapi import HTTPException

from app.menu import (
    load_menu_admin_index,
    group_menu_admin_by_category,
    get_menu_product_or_404,
    set_menu_product_active,
    set_menu_product_price,
    set_menu_product_name,
    set_menu_product_category,
    get_menu_categories,
    invalidate_menu_cache,
)
from app.orders import (
    get_order_by_id,
    update_order_status,
)
from app.telegram_api import telegram_send_text
from app.utils import normalize, log_event
from app.stats import resolve_period, build_stats_report_text, build_periods
from app.webhook_helpers import (
    get_sess,
    get_client_bot_token,
    assert_admin_authorized,
    fmt_price_short,
    admin_periods_inline_kb,
)
from app.admin_hours import (
    handle_admin_hours_callback,
    send_admin_hours_menu,
)
from app.admin_menu import (
    send_admin_menu_home,
    send_admin_menu_category,
    send_admin_menu_product_detail,
    send_admin_menu_price_editor,
    apply_price_delta,
)
from app.alerts import (
    alert_order_status_failed,
    alert_system_error,
)
from app.admin_helpers import (
    _safe_str,
    _safe_client_chat_id_from_order,
    _extract_slot_hhmm,
)
from app.admin_consumers import (
    _send_consumers_menu,
    _send_consumers_filters,
    _send_consumers_report,
)
from app.admin_manual_order import (
    _admin_order_reset,
    _admin_order_get_active_categories,
    _send_admin_order_home,
    _send_admin_order_category,
    _send_admin_order_product_qty,
    _admin_order_add_to_cart,
    _admin_order_inc_item,
    _admin_order_dec_item,
    _admin_order_remove_item,
    _send_admin_order_cart,
    _admin_order_time_choice_kb,
)
from app.admin_nav import (
    admin_panel_kb,
)


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
        if data == "admin_panel":
            telegram_send_text(
                bot_token,
                chat_id,
                "🧭 PANEL ADMIN\n\nElige una opción:",
                reply_markup=admin_panel_kb(),
            )
            return {"ok": True}

        if data == "admin_stats":
            assert_admin_authorized(tenant, chat_id, tenant_id)
            periods = build_periods(tenant_tz)
            telegram_send_text(
                bot_token,
                chat_id,
                "📊 ESTADÍSTICAS\n\nSelecciona el período:",
                reply_markup=admin_periods_inline_kb(tenant_id, periods),
            )
            return {"ok": True}

        if data == "admin_consumers":
            assert_admin_authorized(tenant, chat_id, tenant_id)
            return {"ok": _send_consumers_menu(bot_token, chat_id, tenant_id)}

        if data == "admin_order":
            assert_admin_authorized(tenant, chat_id, tenant_id)
            sess = get_sess(tenant_id, chat_id)
            tmp = sess.setdefault("tmp", {})
            _admin_order_reset(tmp)
            tmp["admin_order_cart"] = []
            return {"ok": _send_admin_order_home(bot_token, chat_id, tenant_id, orders_sh, sess)}

        if data == "admin_hours":
            assert_admin_authorized(tenant, chat_id, tenant_id)
            return {"ok": send_admin_hours_menu(bot_token, chat_id, tenant_id, orders_sh, tenant_tz)}

        if data == "admin_menu":
            assert_admin_authorized(tenant, chat_id, tenant_id)
            sess = get_sess(tenant_id, chat_id)
            return {"ok": send_admin_menu_home(bot_token, chat_id, tenant_id, orders_sh, sess)}

        # =========================
        # MENU ADMIN (EXTENDIDO)
        # =========================
        if data.startswith("admmenu|"):
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

            # ===== NUEVO: EDITAR NOMBRE =====
            if action == "edit_name" and len(parts) == 4:
                sku = parts[3].strip()
                tmp["admin_menu_input_mode"] = "edit_name"
                tmp["admin_menu_price_sku"] = sku

                item = get_menu_product_or_404(orders_sh, sku)

                telegram_send_text(
                    bot_token,
                    chat_id,
                    f"✏️ Nuevo nombre para:\n{item.get('name', '')}",
                )
                return {"ok": True}

            # ===== NUEVO: CAMBIAR CATEGORÍA =====
            if action == "edit_category" and len(parts) == 4:
                sku = parts[3].strip()
                categories = get_menu_categories(orders_sh)

                rows = []
                for i, c in enumerate(categories[:20]):
                    rows.append([(c, f"admmenu|{tenant_id}|setcat|{sku}|{i}")])

                rows.append([("➕ Nueva categoría", f"admmenu|{tenant_id}|newcat|{sku}")])

                tmp["admin_menu_categories"] = categories

                telegram_send_text(
                    bot_token,
                    chat_id,
                    "Selecciona categoría:",
                    reply_markup={"inline_keyboard": rows},
                )
                return {"ok": True}

            if action == "setcat" and len(parts) == 5:
                sku = parts[3].strip()
                idx = int(parts[4])

                categories = tmp.get("admin_menu_categories", [])
                if idx >= len(categories):
                    return {"ok": True}

                category = categories[idx]
                set_menu_product_category(orders_sh, sku, category)

                telegram_send_text(bot_token, chat_id, "✅ Categoría actualizada")
                return {"ok": send_admin_menu_product_detail(bot_token, chat_id, tenant_id, orders_sh, sess, sku)}

            if action == "newcat" and len(parts) == 4:
                sku = parts[3].strip()
                tmp["admin_menu_input_mode"] = "new_category"
                tmp["admin_menu_price_sku"] = sku

                telegram_send_text(bot_token, chat_id, "Escribe la nueva categoría:")
                return {"ok": True}

        return {"ok": True}

    except Exception as e:
        log_event(
            "admin_callback_error",
            tenant_id=tenant_id,
            chat_id=chat_id,
            data=data,
            error_type=type(e).__name__,
            error=str(e),
        )
        alert_system_error(error=str(e), module="admin_callback")
        telegram_send_text(bot_token, chat_id, "⚠️ Ocurrió un error en el panel admin.")
        return {"ok": True}
