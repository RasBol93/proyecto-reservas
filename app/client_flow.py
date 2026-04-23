# app/client_flow.py

import json
import time
from typing import Any, Dict, List

from fastapi import HTTPException

from app.menu import load_menu_index, group_menu_by_category
from app.orders import (
    append_order_row,
    update_order_payment_proof,
    find_latest_pending_order_for_contact,
    get_order_by_id,
    gen_order_id,
    build_items_snapshot,
)
from app.telegram_api import telegram_send_text, telegram_send_photo
from app.telegram_keyboard import kb
from app.utils import normalize, log_event
from app.stats import log_event_to_sheet
from app.webhook_helpers import (
    REMINDER_COOLDOWN_SECONDS,
    CONTACT_AFTER_SECONDS,
    clear_sess,
    get_sess,
    get_payment_qr_file_id,
    get_payment_qr_url,
    fmt_cart_lines,
    fmt_snapshot_lines,
    build_order_recap_text,
    get_business_status_safe,
    send_business_blocked_text,
    contact_link_for_admin,
    build_client_cart_manage_kb,
    i_paid_kb,
    paid_actions_kb,
    contact_admin_kb,
)
from app.payment_flow import notify_admin_payment_reported
from app.alerts import (
    alert_order_failed,
    alert_payment_proof_failed,
    alert_payment_failed,
    alert_menu_error,
    alert_tenant_error,
    alert_system_error,
)
from app.content import (
    build_start_text,
    build_location_text,
    build_faq_text,
    load_content_map,
    has_location,
    has_faq,
)
from app.pickup import (
    generate_pickup_slots,
    build_pickup_slots_kb,
    build_pickup_offer_text,
)
from app.client_time import parse_manual_time_text
from app.survey import (
    survey_runtime_available,
    validate_survey_password,
    get_runtime_survey_questions,
    save_survey_answers,
    create_survey_coupon,
    get_survey_reward_text,
    has_answered_survey_today,
    _normalize_phone,
)


# -------------------------------------------------
# Anti-duplicación de pedidos
# -------------------------------------------------

_ORDER_DUPLICATION_GUARD_SECONDS = 120


def _safe_json_dumps(v: Any) -> str:
    try:
        return json.dumps(v, ensure_ascii=False, sort_keys=True)
    except Exception:
        return ""


def _build_order_creation_signature(
    tenant_id: str,
    chat_id: int,
    customer_name: str,
    customer_phone: str,
    requested_time: str,
    items_snapshot: List[Dict[str, Any]],
) -> str:
    payload = {
        "tenant_id": str(tenant_id or "").strip(),
        "chat_id": str(chat_id),
        "customer_name": str(customer_name or "").strip(),
        "customer_phone": str(customer_phone or "").strip(),
        "requested_time": str(requested_time or "").strip(),
        "items_snapshot": items_snapshot or [],
    }
    return _safe_json_dumps(payload)


def _get_pending_order_from_session(sess: Dict[str, Any]) -> str:
    tmp = sess.get("tmp") or {}
    return str(tmp.get("pending_order_id") or "").strip()


def _remember_created_order(
    sess: Dict[str, Any],
    order_id: str,
    signature: str,
) -> None:
    tmp = sess.setdefault("tmp", {})
    now_ts = int(time.time())

    tmp["pending_order_id"] = str(order_id or "").strip()
    tmp["last_created_order_id"] = str(order_id or "").strip()
    tmp["last_created_order_signature"] = str(signature or "").strip()
    tmp["last_created_order_ts"] = now_ts


def _is_recent_duplicate_attempt(sess: Dict[str, Any], signature: str) -> bool:
    tmp = sess.get("tmp") or {}
    last_sig = str(tmp.get("last_created_order_signature") or "").strip()
    last_ts = int(tmp.get("last_created_order_ts") or 0)
    now_ts = int(time.time())

    if not last_sig or not signature:
        return False

    if last_sig != signature:
        return False

    if last_ts <= 0:
        return False

    return (now_ts - last_ts) <= _ORDER_DUPLICATION_GUARD_SECONDS


def _get_existing_pending_order_message(order_id: str) -> str:
    return (
        "⚠️ Ya tienes un pedido en proceso.\n\n"
        f"Código de pedido: {order_id}\n\n"
        "Primero envía el comprobante de ese pedido o vuelve a /start si deseas reiniciar."
    )


def build_dynamic_home_kb(content_map: Dict[str, str], orders_sh=None):
    rows = [
        [("📋 Ver menú", "menu")],
        [("🛒 Ver carrito", "cart")],
    ]

    if has_location(content_map):
        rows.append([("📍 Ubicación", "location")])

    rows.append([("⏰ Horarios", "hours")])

    if has_faq(content_map):
        rows.append([("❓ FAQ", "faq")])

    try:
        if orders_sh is not None and survey_runtime_available(orders_sh):
            rows.append([("🌟 Encuesta", "survey_start")])
    except Exception:
        pass

    return kb(rows)


def _send_home(bot_token: str, chat_id: int, orders_sh) -> bool:
    content_map = load_content_map(orders_sh)
    return telegram_send_text(
        bot_token,
        chat_id,
        build_start_text(orders_sh, content_map=content_map),
        build_dynamic_home_kb(content_map, orders_sh),
    )


