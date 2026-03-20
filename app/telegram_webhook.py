import time
from typing import Any, Dict, List

from fastapi import APIRouter, HTTPException

from app.tenants import get_tenant_or_404, resolve_bot_by_secret
from app.sheets import get_gspread_client, open_spreadsheet_by_key
from app.menu import (
    load_menu_index,
    group_menu_by_category,
    load_menu_admin_index,
    group_menu_admin_by_category,
    get_menu_product_or_404,
    set_menu_product_active,
    set_menu_product_price,
    invalidate_menu_cache,
)
from app.orders import (
    append_order_row,
    update_order_status,
    update_order_payment_proof,
    find_latest_pending_order_for_contact,
    get_order_by_id,
    gen_order_id,
    build_items_snapshot,
)
from app.telegram_keyboard import kb
from app.telegram_api import (
    telegram_answer_callback,
    telegram_send_text,
    telegram_send_photo,
    telegram_get_file_path,
    telegram_download_file_bytes,
    telegram_send_file_bytes,
)
from app.utils import normalize, log_event
from app.stats import build_periods, resolve_period, build_stats_report_text, log_event_to_sheet
from app.image_storage import upload_product_photo_for_tenant

from app.webhook_helpers import (
    REMINDER_COOLDOWN_SECONDS,
    CONTACT_AFTER_SECONDS,
    get_sess,
    clear_sess,
    get_admin_bot_token,
    get_client_bot_token,
    get_admin_chat_id,
    get_payment_qr_file_id,
    get_payment_qr_url,
    parse_items_field,
    fmt_cart_lines,
    fmt_snapshot_lines,
    build_order_recap_text,
    fmt_price_short,
    extract_first_number,
    get_business_status_safe,
    send_business_blocked_text,
    safe_int,
    assert_admin_authorized,
    contact_link_for_admin,
    set_menu_photo_url,
    client_home_kb,
    cart_kb,
    i_paid_kb,
    paid_actions_kb,
    contact_admin_kb,
    admin_fixed_kb,
    admin_periods_inline_kb,
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

router = APIRouter()


def client_orders_allowed_or_notify(bot_token: str, chat_id: int, orders_sh, tenant_tz: str) -> bool:
    bs = get_business_status_safe(orders_sh=orders_sh, tenant_tz=tenant_tz)
    if bool(bs.get("accepts_orders_now")):
        return True
    telegram_send_text(bot_token, chat_id, send_business_blocked_text(bs))
    return False


def forward_proof_to_admin(
    tenant: Dict[str, Any],
    tenant_id: str,
    proof_file_id: str,
    proof_type: str,
    proof_caption: str,
) -> bool:
    client_token = get_client_bot_token(tenant)
    admin_token = get_admin_bot_token(tenant)
    admin_chat_id = get_admin_chat_id(tenant)

    if not client_token or not admin_token or not admin_chat_id:
        log_event(
            "forward_proof_missing_config",
            tenant_id=tenant_id,
            has_client=bool(client_token),
            has_admin=bool(admin_token),
            has_admin_chat=bool(admin_chat_id),
        )
        return False

    try:
        file_path = telegram_get_file_path(client_token, proof_file_id)
        file_bytes = telegram_download_file_bytes(client_token, file_path)
        filename = file_path.split("/")[-1] if file_path else "proof"
        caption = proof_caption or ("Comprobante (foto)" if proof_type == "photo" else "Comprobante (archivo)")

        if proof_type == "photo":
            return telegram_send_file_bytes(
                bot_token=admin_token,
                method="sendPhoto",
                chat_id=admin_chat_id,
                file_field="photo",
                filename=filename or "proof.jpg",
                content_type="image/jpeg",
                file_bytes=file_bytes,
                caption=caption,
            )

        return telegram_send_file_bytes(
            bot_token=admin_token,
            method="sendDocument",
            chat_id=admin_chat_id,
            file_field="document",
            filename=filename or "proof.pdf",
            content_type="application/octet-stream",
            file_bytes=file_bytes,
            caption=caption,
        )

    except Exception as e:
        log_event("forward_proof_failed", tenant_id=tenant_id, error=str(e))
        return False


def notify_admin_payment_reported(
    tenant: Dict[str, Any],
    tenant_id: str,
    orders_sh,
    order_id: str,
    is_reminder: bool = False,
) -> bool:
    admin_token = get_admin_bot_token(tenant)
    admin_chat_id = get_admin_chat_id(tenant)

    if not admin_token or not admin_chat_id:
        log_event("admin_notify_failed", tenant_id=tenant_id, reason="missing_admin_token_or_chat")
        return False

    order = get_order_by_id(orders_sh, order_id)
    if not order:
        telegram_send_text(admin_token, admin_chat_id, f"⚠️ Pedido {order_id} no encontrado en Sheets.")
        return False

    items_snapshot = parse_items_field(order.get("items_snapshot"))
    if items_snapshot:
        lines_txt, snapshot_total, total_qty = fmt_snapshot_lines(items_snapshot)
        total = snapshot_total
    else:
        try:
            menu_idx = load_menu_index(orders_sh)
        except Exception as e:
            log_event("admin_menu_load_error", tenant_id=tenant_id, error=str(e))
            menu_idx = {}
        cart = parse_items_field(order.get("items"))
        lines_txt, _, total_qty = fmt_cart_lines(cart, menu_idx)
        try:
            total = float(order.get("total_amount") or 0)
        except Exception:
            total = 0.0

    proof_file_id = (order.get("payment_proof_file_id") or "").strip()
    proof_type = (order.get("payment_proof_type") or "").strip()
    proof_caption = (order.get("payment_proof_caption") or "").strip()

    confirm_btn = kb([[("✅ Confirmar pago", f"paid|{tenant_id}|{order_id}")]])

    title = "🔔 RECORDATORIO — PAGO REPORTADO" if is_reminder else "💳 PAGO REPORTADO"
    txt = (
        f"{title}\n\n"
        f"Tenant: {tenant_id}\n"
        f"ID: {order_id}\n"
        f"Cliente: {order.get('customer_name','')}\n"
        f"Contacto(chat_id): {order.get('customer_contact','')}\n"
        f"Hora recogida: {order.get('requested_time','pendiente')}\n"
        f"Cantidad total: {total_qty}\n"
        f"Total: {total:.2f} BOB\n\n"
        f"Detalle:\n{lines_txt}\n\n"
        "Presiona ✅ Confirmar pago cuando verifiques."
    )

    ok_txt = telegram_send_text(admin_token, admin_chat_id, txt, reply_markup=confirm_btn)

    ok_proof = False
    if proof_file_id and proof_type:
        ok_proof = forward_proof_to_admin(tenant, tenant_id, proof_file_id, proof_type, proof_caption)
    else:
        log_event("admin_missing_proof", tenant_id=tenant_id, order_id=order_id)

    log_event(
        "admin_notify_result",
        tenant_id=tenant_id,
        order_id=order_id,
        ok_txt=bool(ok_txt),
        ok_proof=bool(ok_proof),
        is_reminder=bool(is_reminder),
    )
    return bool(ok_txt)


@router.post("/telegram/webhook/{tenant_id}/{secret}")
async def telegram_webhook(tenant_id: str, secret: str, update: Dict[str, Any]):
    tenant_id = (tenant_id or "").strip()
    if not tenant_id:
        return {"ok": True}

    gc = get_gspread_client()
    tenant = get_tenant_or_404(tenant_id, gc=gc)

    mode, bot_token = resolve_bot_by_secret(tenant, secret)
    if not bot_token:
        return {"ok": True}

    orders_sheet_id = (tenant.get("orders_sheet_id") or "").strip()
    if not orders_sheet_id:
        raise HTTPException(status_code=500, detail=f"orders_sheet_id missing for tenant: {tenant_id}")

    orders_sh = open_spreadsheet_by_key(gc, orders_sheet_id)
    tenant_tz = (tenant.get("timezone") or "America/La_Paz").strip()

    cb = update.get("callback_query")
    if cb:
        data = (cb.get("data") or "").strip()
        cb_id = cb.get("id")

        msg_obj = cb.get("message") or {}
        chat_obj = msg_obj.get("chat") or {}
        chat_id = safe_int(chat_obj.get("id"))
        if chat_id is None:
            log_event("callback_missing_chat_id", tenant_id=tenant_id, data=data)
            return {"ok": True}

        if cb_id:
            telegram_answer_callback(bot_token, cb_id, "OK")

        if mode == "admin" and data.startswith("paid|"):
            parts = data.split("|")
            if len(parts) != 3:
                return {"ok": True}

            cb_tenant_id = parts[1].strip()
            order_id = parts[2].strip()

            if cb_tenant_id != tenant_id:
                raise HTTPException(status_code=400, detail="Tenant mismatch in callback")

            assert_admin_authorized(tenant, chat_id, tenant_id)

            res = update_order_status(orders_sh, order_id, "PAID")
            if not res.get("found"):
                telegram_send_text(bot_token, chat_id, f"⚠️ Pedido {order_id} no encontrado en Sheets.", reply_markup=admin_fixed_kb())
                return {"ok": True}

            telegram_send_text(bot_token, chat_id, f"✅ Pedido {order_id} marcado como PAID", reply_markup=admin_fixed_kb())

            order = get_order_by_id(orders_sh, order_id)
            if order:
                client_token = get_client_bot_token(tenant)
                client_chat = (order.get("customer_contact") or "").strip()
                if client_token and client_chat:
                    try:
                        telegram_send_text(client_token, int(client_chat), f"✅ Pago validado. Tu pedido {order_id} fue confirmado. ¡Gracias!")
                    except Exception as e:
                        log_event("notify_client_paid_failed", tenant_id=tenant_id, order_id=order_id, error=str(e))

            return {"ok": True}

        if mode == "admin" and data.startswith("admin_stats_period|"):
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

            telegram_send_text(bot_token, chat_id, txt, reply_markup=admin_fixed_kb())
            return {"ok": True}

        if mode == "admin" and data.startswith("admhrs|"):
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
                telegram_send_text(bot_token, chat_id, "✅ Se restauró la configuración habitual de hoy.")
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

                telegram_send_text(bot_token, chat_id, msg)
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
                )
                return {"ok": send_admin_hours_menu(bot_token, chat_id, tenant_id, orders_sh, tenant_tz)}

            if action == "closed":
                admin_set_today_closed(orders_sh=orders_sh, enabled=True, updated_by=updated_by)
                admin_set_today_open_force(orders_sh=orders_sh, enabled=False, updated_by=updated_by)
                admin_set_today_open_override(orders_sh=orders_sh, open_time="", updated_by=updated_by)
                admin_set_today_close_override(orders_sh=orders_sh, close_time="", updated_by=updated_by)
                admin_set_today_last_order_override(orders_sh=orders_sh, last_order_time="", updated_by=updated_by)
                telegram_send_text(bot_token, chat_id, "✅ Hoy quedó marcado como NO abrir.")
                return {"ok": send_admin_hours_menu(bot_token, chat_id, tenant_id, orders_sh, tenant_tz)}

            if action == "openforce":
                admin_set_today_open_force(orders_sh=orders_sh, enabled=True, updated_by=updated_by)
                admin_set_today_closed(orders_sh=orders_sh, enabled=False, updated_by=updated_by)
                admin_set_today_open_override(orders_sh=orders_sh, open_time="", updated_by=updated_by)
                admin_set_today_close_override(orders_sh=orders_sh, close_time="", updated_by=updated_by)
                admin_set_today_last_order_override(orders_sh=orders_sh, last_order_time="", updated_by=updated_by)
                telegram_send_text(bot_token, chat_id, "✅ Hoy quedó marcado como abrir excepcionalmente.")
                return {"ok": send_admin_hours_menu(bot_token, chat_id, tenant_id, orders_sh, tenant_tz)}

            return {"ok": True}

        if mode == "admin" and data.startswith("admmenu|"):
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
                telegram_send_text(bot_token, chat_id, "Panel admin:", reply_markup=admin_fixed_kb())
                return {"ok": True}

            if action == "home":
                return {"ok": send_admin_menu_home(bot_token, chat_id, tenant_id, orders_sh, sess)}

            if action == "refresh":
                invalidate_menu_cache(orders_sh)
                telegram_send_text(bot_token, chat_id, "✅ Menú refrescado.")
                return {"ok": send_admin_menu_home(bot_token, chat_id, tenant_id, orders_sh, sess)}

            if action == "catrefresh":
                invalidate_menu_cache(orders_sh)
                current_category = str(tmp.get("admin_menu_current_category") or "").strip()
                if not current_category:
                    return {"ok": send_admin_menu_home(bot_token, chat_id, tenant_id, orders_sh, sess)}
                telegram_send_text(bot_token, chat_id, "✅ Categoría refrescada.")
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
                    f"✅ Estado actualizado.\nProducto: {item_after.get('name','')}\nActivo: {'Sí' if item_after.get('active') else 'No'}",
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
                        f"Producto: {item.get('name','')}\n"
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
                        f"Producto: {item.get('name','')}\n"
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
                    f"📷 Envía ahora la foto para:\n{item.get('name','')}"
                )
                return {"ok": True}

            return {"ok": True}

        if mode == "client":
            sess = get_sess(tenant_id, chat_id)
            tmp = sess.get("tmp") or {}
            sess["tmp"] = tmp

            if data == "home":
                telegram_send_text(bot_token, chat_id, "Elige una opción:", client_home_kb())
                return {"ok": True}

            if data == "menu":
                if not client_orders_allowed_or_notify(bot_token, chat_id, orders_sh, tenant_tz):
                    return {"ok": True}

                menu_idx = load_menu_index(orders_sh)
                cats = group_menu_by_category(menu_idx)

                if not cats:
                    telegram_send_text(bot_token, chat_id, "No hay menú activo.", client_home_kb())
                    return {"ok": True}

                rows = []
                for c in sorted(cats.keys(), key=lambda x: normalize(x)):
                    rows.append([(c, f"cat|{normalize(c)}")])
                rows.append([("🛒 Carrito", "cart")])
                rows.append([("🏠 Inicio", "home")])

                telegram_send_text(bot_token, chat_id, "📋 Elige una categoría:", kb(rows))
                return {"ok": True}

            if data.startswith("cat|"):
                if not client_orders_allowed_or_notify(bot_token, chat_id, orders_sh, tenant_tz):
                    return {"ok": True}

                cat_norm = data.split("|", 1)[1].strip()

                menu_idx = load_menu_index(orders_sh)
                cats = group_menu_by_category(menu_idx)

                real_cat = None
                for c in cats.keys():
                    if normalize(c) == cat_norm:
                        real_cat = c
                        break

                if not real_cat:
                    telegram_send_text(bot_token, chat_id, "Categoría no encontrada.", reply_markup=client_home_kb())
                    return {"ok": True}

                items = cats.get(real_cat, [])
                if not items:
                    telegram_send_text(bot_token, chat_id, "No hay productos activos.", reply_markup=client_home_kb())
                    return {"ok": True}

                rows = []
                for it in items[:25]:
                    rows.append([(f"{it['name']} ({it['price']:.0f})", f"prd|{it['sku']}")])
                rows.append([("🛒 Carrito", "cart")])
                rows.append([("⬅️ Categorías", "menu")])
                rows.append([("🏠 Inicio", "home")])

                telegram_send_text(bot_token, chat_id, f"🍽 {real_cat} — elige un producto:", kb(rows))

                for it in items:
                    photo_url = str(it.get("photo_url") or "").strip()
                    photo_file_id = str(it.get("photo_file_id") or "").strip()

                    if photo_url:
                        telegram_send_photo(
                            bot_token,
                            chat_id,
                            photo_url,
                            caption=f"{it['name']}\nBs {it['price']}",
                        )
                    elif photo_file_id:
                        telegram_send_photo(
                            bot_token,
                            chat_id,
                            photo_file_id,
                            caption=f"{it['name']}\nBs {it['price']}",
                        )

                return {"ok": True}

            if data.startswith("prd|"):
                if not client_orders_allowed_or_notify(bot_token, chat_id, orders_sh, tenant_tz):
                    return {"ok": True}

                sku = data.split("|", 1)[1].strip()
                rows = [
                    [("1", f"qty|{sku}|1"), ("2", f"qty|{sku}|2"), ("3", f"qty|{sku}|3"), ("4", f"qty|{sku}|4")],
                    [("🛒 Carrito", "cart")],
                    [("⬅️ Volver", "menu")],
                    [("🏠 Inicio", "home")],
                ]
                telegram_send_text(bot_token, chat_id, "Selecciona cantidad:", kb(rows))
                return {"ok": True}

            if data.startswith("qty|"):
                if not client_orders_allowed_or_notify(bot_token, chat_id, orders_sh, tenant_tz):
                    return {"ok": True}

                parts = data.split("|")
                if len(parts) != 3:
                    return {"ok": True}

                _, sku, qty_s = parts
                try:
                    qty = int(qty_s)
                except Exception:
                    qty = 1
                qty = max(1, qty)

                menu_idx = load_menu_index(orders_sh)
                if sku not in menu_idx:
                    telegram_send_text(bot_token, chat_id, "Producto no disponible.", reply_markup=client_home_kb())
                    return {"ok": True}

                cart = sess.get("cart") or []
                found = False
                for it in cart:
                    if it.get("sku") == sku:
                        it["qty"] = int(it.get("qty") or 0) + qty
                        found = True
                        break
                if not found:
                    cart.append({"sku": sku, "qty": qty})
                sess["cart"] = cart

                _, total, total_qty = fmt_cart_lines(cart, menu_idx)
                name = menu_idx[sku]["name"]

                telegram_send_text(
                    bot_token,
                    chat_id,
                    f"✅ Agregado al carrito: {qty} x {name}\n\nCantidad: {total_qty}\nTotal: {total:.2f} BOB",
                    reply_markup=kb([
                        [("🛒 Ver carrito", "cart")],
                        [("⬅️ Seguir comprando", "menu")],
                        [("🏠 Inicio", "home")],
                    ]),
                )
                return {"ok": True}

            if data == "cart":
                menu_idx = load_menu_index(orders_sh)
                cart = sess.get("cart") or []
                lines_txt, total, total_qty = fmt_cart_lines(cart, menu_idx)

                has_items = total_qty > 0
                msg = (
                    f"🛒 *Tu carrito*\n"
                    f"Cantidad: *{total_qty}*\n"
                    f"Total: *{total:.2f}* BOB\n\n"
                    f"{lines_txt}"
                )
                telegram_send_text(bot_token, chat_id, msg, reply_markup=cart_kb(has_items), parse_mode="Markdown")
                return {"ok": True}

            if data == "cart_clear":
                sess["cart"] = []
                sess["stage"] = "idle"
                sess["tmp"] = {}
                telegram_send_text(bot_token, chat_id, "🧹 Carrito vaciado.", reply_markup=client_home_kb())
                return {"ok": True}

            if data == "cart_confirm":
                if not client_orders_allowed_or_notify(bot_token, chat_id, orders_sh, tenant_tz):
                    return {"ok": True}

                cart = sess.get("cart") or []
                if not cart:
                    telegram_send_text(bot_token, chat_id, "Tu carrito está vacío.", reply_markup=client_home_kb())
                    return {"ok": True}

                sess["stage"] = "awaiting_name"
                telegram_send_text(bot_token, chat_id, "Perfecto. ¿Cuál es tu *nombre* para el pedido?", parse_mode="Markdown")
                return {"ok": True}

            if data.startswith("i_paid|"):
                parts = data.split("|")
                if len(parts) != 3:
                    return {"ok": True}

                _, cb_tenant_id, order_id = parts
                cb_tenant_id = cb_tenant_id.strip()
                order_id = order_id.strip()

                if cb_tenant_id != tenant_id:
                    raise HTTPException(status_code=400, detail="Tenant mismatch in i_paid callback")

                order = get_order_by_id(orders_sh, order_id)
                if not order:
                    telegram_send_text(bot_token, chat_id, "No encontré tu pedido. Vuelve a /start.", reply_markup=client_home_kb())
                    return {"ok": True}

                proof_file_id = (order.get("payment_proof_file_id") or "").strip()
                if not proof_file_id:
                    telegram_send_text(bot_token, chat_id, "Aún no recibí tu comprobante.\nEnvía una foto o PDF del pago primero.")
                    return {"ok": True}

                ok_sent = notify_admin_payment_reported(tenant, tenant_id, orders_sh, order_id, is_reminder=False)

                tmp["paid_pressed_at_ts"] = int(time.time())
                tmp["last_notified_order_id"] = order_id
                tmp["last_admin_notify_ok"] = bool(ok_sent)

                telegram_send_text(
                    bot_token,
                    chat_id,
                    "✅ Recibido. Espera unos minutos mientras verificamos tu pago.\n"
                    "Si no hay respuesta, podrás enviar un recordatorio.",
                    reply_markup=paid_actions_kb(tenant_id, order_id),
                )
                return {"ok": True}

            if data.startswith("remind|"):
                parts = data.split("|")
                if len(parts) != 3:
                    return {"ok": True}

                _, cb_tenant_id, order_id = parts
                cb_tenant_id = cb_tenant_id.strip()
                order_id = order_id.strip()

                if cb_tenant_id != tenant_id:
                    raise HTTPException(status_code=400, detail="Tenant mismatch in remind callback")

                paid_at = int(tmp.get("paid_pressed_at_ts") or 0)
                now = int(time.time())

                if not paid_at:
                    telegram_send_text(bot_token, chat_id, "Primero presiona “✅ Ya pagué”.", reply_markup=paid_actions_kb(tenant_id, order_id))
                    return {"ok": True}

                if (now - paid_at) < REMINDER_COOLDOWN_SECONDS:
                    left = REMINDER_COOLDOWN_SECONDS - (now - paid_at)
                    mins = max(1, int((left + 59) / 60))
                    telegram_send_text(
                        bot_token,
                        chat_id,
                        f"🙏 Gracias. Por favor espera un momento.\nPodrás enviar un recordatorio en aproximadamente *{mins} minuto(s)*.",
                        reply_markup=paid_actions_kb(tenant_id, order_id),
                        parse_mode="Markdown",
                    )
                    return {"ok": True}

                ok_sent = notify_admin_payment_reported(tenant, tenant_id, orders_sh, order_id, is_reminder=True)
                tmp["reminder_sent_at_ts"] = now
                tmp["last_admin_reminder_ok"] = bool(ok_sent)

                if ok_sent:
                    telegram_send_text(
                        bot_token,
                        chat_id,
                        "🔔 Listo. Enviamos un *recordatorio* al administrador.\n"
                        "Si no responde, en unos minutos podrás contactarlo directamente.",
                        reply_markup=contact_admin_kb(tenant_id, order_id),
                        parse_mode="Markdown",
                    )
                else:
                    telegram_send_text(
                        bot_token,
                        chat_id,
                        "😕 Intenté enviar el recordatorio, pero falló.\nIntenta nuevamente en unos segundos.",
                        reply_markup=paid_actions_kb(tenant_id, order_id),
                    )
                return {"ok": True}

            if data.startswith("contact|"):
                parts = data.split("|")
                if len(parts) != 3:
                    return {"ok": True}

                _, cb_tenant_id, order_id = parts
                cb_tenant_id = cb_tenant_id.strip()
                order_id = order_id.strip()

                if cb_tenant_id != tenant_id:
                    raise HTTPException(status_code=400, detail="Tenant mismatch in contact callback")

                paid_at = int(tmp.get("paid_pressed_at_ts") or 0)
                now = int(time.time())

                if not paid_at:
                    telegram_send_text(bot_token, chat_id, "Primero presiona “✅ Ya pagué”.", reply_markup=paid_actions_kb(tenant_id, order_id))
                    return {"ok": True}

                if (now - paid_at) < CONTACT_AFTER_SECONDS:
                    left = CONTACT_AFTER_SECONDS - (now - paid_at)
                    mins = max(1, int((left + 59) / 60))
                    telegram_send_text(
                        bot_token,
                        chat_id,
                        f"🙏 Aún es pronto.\nPodrás contactar al administrador en aproximadamente *{mins} minuto(s)*.",
                        reply_markup=contact_admin_kb(tenant_id, order_id),
                        parse_mode="Markdown",
                    )
                    return {"ok": True}

                link = contact_link_for_admin(tenant)
                if not link:
                    telegram_send_text(bot_token, chat_id, "No tengo configurado el contacto directo del administrador.", reply_markup=client_home_kb())
                    return {"ok": True}

                telegram_send_text(bot_token, chat_id, "💬 Contacto directo habilitado.\nToca el enlace para escribirle al administrador:")
                telegram_send_text(bot_token, chat_id, link)
                return {"ok": True}

            return {"ok": True}

        return {"ok": True}

    msg = update.get("message") or update.get("edited_message")
    if msg:
        chat_id = safe_int((msg.get("chat") or {}).get("id"))
        if chat_id is None:
            return {"ok": True}

        text = (msg.get("text") or "").strip()

        if normalize(text) in ("/id", "id"):
            telegram_send_text(bot_token, chat_id, f"chat_id = {chat_id}")
            return {"ok": True}

        if mode == "client":
            sess = get_sess(tenant_id, chat_id)

            proof_file_id = None
            proof_type = None
            proof_caption = (msg.get("caption") or "").strip()

            if msg.get("photo"):
                proof_file_id = msg["photo"][-1].get("file_id")
                proof_type = "photo"
            elif msg.get("document"):
                proof_file_id = (msg.get("document") or {}).get("file_id")
                proof_type = "document"
                if not proof_caption:
                    proof_caption = ((msg.get("document") or {}).get("file_name") or "").strip()

            if proof_file_id and proof_type:
                order_id = (sess.get("tmp") or {}).get("pending_order_id")
                if not order_id:
                    order_id = find_latest_pending_order_for_contact(
                        orders_sh=orders_sh,
                        customer_contact=str(chat_id),
                        status="PENDING_PAYMENT",
                    )

                if not order_id:
                    telegram_send_text(bot_token, chat_id, "No encontré un pedido pendiente. Crea uno nuevo con /start.", reply_markup=client_home_kb())
                    return {"ok": True}

                update_order_payment_proof(
                    orders_sh=orders_sh,
                    order_id=order_id,
                    proof_file_id=proof_file_id,
                    proof_type=proof_type,
                    proof_caption=proof_caption,
                )

                telegram_send_text(
                    bot_token,
                    chat_id,
                    "✅ Comprobante recibido.\nAhora presiona “✅ Ya pagué” para avisar al administrador.",
                    reply_markup=i_paid_kb(tenant_id, order_id),
                )
                return {"ok": True}

            if normalize(text) in ("start", "/start", "hola"):
                clear_sess(tenant_id, chat_id)

                log_event_to_sheet(
                    orders_sh=orders_sh,
                    tenant_id=tenant_id,
                    chat_id=str(chat_id),
                    event_type="client_start",
                    meta={"source": "telegram", "text": text[:50]},
                )

                bs = get_business_status_safe(orders_sh=orders_sh, tenant_tz=tenant_tz)
                if not bool(bs.get("accepts_orders_now")):
                    telegram_send_text(bot_token, chat_id, send_business_blocked_text(bs))
                    return {"ok": True}

                telegram_send_text(bot_token, chat_id, "Bienvenido 👋\nElige una opción:", client_home_kb())
                return {"ok": True}

            if sess.get("stage") == "awaiting_name":
                if not client_orders_allowed_or_notify(bot_token, chat_id, orders_sh, tenant_tz):
                    sess["stage"] = "idle"
                    return {"ok": True}

                customer_name = text.strip()
                if not customer_name:
                    telegram_send_text(bot_token, chat_id, "Dime tu nombre, por favor.")
                    return {"ok": True}

                menu_idx = load_menu_index(orders_sh)
                cart = sess.get("cart") or []

                items_list: List[Dict[str, Any]] = []
                for it in cart:
                    sku = str(it.get("sku") or "").strip()
                    if not sku:
                        continue
                    try:
                        qty = int(it.get("qty") or 1)
                    except Exception:
                        qty = 1
                    qty = max(1, qty)
                    if sku in menu_idx:
                        items_list.append({"sku": sku, "qty": qty})

                if not items_list:
                    telegram_send_text(bot_token, chat_id, "Tu carrito está vacío.", reply_markup=client_home_kb())
                    sess["stage"] = "idle"
                    return {"ok": True}

                items_snapshot = build_items_snapshot(items_list, menu_idx)
                lines_real, total_real, total_qty_real = fmt_snapshot_lines(items_snapshot)

                order_id = gen_order_id()
                requested_time = "pendiente"

                append_order_row(
                    orders_sh=orders_sh,
                    tenant_id=tenant_id,
                    order_id=order_id,
                    customer_name=customer_name,
                    customer_contact=str(chat_id),
                    items=items_list,
                    items_snapshot=items_snapshot,
                    currency="BOB",
                    pricing_version="v1",
                    delivery_type="pickup",
                    requested_time=requested_time,
                    status="PENDING_PAYMENT",
                    source="telegram",
                    total_amount=total_real,
                )

                sess["stage"] = "awaiting_proof"
                sess["tmp"] = sess.get("tmp") or {}
                sess["tmp"]["pending_order_id"] = order_id
                sess["tmp"]["customer_name"] = customer_name

                recap = build_order_recap_text(
                    order_id=order_id,
                    customer_name=customer_name,
                    customer_contact=str(chat_id),
                    requested_time=requested_time,
                    detail_lines=lines_real,
                    total_qty=total_qty_real,
                    total=total_real,
                )

                telegram_send_text(
                    bot_token,
                    chat_id,
                    recap + "\n💳 *Ahora realiza el pago.*\nTe enviamos el QR a continuación.",
                    parse_mode="Markdown",
                )

                qr_file_id = get_payment_qr_file_id(tenant)
                qr_url = get_payment_qr_url(tenant)

                if qr_file_id:
                    telegram_send_photo(bot_token, chat_id, qr_file_id, caption="QR de pago")
                elif qr_url:
                    telegram_send_photo(bot_token, chat_id, qr_url, caption="QR de pago")
                else:
                    telegram_send_text(bot_token, chat_id, "⚠️ No tengo QR configurado para este tenant (payment_qr_file_id / payment_qr_url).")
                    log_event("missing_qr_config", tenant_id=tenant_id)

                telegram_send_text(
                    bot_token,
                    chat_id,
                    "📎 Cuando pagues, envía aquí tu *comprobante* (foto o PDF).\n"
                    "Después de enviarlo, podrás presionar “✅ Ya pagué”.",
                    parse_mode="Markdown",
                )
                return {"ok": True}

            telegram_send_text(bot_token, chat_id, "Usa /start para ver el menú.", reply_markup=client_home_kb())
            return {"ok": True}

        if mode == "admin":
            txt_norm = normalize(text)
            sess = get_sess(tenant_id, chat_id)
            tmp = sess.setdefault("tmp", {})

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
                        return {"ok": True}

                    found = set_menu_photo_url(orders_sh, input_sku, photo_url)

                    if not found:
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

                    return {"ok": send_admin_menu_product_detail(
                        bot_token, chat_id, tenant_id, orders_sh, sess, input_sku
                    )}

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

        return {"ok": True}

    return {"ok": True}
