# app/admin_callbacks.py — callbacks admin sin teclado persistente inferior y con cierre correcto de pedido manual

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
    append_order_row,
    gen_order_id,
    build_items_snapshot,
)
from app.telegram_api import telegram_send_text
from app.telegram_keyboard import kb
from app.utils import normalize, log_event
from app.stats import resolve_period, build_stats_report_text, build_periods
from app.webhook_helpers import (
    get_sess,
    get_client_bot_token,
    assert_admin_authorized,
    fmt_price_short,
    admin_periods_inline_kb,
    fmt_snapshot_lines,
    build_order_recap_text,
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
    alert_order_failed,
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
)
from app.admin_nav import (
    admin_panel_kb,
)


def _finalize_admin_manual_order_from_tmp(
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
                )
                return {"ok": True}

            if not res.get("found"):
                telegram_send_text(
                    bot_token,
                    chat_id,
                    f"⚠️ Pedido {order_id} no encontrado en Sheets.",
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
                    )
                else:
                    telegram_send_text(
                        bot_token,
                        chat_id,
                        f"✅ El pedido de {customer_name}, con código de pedido {order_id}, ha sido confirmado.",
                    )
            else:
                telegram_send_text(
                    bot_token,
                    chat_id,
                    f"✅ El pedido con código de pedido {order_id} ha sido confirmado.",
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

            if action == "noop":
                return {"ok": True}

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
                )
                return {"ok": _send_admin_order_cart(bot_token, chat_id, tenant_id, orders_sh, sess)}

            if action == "inc" and len(parts) == 4:
                sku = parts[3].strip()
                _admin_order_inc_item(tmp, sku)
                return {"ok": _send_admin_order_cart(bot_token, chat_id, tenant_id, orders_sh, sess)}

            if action == "dec" and len(parts) == 4:
                sku = parts[3].strip()
                _admin_order_dec_item(tmp, sku)
                return {"ok": _send_admin_order_cart(bot_token, chat_id, tenant_id, orders_sh, sess)}

            if action == "rem" and len(parts) == 4:
                sku = parts[3].strip()
                _admin_order_remove_item(tmp, sku)
                return {"ok": _send_admin_order_cart(bot_token, chat_id, tenant_id, orders_sh, sess)}

            if action == "cart":
                return {"ok": _send_admin_order_cart(bot_token, chat_id, tenant_id, orders_sh, sess)}

            if action == "clear":
                tmp["admin_order_cart"] = []
                telegram_send_text(
                    bot_token,
                    chat_id,
                    "🧹 Carrito manual vaciado.",
                )
                return {"ok": _send_admin_order_home(bot_token, chat_id, tenant_id, orders_sh, sess)}

            if action == "confirm":
                cart = tmp.get("admin_order_cart") or []
                if not cart:
                    telegram_send_text(
                        bot_token,
                        chat_id,
                        "⚠️ El carrito está vacío.",
                    )
                    return {"ok": _send_admin_order_home(bot_token, chat_id, tenant_id, orders_sh, sess)}

                tmp["admin_order_step"] = "awaiting_name"
                telegram_send_text(
                    bot_token,
                    chat_id,
                    "Escribe el nombre del cliente:",
                )
                return {"ok": True}

            if action == "timenow":
                tmp["admin_order_requested_time"] = "ahora"
                return _finalize_admin_manual_order_from_tmp(
                    tenant_id=tenant_id,
                    bot_token=bot_token,
                    chat_id=chat_id,
                    orders_sh=orders_sh,
                    tmp=tmp,
                )

            if action == "timelater":
                tmp["admin_order_step"] = "awaiting_time_manual"
                telegram_send_text(
                    bot_token,
                    chat_id,
                    "Escribe la hora solicitada.\nEjemplos: 19:30, 20h",
                )
                return {"ok": True}

            return {"ok": True}

        if data.startswith("admhrs|"):
            assert_admin_authorized(tenant, chat_id, tenant_id)
            handled = handle_admin_hours_callback(
                bot_token=bot_token,
                chat_id=chat_id,
                tenant_id=tenant_id,
                data=data,
                orders_sh=orders_sh,
                tenant_tz=tenant_tz,
            )
            if handled.get("ok"):
                return handled
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
                tmp.pop("admin_menu_category_options", None)
                tmp.pop("admin_menu_create_step", None)
                tmp.pop("admin_menu_create_name", None)
                tmp.pop("admin_menu_create_category", None)
                tmp.pop("admin_menu_create_price", None)
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
