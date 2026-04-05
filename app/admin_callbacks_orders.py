# app/admin_callbacks_orders.py

from typing import Any, Dict, Optional

from fastapi import HTTPException

from app.orders import (
    get_order_by_id,
    update_order_status,
)
from app.menu import get_menu_product_or_404
from app.telegram_api import telegram_send_text
from app.telegram_keyboard import kb
from app.utils import log_event
from app.webhook_helpers import (
    get_sess,
    get_client_bot_token,
    assert_admin_authorized,
    fmt_snapshot_lines,
    build_order_recap_text,
    parse_items_field,
    admin_fixed_kb,
)
from app.alerts import (
    alert_order_status_failed,
)
from app.admin_helpers import (
    _safe_str,
    _safe_client_chat_id_from_order,
    _extract_slot_hhmm,
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
from app.admin_nav import admin_panel_kb
from app.admin_survey_runtime import (
    clear_admin_survey_runtime,
    send_admin_survey_runtime_question,
    finalize_admin_survey_runtime,
)
from app.admin_order_runtime import (
    finalize_admin_manual_order_from_tmp,
)
from app.survey import (
    get_survey_reward_text,
    get_runtime_survey_questions,
)


def handle_admin_orders_callback(
    tenant: Dict[str, Any],
    tenant_id: str,
    bot_token: str,
    chat_id: int,
    data: str,
    orders_sh,
    tenant_tz: str,
    get_effective_admin_role,
) -> Optional[Dict[str, Any]]:
    if data == "admin_order":
        assert_admin_authorized(tenant, chat_id, tenant_id)

        user_role = get_effective_admin_role(tenant, chat_id)
        if user_role == "owner":
            telegram_send_text(
                bot_token,
                chat_id,
                "🚫 Esta opción no está disponible para el propietario.",
            )
            return {"ok": True}

        sess = get_sess(tenant_id, chat_id)
        tmp = sess.setdefault("tmp", {})
        _admin_order_reset(tmp)
        tmp["admin_order_cart"] = []
        return {"ok": _send_admin_order_home(bot_token, chat_id, tenant_id, orders_sh, sess)}

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
            customer_contact = _safe_str(order_after.get("customer_contact"))
            requested_time = _safe_str(order_after.get("requested_time"))

            items_snapshot = parse_items_field(order_after.get("items_snapshot"))
            detail_lines, total_amount, total_qty = fmt_snapshot_lines(items_snapshot)

            recap = build_order_recap_text(
                order_id=order_id,
                customer_name=customer_name,
                customer_contact=customer_contact,
                requested_time=requested_time,
                detail_lines=detail_lines,
                total_qty=total_qty,
                total=total_amount,
            )

            admin_msg = (
                "✅ *Pago confirmado correctamente.*\n\n"
                f"{recap}"
            )

            telegram_send_text(
                bot_token,
                chat_id,
                admin_msg,
                parse_mode="Markdown",
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

        if order_after:
            try:
                owner_enabled = str(tenant.get("owner_enabled") or "").strip().lower() == "true"
                owner_chat = str(tenant.get("owner_chat_id") or "").strip()
                owner_token = str(tenant.get("owner_bot_token") or "").strip()

                if owner_enabled and owner_chat and owner_token:
                    customer_name = _safe_str(order_after.get("customer_name"))
                    customer_contact = _safe_str(order_after.get("customer_contact"))
                    requested_time = _safe_str(order_after.get("requested_time"))

                    items_snapshot = parse_items_field(order_after.get("items_snapshot"))
                    detail_lines, total_amount, total_qty = fmt_snapshot_lines(items_snapshot)

                    owner_recap = build_order_recap_text(
                        order_id=order_id,
                        customer_name=customer_name,
                        customer_contact=customer_contact,
                        requested_time=requested_time,
                        detail_lines=detail_lines,
                        total_qty=total_qty,
                        total=total_amount,
                    )

                    owner_msg = (
                        "✅ *Pedido confirmado por el administrador.*\n\n"
                        f"{owner_recap}"
                    )

                    telegram_send_text(
                        owner_token,
                        int(owner_chat),
                        owner_msg,
                        parse_mode="Markdown",
                    )
            except Exception as e:
                log_event(
                    "notify_owner_paid_validated_failed",
                    tenant_id=tenant_id,
                    order_id=order_id,
                    error=str(e),
                )

        return {"ok": True}

    if not data.startswith("admord|"):
        return None

    assert_admin_authorized(tenant, chat_id, tenant_id)

    user_role = get_effective_admin_role(tenant, chat_id)
    if user_role == "owner":
        telegram_send_text(
            bot_token,
            chat_id,
            "🚫 Esta opción no está disponible para el propietario.",
        )
        return {"ok": True}

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
        user_role = get_effective_admin_role(tenant, chat_id)
        telegram_send_text(
            bot_token,
            chat_id,
            "🧭 PANEL ADMIN\n\nElige una opción:",
            reply_markup=admin_panel_kb(user_role=user_role),
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
        return finalize_admin_manual_order_from_tmp(
            tenant_id=tenant_id,
            bot_token=bot_token,
            chat_id=chat_id,
            orders_sh=orders_sh,
            tmp=tmp,
            tenant=tenant,
        )

    if action == "timelater":
        tmp["admin_order_step"] = "awaiting_time_manual"
        telegram_send_text(
            bot_token,
            chat_id,
            "Escribe la hora solicitada.\nEjemplos: 19:30, 20h",
        )
        return {"ok": True}

    if action == "proof":
        last_order_id = str(tmp.get("admin_order_last_id") or "").strip()
        if not last_order_id:
            telegram_send_text(
                bot_token,
                chat_id,
                "⚠️ No encontré el pedido recién creado.",
            )
            return {"ok": True}

        tmp["admin_order_waiting_proof"] = True
        tmp["admin_order_proof_received"] = False

        telegram_send_text(
            bot_token,
            chat_id,
            f"📷 Envía la foto del comprobante para el pedido {last_order_id}.",
        )
        return {"ok": True}

    if action == "proof_ok":
        if not bool(tmp.get("admin_order_proof_received")):
            telegram_send_text(
                bot_token,
                chat_id,
                "⚠️ Aún no recibí la foto del comprobante.",
            )
            return {"ok": True}

        telegram_send_text(
            bot_token,
            chat_id,
            "✅ Fotografía confirmada. Ahora puedes abrir la encuesta.",
            reply_markup=kb([
                [("📝 Encuesta", f"admord|{tenant_id}|survey")],
                [("🧭 Panel admin", "admin_panel")],
            ]),
        )
        return {"ok": True}

    if action == "survey":
        questions = get_runtime_survey_questions(orders_sh)
        if not questions:
            telegram_send_text(
                bot_token,
                chat_id,
                "⚠️ No hay preguntas activas configuradas para la encuesta.",
            )
            return {"ok": True}

        customer_phone = str(tmp.get("admin_order_last_phone") or "").strip()
        customer_name = str(tmp.get("admin_order_last_name") or "").strip()

        if not customer_phone:
            telegram_send_text(
                bot_token,
                chat_id,
                "⚠️ No encontré el número del cliente del pedido.",
            )
            return {"ok": True}

        reward_text = get_survey_reward_text(orders_sh)
        tmp["admin_survey_runtime"] = True
        tmp["admin_survey_step"] = "start"
        tmp["admin_survey_answers"] = []
        tmp["admin_survey_phone"] = customer_phone
        tmp["admin_survey_name"] = customer_name

        intro = "📝 Iniciaremos la encuesta del cliente."
        if reward_text:
            intro += f"\n🎁 Recompensa configurada: {reward_text}"
        intro += "\n\nUsaremos los datos del pedido ya registrado."

        telegram_send_text(
            bot_token,
            chat_id,
            intro,
        )
        return {"ok": True}

    if action == "sstar" and len(parts) == 5:
        try:
            q_idx = int(parts[3].strip())
            stars_value = int(parts[4].strip())
        except Exception:
            telegram_send_text(
                bot_token,
                chat_id,
                "⚠️ No pude leer esa calificación.",
                reply_markup=admin_fixed_kb(),
            )
            return {"ok": True}

        if stars_value < 1 or stars_value > 5:
            telegram_send_text(
                bot_token,
                chat_id,
                "⚠️ La calificación debe estar entre 1 y 5.",
                reply_markup=admin_fixed_kb(),
            )
            return {"ok": True}

        if not bool(tmp.get("admin_survey_runtime")):
            telegram_send_text(
                bot_token,
                chat_id,
                "⚠️ No hay una encuesta activa en este momento.",
                reply_markup=admin_fixed_kb(),
            )
            return {"ok": True}

        questions = get_runtime_survey_questions(orders_sh)
        if not questions or q_idx < 0 or q_idx >= len(questions):
            clear_admin_survey_runtime(tmp)
            telegram_send_text(
                bot_token,
                chat_id,
                "⚠️ Error en el flujo de encuesta.",
                reply_markup=admin_fixed_kb(),
            )
            return {"ok": True}

        current_q = questions[q_idx]
        qtype = str(current_q.get("type") or "").strip().lower()
        if qtype != "stars":
            telegram_send_text(
                bot_token,
                chat_id,
                "⚠️ Esta pregunta no es de estrellas.",
                reply_markup=admin_fixed_kb(),
            )
            return {"ok": True}

        answers = tmp.setdefault("admin_survey_answers", [])
        answers.append({
            "question_id": str(current_q.get("question_id") or ""),
            "question_order": int(current_q.get("order", 0) or 0),
            "question_text": str(current_q.get("question_text") or ""),
            "answer_type": qtype,
            "answer_value": str(stars_value),
        })

        next_idx = q_idx + 1
        if next_idx < len(questions):
            next_q = questions[next_idx]
            tmp["admin_survey_step"] = f"q_{next_idx}"
            send_admin_survey_runtime_question(
                bot_token=bot_token,
                chat_id=chat_id,
                tenant_id=tenant_id,
                question=next_q,
                q_idx=next_idx,
            )
            return {"ok": True}

        return finalize_admin_survey_runtime(
            tenant_id=tenant_id,
            bot_token=bot_token,
            chat_id=chat_id,
            orders_sh=orders_sh,
            tenant_tz=tenant_tz,
            tmp=tmp,
        )

    return {"ok": True}