def _format_open_days(days: List[str]) -> str:
    if not days:
        return "No configurado"

    alias_map = {
        "MON": "Lunes",
        "TUE": "Martes",
        "WED": "Miércoles",
        "THU": "Jueves",
        "FRI": "Viernes",
        "SAT": "Sábado",
        "SUN": "Domingo",
        "LUN": "Lunes",
        "MAR": "Martes",
        "MIE": "Miércoles",
        "MIÉ": "Miércoles",
        "JUE": "Jueves",
        "VIE": "Viernes",
        "SAB": "Sábado",
        "SÁB": "Sábado",
        "DOM": "Domingo",
    }

    normalized_days = []
    seen = set()

    for d in days:
        d_norm = str(d or "").strip().upper()
        if not d_norm:
            continue
        if d_norm in alias_map:
            nice = alias_map[d_norm]
            if nice not in seen:
                seen.add(nice)
                normalized_days.append(nice)

    if normalized_days:
        desired_order = ["Lunes", "Martes", "Miércoles", "Jueves", "Viernes", "Sábado", "Domingo"]
        ordered_names = [name for name in desired_order if name in normalized_days]
        return ", ".join(ordered_names)

    return "No configurado"


def _format_cart_detail_lines(cart: List[Dict[str, Any]], menu_idx: Dict[str, Dict[str, Any]]) -> str:
    lines: List[str] = []

    for it in cart:
        sku = str(it.get("sku") or "").strip()
        if not sku or sku not in menu_idx:
            continue

        try:
            qty = int(it.get("qty") or 0)
        except Exception:
            qty = 0

        if qty <= 0:
            continue

        name = str(menu_idx[sku].get("name") or sku).strip()
        unit_price = float(menu_idx[sku].get("price") or 0)
        line_total = unit_price * qty

        lines.append(f"• {qty} x {name} — Bs {line_total:.2f}")

    return "\n".join(lines) if lines else "Tu carrito está vacío."


def _send_category_products(
    bot_token: str,
    chat_id: int,
    real_cat: str,
    items: List[Dict[str, Any]],
) -> None:
    with_photo = []
    without_photo = []

    for it in items:
        photo_url = str(it.get("photo_url") or "").strip()
        photo_file_id = str(it.get("photo_file_id") or "").strip()
        if photo_url or photo_file_id:
            with_photo.append(it)
        else:
            without_photo.append(it)

    telegram_send_text(bot_token, chat_id, f"🍽 {real_cat}")

    for it in with_photo:
        photo_url = str(it.get("photo_url") or "").strip()
        photo_file_id = str(it.get("photo_file_id") or "").strip()
        price_txt = f"{float(it['price']):.0f}"

        reply_markup = kb([
            [(f"⬆️ {it['name']} — Bs {price_txt}", f"prd|{it['sku']}")],
        ])

        if photo_url:
            telegram_send_photo(
                bot_token,
                chat_id,
                photo_url,
                caption="",
                reply_markup=reply_markup,
            )
        elif photo_file_id:
            telegram_send_photo(
                bot_token,
                chat_id,
                photo_file_id,
                caption="",
                reply_markup=reply_markup,
            )

    if without_photo:
        rows = []
        for it in without_photo[:25]:
            rows.append([(f"{it['name']} — Bs {float(it['price']):.0f}", f"prd|{it['sku']}")])

        telegram_send_text(
            bot_token,
            chat_id,
            "Productos sin foto:",
            kb(rows),
        )

    telegram_send_text(
        bot_token,
        chat_id,
        "Otras opciones",
        kb([
            [("🛒 Carrito", "cart")],
            [("⬅️ Categorías", "menu")],
            [("🏠 Inicio", "home")],
        ]),
    )


def client_orders_allowed_or_notify(bot_token: str, chat_id: int, orders_sh, tenant_tz: str) -> bool:
    try:
        bs = get_business_status_safe(orders_sh=orders_sh, tenant_tz=tenant_tz)
        if bool(bs.get("accepts_orders_now")):
            return True
        telegram_send_text(bot_token, chat_id, send_business_blocked_text(bs))
        return False
    except Exception as e:
        log_event(
            "client_orders_allowed_check_error",
            chat_id=chat_id,
            error_type=type(e).__name__,
            error=str(e),
        )
        alert_system_error(error=str(e), module="client_orders_allowed_or_notify")
        telegram_send_text(bot_token, chat_id, "⚠️ Ocurrió un error verificando el horario del negocio.")
        return False


def _cart_add(sess: Dict[str, Any], sku: str, qty: int) -> None:
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


def _cart_inc(sess: Dict[str, Any], sku: str) -> None:
    cart = sess.get("cart") or []
    for it in cart:
        if it.get("sku") == sku:
            it["qty"] = max(1, int(it.get("qty") or 1) + 1)
            break
    sess["cart"] = cart


