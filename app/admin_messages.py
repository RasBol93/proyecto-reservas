# app/admin_messages.py — admin por texto "panel", pedido manual mejorado, QR de pagos,
# comprobante manual y encuesta runtime

from typing import Any, Dict, Optional

from app.admin_messages_menu import handle_admin_menu_message

from app.telegram_api import telegram_send_text
from app.telegram_keyboard import kb
from app.utils import normalize, log_event
from app.stats import build_periods
from app.webhook_helpers import (
    get_sess,
    assert_admin_authorized,
    admin_periods_inline_kb,
    admin_fixed_kb,
)
from app.admin_hours import send_admin_hours_menu
from app.admin_menu import (
    send_admin_menu_home,
)
from app.alerts import (
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
from app.admin_survey_runtime import (
    survey_runtime_stars_kb,
    clear_admin_survey_runtime,
    finalize_admin_survey_runtime,
)
from app.admin_order_runtime import (
    finalize_admin_manual_order,
)
from app.survey import (
    save_survey_password,
    save_survey_reward,
    load_survey_questions,
    get_runtime_survey_questions,
    has_answered_survey_today,
)
from app.sheets import get_ws
from app.tenants import update_tenant_payment_qr
from app.image_storage import upload_product_photo_for_tenant


SURVEY_CONFIG_WS = "Survey_Config"


def _is_owner_bot(tenant: Dict[str, Any]) -> bool:
    return bool(tenant.get("_is_owner_bot"))


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

        admin_payment_mode = str(tmp.get("admin_payment_mode") or "").strip()

        if admin_payment_mode == "awaiting_qr":
            assert_admin_authorized(tenant, chat_id, tenant_id)

            if msg.get("photo"):
                file_id = msg["photo"][-1]["file_id"]

                try:
                    from app.telegram_api import telegram_get_file_path, telegram_download_file_bytes

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

        if bool(tmp.get("admin_survey_runtime")):
            assert_admin_authorized(tenant, chat_id, tenant_id)

            questions = get_runtime_survey_questions(orders_sh)
            if not questions:
                clear_admin_survey_runtime(tmp)
                telegram_send_text(
                    bot_token,
                    chat_id,
                    "⚠️ No hay preguntas activas configuradas para la encuesta.",
                    reply_markup=admin_fixed_kb(),
                )
                return {"ok": True}

            step = str(tmp.get("admin_survey_step") or "").strip()

            if step == "start":
                phone = str(tmp.get("admin_order_last_phone") or "").strip()
                customer_name = str(tmp.get("admin_order_last_name") or "").strip()

                if not phone:
                    clear_admin_survey_runtime(tmp)
                    telegram_send_text(
                        bot_token,
                        chat_id,
                        "⚠️ No encontré el número del cliente del pedido.",
                        reply_markup=admin_fixed_kb(),
                    )
                    return {"ok": True}

                if has_answered_survey_today(orders_sh, tenant_tz, phone):
                    clear_admin_survey_runtime(tmp)
                    telegram_send_text(
                        bot_token,
                        chat_id,
                        "⚠️ Este cliente ya respondió una encuesta hoy.",
                        reply_markup=admin_fixed_kb(),
                    )
                    return {"ok": True}

                tmp["admin_survey_phone"] = phone
                tmp["admin_survey_name"] = customer_name
                tmp["admin_survey_step"] = "q_0"

                first_q = questions[0]
                first_qtype = str(first_q.get("type") or "").strip().lower()

                if first_qtype == "stars":
                    telegram_send_text(
                        bot_token,
                        chat_id,
                        f"❓ {first_q.get('question_text', '')}",
                        reply_markup=survey_runtime_stars_kb(tenant_id, 0),
                    )
                else:
                    telegram_send_text(
                        bot_token,
                        chat_id,
                        f"❓ {first_q.get('question_text', '')}",
                        reply_markup=admin_fixed_kb(),
                    )
                return {"ok": True}

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
                    clear_admin_survey_runtime(tmp)
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

                tmp["admin_survey_name"] = customer_name
                tmp["admin_survey_step"] = "q_0"

                first_q = questions[0]
                first_qtype = str(first_q.get("type") or "").strip().lower()

                if first_qtype == "stars":
                    telegram_send_text(
                        bot_token,
                        chat_id,
                        f"❓ {first_q.get('question_text', '')}",
                        reply_markup=survey_runtime_stars_kb(tenant_id, 0),
                    )
                else:
                    telegram_send_text(
                        bot_token,
                        chat_id,
                        f"❓ {first_q.get('question_text', '')}",
                        reply_markup=admin_fixed_kb(),
                    )
                return {"ok": True}

            if step.startswith("q_"):
                try:
                    idx = int(step.split("_")[1])
                except Exception:
                    idx = -1

                if idx < 0 or idx >= len(questions):
                    clear_admin_survey_runtime(tmp)
                    telegram_send_text(
                        bot_token,
                        chat_id,
                        "⚠️ Error en el flujo de encuesta.",
                        reply_markup=admin_fixed_kb(),
                    )
                    return {"ok": True}

                current_q = questions[idx]
                qtype = str(current_q.get("type") or "").strip().lower()

                if qtype == "stars":
                    telegram_send_text(
                        bot_token,
                        chat_id,
                        "⭐ Esta pregunta se responde tocando una estrella en pantalla.",
                        reply_markup=admin_fixed_kb(),
                    )
                    return {"ok": True}

                answers = tmp.setdefault("admin_survey_answers", [])
                answers.append({
                    "question_id": str(current_q.get("question_id") or ""),
                    "question_order": int(current_q.get("order", 0) or 0),
                    "question_text": str(current_q.get("question_text") or ""),
                    "answer_type": qtype,
                    "answer_value": text.strip(),
                })

                next_idx = idx + 1
                if next_idx < len(questions):
                    next_q = questions[next_idx]
                    tmp["admin_survey_step"] = f"q_{next_idx}"

                    next_qtype = str(next_q.get("type") or "").strip().lower()
                    if next_qtype == "stars":
                        telegram_send_text(
                            bot_token,
                            chat_id,
                            f"❓ {next_q.get('question_text', '')}",
                            reply_markup=survey_runtime_stars_kb(tenant_id, next_idx),
                        )
                    else:
                        telegram_send_text(
                            bot_token,
                            chat_id,
                            f"❓ {next_q.get('question_text', '')}",
                            reply_markup=admin_fixed_kb(),
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
                return finalize_admin_manual_order(
                    tenant_id=tenant_id,
                    bot_token=bot_token,
                    chat_id=chat_id,
                    orders_sh=orders_sh,
                    tmp=tmp,
                )

            if admin_order_step == "finalize_manual_order":
                return finalize_admin_manual_order(
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

        menu_result = handle_admin_menu_message(
            tenant=tenant,
            tenant_id=tenant_id,
            bot_token=bot_token,
            chat_id=chat_id,
            msg=msg,
            orders_sh=orders_sh,
            sess=sess,
        )
        if menu_result is not None:
            return menu_result

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
