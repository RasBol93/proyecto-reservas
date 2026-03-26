# app/client_flow.py

# =========================
# IMPORTS NUEVOS (AGREGADO)
# =========================
from app.survey import (
    survey_runtime_available,
    validate_survey_password,
    get_runtime_survey_questions,
    save_survey_answers,
    create_survey_coupon,
    get_survey_reward_text,
    has_answered_survey_today,
    _normalize_phone,  # reutilizamos helper
)

# =========================
# MODIFICACIÓN BOTÓN HOME
# =========================

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

    # 🔥 NUEVO: ENCUESTA REAL
    try:
        if orders_sh and survey_runtime_available(orders_sh):
            rows.append([("🌟 Encuesta", "survey_start")])
    except Exception:
        pass

    return kb(rows)


def _send_home(bot_token: str, chat_id: int, orders_sh) -> bool:
    content_map = load_content_map(orders_sh)
    return telegram_send_text(
        bot_token,
        chat_id,
        build_start_text(orders_sh),
        build_dynamic_home_kb(content_map, orders_sh),  # 👈 cambio aquí
    )


# =========================
# CALLBACK ENCUESTA
# =========================

# Dentro de handle_client_callback agregar:

        if data == "survey_start":
            if not survey_runtime_available(orders_sh):
                telegram_send_text(bot_token, chat_id, "Encuesta no disponible.")
                return {"ok": True}

            sess["stage"] = "survey_password"
            sess["survey"] = {}
            telegram_send_text(bot_token, chat_id, "🔒 Ingresa el password de la encuesta:")
            return {"ok": True}


# =========================
# HANDLE MESSAGE ENCUESTA
# =========================

# Dentro de handle_client_message, ANTES de lógica existente agregar:

        # =========================
        # ENCUESTA: PASSWORD
        # =========================
        if sess.get("stage") == "survey_password":
            if not validate_survey_password(orders_sh, text):
                telegram_send_text(bot_token, chat_id, "❌ Password incorrecto.")
                return {"ok": True}

            sess["stage"] = "survey_phone"
            telegram_send_text(bot_token, chat_id, "📱 Ingresa tu número de teléfono:")
            return {"ok": True}

        # =========================
        # ENCUESTA: TELEFONO
        # =========================
        if sess.get("stage") == "survey_phone":
            phone = _normalize_phone(text)

            if not phone or len(phone) < 7:
                telegram_send_text(bot_token, chat_id, "Número inválido. Intenta nuevamente.")
                return {"ok": True}

            if has_answered_survey_today(orders_sh, tenant_tz, phone):
                telegram_send_text(bot_token, chat_id, "⚠️ Ya respondiste la encuesta hoy.")
                sess["stage"] = "idle"
                return {"ok": True}

            sess["survey"] = {
                "phone": phone,
                "answers": [],
                "q_idx": 0,
            }

            questions = get_runtime_survey_questions(orders_sh)

            if not questions:
                telegram_send_text(bot_token, chat_id, "Encuesta no configurada.")
                sess["stage"] = "idle"
                return {"ok": True}

            q = questions[0]

            sess["stage"] = "survey_q"
            telegram_send_text(bot_token, chat_id, q["question_text"])
            return {"ok": True}

        # =========================
        # ENCUESTA: RESPUESTAS
        # =========================
        if sess.get("stage") == "survey_q":
            survey = sess.get("survey") or {}
            questions = get_runtime_survey_questions(orders_sh)

            idx = int(survey.get("q_idx") or 0)

            if idx >= len(questions):
                telegram_send_text(bot_token, chat_id, "Error de encuesta.")
                sess["stage"] = "idle"
                return {"ok": True}

            q = questions[idx]

            answer = text.strip()

            if q["type"] == "stars":
                if answer not in ("1", "2", "3", "4", "5"):
                    telegram_send_text(bot_token, chat_id, "Responde con 1 a 5.")
                    return {"ok": True}

            survey["answers"].append({
                "question_id": q["question_id"],
                "question_order": q["order"],
                "question_text": q["question_text"],
                "answer_type": q["type"],
                "answer_value": answer,
            })

            idx += 1
            survey["q_idx"] = idx
            sess["survey"] = survey

            # siguiente pregunta
            if idx < len(questions):
                next_q = questions[idx]
                telegram_send_text(bot_token, chat_id, next_q["question_text"])
                return {"ok": True}

            # =========================
            # FINAL ENCUESTA
            # =========================
            phone = survey.get("phone")

            coupon_res = create_survey_coupon(
                orders_sh=orders_sh,
                tenant_id=tenant_id,
                phone=phone,
                reward_text=get_survey_reward_text(orders_sh),
            )

            coupon_code = coupon_res.get("coupon_code") if coupon_res.get("ok") else ""

            save_survey_answers(
                orders_sh=orders_sh,
                tenant_id=tenant_id,
                tenant_tz=tenant_tz,
                customer_phone=phone,
                customer_name="",
                answers=survey["answers"],
                coupon_code=coupon_code,
            )

            reward = get_survey_reward_text(orders_sh)

            telegram_send_text(
                bot_token,
                chat_id,
                f"🙏 Gracias por completar la encuesta\n\n🎁 Recompensa:\n{reward}\n\n🔑 Código:\n{coupon_code}",
            )

            sess["stage"] = "idle"
            return {"ok": True}
