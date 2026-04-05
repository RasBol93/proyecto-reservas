# app/admin_messages_surveys.py

from typing import Any, Dict, Optional

from app.telegram_api import telegram_send_text
from app.telegram_keyboard import kb
from app.webhook_helpers import assert_admin_authorized, admin_fixed_kb
from app.admin_survey_runtime import (
    survey_runtime_stars_kb,
    clear_admin_survey_runtime,
    finalize_admin_survey_runtime,
)
from app.survey import (
    save_survey_password,
    save_survey_reward,
    load_survey_questions,
    get_runtime_survey_questions,
    has_answered_survey_today,
)
from app.sheets import get_ws


SURVEY_CONFIG_WS = "Survey_Config"


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


def _survey_find_last_active_question_row(ws, question_id: str):
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


def handle_admin_surveys_message(
    tenant: Dict[str, Any],
    tenant_id: str,
    bot_token: str,
    chat_id: int,
    text: str,
    orders_sh,
    tenant_tz: str,
    tmp: Dict[str, Any],
) -> Optional[Dict[str, Any]]:
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

    admin_survey_mode = str(tmp.get("admin_survey_mode") or "").strip()

    if not admin_survey_mode:
        return None

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

    return None
