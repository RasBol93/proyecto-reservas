# app/admin_callbacks.py

from typing import Any, Dict

from fastapi import HTTPException

from app.menu import (
    load_menu_admin_index,
    group_menu_admin_by_category,
    get_menu_product_or_404,
    set_menu_product_active,
    set_menu_product_price,
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
    get_business_status_safe,
)
from app.admin_hours import (
    DAY_ORDER,
    send_admin_hours_menu,
    send_admin_days_menu,
    send_admin_norm_open_menu,
    send_admin_norm_close_menu,
    send_admin_norm_last_menu,
    send_admin_early_close_menu,
    send_admin_early_last_menu,
    send_admin_late_open_menu,
    compact_to_hhmm,
    admin_restore_habitual,
    admin_set_weekly_open_days,
    admin_set_weekly_normal_hours,
    admin_set_today_closed,
    admin_set_today_open_force,
    admin_set_today_open_override,
    admin_set_today_close_override,
    admin_set_today_last_order_override,
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
    _send_admin_order_cart,
)
from app.admin_nav import (
    admin_root_kb,
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
                "📊 ESTADÍSTICAS\n\nElige el período:",
                reply_markup=admin_panel_kb(),
            )
            telegram_send_text(
                bot_token,
                chat_id,
                "Selecciona el período:",
                reply_markup={
                    "inline_keyboard": [
                        [{"text": p.label, "callback_data": f"admin_stats_period|{tenant_id}|{p.key}"}]
                        for p in periods
                    ]
                },
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

        if data.startswith("paid|"):
            parts = data.split("|")
            if len(parts) != 3:
                return {"ok": True}

            cb_tenant_id = parts[1].strip()
            order_id = parts[2].strip()

            if cb_tenant_id != tenant_id:
                raise HTTPException(status_code=400, detail="Tenant mismatch in callback")

            assert_admin_authorized(tenant, chat_id, tenant_id)

            res = update_order_status(orders_sh, order_id, "PAID")
            if not res.get("ok"):
                alert_order_status_failed(
                    tenant_id=tenant_id,
                    order_id=order_id,
                    new_status="PAID",
                    error=res.get("error") or "update_order_status failed",
                )
                telegram_send_text(
                    bot_token,
                    chat_id,
                    "⚠️ Error actualizando el estado.",
                    reply_markup=admin_root_kb(),
                )
                return {"ok": True}

            if not res.get("found"):
                telegram_send_text(
                    bot_token,
                    chat_id,
                    f"⚠️ Pedido {order_id} no encontrado en Sheets.",
                    reply_markup=admin_root_kb(),
                )
                return {"ok": True}

            order_after = get_order_by_id(orders_sh, order_id)

            if order_after:
                customer_name = _safe_str(order_after.get("customer_name"))
                final_hhmm = _extract_slot_hhmm(order_after.get("requested_time"))

                if final_hhmm:
                    telegram_send_text(
                        bot_token,
                        chat_id,
                        f"✅ El pedido de {customer_name}, con código de pedido {order_id}, ha sido confirmado.\nHora de recojo: {final_hhmm}.",
                        reply_markup=admin_root_kb(),
                    )
                else:
                    telegram_send_text(
                        bot_token,
                        chat_id,
                        f"✅ El pedido de {customer_name}, con código de pedido {order_id}, ha sido confirmado.",
                        reply_markup=admin_root_kb(),
                    )
            else:
                telegram_send_text(
                    bot_token,
                    chat_id,
                    f"✅ El pedido con código de pedido {order_id} ha sido confirmado.",
                    reply_markup=admin_root_kb(),
                )

            if order_after:
                client_token = get_client_bot_token(tenant)
                client_chat = _safe_client_chat_id_from_order(order_after)

                if client_token and client_chat:
                    try:
                        final_slot_for_msg = _safe_str(_extract_slot_hhmm(order_after.get("requested_time")))
                        if final_slot_for_msg:
                            msg_client = (
                                f"✅ Tu pedido ha sido confirmado.\n"
                                f"Código de pedido: {order_id}\n\n"
                                f"Hora de recojo: *{final_slot_for_msg}*."
                            )
                        else:
                            msg_client = (
                                f"✅ Tu pedido ha sido confirmado.\n"
                                f"Código de pedido: {order_id}\n\n"
                                "¡Gracias!"
                            )

                        telegram_send_text(
                            client_token,
                            int(client_chat),
                            msg_client,
                            parse_mode="Markdown",
                        )
                    except Exception as e:
                        log_event(
                            "notify_client_paid_failed",
                            tenant_id=tenant_id,
                            order_id=order_id,
                            client_chat=client_chat,
                            error=str(e),
                        )
                else:
                    log_event(
                        "notify_client_paid_skipped",
                        tenant_id=tenant_id,
                        order_id=order_id,
                        reason="missing_client_token_or_chat_id",
                    )

            return {"ok": True}

        if data.startswith("admin_stats_period|"):
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
            txt = build_stats_report_text(orders_sh, tenant_id=tenant_id, tenant_tz=tenant_tz, period=period)

            telegram_send_text(bot_token, chat_id, txt, reply_markup=admin_root_kb())
            return {"ok": True}

        if data.startswith("admcons|"):
            assert_admin_authorized(tenant, chat_id, tenant_id)

            parts = data.split("|")
            if len(parts) < 3:
                return {"ok": True}

            cb_tenant_id = parts[1].strip()
            if cb_tenant_id != tenant_id:
                raise HTTPException(status_code=400, detail="Tenant mismatch in consumer db callback")

            action = parts[2].strip()

            if action == "panel":
                telegram_send_text(
                    bot_token,
                    chat_id,
                    "🧭 PANEL ADMIN\n\nElige una opción:",
                    reply_markup=admin_panel_kb(),
                )
                return {"ok": True}

            if action == "menu":
                return {"ok": _send_consumers_menu(bot_token, chat_id, tenant_id)}

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

        if data.startswith("admord|"):
            assert_admin_authorized(tenant, chat_id, tenant_id)

            parts = data.split("|")
            if len(parts) < 3:
                return {"ok": True}

            cb_tenant_id = parts[1].strip()
            if cb_tenant_id != tenant_id:
                raise HTTPException(status_code=400, detail="Tenant mismatch in admin order callback")

            action = parts[2].strip()
            sess = get_sess(tenant_id, chat_id)
            tmp = sess.setdefault("tmp", {})

            if action == "start":
                _admin_order_reset(tmp)
                tmp["admin_order_cart"] = []
                return {"ok": _send_admin_order_home(bot_token, chat_id, tenant_id, orders_sh, sess)}

            if action == "panel":
                _admin_order_reset(tmp)
                telegram_send_text(
                    bot_token,
                    chat_id,
                    "🧭 PANEL ADMIN\n\nElige una opción:",
                    reply_markup=admin_panel_kb(),
                )
                return {"ok": True}

            if action == "home":
                return {"ok": _send_admin_order_home(bot_token, chat_id, tenant_id, orders_sh, sess)}

            if action == "cat" and len(parts) == 4:
                try:
                    idx = int(parts[3].strip())
                except Exception:
                    idx = -1

                _, cats, cat_names = _admin_order_get_active_categories(orders_sh)
                if idx < 0 or idx >= len(cat_names):
                    return {"ok": _send_admin_order_home(bot_token, chat_id, tenant_id, orders_sh, sess)}

                category = cat_names[idx]
                return {"ok": _send_admin_order_category(bot_token, chat_id, tenant_id, orders_sh, sess, category)}

            if action == "catback":
                current_category = str(tmp.get("admin_order_current_category") or "").strip()
                if not current_category:
                    return {"ok": _send_admin_order_home(bot_token, chat_id, tenant_id, orders_sh, sess)}
                return {"ok": _send_admin_order_category(bot_token, chat_id, tenant_id, orders_sh, sess, current_category)}

            if action == "prd" and len(parts) == 4:
                sku = parts[3].strip()
                return {"ok": _send_admin_order_product_qty(bot_token, chat_id, tenant_id, orders_sh, sku)}

            if action == "qty" and len(parts) == 5:
                sku = parts[3].strip()
                try:
                    qty = int(parts[4].strip())
                except Exception:
                    qty = 1
                qty = max(1, qty)

                item = get_menu_product_or_404(orders_sh, sku)
                _admin_order_add_to_cart(tmp, sku, qty)

                telegram_send_text(
                    bot_token,
                    chat_id,
                    f"✅ Agregado al pedido: {qty} x {item.get('name', '')}",
                    reply_markup=admin_root_kb(),
                )
                return {"ok": _send_admin_order_cart(bot_token, chat_id, tenant_id, orders_sh, sess)}

            if action == "cart":
                return {"ok": _send_admin_order_cart(bot_token, chat_id, tenant_id, orders_sh, sess)}

            if action == "clear":
                tmp["admin_order_cart"] = []
                telegram_send_text(
                    bot_token,
                    chat_id,
                    "🧹 Carrito manual vaciado.",
                    reply_markup=admin_root_kb(),
                )
                return {"ok": _send_admin_order_home(bot_token, chat_id, tenant_id, orders_sh, sess)}

            if action == "confirm":
                cart = tmp.get("admin_order_cart") or []
                if not cart:
                    telegram_send_text(
                        bot_token,
                        chat_id,
                        "⚠️ El carrito está vacío.",
                        reply_markup=admin_root_kb(),
                    )
                    return {"ok": _send_admin_order_home(bot_token, chat_id, tenant_id, orders_sh, sess)}

                tmp["admin_order_step"] = "awaiting_name"
                telegram_send_text(
                    bot_token,
                    chat_id,
                    "Escribe el nombre del cliente:",
                    reply_markup=admin_root_kb(),
                )
                return {"ok": True}

            return {"ok": True}

        if data.startswith("admhrs|"):
            assert_admin_authorized(tenant, chat_id, tenant_id)

            parts = data.split("|")
            if len(parts) < 3:
                return {"ok": True}

            cb_tenant_id = parts[1].strip()
            if cb_tenant_id != tenant_id:
                raise HTTPException(status_code=400, detail="Tenant mismatch in admin hours callback")

            action = parts[2].strip()
            sess = get_sess(tenant_id, chat_id)
            tmp = sess.setdefault("tmp", {})
            updated_by = f"admin_bot:{chat_id}"

            if action == "menu":
                tmp.pop("admin_days_selected", None)
                tmp.pop("admin_norm_open", None)
                tmp.pop("admin_norm_close", None)
                tmp.pop("admin_early_close", None)
                return {"ok": send_admin_hours_menu(bot_token, chat_id, tenant_id, orders_sh, tenant_tz)}

            if action == "habitual":
                tmp.pop("admin_days_selected", None)
                tmp.pop("admin_norm_open", None)
                tmp.pop("admin_norm_close", None)
                tmp.pop("admin_early_close", None)
                admin_restore_habitual(orders_sh=orders_sh, updated_by=updated_by)
                telegram_send_text(
                    bot_token,
                    chat_id,
                    "✅ Se restauró la configuración habitual de hoy.",
                    reply_markup=admin_root_kb(),
                )
                return {"ok": send_admin_hours_menu(bot_token, chat_id, tenant_id, orders_sh, tenant_tz)}

            if action == "days":
                bs = get_business_status_safe(orders_sh=orders_sh, tenant_tz=tenant_tz)
                tmp["admin_days_selected"] = list(bs.get("weekly_open_days") or [])
                return {"ok": send_admin_days_menu(bot_token, chat_id, tenant_id, sess, bs)}

            if action == "dayt" and len(parts) == 4:
                code = parts[3].strip()
                if code not in DAY_ORDER:
                    return {"ok": True}
                current = set(tmp.get("admin_days_selected") or [])
                if code in current:
                    current.remove(code)
                else:
                    current.add(code)
                tmp["admin_days_selected"] = list(current)
                bs = get_business_status_safe(orders_sh=orders_sh, tenant_tz=tenant_tz)
                return {"ok": send_admin_days_menu(bot_token, chat_id, tenant_id, sess, bs)}

            if action == "dayssave":
                selected = [d for d in DAY_ORDER if d in set(tmp.get("admin_days_selected") or [])]
                admin_set_weekly_open_days(
                    orders_sh=orders_sh,
                    days=selected,
                    updated_by=updated_by,
                )
                tmp.pop("admin_days_selected", None)

                bs_after = get_business_status_safe(orders_sh=orders_sh, tenant_tz=tenant_tz)
                today_code = str(bs_after.get("today_weekday_code") or "").strip()
                today_in = today_code in set(bs_after.get("weekly_open_days") or [])
                force_open = bool(bs_after.get("today_open_force"))
                today_closed = bool(bs_after.get("today_closed"))

                msg = "✅ Días normales actualizados."
                if today_code and (not today_in) and (not force_open) and (not today_closed):
                    msg += f"\n⚠️ Ojo: hoy ({today_code}) quedó fuera de los días normales."

                telegram_send_text(bot_token, chat_id, msg, reply_markup=admin_root_kb())
                return {"ok": send_admin_hours_menu(bot_token, chat_id, tenant_id, orders_sh, tenant_tz)}

            if action == "norm":
                tmp.pop("admin_norm_open", None)
                tmp.pop("admin_norm_close", None)
                return {"ok": send_admin_norm_open_menu(bot_token, chat_id, tenant_id)}

            if action == "normopen" and len(parts) == 4:
                open_time = compact_to_hhmm(parts[3].strip())
                tmp["admin_norm_open"] = open_time
                return {"ok": send_admin_norm_close_menu(bot_token, chat_id, tenant_id, open_time)}

            if action == "normclose" and len(parts) == 4:
                close_time = compact_to_hhmm(parts[3].strip())
                open_time = str(tmp.get("admin_norm_open") or "").strip()
                if not open_time:
                    return {"ok": send_admin_norm_open_menu(bot_token, chat_id, tenant_id)}
                tmp["admin_norm_close"] = close_time
                return {"ok": send_admin_norm_last_menu(bot_token, chat_id, tenant_id, open_time, close_time)}

            if action == "normlast" and len(parts) == 4:
                last_time = compact_to_hhmm(parts[3].strip())
                open_time = str(tmp.get("admin_norm_open") or "").strip()
                close_time = str(tmp.get("admin_norm_close") or "").strip()
                if not open_time or not close_time:
                    return {"ok": send_admin_norm_open_menu(bot_token, chat_id, tenant_id)}

                admin_set_weekly_normal_hours(
                    orders_sh=orders_sh,
                    open_time=open_time,
                    close_time=close_time,
                    last_order_time=last_time,
                    updated_by=updated_by,
                )

                tmp.pop("admin_norm_open", None)
                tmp.pop("admin_norm_close", None)

                telegram_send_text(
                    bot_token,
                    chat_id,
                    f"✅ Horario normal actualizado.\nApertura: {open_time}\nCierre: {close_time}\nÚltima hora de pedido: {last_time}",
                    reply_markup=admin_root_kb(),
                )
                return {"ok": send_admin_hours_menu(bot_token, chat_id, tenant_id, orders_sh, tenant_tz)}

            if action == "early":
                tmp.pop("admin_early_close", None)
                return {"ok": send_admin_early_close_menu(bot_token, chat_id, tenant_id)}

            if action == "earlyclose" and len(parts) == 4:
                close_time = compact_to_hhmm(parts[3].strip())
                tmp["admin_early_close"] = close_time
                return {"ok": send_admin_early_last_menu(bot_token, chat_id, tenant_id, close_time)}

            if action == "earlylast" and len(parts) == 4:
                last_time = compact_to_hhmm(parts[3].strip())
                close_time = str(tmp.get("admin_early_close") or "").strip()
                if not close_time:
                    return {"ok": send_admin_early_close_menu(bot_token, chat_id, tenant_id)}

                admin_set_today_closed(orders_sh=orders_sh, enabled=False, updated_by=updated_by)
                admin_set_today_open_force(orders_sh=orders_sh, enabled=False, updated_by=updated_by)
                admin_set_today_close_override(orders_sh=orders_sh, close_time=close_time, updated_by=updated_by)
                admin_set_today_last_order_override(orders_sh=orders_sh, last_order_time=last_time, updated_by=updated_by)

                tmp.pop("admin_early_close", None)
                telegram_send_text(
                    bot_token,
                    chat_id,
                    f"✅ Cierre temprano configurado para hoy.\nCierre: {close_time}\nÚltima hora de pedido: {last_time}",
                    reply_markup=admin_root_kb(),
                )
                return {"ok": send_admin_hours_menu(bot_token, chat_id, tenant_id, orders_sh, tenant_tz)}

            if action == "late":
                return {"ok": send_admin_late_open_menu(bot_token, chat_id, tenant_id)}

            if action == "lateopen" and len(parts) == 4:
                open_time = compact_to_hhmm(parts[3].strip())
                admin_set_today_closed(orders_sh=orders_sh, enabled=False, updated_by=updated_by)
                admin_set_today_open_force(orders_sh=orders_sh, enabled=False, updated_by=updated_by)
                admin_set_today_open_override(orders_sh=orders_sh, open_time=open_time, updated_by=updated_by)
                telegram_send_text(
                    bot_token,
                    chat_id,
                    f"✅ Apertura tardía configurada para hoy.\nNueva apertura: {open_time}",
                    reply_markup=admin_root_kb(),
                )
                return {"ok": send_admin_hours_menu(bot_token, chat_id, tenant_id, orders_sh, tenant_tz)}

            if action == "closed":
                admin_set_today_closed(orders_sh=orders_sh, enabled=True, updated_by=updated_by)
                admin_set_today_open_force(orders_sh=orders_sh, enabled=False, updated_by=updated_by)
                admin_set_today_open_override(orders_sh=orders_sh, open_time="", updated_by=updated_by)
                admin_set_today_close_override(orders_sh=orders_sh, close_time="", updated_by=updated_by)
                admin_set_today_last_order_override(orders_sh=orders_sh, last_order_time="", updated_by=updated_by)
                telegram_send_text(
                    bot_token,
                    chat_id,
                    "✅ Hoy quedó marcado como NO abrir.",
                    reply_markup=admin_root_kb(),
                )
                return {"ok": send_admin_hours_menu(bot_token, chat_id, tenant_id, orders_sh, tenant_tz)}

            if action == "openforce":
                admin_set_today_open_force(orders_sh=orders_sh, enabled=True, updated_by=updated_by)
                admin_set_today_closed(orders_sh=orders_sh, enabled=False, updated_by=updated_by)
                admin_set_today_open_override(orders_sh=orders_sh, open_time="", updated_by=updated_by)
                admin_set_today_close_override(orders_sh=orders_sh, close_time="", updated_by=updated_by)
                admin_set_today_last_order_override(orders_sh=orders_sh, last_order_time="", updated_by=updated_by)
                telegram_send_text(
                    bot_token,
                    chat_id,
                    "✅ Hoy quedó marcado como abrir excepcionalmente.",
                    reply_markup=admin_root_kb(),
                )
                return {"ok": send_admin_hours_menu(bot_token, chat_id, tenant_id, orders_sh, tenant_tz)}

            return {"ok": True}

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

            if action == "panel":
                tmp.pop("admin_menu_categories", None)
                tmp.pop("admin_menu_current_category", None)
                tmp.pop("admin_menu_last_sku", None)
                tmp.pop("admin_menu_price_sku", None)
                tmp.pop("admin_menu_price_work", None)
                tmp.pop("admin_menu_input_mode", None)
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
                telegram_send_text(
                    bot_token,
                    chat_id,
                    "✅ Menú refrescado.",
                    reply_markup=admin_root_kb(),
                )
                return {"ok": send_admin_menu_home(bot_token, chat_id, tenant_id, orders_sh, sess)}

            if action == "catrefresh":
                invalidate_menu_cache(orders_sh)
                current_category = str(tmp.get("admin_menu_current_category") or "").strip()
                if not current_category:
                    return {"ok": send_admin_menu_home(bot_token, chat_id, tenant_id, orders_sh, sess)}
                telegram_send_text(
                    bot_token,
                    chat_id,
                    "✅ Categoría refrescada.",
                    reply_markup=admin_root_kb(),
                )
                return {"ok": send_admin_menu_category(bot_token, chat_id, tenant_id, orders_sh, sess, current_category)}

            if action == "cat" and len(parts) == 4:
                try:
                    idx = int(parts[3].strip())
                except Exception:
                    idx = -1

                menu_idx = load_menu_admin_index(orders_sh, force=False)
                cats = group_menu_admin_by_category(menu_idx)
                cat_names = sorted(cats.keys(), key=lambda x: normalize(x))

                if idx < 0 or idx >= len(cat_names):
                    return {"ok": send_admin_menu_home(bot_token, chat_id, tenant_id, orders_sh, sess)}

                category = cat_names[idx]
                return {"ok": send_admin_menu_category(bot_token, chat_id, tenant_id, orders_sh, sess, category)}

            if action == "catback":
                current_category = str(tmp.get("admin_menu_current_category") or "").strip()
                if not current_category:
                    return {"ok": send_admin_menu_home(bot_token, chat_id, tenant_id, orders_sh, sess)}
                return {"ok": send_admin_menu_category(bot_token, chat_id, tenant_id, orders_sh, sess, current_category)}

            if action == "prd" and len(parts) == 4:
                sku = parts[3].strip()
                return {"ok": send_admin_menu_product_detail(bot_token, chat_id, tenant_id, orders_sh, sess, sku)}

            if action == "toggle" and len(parts) == 4:
                sku = parts[3].strip()
                item_before = get_menu_product_or_404(orders_sh, sku)
                new_active = not bool(item_before.get("active", False))
                set_menu_product_active(orders_sh, sku, new_active)
                item_after = get_menu_product_or_404(orders_sh, sku)

                telegram_send_text(
                    bot_token,
                    chat_id,
                    f"✅ Estado actualizado.\nProducto: {item_after.get('name', '')}\nActivo: {'Sí' if item_after.get('active') else 'No'}",
                    reply_markup=admin_root_kb(),
                )
                return {"ok": send_admin_menu_product_detail(bot_token, chat_id, tenant_id, orders_sh, sess, sku)}

            if action == "price" and len(parts) == 4:
                sku = parts[3].strip()
                return {"ok": send_admin_menu_price_editor(bot_token, chat_id, tenant_id, orders_sh, sess, sku)}

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

                tmp.pop("admin_menu_price_sku", None)
                tmp.pop("admin_menu_price_work", None)
                tmp.pop("admin_menu_input_mode", None)

                telegram_send_text(
                    bot_token,
                    chat_id,
                    f"✅ Precio actualizado.\nSKU: {sku}\nNuevo precio: Bs {fmt_price_short(result.get('price', 0))}",
                    reply_markup=admin_root_kb(),
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
                    reply_markup=admin_root_kb(),
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
                    reply_markup=admin_root_kb(),
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
                    reply_markup=admin_root_kb(),
                )
                return {"ok": True}

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
        telegram_send_text(bot_token, chat_id, "⚠️ Ocurrió un error en el panel admin.", reply_markup=admin_root_kb())
        return {"ok": True}
