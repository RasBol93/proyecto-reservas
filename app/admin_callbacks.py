# app/admin_callbacks.py — callbacks admin con promociones integradas

from typing import Any, Dict

from app.admin_callbacks_menu import handle_admin_menu_callback
from app.admin_callbacks_surveys import handle_admin_surveys_callback
from app.admin_callbacks_orders import handle_admin_orders_callback
from app.admin_callbacks_hours import handle_admin_hours_routed_callback
from app.admin_callbacks_promotions import handle_admin_promotions_callback
from app.admin_callbacks_business import handle_admin_business_callback
from app.admin_callbacks_sales_goals import handle_admin_sales_goals_callback

from app.telegram_api import telegram_send_text
from app.utils import log_event
from app.stats import resolve_period, build_stats_report_text, build_periods
from app.webhook_helpers import (
    get_sess,
    assert_admin_authorized,
    get_user_role,
    admin_periods_inline_kb,
)
from app.alerts import (
    alert_system_error,
)
from app.admin_consumers import (
    _send_consumers_menu,
    _send_consumers_filters,
    _send_consumers_report,
)
from app.admin_nav import (
    admin_panel_kb,
)
from app.admin_menu import (
    send_admin_menu_home,
)
from app.admin_promotions import (
    send_admin_promotions_home,
)


