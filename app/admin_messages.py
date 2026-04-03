# app/admin_messages.py — admin por texto "panel", pedido manual mejorado, QR de pagos y encuesta runtime

from typing import Any, Dict, Optional

from app.menu import (
    load_menu_admin_index,
    get_menu_product_or_404,
    set_menu_product_price,
    invalidate_menu_cache,
    set_menu_product_name,
    set_menu_product_category,
    create_menu_product,
    get_menu_categories,
)
from app.orders import (
    append_order_row,
    gen_order_id,
    build_items_snapshot,
)
from app.telegram_api import telegram_send_text, telegram_get_file_path, telegram_download_file_bytes
from app.telegram_keyboard import kb
from app.utils import normalize, log_event
from app.stats import build_periods
from app.image_storage import upload_product_photo_for_tenant
from app.webhook_helpers import (
    get_sess,
    assert_admin_authorized,
    set_menu_photo_url,
    admin_periods_inline_kb,
    fmt_price_short,
    extract_first_number,
    fmt_snapshot_lines,
    build_order_recap_text,
    admin_fixed_kb,
)
from app.admin_hours import send_admin_hours_menu
from app.admin_menu import (
    send_admin_menu_home,
    send_admin_menu_product_detail,
)
from app.alerts import (
    alert_order_failed,
    alert_menu_error,
    alert_photo_upload_failed,
    alert_system_error,
)
from app.admin_consumers import _send_consumers_menu
from app.admin_manual_order import (
    _admin_order_reset,
    _send_admin_order_home,
    _admin_order_time_choice_kb,
)
from app.admin_nav import (
    admin_panel_kb,
)
from app.survey import (
    save_survey_password,
    save_survey_reward,
    add_survey_question,
    load_survey_questions,
    get_runtime_survey_questions,
    get_survey_reward_text,
    create_survey_coupon,
    save_survey_answers,
    has_answered_survey_today,
)
from app.sheets import get_ws
from app.tenants import update_tenant_payment_qr


SURVEY_CONFIG_WS = "Survey_Config"


def _is_owner_bot(tenant: Dict[str, Any]) -> bool:
    return bool(tenant.get("_is_owner_bot"))


def _finalize_admin_manual_order(
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
            reply_markup=admin_fixed_kb(),
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
            reply_markup=admin_fixed_kb(),
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
        reply_markup=admin_fixed_kb(),
    )
    return {"ok": True}


def _survey_type_label(qtype: str) -> str:
    q = str(qtype or "").strip().lower()
    if q == "stars":
        return "Estrellas"
    if q == "text":
        return "Texto"
    return qtype or ""


def _send_admin_surveys_questions(
    bot_token: str,
    chat_id: int,
    tenant_id: str,
    orders_sh,
) -> bool:
    questions = load_survey_questions(orders_sh)

    lines = [
        "❓ GESTIONAR PREGUNTAS",
        "",
    ]

    if not questions:
        lines.append("No hay preguntas activas.")
    else:
        for idx, q in enumerate(questions, start=1):
            qid = str(q.get("question_id") or "").strip()
            qtype = _survey_type_label(str(q.get("type") or "").strip())
            qtext = str(q.get("question_text") or "").strip()
            lines.append(f"{idx}. [{qid}] {qtext}")
            lines.append(f"   {qtype}")
            lines.append("")

    rows = []
    if questions:
        for q in questions[:20]:
            qid = str(q.get("question_id") or "").strip()
            qtext = str(q.get("question_text") or "").strip()
            short_label = qtext[:18] + "..." if len(qtext) > 18 else qtext

            rows.append([
                (f"✏️ {short_label}", f"admsurv|{tenant_id}|editq|{qid}"),
                ("🔁 Tipo", f"admsurv|{tenant_id}|chtype|{qid}"),
                ("🗑", f"admsurv|{tenant_id}|delq|{qid}"),
            ])

    rows.extend([
        [("➕ Agregar pregunta", f"admsurv|{tenant_id}|addq")],
        [("⬅️ Volver a encuestas", "admin_surveys")],
        [("🧭 Panel admin", "admin_panel")],
    ])

    telegram_send_text(
        bot_token,
        chat_id,
        "\n".join(lines),
        reply_markup=kb(rows),
    )
    return True