def _cart_dec(sess: Dict[str, Any], sku: str) -> None:
    cart = sess.get("cart") or []
    new_cart = []
    for it in cart:
        if it.get("sku") == sku:
            new_qty = int(it.get("qty") or 1) - 1
            if new_qty > 0:
                it["qty"] = new_qty
                new_cart.append(it)
        else:
            new_cart.append(it)
    sess["cart"] = new_cart


def _cart_remove(sess: Dict[str, Any], sku: str) -> None:
    cart = sess.get("cart") or []
    sess["cart"] = [it for it in cart if str(it.get("sku") or "").strip() != sku]


def _send_survey_question(bot_token: str, chat_id: int, q: Dict[str, Any], question_number: int) -> bool:
    q_text = str(q.get("question_text") or "").strip()
    q_type = str(q.get("type") or "").strip().lower()
    header = f"Pregunta {question_number}\n\n{q_text}"

    if q_type == "stars":
        return telegram_send_text(
            bot_token,
            chat_id,
            f"{header}\n\nElige una calificación:",
            reply_markup=kb([
                [
                    ("1 ⭐", "survey_ans|1"),
                    ("2 ⭐", "survey_ans|2"),
                    ("3 ⭐", "survey_ans|3"),
                    ("4 ⭐", "survey_ans|4"),
                    ("5 ⭐", "survey_ans|5"),
                ],
            ]),
        )

    return telegram_send_text(bot_token, chat_id, header)


def _save_current_survey_answer_and_advance(
    tenant_id: str,
    bot_token: str,
    chat_id: int,
    orders_sh,
    tenant_tz: str,
    sess: Dict[str, Any],
    raw_answer: str,
) -> Dict[str, Any]:
    survey = sess.get("survey") or {}
    questions = get_runtime_survey_questions(orders_sh)

    try:
        idx = int(survey.get("q_idx") or 0)
    except Exception:
        idx = 0

    if idx < 0 or idx >= len(questions):
        telegram_send_text(bot_token, chat_id, "⚠️ Error de encuesta. Intenta de nuevo.")
        sess["stage"] = "idle"
        sess["survey"] = {}
        return {"ok": True}

    q = questions[idx]
    answer = str(raw_answer or "").strip()
    q_type = str(q.get("type") or "").strip().lower()

    if q_type == "stars":
        if answer not in ("1", "2", "3", "4", "5"):
            telegram_send_text(bot_token, chat_id, "Responde con una opción válida del 1 al 5.")
            return {"ok": True}
    else:
        if not answer:
            telegram_send_text(bot_token, chat_id, "Tu respuesta no puede estar vacía.")
            return {"ok": True}

    survey_answers = survey.get("answers") or []
    survey_answers.append({
        "question_id": str(q.get("question_id") or "").strip(),
        "question_order": int(q.get("order") or 0),
        "question_text": str(q.get("question_text") or "").strip(),
        "answer_type": str(q.get("type") or "").strip(),
        "answer_value": answer,
    })

    idx += 1
    survey["answers"] = survey_answers
    survey["q_idx"] = idx
    sess["survey"] = survey

    if idx < len(questions):
        _send_survey_question(bot_token, chat_id, questions[idx], idx + 1)
        return {"ok": True}

    phone = str(survey.get("phone") or "").strip()
    reward = get_survey_reward_text(orders_sh)

    coupon_res = create_survey_coupon(
        orders_sh=orders_sh,
        tenant_id=tenant_id,
        phone=phone,
        reward_text=reward,
    )
    if not coupon_res.get("ok"):
        telegram_send_text(bot_token, chat_id, "⚠️ No pude generar tu cupón en este momento.")
        sess["stage"] = "idle"
        sess["survey"] = {}
        return {"ok": True}

    coupon_code = str(coupon_res.get("coupon_code") or "").strip()

    save_res = save_survey_answers(
        orders_sh=orders_sh,
        tenant_id=tenant_id,
        tenant_tz=tenant_tz,
        customer_phone=phone,
        customer_name="",
        answers=survey_answers,
        coupon_code=coupon_code,
    )
    if not save_res.get("ok"):
        telegram_send_text(bot_token, chat_id, "⚠️ No pude guardar tu encuesta en este momento.")
        sess["stage"] = "idle"
        sess["survey"] = {}
        return {"ok": True}

    telegram_send_text(
        bot_token,
        chat_id,
        (
            "🙏 Gracias por completar esta encuesta.\n\n"
            f"Como agradecimiento te damos la siguiente recompensa:\n{reward}\n\n"
            f"🎁 Código único de descuento:\n{coupon_code}"
        ),
    )

    sess["stage"] = "idle"
    sess["survey"] = {}
    return {"ok": True}


