from typing import Any, Dict, Optional

from fastapi import HTTPException

from app.telegram_api import telegram_send_text
from app.telegram_keyboard import kb
from app.webhook_helpers import (
    get_sess,
    assert_admin_authorized,
)
from app.survey import (
    survey_is_enabled,
    save_survey_enabled,
    get_survey_password,
    get_survey_reward_text,
    build_survey_analytics_text,
    load_survey_questions,
    survey_period_options,
    add_survey_question,
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


def _survey_get_question_by_id(orders_sh, question_id: str) -> Optional[Dict[str, Any]]:
    questions = load_survey_questions(orders_sh)
    for q in questions:
        if str(q.get("question_id") or "").strip() == question_id:
            return q
    return None


def _survey_update_question_in_place(
    orders_sh,
    question_id: str,
    *,
    question_text: Optional[str] = None,
    question_type: Optional[str] = None,
    active: Optional[bool] = None,
) -> bool:
    ws = _survey_config_ws(orders_sh)
    hmap = _survey_header_map(ws)
    target_row = _survey_find_last_active_question_row(ws, question_id)

    if not target_row:
        return False

    idx_qtext = hmap.get("question_text")
    idx_qtype = hmap.get("type")
    idx_active = hmap.get("active")

    if question_text is not None and idx_qtext is not None:
        ws.update_cell(target_row, idx_qtext + 1, str(question_text).strip())

    if question_type is not None and idx_qtype is not None:
        ws.update_cell(target_row, idx_qtype + 1, str(question_type).strip().lower())

    if active is not None and idx_active is not None:
        ws.update_cell(target_row, idx_active + 1, "TRUE" if active else "FALSE")

    return True


def _send_admin_surveys_home(
    bot_token: str,
    chat_id: int,
    tenant_id: str,
    orders_sh,
) -> bool:
    enabled = survey_is_enabled(orders_sh)
    status = "🟢 Activa" if enabled else "🔴 Inactiva"

    telegram_send_text(
        bot_token,
        chat_id,
        (
            "📝 ENCUESTAS\n\n"
            f"Estado actual: {status}\n\n"
            "¿Qué deseas hacer?"
        ),
        reply_markup=kb([
            [("⚙️ Configuración", f"admsurv|{tenant_id}|config")],
            [("❓ Gestionar preguntas", f"admsurv|{tenant_id}|questions")],
            [("📊 Ver resultados", f"admsurv|{tenant_id}|analytics")],
            [("⬅️ Volver", "admin_panel")],
        ]),
    )
    return True


def _send_admin_surveys_config(
    bot_token: str,
    chat_id: int,
    tenant_id: str,
    orders_sh,
) -> bool:
    enabled = survey_is_enabled(orders_sh)
    status = "🟢 Activa" if enabled else "🔴 Inactiva"
    password = get_survey_password(orders_sh) or "(sin definir)"
    reward = get_survey_reward_text(orders_sh) or "(sin definir)"

    telegram_send_text(
        bot_token,
        chat_id,
        (
            "⚙️ CONFIGURACIÓN DE ENCUESTAS\n\n"
            f"Estado: {status}\n"
            f"Password actual: {password}\n"
            f"Recompensa actual: {reward}\n\n"
            "Elige una opción:"
        ),
        reply_markup=kb([
            [("🔁 Activar / Desactivar", f"admsurv|{tenant_id}|toggle")],
            [("🔑 Cambiar password", f"admsurv|{tenant_id}|password")],
            [("🎁 Cambiar recompensa", f"admsurv|{tenant_id}|reward")],
            [("⬅️ Volver a encuestas", "admin_surveys")],
            [("🧭 Panel admin", "admin_panel")],
        ]),
    )
    return True


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


def _send_admin_surveys_periods(
    bot_token: str,
    chat_id: int,
    tenant_id: str,
    tenant_tz: str,
) -> bool:
    rows = []
    for label, key in survey_period_options(tenant_tz):
        rows.append([(f"📊 {label}", f"admsurv|{tenant_id}|period|{key}")])

    rows.extend([
        [("⬅️ Volver a encuestas", "admin_surveys")],
        [("🧭 Panel admin", "admin_panel")],
    ])

    telegram_send_text(
        bot_token,
        chat_id,
        "📊 RESULTADOS DE ENCUESTAS\n\nSelecciona el período:",
        reply_markup=kb(rows),
    )
    return True


def handle_admin_surveys_callback(
    tenant: Dict[str, Any],
    tenant_id: str,
    bot_token: str,
    chat_id: int,
    data: str,
    orders_sh,
    tenant_tz: str,
) -> Optional[Dict[str, Any]]:
    if data == "admin_surveys":
        assert_admin_authorized(tenant, chat_id, tenant_id)
        return {"ok": _send_admin_surveys_home(bot_token, chat_id, tenant_id, orders_sh)}

    if not data.startswith("admsurv|"):
        return None

    assert_admin_authorized(tenant, chat_id, tenant_id)

    parts = data.split("|")
    if len(parts) < 3:
        return {"ok": True}

    cb_tenant_id = parts[1].strip()
    if cb_tenant_id != tenant_id:
        raise HTTPException(status_code=400, detail="Tenant mismatch in survey callback")

    action = parts[2].strip()
    sess = get_sess(tenant_id, chat_id)
    tmp = sess.setdefault("tmp", {})

    if action == "config":
        return {"ok": _send_admin_surveys_config(bot_token, chat_id, tenant_id, orders_sh)}

    if action == "toggle":
        current = survey_is_enabled(orders_sh)
        ok = save_survey_enabled(orders_sh, not current)
        if ok:
            telegram_send_text(
                bot_token,
                chat_id,
                f"✅ Encuesta {'activada' if not current else 'desactivada'}.",
            )
        else:
            telegram_send_text(
                bot_token,
                chat_id,
                "⚠️ No pude cambiar el estado de la encuesta.",
            )
        return {"ok": _send_admin_surveys_config(bot_token, chat_id, tenant_id, orders_sh)}

    if action == "password":
        tmp["admin_survey_mode"] = "awaiting_password"
        telegram_send_text(
            bot_token,
            chat_id,
            "🔑 Escribe el nuevo password de la encuesta:",
        )
        return {"ok": True}

    if action == "reward":
        tmp["admin_survey_mode"] = "awaiting_reward"
        telegram_send_text(
            bot_token,
            chat_id,
            "🎁 Escribe la nueva recompensa literal.\nEjemplo: 50% de descuento",
        )
        return {"ok": True}

    if action == "questions":
        return {"ok": _send_admin_surveys_questions(bot_token, chat_id, tenant_id, orders_sh)}

    if action == "addq":
        tmp["admin_survey_mode"] = "awaiting_new_question_text"
        telegram_send_text(
            bot_token,
            chat_id,
            "✍️ Escribe el texto de la nueva pregunta:",
        )
        return {"ok": True}

    if action == "editq" and len(parts) == 4:
        question_id = parts[3].strip()
        question = _survey_get_question_by_id(orders_sh, question_id)

        if not question:
            telegram_send_text(
                bot_token,
                chat_id,
                "⚠️ No encontré esa pregunta.",
            )
            return {"ok": _send_admin_surveys_questions(bot_token, chat_id, tenant_id, orders_sh)}

        tmp["admin_survey_mode"] = "awaiting_edit_question_text"
        tmp["admin_survey_edit_qid"] = question_id

        telegram_send_text(
            bot_token,
            chat_id,
            (
                "✏️ CAMBIAR TEXTO DE PREGUNTA\n\n"
                f"Pregunta actual:\n{question.get('question_text', '')}\n\n"
                "Escribe el nuevo texto:"
            ),
        )
        return {"ok": True}

    if action == "chtype" and len(parts) == 4:
        question_id = parts[3].strip()
        question = _survey_get_question_by_id(orders_sh, question_id)

        if not question:
            telegram_send_text(
                bot_token,
                chat_id,
                "⚠️ No encontré esa pregunta.",
            )
            return {"ok": _send_admin_surveys_questions(bot_token, chat_id, tenant_id, orders_sh)}

        current_type = _survey_type_label(str(question.get("type") or "").strip())

        telegram_send_text(
            bot_token,
            chat_id,
            (
                "🔁 CAMBIAR TIPO DE PREGUNTA\n\n"
                f"Pregunta:\n{question.get('question_text', '')}\n\n"
                f"Tipo actual: {current_type}\n\n"
                "Elige el nuevo tipo:"
            ),
            reply_markup=kb([
                [("✍️ Texto", f"admsurv|{tenant_id}|settype_existing|{question_id}|text")],
                [("⭐ Estrellas", f"admsurv|{tenant_id}|settype_existing|{question_id}|stars")],
                [("⬅️ Volver a preguntas", f"admsurv|{tenant_id}|questions")],
                [("🧭 Panel admin", "admin_panel")],
            ]),
        )
        return {"ok": True}

    if action == "settype_existing" and len(parts) == 5:
        question_id = parts[3].strip()
        qtype = parts[4].strip().lower()

        if qtype not in ("text", "stars"):
            telegram_send_text(
                bot_token,
                chat_id,
                "⚠️ Tipo inválido.",
            )
            return {"ok": True}

        ok = _survey_update_question_in_place(
            orders_sh,
            question_id,
            question_type=qtype,
        )

        if ok:
            telegram_send_text(
                bot_token,
                chat_id,
                f"✅ Tipo actualizado a {_survey_type_label(qtype)}.",
            )
        else:
            telegram_send_text(
                bot_token,
                chat_id,
                "⚠️ No pude actualizar el tipo de esa pregunta.",
            )

        return {"ok": _send_admin_surveys_questions(bot_token, chat_id, tenant_id, orders_sh)}

    if action == "delq" and len(parts) == 4:
        question_id = parts[3].strip()
        ok = _survey_update_question_in_place(
            orders_sh,
            question_id,
            active=False,
        )

        if ok:
            telegram_send_text(
                bot_token,
                chat_id,
                f"✅ Pregunta {question_id} eliminada.",
            )
        else:
            telegram_send_text(
                bot_token,
                chat_id,
                "⚠️ No pude eliminar esa pregunta.",
            )

        return {"ok": _send_admin_surveys_questions(bot_token, chat_id, tenant_id, orders_sh)}

    if action == "settype" and len(parts) == 4:
        qtype = parts[3].strip().lower()
        pending_qtext = str(tmp.get("admin_survey_new_question_text") or "").strip()

        if not pending_qtext:
            telegram_send_text(
                bot_token,
                chat_id,
                "⚠️ No encontré el texto de la nueva pregunta. Vuelve a empezar.",
            )
            tmp.pop("admin_survey_mode", None)
            tmp.pop("admin_survey_new_question_text", None)
            return {"ok": _send_admin_surveys_questions(bot_token, chat_id, tenant_id, orders_sh)}

        if qtype not in ("text", "stars"):
            telegram_send_text(
                bot_token,
                chat_id,
                "⚠️ Tipo inválido.",
            )
            return {"ok": True}

        result = add_survey_question(orders_sh, pending_qtext, qtype)

        tmp.pop("admin_survey_mode", None)
        tmp.pop("admin_survey_new_question_text", None)

        if result.get("ok"):
            telegram_send_text(
                bot_token,
                chat_id,
                f"✅ Pregunta creada.\nTipo: {_survey_type_label(qtype)}\nTexto: {pending_qtext}",
            )
        else:
            telegram_send_text(
                bot_token,
                chat_id,
                "⚠️ No pude crear la pregunta.",
            )

        return {"ok": _send_admin_surveys_questions(bot_token, chat_id, tenant_id, orders_sh)}

    if action == "analytics":
        return {"ok": _send_admin_surveys_periods(bot_token, chat_id, tenant_id, tenant_tz)}

    if action == "period" and len(parts) == 4:
        period_key = parts[3].strip()
        txt = build_survey_analytics_text(
            orders_sh,
            tenant_tz=tenant_tz,
            period_key=period_key,
        )
        telegram_send_text(
            bot_token,
            chat_id,
            txt,
            reply_markup=kb([
                [("⬅️ Períodos", f"admsurv|{tenant_id}|analytics")],
                [("⬅️ Volver a encuestas", "admin_surveys")],
                [("🧭 Panel admin", "admin_panel")],
            ]),
        )
        return {"ok": True}

    return {"ok": True}