def _survey_config_ws(orders_sh):
    return get_ws(orders_sh, SURVEY_CONFIG_WS)


def _survey_header_map(ws) -> Dict[str, int]:
    values = ws.get_all_values()
    if not values:
        return {}
    header = [str(x or "").strip() for x in values[0]]
    return {name: idx for idx, name in enumerate(header)}


def _survey_bool_cell(value: str) -> bool:
    v = str(value or "").strip().lower()
    return v in ("true", "1", "yes", "si", "sí", "y")


def _survey_find_last_active_question_row(ws, question_id: str) -> Optional[int]:
    values = ws.get_all_values()
    if len(values) < 2:
        return None

    hmap = _survey_header_map(ws)
    idx_qid = hmap.get("question_id")
    idx_active = hmap.get("active")

    if idx_qid is None or idx_active is None:
        return None

    for row_num in range(len(values), 1, -1):
        row = values[row_num - 1]
        row_qid = row[idx_qid].strip() if idx_qid < len(row) else ""
        row_active = row[idx_active].strip() if idx_active < len(row) else ""
        if row_qid == question_id and _survey_bool_cell(row_active):
            return row_num

    return None


def _survey_update_question_text_in_place(
    orders_sh,
    question_id: str,
    new_text: str,
) -> bool:
    ws = _survey_config_ws(orders_sh)
    hmap = _survey_header_map(ws)
    target_row = _survey_find_last_active_question_row(ws, question_id)

    if not target_row:
        return False

    idx_qtext = hmap.get("question_text")
    if idx_qtext is None:
        return False

    ws.update_cell(target_row, idx_qtext + 1, str(new_text).strip())
    return True