def _effective_admin_role(tenant: Dict[str, Any], chat_id: int) -> str:
    if bool(tenant.get("_is_owner_bot")):
        return "owner"
    return get_user_role(tenant, chat_id)


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
            user_role = _effective_admin_role(tenant, chat_id)
            telegram_send_text(
                bot_token,
                chat_id,
                "🧭 PANEL ADMIN\n\nElige una opción:",
                reply_markup=admin_panel_kb(user_role=user_role),
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
            return {"ok": _send_consumers_menu(bot_token, chat_id, tenant_id, tenant_tz)}

        if data == "admin_menu":
            assert_admin_authorized(tenant, chat_id, tenant_id)
            sess = get_sess(tenant_id, chat_id)
            return {"ok": send_admin_menu_home(bot_token, chat_id, tenant_id, orders_sh, sess)}

        if data == "admin_promotions":
            assert_admin_authorized(tenant, chat_id, tenant_id)
            sess = get_sess(tenant_id, chat_id)
            return {"ok": send_admin_promotions_home(bot_token, chat_id, tenant_id, orders_sh, sess)}

        if data == "admin_business":
            business_result = handle_admin_business_callback(
                tenant=tenant,
                tenant_id=tenant_id,
                bot_token=bot_token,
                chat_id=chat_id,
                data=data,
                get_effective_admin_role=_effective_admin_role,
            )
            if business_result is not None:
                return business_result

        if data == "admin_sales_goals":
            sales_goals_result = handle_admin_sales_goals_callback(
                tenant=tenant,
                tenant_id=tenant_id,
                bot_token=bot_token,
                chat_id=chat_id,
                data=data,
                orders_sh=orders_sh,
                get_effective_admin_role=_effective_admin_role,
            )
            if sales_goals_result is not None:
                return sales_goals_result

        if data == "admin_payments":
            assert_admin_authorized(tenant, chat_id, tenant_id)
            from app.telegram_keyboard import kb

            telegram_send_text(
                bot_token,
                chat_id,
                "💳 *Gestión de pagos*\n\nPuedes subir o actualizar el QR de pagos.",
                parse_mode="Markdown",
                reply_markup=kb([
                    [("📷 Subir QR", "admin_payments_upload")],
                    [("🧭 Panel admin", "admin_panel")],
                ]),
            )
            return {"ok": True}

        if data == "admin_payments_upload":
            assert_admin_authorized(tenant, chat_id, tenant_id)
            sess = get_sess(tenant_id, chat_id)
            tmp = sess.setdefault("tmp", {})
            tmp["admin_payment_mode"] = "awaiting_qr"
            telegram_send_text(
                bot_token,
                chat_id,
                "📷 Envíame la imagen del QR de pagos.",
            )
            return {"ok": True}

        if data.startswith("admin_stats_period|"):
            from fastapi import HTTPException

            parts = data.split("|")
            if len(parts) != 3:
                return {"ok": True}

            _, cb_tenant_id, period_key = parts
            cb_tenant_id = cb_tenant_id.strip()
            period_key = period_key.strip()

            if cb_tenant_id != tenant_id:
                raise HTTPException(status_code=400, detail="Tenant mismatch in stats callback")

            assert_admin_authorized(tenant, chat_id, tenant_id)

            period = resolve_period(tenant_tz, period_key)
            txt = build_stats_report_text(
                orders_sh,
                tenant_id=tenant_id,
                tenant_tz=tenant_tz,
                period=period,
            )

            telegram_send_text(
                bot_token,
                chat_id,
                txt,
            )
            return {"ok": True}

        if data.startswith("admcons|"):
            from fastapi import HTTPException

            assert_admin_authorized(tenant, chat_id, tenant_id)

            parts = data.split("|")
            if len(parts) < 3:
                return {"ok": True}

            cb_tenant_id = parts[1].strip()
            if cb_tenant_id != tenant_id:
                raise HTTPException(status_code=400, detail="Tenant mismatch in consumer db callback")

            action = parts[2].strip()

            if action == "panel":
                user_role = _effective_admin_role(tenant, chat_id)
                telegram_send_text(
                    bot_token,
                    chat_id,
                    "🧭 PANEL ADMIN\n\nElige una opción:",
                    reply_markup=admin_panel_kb(user_role=user_role),
                )
                return {"ok": True}

            if action == "menu":
                return {"ok": _send_consumers_menu(bot_token, chat_id, tenant_id, tenant_tz)}

            if action == "period" and len(parts) == 4:
                period_key = parts[3].strip()
                return {"ok": _send_consumers_filters(bot_token, chat_id, tenant_id, period_key, tenant_tz)}

            if action == "report" and len(parts) == 5:
                period_key = parts[3].strip()
                filter_key = parts[4].strip()
                return {
                    "ok": _send_consumers_report(
                        bot_token=bot_token,
                        chat_id=chat_id,
                        tenant_id=tenant_id,
                        orders_sh=orders_sh,
                        tenant_tz=tenant_tz,
                        period_key=period_key,
                        filter_key=filter_key,
                    )
                }

            return {"ok": True}

        hours_result = handle_admin_hours_routed_callback(
            tenant=tenant,
            tenant_id=tenant_id,
            bot_token=bot_token,
            chat_id=chat_id,
            data=data,
            orders_sh=orders_sh,
            tenant_tz=tenant_tz,
        )
        if hours_result is not None:
            return hours_result

        surveys_result = handle_admin_surveys_callback(
            tenant=tenant,
            tenant_id=tenant_id,
            bot_token=bot_token,
            chat_id=chat_id,
            data=data,
            orders_sh=orders_sh,
            tenant_tz=tenant_tz,
        )
        if surveys_result is not None:
            return surveys_result

        orders_result = handle_admin_orders_callback(
            tenant=tenant,
            tenant_id=tenant_id,
            bot_token=bot_token,
            chat_id=chat_id,
            data=data,
            orders_sh=orders_sh,
            tenant_tz=tenant_tz,
            get_effective_admin_role=_effective_admin_role,
        )
        if orders_result is not None:
            return orders_result

        promotions_result = handle_admin_promotions_callback(
            tenant=tenant,
            tenant_id=tenant_id,
            bot_token=bot_token,
            chat_id=chat_id,
            data=data,
            orders_sh=orders_sh,
            get_effective_admin_role=_effective_admin_role,
        )
        if promotions_result is not None:
            return promotions_result

        business_result = handle_admin_business_callback(
            tenant=tenant,
            tenant_id=tenant_id,
            bot_token=bot_token,
            chat_id=chat_id,
            data=data,
            get_effective_admin_role=_effective_admin_role,
        )
        if business_result is not None:
            return business_result

        sales_goals_result = handle_admin_sales_goals_callback(
            tenant=tenant,
            tenant_id=tenant_id,
            bot_token=bot_token,
            chat_id=chat_id,
            data=data,
            orders_sh=orders_sh,
            get_effective_admin_role=_effective_admin_role,
        )
        if sales_goals_result is not None:
            return sales_goals_result

        menu_result = handle_admin_menu_callback(
            tenant=tenant,
            tenant_id=tenant_id,
            bot_token=bot_token,
            chat_id=chat_id,
            data=data,
            orders_sh=orders_sh,
            get_effective_admin_role=_effective_admin_role,
        )
        if menu_result is not None:
            return menu_result

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
