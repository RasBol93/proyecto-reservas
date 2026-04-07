# app/survey_runtime.py

from typing import Any, Dict, List

from app.sheets import read_records_manual
from app.utils import log_event
from app.alerts import alert_sheet_error

from app.survey_core import (
    SURVEY_RESPONSES_WS,
    SURVEY_RESPONSES_HEADERS,
    SURVEY_COUPONS_WS,
    SURVEY_COUPONS_HEADERS,
    _ensure_ws,
    _safe_str,
    _normalize_phone,
    _is_valid_phone,
    _make_response_id,
    _make_coupon_code,
    _now_utc_iso,
    _survey_date_local,
)
from app.survey_settings import (
    survey_is_enabled,
    get_survey_password,
)
from app.survey_questions import (
    load_survey_questions,
)


def has_answered_survey_today(orders_sh, tenant_tz: str, phone: str) -> bool:
    try:
        phone_norm = _normalize_phone(phone)
        if not phone_norm:
            return False

        ws = _ensure_ws(orders_sh, SURVEY_RESPONSES_WS, SURVEY_RESPONSES_HEADERS)
        rows = read_records_manual(ws, required_headers=SURVEY_RESPONSES_HEADERS)
        today = _survey_date_local(tenant_tz)

        for r in rows:
            row_phone = _normalize_phone(r.get("customer_phone"))
            row_date = _safe_str(r.get("survey_date"))
            if row_phone == phone_norm and row_date == today:
                return True

        return False

    except Exception as e:
        log_event(
            "survey_has_answered_today_error",
            phone=_normalize_phone(phone),
            error_type=type(e).__name__,
            error=str(e),
        )
        return False


def coupon_exists(orders_sh, coupon_code: str) -> bool:
    try:
        code = _safe_str(coupon_code)
        if not code:
            return False

        ws = _ensure_ws(orders_sh, SURVEY_COUPONS_WS, SURVEY_COUPONS_HEADERS)
        rows = read_records_manual(ws, required_headers=SURVEY_COUPONS_HEADERS)

        for r in rows:
            if _safe_str(r.get("coupon_code")) == code:
                return True

        return False

    except Exception:
        return False


def generate_unique_coupon_code(orders_sh, phone: str) -> str:
    phone_norm = _normalize_phone(phone)
    for _ in range(50):
        code = _make_coupon_code(phone_norm)
        if not coupon_exists(orders_sh, code):
            return code
    fallback = f"{phone_norm}{_now_utc_iso().replace('-', '').replace(':', '').replace('T', '').replace('Z', '')[-6:]}"
    return fallback


def create_survey_coupon(orders_sh, tenant_id: str, phone: str, reward_text: str) -> Dict[str, Any]:
    try:
        phone_norm = _normalize_phone(phone)
        if not phone_norm:
            return {"ok": False, "error": "invalid_phone"}

        code = generate_unique_coupon_code(orders_sh, phone_norm)
        ws = _ensure_ws(orders_sh, SURVEY_COUPONS_WS, SURVEY_COUPONS_HEADERS)

        ws.append_row(
            [
                code,
                _now_utc_iso(),
                _safe_str(tenant_id),
                phone_norm,
                _safe_str(reward_text),
                "FALSE",
                "",
            ],
            value_input_option="USER_ENTERED",
        )

        return {
            "ok": True,
            "coupon_code": code,
            "customer_phone": phone_norm,
            "reward_text": _safe_str(reward_text),
        }

    except Exception as e:
        log_event(
            "survey_create_coupon_error",
            tenant_id=_safe_str(tenant_id),
            phone=_normalize_phone(phone),
            error_type=type(e).__name__,
            error=str(e),
        )
        alert_sheet_error(
            tenant_id=_safe_str(tenant_id),
            error=f"survey create coupon failed: {e}",
            extra_key="survey.create_survey_coupon",
        )
        return {"ok": False, "error": str(e)}


def save_survey_answers(
    orders_sh,
    tenant_id: str,
    tenant_tz: str,
    customer_phone: str,
    customer_name: str,
    answers: List[Dict[str, Any]],
    coupon_code: str,
) -> Dict[str, Any]:
    """
    answers esperado:
    [
      {
        "question_id": "...",
        "question_order": 1,
        "question_text": "...",
        "answer_type": "text" | "stars",
        "answer_value": "..."
      },
      ...
    ]
    """
    try:
        raw_phone = str(customer_phone or "").strip()
        phone_norm = _normalize_phone(raw_phone)

        # Permitir encuestas admin sin celular real.
        # Si no hay número utilizable, guardamos un marcador estable.
        if not phone_norm:
            phone_norm = "SIN_CONTACTO"

        # Solo validamos si realmente hay número.
        if phone_norm != "SIN_CONTACTO" and not _is_valid_phone(phone_norm):
            return {"ok": False, "error": "invalid_phone"}

        # Solo aplicamos control de duplicado por día cuando sí existe teléfono.
        if phone_norm != "SIN_CONTACTO":
            if has_answered_survey_today(orders_sh, tenant_tz, phone_norm):
                return {"ok": False, "error": "already_answered_today"}

        if not answers:
            return {"ok": False, "error": "empty_answers"}

        ws = _ensure_ws(orders_sh, SURVEY_RESPONSES_WS, SURVEY_RESPONSES_HEADERS)
        response_id = _make_response_id(phone_norm)
        created_at = _now_utc_iso()
        survey_date = _survey_date_local(tenant_tz)

        for a in answers:
            ws.append_row(
                [
                    response_id,
                    created_at,
                    survey_date,
                    _safe_str(tenant_id),
                    phone_norm,
                    _safe_str(customer_name),
                    _safe_str(a.get("question_id")),
                    str(int(a.get("question_order", 0) or 0)),
                    _safe_str(a.get("question_text")),
                    _safe_str(a.get("answer_type")),
                    _safe_str(a.get("answer_value")),
                    _safe_str(coupon_code),
                ],
                value_input_option="USER_ENTERED",
            )

        return {
            "ok": True,
            "response_id": response_id,
            "survey_date": survey_date,
            "customer_phone": phone_norm,
            "answers_saved": len(answers),
            "coupon_code": _safe_str(coupon_code),
        }

    except Exception as e:
        log_event(
            "survey_save_answers_error",
            tenant_id=_safe_str(tenant_id),
            phone=_normalize_phone(customer_phone),
            error_type=type(e).__name__,
            error=str(e),
        )
        alert_sheet_error(
            tenant_id=_safe_str(tenant_id),
            error=f"survey save answers failed: {e}",
            extra_key="survey.save_survey_answers",
        )
        return {"ok": False, "error": str(e)}


def get_runtime_survey_questions(orders_sh) -> List[Dict[str, Any]]:
    """
    Devuelve preguntas activas para el flujo cliente.
    Si no existe pregunta de teléfono, NO la agrega aquí;
    el teléfono se manejará como paso obligatorio del flujo.
    """
    questions = load_survey_questions(orders_sh)
    return questions


def survey_runtime_available(orders_sh) -> bool:
    try:
        if not survey_is_enabled(orders_sh):
            return False
        qs = get_runtime_survey_questions(orders_sh)
        return len(qs) > 0 and bool(get_survey_password(orders_sh))
    except Exception:
        return False