def handle_client_callback(
    tenant: Dict[str, Any],
    tenant_id: str,
    bot_token: str,
    chat_id: int,
    data: str,
    orders_sh,
    tenant_tz: str,
) -> Dict[str, Any]:
    try:
        sess = get_sess(tenant_id, chat_id)
        tmp = sess.get("tmp") or {}
        sess["tmp"] = tmp

        log_event("client_callback", tenant_id=tenant_id, chat_id=chat_id, data=data)

        if data == "home":
            _send_home(bot_token, chat_id, orders_sh)
            return {"ok": True}

        if data == "location":
            telegram_send_text(bot_token, chat_id, build_location_text(orders_sh))
            return {"ok": True}

        if data == "faq":
            telegram_send_text(bot_token, chat_id, build_faq_text(orders_sh))
            return {"ok": True}

        if data == "survey_start":
            if not survey_runtime_available(orders_sh):
                telegram_send_text(bot_token, chat_id, "Encuesta no disponible.")
                return {"ok": True}

            sess["stage"] = "survey_password"
            sess["survey"] = {}
            telegram_send_text(bot_token, chat_id, "🔒 Ingresa el password de la encuesta:")
            return {"ok": True}

        if data.startswith("survey_ans|"):
            if sess.get("stage") != "survey_q":
                telegram_send_text(bot_token, chat_id, "La encuesta ya no está activa. Vuelve a empezar.")
                return {"ok": True}

            parts = data.split("|", 1)
            if len(parts) != 2:
                return {"ok": True}

            answer_value = parts[1].strip()
            return _save_current_survey_answer_and_advance(
                tenant_id=tenant_id,
                bot_token=bot_token,
                chat_id=chat_id,
                orders_sh=orders_sh,
                tenant_tz=tenant_tz,
                sess=sess,
                raw_answer=answer_value,
            )

        if data == "hours":
            bs = get_business_status_safe(orders_sh=orders_sh, tenant_tz=tenant_tz)
            open_now = "🟢 Hoy estamos abiertos" if bs.get("accepts_orders_now") else "🔴 Hoy estamos cerrados"

            open_time = str(bs.get("open_time") or "").strip()
            close_time = str(bs.get("close_time") or "").strip()
            last_order_time = str(bs.get("last_order_time") or "").strip()
            weekly_open_days = bs.get("weekly_open_days") or []

            parts = [open_now]

            if weekly_open_days:
                parts.append(f"📅 Días regulares: {_format_open_days(weekly_open_days)}")

            if open_time and close_time:
                parts.append(f"🕒 Horario regular: {open_time} - {close_time}")

            if last_order_time:
                parts.append(f"⏳ Última hora de pedido: {last_order_time}")

            telegram_send_text(bot_token, chat_id, "\n\n".join(parts))
            return {"ok": True}

        if data == "menu":
            if not client_orders_allowed_or_notify(bot_token, chat_id, orders_sh, tenant_tz):
                return {"ok": True}

            try:
                menu_idx = load_menu_index(orders_sh)
                cats = group_menu_by_category(menu_idx)
            except Exception as e:
                log_event(
                    "client_menu_load_error",
                    tenant_id=tenant_id,
                    chat_id=chat_id,
                    error_type=type(e).__name__,
                    error=str(e),
                )
                alert_menu_error(tenant_id=tenant_id, error=str(e))
                telegram_send_text(bot_token, chat_id, "⚠️ No pude cargar el menú en este momento.")
                return {"ok": True}

            if not cats:
                _send_home(bot_token, chat_id, orders_sh)
                telegram_send_text(bot_token, chat_id, "No hay menú activo.")
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

            try:
                menu_idx = load_menu_index(orders_sh)
                cats = group_menu_by_category(menu_idx)
            except Exception as e:
                log_event(
                    "client_category_load_error",
                    tenant_id=tenant_id,
                    chat_id=chat_id,
                    category=cat_norm,
                    error_type=type(e).__name__,
                    error=str(e),
                )
                alert_menu_error(tenant_id=tenant_id, error=str(e))
                telegram_send_text(bot_token, chat_id, "⚠️ No pude cargar esa categoría.")
                return {"ok": True}

            real_cat = None
            for c in cats.keys():
                if normalize(c) == cat_norm:
                    real_cat = c
                    break

            if not real_cat:
                _send_home(bot_token, chat_id, orders_sh)
                telegram_send_text(bot_token, chat_id, "Categoría no encontrada.")
                return {"ok": True}

            items = cats.get(real_cat, [])
            if not items:
                _send_home(bot_token, chat_id, orders_sh)
                telegram_send_text(bot_token, chat_id, "No hay productos activos.")
                return {"ok": True}

            _send_category_products(bot_token, chat_id, real_cat, items)
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

            try:
                menu_idx = load_menu_index(orders_sh)
            except Exception as e:
                log_event(
                    "client_qty_menu_load_error",
                    tenant_id=tenant_id,
                    chat_id=chat_id,
                    sku=sku,
                    error_type=type(e).__name__,
                    error=str(e),
                )
                alert_menu_error(tenant_id=tenant_id, sku=sku, error=str(e))
                telegram_send_text(bot_token, chat_id, "⚠️ No pude validar el producto en este momento.")
                return {"ok": True}

            if sku not in menu_idx:
                _send_home(bot_token, chat_id, orders_sh)
                telegram_send_text(bot_token, chat_id, "Producto no disponible.")
                return {"ok": True}

            _cart_add(sess, sku, qty)

            name = menu_idx[sku]["name"]
            unit_price = float(menu_idx[sku]["price"])
            added_subtotal = unit_price * qty

            telegram_send_text(
                bot_token,
                chat_id,
                f"✅ Agregado: {qty} x {name}\nSubtotal de este agregado: Bs {added_subtotal:.2f}",
                reply_markup=kb([
                    [("🛒 Ver carrito", "cart")],
                    [("⬅️ Seguir comprando", "menu")],
                    [("🏠 Inicio", "home")],
                ]),
            )
            return {"ok": True}

        if data.startswith("cinc|"):
            sku = data.split("|", 1)[1].strip()
            _cart_inc(sess, sku)
            data = "cart"

        if data.startswith("cdec|"):
            sku = data.split("|", 1)[1].strip()
            _cart_dec(sess, sku)
            data = "cart"

        if data.startswith("crem|"):
            sku = data.split("|", 1)[1].strip()
            _cart_remove(sess, sku)
            data = "cart"

        if data == "cart":
            try:
                menu_idx = load_menu_index(orders_sh)
            except Exception as e:
                log_event(
                    "client_cart_menu_load_error",
                    tenant_id=tenant_id,
                    chat_id=chat_id,
                    error_type=type(e).__name__,
                    error=str(e),
                )
                alert_menu_error(tenant_id=tenant_id, error=str(e))
                telegram_send_text(bot_token, chat_id, "⚠️ No pude cargar tu carrito.")
                return {"ok": True}

            cart = sess.get("cart") or []
            _, total, _ = fmt_cart_lines(cart, menu_idx)
            detail_lines = _format_cart_detail_lines(cart, menu_idx)

            msg = (
                f"🛒 *Tu carrito*\n\n"
                f"{detail_lines}\n\n"
                f"*Total: Bs {total:.2f}*"
            )
            telegram_send_text(
                bot_token,
                chat_id,
                msg,
                reply_markup=build_client_cart_manage_kb(cart, menu_idx),
                parse_mode="Markdown",
            )
            return {"ok": True}

        if data == "cart_clear":
            sess["cart"] = []
            sess["stage"] = "idle"
            sess["tmp"] = {}
            _send_home(bot_token, chat_id, orders_sh)
            telegram_send_text(bot_token, chat_id, "🧹 Carrito vaciado.")
            return {"ok": True}

        if data == "cart_confirm":
            if not client_orders_allowed_or_notify(bot_token, chat_id, orders_sh, tenant_tz):
                return {"ok": True}

            cart = sess.get("cart") or []
            if not cart:
                _send_home(bot_token, chat_id, orders_sh)
                telegram_send_text(bot_token, chat_id, "Tu carrito está vacío.")
                return {"ok": True}

            pickup_data = generate_pickup_slots(
                orders_sh=orders_sh,
                tenant_tz=tenant_tz,
            )

            if not pickup_data.get("ok"):
                telegram_send_text(bot_token, chat_id, pickup_data.get("message") or "No hay horarios disponibles.")
                return {"ok": True}

            sess["stage"] = "awaiting_pickup_time"

            telegram_send_text(
                bot_token,
                chat_id,
                build_pickup_offer_text(pickup_data),
                reply_markup=build_pickup_slots_kb(tenant_id, pickup_data["slots"]),
            )
            return {"ok": True}

        if data == "pickup|asap":
            if sess.get("stage") != "awaiting_pickup_time":
                telegram_send_text(bot_token, chat_id, "Primero confirma tu carrito.")
                return {"ok": True}

            pickup_data = generate_pickup_slots(orders_sh=orders_sh, tenant_tz=tenant_tz)
            if not pickup_data.get("ok") or not pickup_data.get("slots"):
                telegram_send_text(bot_token, chat_id, pickup_data.get("message") or "No hay horarios disponibles.")
                return {"ok": True}

            chosen_hhmm = pickup_data["slots"][0]["hhmm"]

            sess["tmp"]["pickup_time_hhmm"] = chosen_hhmm
            sess["tmp"]["pickup_time_label"] = chosen_hhmm
            sess["stage"] = "awaiting_name"

            telegram_send_text(
                bot_token,
                chat_id,
                f"Perfecto. Hora de recojo: *{chosen_hhmm}*\n\nAhora dime tu *nombre*.",
                parse_mode="Markdown",
            )
            return {"ok": True}

        if data.startswith("pickup|slot|"):
            if sess.get("stage") != "awaiting_pickup_time":
                telegram_send_text(bot_token, chat_id, "Primero confirma tu carrito.")
                return {"ok": True}

            compact = data.split("|", 2)[2].strip()
            if len(compact) != 4 or not compact.isdigit():
                telegram_send_text(bot_token, chat_id, "Horario inválido.")
                return {"ok": True}

            hhmm = f"{compact[:2]}:{compact[2:]}"

            sess["tmp"]["pickup_time_hhmm"] = hhmm
            sess["tmp"]["pickup_time_label"] = hhmm
            sess["stage"] = "awaiting_name"

            telegram_send_text(
                bot_token,
                chat_id,
                f"Perfecto. Hora de recojo: *{hhmm}*\n\nAhora dime tu *nombre*.",
                parse_mode="Markdown",
            )
            return {"ok": True}

        if data == "pickup|custom":
            if sess.get("stage") != "awaiting_pickup_time":
                telegram_send_text(bot_token, chat_id, "Primero confirma tu carrito.")
                return {"ok": True}

            sess["stage"] = "awaiting_pickup_custom_time"
            telegram_send_text(
                bot_token,
                chat_id,
                "Escribe la hora de recojo que prefieres.\n\nEjemplos: 20:15, 8:15 pm, 2015, 8 pm",
            )
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
                _send_home(bot_token, chat_id, orders_sh)
                telegram_send_text(bot_token, chat_id, "No encontré tu pedido. Vuelve a /start.")
                return {"ok": True}

            proof_file_id = (order.get("payment_proof_file_id") or "").strip()
            if not proof_file_id:
                telegram_send_text(bot_token, chat_id, "Aún no recibí tu comprobante.\nEnvía una foto o PDF del pago primero.")
                return {"ok": True}

            try:
                ok_sent = notify_admin_payment_reported(
                    tenant,
                    tenant_id,
                    orders_sh,
                    order_id,
                    is_reminder=False,
                    order=order,
                )
            except Exception as e:
                log_event(
                    "notify_admin_payment_reported_error",
                    tenant_id=tenant_id,
                    chat_id=chat_id,
                    order_id=order_id,
                    error_type=type(e).__name__,
                    error=str(e),
                )
                alert_payment_failed(tenant_id=tenant_id, order_id=order_id, error=str(e))
                telegram_send_text(bot_token, chat_id, "⚠️ No pude avisar al administrador en este momento. Intenta de nuevo.")
                return {"ok": True}

            tmp["paid_pressed_at_ts"] = int(time.time())
            tmp["last_notified_order_id"] = order_id
            tmp["last_admin_notify_ok"] = bool(ok_sent)

            telegram_send_text(
                bot_token,
                chat_id,
                "✅ Recibido. Espera unos minutos mientras verificamos tu pago.\nSi no hay respuesta, podrás enviar un recordatorio.",
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

            try:
                ok_sent = notify_admin_payment_reported(tenant, tenant_id, orders_sh, order_id, is_reminder=True)
            except Exception as e:
                log_event(
                    "notify_admin_payment_reminder_error",
                    tenant_id=tenant_id,
                    chat_id=chat_id,
                    order_id=order_id,
                    error_type=type(e).__name__,
                    error=str(e),
                )
                alert_payment_failed(tenant_id=tenant_id, order_id=order_id, error=str(e))
                telegram_send_text(bot_token, chat_id, "⚠️ No pude enviar el recordatorio en este momento.")
                return {"ok": True}

            tmp["reminder_sent_at_ts"] = now
            tmp["last_admin_reminder_ok"] = bool(ok_sent)

            if ok_sent:
                telegram_send_text(
                    bot_token,
                    chat_id,
                    "🔔 Listo. Enviamos un *recordatorio* al administrador.\nSi no responde, en unos minutos podrás contactarlo directamente.",
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
                alert_tenant_error(tenant_id=tenant_id, error="contact_link_for_admin missing")
                _send_home(bot_token, chat_id, orders_sh)
                telegram_send_text(bot_token, chat_id, "No tengo configurado el contacto directo del administrador.")
                return {"ok": True}

            telegram_send_text(bot_token, chat_id, "💬 Contacto directo habilitado.\nToca el enlace para escribirle al administrador:")
            telegram_send_text(bot_token, chat_id, link)
            return {"ok": True}

        return {"ok": True}

    except Exception as e:
        log_event(
            "client_callback_error",
            tenant_id=tenant_id,
            chat_id=chat_id,
            data=data,
            error_type=type(e).__name__,
            error=str(e),
        )
        alert_system_error(error=str(e), module="client_callback")
        telegram_send_text(bot_token, chat_id, "⚠️ Ocurrió un error. Intenta nuevamente.")
        return {"ok": True}


def handle_client_message(
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
            order_id = _get_pending_order_from_session(sess)
            if not order_id:
                order_id = find_latest_pending_order_for_contact(
                    orders_sh=orders_sh,
                    customer_contact=str(chat_id),
                    status="PENDING_PAYMENT",
                )

            if not order_id:
                _send_home(bot_token, chat_id, orders_sh)
                telegram_send_text(bot_token, chat_id, "No encontré un pedido pendiente. Crea uno nuevo con /start.")
                return {"ok": True}

            result = update_order_payment_proof(
                orders_sh=orders_sh,
                order_id=order_id,
                proof_file_id=proof_file_id,
                proof_type=proof_type,
                proof_caption=proof_caption,
            )

            if not result.get("ok"):
                alert_payment_proof_failed(
                    tenant_id=tenant_id,
                    order_id=order_id,
                    chat_id=chat_id,
                    error=result.get("error") or "update_order_payment_proof failed",
                )
                telegram_send_text(bot_token, chat_id, "⚠️ Ocurrió un error guardando tu comprobante. Intenta nuevamente.")
                return {"ok": True}

            telegram_send_text(
                bot_token,
                chat_id,
                "✅ Comprobante recibido.\n\n*Puede tardar unos segundos en aparecer la opción “✅ Ya pagué” después de que subas el comprobante.*",
                reply_markup=i_paid_kb(tenant_id, order_id),
                parse_mode="Markdown",
            )
            return {"ok": True}

        if normalize(text) in ("start", "/start", "hola"):
            clear_sess(tenant_id, chat_id)

            try:
                log_event_to_sheet(
                    orders_sh=orders_sh,
                    tenant_id=tenant_id,
                    chat_id=str(chat_id),
                    event_type="client_start",
                    meta={"source": "telegram", "text": text[:50]},
                )
            except Exception as e:
                log_event(
                    "client_start_log_event_to_sheet_error",
                    tenant_id=tenant_id,
                    chat_id=chat_id,
                    error_type=type(e).__name__,
                    error=str(e),
                )

            _send_home(bot_token, chat_id, orders_sh)
            return {"ok": True}

        if sess.get("stage") == "survey_password":
            if not validate_survey_password(orders_sh, text):
                telegram_send_text(bot_token, chat_id, "❌ Password incorrecto.")
                return {"ok": True}

            sess["stage"] = "survey_phone"
            telegram_send_text(
                bot_token,
                chat_id,
                "😊 Ahora empezamos la encuesta.\n\n📱 Ingresa tu número de teléfono:",
            )
            return {"ok": True}

        if sess.get("stage") == "survey_phone":
            phone = _normalize_phone(text)

            if not phone or len(phone) < 7:
                telegram_send_text(bot_token, chat_id, "Número inválido. Intenta nuevamente.")
                return {"ok": True}

            if has_answered_survey_today(orders_sh, tenant_tz, phone):
                telegram_send_text(bot_token, chat_id, "⚠️ Ya respondiste la encuesta hoy.")
                sess["stage"] = "idle"
                sess["survey"] = {}
                return {"ok": True}

            questions = get_runtime_survey_questions(orders_sh)
            if not questions:
                telegram_send_text(bot_token, chat_id, "Encuesta no configurada.")
                sess["stage"] = "idle"
                sess["survey"] = {}
                return {"ok": True}

            sess["survey"] = {
                "phone": phone,
                "answers": [],
                "q_idx": 0,
            }
            sess["stage"] = "survey_q"

            _send_survey_question(bot_token, chat_id, questions[0], 1)
            return {"ok": True}

        if sess.get("stage") == "survey_q":
            survey = sess.get("survey") or {}
            questions = get_runtime_survey_questions(orders_sh)

            try:
                idx = int(survey.get("q_idx") or 0)
            except Exception:
                idx = 0

            if idx < 0 or idx >= len(questions):
                telegram_send_text(bot_token, chat_id, "⚠️ Error de encuesta. Intenta de nuevo.")
                sess["stage"] = "idle"
                sess["survey"] = {}
                return {"ok": True}

            q = questions[idx]
            q_type = str(q.get("type") or "").strip().lower()

            if q_type == "stars":
                telegram_send_text(bot_token, chat_id, "Usa los botones con estrellas para responder esta pregunta.")
                return {"ok": True}

            return _save_current_survey_answer_and_advance(
                tenant_id=tenant_id,
                bot_token=bot_token,
                chat_id=chat_id,
                orders_sh=orders_sh,
                tenant_tz=tenant_tz,
                sess=sess,
                raw_answer=text,
            )

        if sess.get("stage") == "awaiting_pickup_custom_time":
            parsed_hhmm = parse_manual_time_text(text)

            if not parsed_hhmm:
                telegram_send_text(
                    bot_token,
                    chat_id,
                    "No entendí esa hora. Escríbela así: 20:15, 8:15 pm, 2015 o 8 pm.",
                )
                return {"ok": True}

            sess["tmp"]["pickup_time_hhmm"] = parsed_hhmm
            sess["tmp"]["pickup_time_label"] = parsed_hhmm
            sess["stage"] = "awaiting_name"

            telegram_send_text(
                bot_token,
                chat_id,
                f"Perfecto. Hora de recojo: *{parsed_hhmm}*\n\nAhora dime tu *nombre*.",
                parse_mode="Markdown",
            )
            return {"ok": True}

        if sess.get("stage") == "awaiting_name":
            customer_name = text.strip()
            if not customer_name:
                telegram_send_text(bot_token, chat_id, "Dime tu nombre, por favor.")
                return {"ok": True}

            sess["tmp"]["customer_name"] = customer_name
            sess["stage"] = "awaiting_phone"

            telegram_send_text(
                bot_token,
                chat_id,
                "Perfecto. Ahora dime tu *número de teléfono*.",
                parse_mode="Markdown",
            )
            return {"ok": True}

        if sess.get("stage") == "awaiting_phone":
            if not client_orders_allowed_or_notify(bot_token, chat_id, orders_sh, tenant_tz):
                sess["stage"] = "idle"
                return {"ok": True}

            customer_phone = text.strip()
            if not customer_phone:
                telegram_send_text(bot_token, chat_id, "Dime tu número de teléfono, por favor.")
                return {"ok": True}

            pickup_time_hhmm = str((sess.get("tmp") or {}).get("pickup_time_hhmm") or "").strip()
            pickup_time_label = str((sess.get("tmp") or {}).get("pickup_time_label") or "").strip()
            customer_name = str((sess.get("tmp") or {}).get("customer_name") or "").strip()

            if not pickup_time_hhmm:
                telegram_send_text(bot_token, chat_id, "Primero elige la hora de recojo.")
                sess["stage"] = "awaiting_pickup_time"
                return {"ok": True}

            if not customer_name:
                sess["stage"] = "awaiting_name"
                telegram_send_text(bot_token, chat_id, "Primero dime tu nombre.")
                return {"ok": True}

            try:
                menu_idx = load_menu_index(orders_sh)
            except Exception as e:
                log_event(
                    "client_awaiting_phone_menu_load_error",
                    tenant_id=tenant_id,
                    chat_id=chat_id,
                    error_type=type(e).__name__,
                    error=str(e),
                )
                alert_menu_error(tenant_id=tenant_id, error=str(e))
                telegram_send_text(bot_token, chat_id, "⚠️ No pude cargar el menú para crear tu pedido.")
                return {"ok": True}

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
                _send_home(bot_token, chat_id, orders_sh)
                telegram_send_text(bot_token, chat_id, "Tu carrito está vacío.")
                sess["stage"] = "idle"
                return {"ok": True}

            items_snapshot = build_items_snapshot(items_list, menu_idx)
            lines_real, total_real, total_qty_real = fmt_snapshot_lines(items_snapshot)

            order_id = gen_order_id()
            requested_time = pickup_time_label or pickup_time_hhmm

            # ---------------------------------
            # Anti-duplicación de pedido
            # ---------------------------------
            pending_order_id = _get_pending_order_from_session(sess)
            if pending_order_id:
                existing_order = get_order_by_id(orders_sh, pending_order_id)
                existing_status = str((existing_order or {}).get("status") or "").strip().upper()

                if existing_order and existing_status in ("PENDING_PAYMENT", "PAID"):
                    telegram_send_text(
                        bot_token,
                        chat_id,
                        _get_existing_pending_order_message(pending_order_id),
                    )
                    return {"ok": True}

            creation_signature = _build_order_creation_signature(
                tenant_id=tenant_id,
                chat_id=chat_id,
                customer_name=customer_name,
                customer_phone=customer_phone,
                requested_time=requested_time,
                items_snapshot=items_snapshot,
            )

            if _is_recent_duplicate_attempt(sess, creation_signature):
                duplicate_order_id = str((sess.get("tmp") or {}).get("last_created_order_id") or "").strip()
                if duplicate_order_id:
                    telegram_send_text(
                        bot_token,
                        chat_id,
                        (
                            "⚠️ Detecté que este pedido ya fue creado hace un momento.\n\n"
                            f"Código de pedido: {duplicate_order_id}\n\n"
                            "Para evitar duplicados, no volveré a crearlo."
                        ),
                    )
                else:
                    telegram_send_text(
                        bot_token,
                        chat_id,
                        "⚠️ Detecté un intento duplicado de creación de pedido. No lo volveré a crear.",
                    )
                return {"ok": True}

            result = append_order_row(
                orders_sh=orders_sh,
                tenant_id=tenant_id,
                order_id=order_id,
                customer_name=customer_name,
                customer_contact=customer_phone,
                customer_telegram_chat_id=str(chat_id),
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

            if not result.get("ok"):
                alert_order_failed(
                    tenant_id=tenant_id,
                    order_id=order_id,
                    chat_id=chat_id,
                    error=result.get("error") or "append_order_row failed",
                )
                telegram_send_text(bot_token, chat_id, "⚠️ No pude crear tu pedido en este momento. Intenta nuevamente.")
                return {"ok": True}

            sess["stage"] = "awaiting_proof"
            sess["tmp"] = sess.get("tmp") or {}
            sess["tmp"]["customer_name"] = customer_name
            sess["tmp"]["customer_phone"] = customer_phone
            _remember_created_order(sess, order_id, creation_signature)

            recap = build_order_recap_text(
                order_id=order_id,
                customer_name=customer_name,
                customer_contact=customer_phone,
                requested_time=requested_time,
                detail_lines=lines_real,
                total_qty=total_qty_real,
                total=total_real,
            )

            telegram_send_text(
                bot_token,
                chat_id,
                recap + "\n\n💳 *Ahora realiza el pago.*\nTe enviamos el QR a continuación.",
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
                alert_tenant_error(tenant_id=tenant_id, error="missing QR config")

            telegram_send_text(
                bot_token,
                chat_id,
                "*📎 Cuando pagues, envía aquí tu comprobante (foto o PDF).\n\nPuede tardar unos segundos en aparecer la opción “✅ Ya pagué” después de que subas el comprobante.*",
                parse_mode="Markdown",
            )
            return {"ok": True}

        telegram_send_text(bot_token, chat_id, "Usa /start para ver el menú.")
        return {"ok": True}

    except Exception as e:
        log_event(
            "client_message_error",
            tenant_id=tenant_id,
            chat_id=chat_id,
            error_type=type(e).__name__,
            error=str(e),
        )
        alert_system_error(error=str(e), module="client_message")
        telegram_send_text(bot_token, chat_id, "⚠️ Ocurrió un error. Intenta nuevamente.")
        return {"ok": True}