def handle_admin_message_impl(
    tenant: Dict[str, Any],
    tenant_id: str,
    bot_token: str,
    chat_id: int,
    msg: Dict[str, Any],
    orders_sh,
    tenant_tz: str,
) -> Dict[str, Any]:
    try:
        is_owner = _is_owner_bot(tenant)

        text = (msg.get("text") or "").strip()
        txt_norm = normalize(text)
        sess = get_sess(tenant_id, chat_id)
        tmp = sess.setdefault("tmp", {})

        if txt_norm in ("panel", "⚙️panel", "⚙️ panel"):
            assert_admin_authorized(tenant, chat_id, tenant_id)
            telegram_send_text(
                bot_token,
                chat_id,
                "🧭 PANEL ADMIN\n\nElige una opción:",
                reply_markup=admin_panel_kb("owner" if is_owner else "admin"),
            )
            return {"ok": True}

        # =========================
        # SURVEY RUNTIME (ADMIN)
        # =========================
        if bool(tmp.get("admin_survey_runtime")):
            assert_admin_authorized(tenant, chat_id, tenant_id)

            step = str(tmp.get("admin_survey_step") or "").strip()

            if step == "phone":
                phone = text.strip()
                if not phone:
                    telegram_send_text(
                        bot_token,
                        chat_id,
                        "📱 Ingresa un número válido:",
                        reply_markup=admin_fixed_kb(),
                    )
                    return {"ok": True}

                if has_answered_survey_today(orders_sh, tenant_tz, phone):
                    tmp.pop("admin_survey_runtime", None)
                    tmp.pop("admin_survey_step", None)
                    tmp.pop("admin_survey_answers", None)
                    tmp.pop("admin_survey_phone", None)
                    tmp.pop("admin_survey_name", None)

                    telegram_send_text(
                        bot_token,
                        chat_id,
                        "⚠️ Este cliente ya respondió una encuesta hoy.",
                        reply_markup=admin_fixed_kb(),
                    )
                    return {"ok": True}

                tmp["admin_survey_phone"] = phone
                tmp["admin_survey_step"] = "name"

                telegram_send_text(
                    bot_token,
                    chat_id,
                    "👤 Nombre del cliente:",
                    reply_markup=admin_fixed_kb(),
                )
                return {"ok": True}

            if step == "name":
                customer_name = text.strip()
                if not customer_name:
                    telegram_send_text(
                        bot_token,
                        chat_id,
                        "👤 Ingresa un nombre válido:",
                        reply_markup=admin_fixed_kb(),
                    )
                    return {"ok": True}

                questions = get_runtime_survey_questions(orders_sh)
                if not questions:
                    tmp.pop("admin_survey_runtime", None)
                    tmp.pop("admin_survey_step", None)
                    tmp.pop("admin_survey_answers", None)
                    tmp.pop("admin_survey_phone", None)
                    tmp.pop("admin_survey_name", None)

                    telegram_send_text(
                        bot_token,
                        chat_id,
                        "⚠️ No hay preguntas configuradas.",
                        reply_markup=admin_fixed_kb(),
                    )
                    return {"ok": True}

                tmp["admin_survey_name"] = customer_name
                tmp["admin_survey_step"] = "q_0"

                q = questions[0]
                telegram_send_text(
                    bot_token,
                    chat_id,
                    f"❓ {q['question_text']}",
                    reply_markup=admin_fixed_kb(),
                )
                return {"ok": True}

            if step.startswith("q_"):
                questions = get_runtime_survey_questions(orders_sh)
                if not questions:
                    tmp.pop("admin_survey_runtime", None)
                    tmp.pop("admin_survey_step", None)
                    tmp.pop("admin_survey_answers", None)
                    tmp.pop("admin_survey_phone", None)
                    tmp.pop("admin_survey_name", None)

                    telegram_send_text(
                        bot_token,
                        chat_id,
                        "⚠️ No hay preguntas configuradas.",
                        reply_markup=admin_fixed_kb(),
                    )
                    return {"ok": True}

                try:
                    idx = int(step.split("_")[1])
                except Exception:
                    idx = -1

                if idx < 0 or idx >= len(questions):
                    tmp.pop("admin_survey_runtime", None)
                    tmp.pop("admin_survey_step", None)
                    tmp.pop("admin_survey_answers", None)
                    tmp.pop("admin_survey_phone", None)
                    tmp.pop("admin_survey_name", None)

                    telegram_send_text(
                        bot_token,
                        chat_id,
                        "⚠️ Error en el flujo de encuesta.",
                        reply_markup=admin_fixed_kb(),
                    )
                    return {"ok": True}

                current_q = questions[idx]
                answers = tmp.setdefault("admin_survey_answers", [])

                answers.append({
                    "question_id": str(current_q.get("question_id") or ""),
                    "question_order": int(current_q.get("order", 0) or 0),
                    "question_text": str(current_q.get("question_text") or ""),
                    "answer_type": str(current_q.get("type") or ""),
                    "answer_value": text.strip(),
                })

                next_idx = idx + 1

                if next_idx < len(questions):
                    tmp["admin_survey_step"] = f"q_{next_idx}"
                    next_q = questions[next_idx]
                    telegram_send_text(
                        bot_token,
                        chat_id,
                        f"❓ {next_q['question_text']}",
                        reply_markup=admin_fixed_kb(),
                    )
                    return {"ok": True}

                phone = str(tmp.get("admin_survey_phone") or "").strip()
                customer_name = str(tmp.get("admin_survey_name") or "").strip()
                reward_text = get_survey_reward_text(orders_sh)

                coupon_res = create_survey_coupon(
                    orders_sh=orders_sh,
                    tenant_id=tenant_id,
                    phone=phone,
                    reward_text=reward_text,
                )

                coupon_code = ""
                if coupon_res.get("ok"):
                    coupon_code = str(coupon_res.get("coupon_code") or "").strip()

                save_res = save_survey_answers(
                    orders_sh=orders_sh,
                    tenant_id=tenant_id,
                    tenant_tz=tenant_tz,
                    customer_phone=phone,
                    customer_name=customer_name,
                    answers=answers,
                    coupon_code=coupon_code,
                )

                tmp.pop("admin_survey_runtime", None)
                tmp.pop("admin_survey_step", None)
                tmp.pop("admin_survey_answers", None)
                tmp.pop("admin_survey_phone", None)
                tmp.pop("admin_survey_name", None)

                if not save_res.get("ok"):
                    error_code = str(save_res.get("error") or "").strip()
                    if error_code == "already_answered_today":
                        telegram_send_text(
                            bot_token,
                            chat_id,
                            "⚠️ Este cliente ya respondió una encuesta hoy.",
                            reply_markup=admin_fixed_kb(),
                        )
                        return {"ok": True}

                    telegram_send_text(
                        bot_token,
                        chat_id,
                        "⚠️ No pude guardar la encuesta.",
                        reply_markup=admin_fixed_kb(),
                    )
                    return {"ok": True}

                final_msg = "✅ Encuesta completada y guardada correctamente."
                if coupon_code:
                    final_msg += f"\n🎁 Cupón generado: {coupon_code}"
                elif reward_text:
                    final_msg += "\n🎁 La encuesta se guardó, pero no pude generar el cupón."

                telegram_send_text(
                    bot_token,
                    chat_id,
                    final_msg,
                    reply_markup=admin_fixed_kb(),
                )
                return {"ok": True}

        # =========================
        # QR de pagos
        # =========================
        admin_payment_mode = str(tmp.get("admin_payment_mode") or "").strip()

        if admin_payment_mode == "awaiting_qr":
            assert_admin_authorized(tenant, chat_id, tenant_id)

            if msg.get("photo"):
                file_id = msg["photo"][-1]["file_id"]

                try:
                    file_path = telegram_get_file_path(bot_token, file_id)
                    file_bytes = telegram_download_file_bytes(bot_token, file_path)

                    content_type = "image/jpeg"
                    low_path = file_path.lower()
                    if low_path.endswith(".png"):
                        content_type = "image/png"
                    elif low_path.endswith(".webp"):
                        content_type = "image/webp"

                    qr_url = upload_product_photo_for_tenant(
                        tenant=tenant,
                        tenant_id=tenant_id,
                        sku="payment_qr",
                        file_bytes=file_bytes,
                        mime_type=content_type,
                    )

                except Exception as e:
                    log_event(
                        "admin_payment_qr_upload_failed",
                        tenant_id=tenant_id,
                        error=str(e),
                    )
                    telegram_send_text(
                        bot_token,
                        chat_id,
                        "⚠️ Error procesando el QR. Intenta nuevamente.",
                        reply_markup=admin_fixed_kb(),
                    )
                    return {"ok": True}

                ok = update_tenant_payment_qr(
                    tenant_id=tenant_id,
                    qr_url=qr_url,
                )

                tmp.pop("admin_payment_mode", None)

                if ok:
                    telegram_send_text(
                        bot_token,
                        chat_id,
                        "✅ QR actualizado correctamente.",
                        reply_markup=admin_fixed_kb(),
                    )
                else:
                    telegram_send_text(
                        bot_token,
                        chat_id,
                        "⚠️ Error guardando el QR.",
                        reply_markup=admin_fixed_kb(),
                    )

                return {"ok": True}

            telegram_send_text(
                bot_token,
                chat_id,
                "📷 Estoy esperando una imagen del QR.",
                reply_markup=admin_fixed_kb(),
            )
            return {"ok": True}

        # =========================
        # comprobante de pedido manual
        # =========================
        if bool(tmp.get("admin_order_waiting_proof")):
            assert_admin_authorized(tenant, chat_id, tenant_id)

            if msg.get("photo"):
                tmp["admin_order_waiting_proof"] = False
                tmp["admin_order_proof_received"] = True

                last_order_id = str(tmp.get("admin_order_last_id") or "").strip()

                telegram_send_text(
                    bot_token,
                    chat_id,
                    (
                        "✅ Comprobante recibido correctamente.\n"
                        f"Pedido: {last_order_id or '(sin referencia)'}"
                    ),
                    reply_markup=kb([
                        [("✅ Fotografía OK", f"admord|{tenant_id}|proof_ok")],
                        [("🧭 Panel admin", "admin_panel")],
                    ]),
                )
                return {"ok": True}

            telegram_send_text(
                bot_token,
                chat_id,
                "📷 Estoy esperando la foto del comprobante.",
                reply_markup=admin_fixed_kb(),
            )
            return {"ok": True}

        admin_order_step = str(tmp.get("admin_order_step") or "").strip()

        if admin_order_step:
            assert_admin_authorized(tenant, chat_id, tenant_id)

            if is_owner:
                _admin_order_reset(tmp)
                tmp.pop("admin_order_step", None)
                telegram_send_text(
                    bot_token,
                    chat_id,
                    "🚫 Como propietario no puedes crear pedidos.",
                    reply_markup=admin_fixed_kb(),
                )
                return {"ok": True}

            if admin_order_step == "awaiting_name":
                customer_name = text.strip()
                if not customer_name:
                    telegram_send_text(
                        bot_token,
                        chat_id,
                        "El nombre no puede estar vacío. Escribe el nombre del cliente:",
                        reply_markup=admin_fixed_kb(),
                    )
                    return {"ok": True}

                tmp["admin_order_name"] = customer_name
                tmp["admin_order_step"] = "awaiting_contact"
                telegram_send_text(
                    bot_token,
                    chat_id,
                    "Escribe el contacto del cliente (teléfono o referencia):",
                    reply_markup=admin_fixed_kb(),
                )
                return {"ok": True}

            if admin_order_step == "awaiting_contact":
                customer_contact = text.strip()
                if not customer_contact:
                    telegram_send_text(
                        bot_token,
                        chat_id,
                        "El contacto no puede estar vacío. Escribe el contacto del cliente:",
                        reply_markup=admin_fixed_kb(),
                    )
                    return {"ok": True}

                tmp["admin_order_contact"] = customer_contact
                tmp["admin_order_step"] = "awaiting_time_choice"
                telegram_send_text(
                    bot_token,
                    chat_id,
                    "Elige cuándo se preparará el pedido:",
                    reply_markup=_admin_order_time_choice_kb(tenant_id),
                )
                return {"ok": True}

            if admin_order_step == "awaiting_time_manual":
                requested_time = text.strip()
                if not requested_time:
                    telegram_send_text(
                        bot_token,
                        chat_id,
                        "Escribe una hora válida.\nEjemplos: 19:30, 20h",
                        reply_markup=admin_fixed_kb(),
                    )
                    return {"ok": True}

                tmp["admin_order_requested_time"] = requested_time
                tmp["admin_order_step"] = "finalize_manual_order"
                return _finalize_admin_manual_order(
                    tenant_id=tenant_id,
                    bot_token=bot_token,
                    chat_id=chat_id,
                    orders_sh=orders_sh,
                    tmp=tmp,
                )

            if admin_order_step == "finalize_manual_order":
                return _finalize_admin_manual_order(
                    tenant_id=tenant_id,
                    bot_token=bot_token,
                    chat_id=chat_id,
                    orders_sh=orders_sh,
                    tmp=tmp,
                )

        admin_survey_mode = str(tmp.get("admin_survey_mode") or "").strip()

        if admin_survey_mode:
            assert_admin_authorized(tenant, chat_id, tenant_id)

            if admin_survey_mode == "awaiting_password":
                new_password = text.strip()
                if not new_password:
                    telegram_send_text(
                        bot_token,
                        chat_id,
                        "El password no puede estar vacío. Escríbelo otra vez:",
                    )
                    return {"ok": True}

                ok = save_survey_password(orders_sh, new_password)
                tmp.pop("admin_survey_mode", None)

                if ok:
                    telegram_send_text(
                        bot_token,
                        chat_id,
                        "✅ Password de encuesta actualizado.",
                        reply_markup=kb([
                            [("⚙️ Configuración", f"admsurv|{tenant_id}|config")],
                            [("📝 Volver a encuestas", "admin_surveys")],
                            [("🧭 Panel admin", "admin_panel")],
                        ]),
                    )
                else:
                    telegram_send_text(
                        bot_token,
                        chat_id,
                        "⚠️ No pude guardar el nuevo password.",
                    )
                return {"ok": True}

            if admin_survey_mode == "awaiting_reward":
                new_reward = text.strip()
                if not new_reward:
                    telegram_send_text(
                        bot_token,
                        chat_id,
                        "La recompensa no puede estar vacía. Escríbela otra vez:",
                    )
                    return {"ok": True}

                ok = save_survey_reward(orders_sh, new_reward)
                tmp.pop("admin_survey_mode", None)

                if ok:
                    telegram_send_text(
                        bot_token,
                        chat_id,
                        "✅ Recompensa de encuesta actualizada.",
                        reply_markup=kb([
                            [("⚙️ Configuración", f"admsurv|{tenant_id}|config")],
                            [("📝 Volver a encuestas", "admin_surveys")],
                            [("🧭 Panel admin", "admin_panel")],
                        ]),
                    )
                else:
                    telegram_send_text(
                        bot_token,
                        chat_id,
                        "⚠️ No pude guardar la nueva recompensa.",
                    )
                return {"ok": True}

            if admin_survey_mode == "awaiting_new_question_text":
                question_text = text.strip()
                if not question_text:
                    telegram_send_text(
                        bot_token,
                        chat_id,
                        "La pregunta no puede estar vacía. Escríbela otra vez:",
                    )
                    return {"ok": True}

                tmp["admin_survey_new_question_text"] = question_text
                tmp["admin_survey_mode"] = "awaiting_new_question_type"

                telegram_send_text(
                    bot_token,
                    chat_id,
                    "Elige el tipo de la nueva pregunta:",
                    reply_markup=kb([
                        [("⭐ Estrellas", f"admsurv|{tenant_id}|settype|stars")],
                        [("✍️ Texto", f"admsurv|{tenant_id}|settype|text")],
                        [("⬅️ Volver a preguntas", f"admsurv|{tenant_id}|questions")],
                        [("🧭 Panel admin", "admin_panel")],
                    ]),
                )
                return {"ok": True}

            if admin_survey_mode == "awaiting_new_question_type":
                telegram_send_text(
                    bot_token,
                    chat_id,
                    "Selecciona el tipo usando los botones: ⭐ Estrellas o ✍️ Texto.",
                )
                return {"ok": True}

            if admin_survey_mode == "awaiting_edit_question_text":
                question_id = str(tmp.get("admin_survey_edit_qid") or "").strip()
                new_question_text = text.strip()

                if not question_id:
                    tmp.pop("admin_survey_mode", None)
                    tmp.pop("admin_survey_edit_qid", None)
                    telegram_send_text(
                        bot_token,
                        chat_id,
                        "⚠️ No encontré la pregunta a editar. Vuelve a entrar.",
                    )
                    return {"ok": True}

                if not new_question_text:
                    telegram_send_text(
                        bot_token,
                        chat_id,
                        "El nuevo texto no puede estar vacío. Escríbelo otra vez:",
                    )
                    return {"ok": True}

                ok = _survey_update_question_text_in_place(
                    orders_sh=orders_sh,
                    question_id=question_id,
                    new_text=new_question_text,
                )

                tmp.pop("admin_survey_mode", None)
                tmp.pop("admin_survey_edit_qid", None)

                if ok:
                    telegram_send_text(
                        bot_token,
                        chat_id,
                        "✅ Texto de pregunta actualizado.",
                    )
                else:
                    telegram_send_text(
                        bot_token,
                        chat_id,
                        "⚠️ No pude actualizar el texto de esa pregunta.",
                    )

                return {"ok": _send_admin_surveys_questions(bot_token, chat_id, tenant_id, orders_sh)}

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

            if create_step == "awaiting_category_SELECTION":
                telegram_send_text(
                    bot_token,
                    chat_id,
                    "Selecciona una categoría usando los botones o toca 'Nueva categoría'.",
                )
                return {"ok": True}

            if create_step == "awaiting_category_selection":
                telegram_send_text(
                    bot_token,
                    chat_id,
                    "Selecciona una categoría usando los botones o toca 'Nueva categoría'.",
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

        if txt_norm in ("estadisticas", "/stats", "stats"):
            assert_admin_authorized(tenant, chat_id, tenant_id)
            periods = build_periods(tenant_tz)
            telegram_send_text(
                bot_token,
                chat_id,
                "📊 Elige el período:",
                reply_markup=admin_periods_inline_kb(tenant_id, periods),
            )
            telegram_send_text(
                bot_token,
                chat_id,
                "Usa el botón inferior ⚙️ Panel cuando quieras volver.",
                reply_markup=admin_fixed_kb(),
            )
            return {"ok": True}

        if (
            txt_norm in ("crear pedido", "crear pedido manual", "pedido manual", "nuevo pedido")
            or "crear pedido" in txt_norm
        ):
            assert_admin_authorized(tenant, chat_id, tenant_id)

            if is_owner:
                telegram_send_text(
                    bot_token,
                    chat_id,
                    "🚫 Como propietario no puedes crear pedidos.",
                )
                return {"ok": True}

            _admin_order_reset(tmp)
            tmp["admin_order_cart"] = []
            return {"ok": _send_admin_order_home(bot_token, chat_id, tenant_id, orders_sh, sess)}

        if txt_norm in (
            "base de consumidores",
            "consumidores",
            "clientes",
            "base consumidores",
        ):
            assert_admin_authorized(tenant, chat_id, tenant_id)
            return {"ok": _send_consumers_menu(bot_token, chat_id, tenant_id)}

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

        if txt_norm in (
            "encuestas",
            "encuesta",
            "config encuestas",
            "configuracion encuestas",
            "configuracion de encuestas",
        ):
            assert_admin_authorized(tenant, chat_id, tenant_id)
            telegram_send_text(
                bot_token,
                chat_id,
                "📝 ENCUESTAS\n\n¿Qué deseas hacer?",
                reply_markup=kb([
                    [("⚙️ Configuración", f"admsurv|{tenant_id}|config")],
                    [("❓ Gestionar preguntas", f"admsurv|{tenant_id}|questions")],
                    [("📊 Ver resultados", f"admsurv|{tenant_id}|analytics")],
                    [("🧭 Panel admin", "admin_panel")],
                ]),
            )
            return {"ok": True}

        if txt_norm in ("start", "/start", "hola"):
            if is_owner:
                telegram_send_text(
                    bot_token,
                    chat_id,
                    "Bot propietario listo ✅\n\nUsa el botón inferior ⚙️ Panel.",
                    reply_markup=admin_fixed_kb(),
                )
            else:
                telegram_send_text(
                    bot_token,
                    chat_id,
                    "Admin bot listo ✅\n\nUsa el botón inferior ⚙️ Panel.",
                    reply_markup=admin_fixed_kb(),
                )
            return {"ok": True}

        if is_owner:
            telegram_send_text(
                bot_token,
                chat_id,
                "OK propietario ✅\n\nUsa el botón inferior ⚙️ Panel.",
                reply_markup=admin_fixed_kb(),
            )
        else:
            telegram_send_text(
                bot_token,
                chat_id,
                "OK admin ✅\n\nUsa el botón inferior ⚙️ Panel.",
                reply_markup=admin_fixed_kb(),
            )

        return {"ok": True}

    except Exception as e:
        log_event(
            "admin_message_error",
            tenant_id=tenant_id,
            chat_id=chat_id,
            error_type=type(e).__name__,
            error=str(e),
        )
        alert_system_error(error=str(e), module="admin_message")
        telegram_send_text(
            bot_token,
            chat_id,
            "⚠️ Ocurrió un error en el panel admin.",
            reply_markup=admin_fixed_kb(),
        )
        return {"ok": True}
