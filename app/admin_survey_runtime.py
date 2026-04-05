# app/admin_survey_runtime.py

from typing import Any, Dict, List

from app.telegram_api import telegram_send_text
from app.telegram_keyboard import kb
from app.webhook_helpers import admin_fixed_kb
from app.survey import (
    get_survey_reward_text,
    create_survey_coupon,
    save_survey_answers,
)


def survey_runtime_stars_kb(tenant_id: str, q_idx: int) -> Dict[str, Any]:
    return kb([
        [("1⭐", f"admord|{tenant_id}|sstar|{q_idx}|1")],
        [("2⭐", f"admord|{tenant_id}|sstar|{q_idx}|2")],
        [("3⭐", f"admord|{tenant_id}|sstar|{q_idx}|3")],
        [("4⭐", f"admord|{tenant_id}|sstar|{q_idx}|4")],
        [("5⭐", f"admord|{tenant_id}|sstar|{q_idx}|5")],
        [("🧭 Panel admin", "admin_panel")],
    ])


def clear_admin_survey_runtime(tmp: Dict[str, Any]) -> None:
    tmp.pop("admin_survey_runtime", None)
    tmp.pop("admin_survey_step", None)
    tmp.pop("admin_survey_answers", None)
    tmp.pop("admin_survey_phone", None)
    tmp.pop("admin_survey_name", None)


def send_admin_survey_runtime_question(
    bot_token: str,
    chat_id: int,
    tenant_id: str,
    question: Dict[str, Any],
    q_idx: int,
) -> None:
    qtext = str(question.get("question_text") or "").strip()
    qtype = str(question.get("type") or "").strip().lower()

    if qtype == "stars":
        telegram_send_text(
            bot_token,
            chat_id,
            f"❓ {qtext}",
            reply_markup=survey_runtime_stars_kb(tenant_id, q_idx),
        )
        return

    telegram_send_text(
        bot_token,
        chat_id,
        f"❓ {qtext}",
        reply_markup=admin_fixed_kb(),
    )


def finalize_admin_survey_runtime(
    tenant_id: str,
    bot_token: str,
    chat_id: int,
    orders_sh,
    tenant_tz: str,
    tmp: Dict[str, Any],
) -> Dict[str, Any]:
    phone = str(tmp.get("admin_survey_phone") or "").strip()
    customer_name = str(tmp.get("admin_survey_name") or "").strip()
    answers: List[Dict[str, Any]] = tmp.get("admin_survey_answers") or []

    reward_text = get_survey_reward_text(orders_sh)
    coupon_code = ""

    if reward_text:
        coupon_res = create_survey_coupon(
            orders_sh=orders_sh,
            tenant_id=tenant_id,
            phone=phone,
            reward_text=reward_text,
        )
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

    clear_admin_survey_runtime(tmp)

    if not save_res.get("ok"):
        err = str(save_res.get("error") or "").strip()
        if err == "already_answered_today":
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

    telegram_send_text(
        bot_token,
        chat_id,
        "✅ Gracias por responder nuestra encuesta.\nTe daremos tu tarjeta de descuento.",
        reply_markup=admin_fixed_kb(),
    )
    return {"ok": True}
