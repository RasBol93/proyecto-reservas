# app/survey.py
#
# Núcleo del sistema de encuestas:
# - settings (activar/desactivar, password, recompensa)
# - preguntas configurables
# - respuestas
# - cupones
# - control "1 encuesta por teléfono por día"
# - helpers de analítica
#
# Diseño:
# - simple
# - Sheets como storage ligero
# - sin dependencias raras
# - fácil de debug
# - fácil de mantener

import random
import string
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple
from zoneinfo import ZoneInfo

from app.sheets import get_ws, read_records_manual
from app.utils import normalize, to_bool, log_event
from app.alerts import alert_system_error, alert_sheet_error


SURVEY_SETTINGS_WS = "Survey_Settings"
SURVEY_CONFIG_WS = "Survey_Config"
SURVEY_RESPONSES_WS = "Survey_Responses"
SURVEY_COUPONS_WS = "Survey_Coupons"

SURVEY_SETTINGS_HEADERS = ["key", "value", "active"]
SURVEY_CONFIG_HEADERS = ["question_id", "order", "question_text", "type", "active"]
SURVEY_RESPONSES_HEADERS = [
    "response_id",
    "created_at",
    "survey_date",
    "tenant_id",
    "customer_phone",
    "customer_name",
    "question_id",
    "question_order",
    "question_text",
    "answer_type",
    "answer_value",
    "coupon_code",
]
SURVEY_COUPONS_HEADERS = [
    "coupon_code",
    "created_at",
    "tenant_id",
    "customer_phone",
    "reward_text",
    "used",
    "used_at",
]

SURVEY_ALLOWED_TYPES = {"text", "stars"}


# ---------------------------------------------------------
# Básicos
# ---------------------------------------------------------

def _safe_str(v: Any) -> str:
    return str(v or "").strip()


def _now_local(tenant_tz: str) -> datetime:
    return datetime.now(ZoneInfo(tenant_tz))


def _now_utc_iso() -> str:
    return datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")


def _survey_date_local(tenant_tz: str) -> str:
    return _now_local(tenant_tz).strftime("%Y-%m-%d")


def _normalize_phone(value: Any) -> str:
    raw = _safe_str(value)
    digits = "".join(ch for ch in raw if ch.isdigit())
    return digits


def _is_valid_phone(value: Any) -> bool:
    phone = _normalize_phone(value)
    return len(phone) >= 7


def _random_letters(n: int = 3) -> str:
    return "".join(random.choices(string.ascii_uppercase, k=n))


def _make_response_id(phone: str) -> str:
    base = datetime.utcnow().strftime("%Y%m%d%H%M%S")
    suffix = _random_letters(4)
    return f"srv_{base}_{phone}_{suffix}"


def _make_coupon_code(phone: str) -> str:
    phone_norm = _normalize_phone(phone)
    if not phone_norm:
        phone_norm = "SINNUMERO"
    return f"{phone_norm}{_random_letters(3)}"


def _spreadsheet_id(spreadsheet) -> str:
    try:
        sid = getattr(spreadsheet, "id", None)
        if sid:
            return str(sid)
    except Exception:
        pass
    return ""


# ---------------------------------------------------------
# Worksheet helpers
# ---------------------------------------------------------

def _ensure_ws(spreadsheet, title: str, headers: List[str]):
    try:
        ws = get_ws(spreadsheet, title)
        values = ws.get_all_values()
        if not values:
            ws.append_row(headers, value_input_option="USER_ENTERED")
        return ws
    except Exception:
        try:
            ws = spreadsheet.add_worksheet(title=title, rows=2000, cols=max(len(headers) + 2, 10))
            ws.append_row(headers, value_input_option="USER_ENTERED")
            return ws
        except Exception as e:
            log_event(
                "survey_ensure_ws_error",
                worksheet=title,
                error_type=type(e).__name__,
                error=str(e),
            )
            alert_sheet_error(
                tenant_id="",
                error=f"survey ensure ws failed for '{title}': {e}",
                extra_key="survey._ensure_ws",
            )
            raise


def ensure_survey_worksheets(orders_sh) -> Dict[str, Any]:
    return {
        "settings": _ensure_ws(orders_sh, SURVEY_SETTINGS_WS, SURVEY_SETTINGS_HEADERS),
        "config": _ensure_ws(orders_sh, SURVEY_CONFIG_WS, SURVEY_CONFIG_HEADERS),
        "responses": _ensure_ws(orders_sh, SURVEY_RESPONSES_WS, SURVEY_RESPONSES_HEADERS),
        "coupons": _ensure_ws(orders_sh, SURVEY_COUPONS_WS, SURVEY_COUPONS_HEADERS),
    }


# ---------------------------------------------------------
# Settings
# ---------------------------------------------------------

def load_survey_settings(orders_sh) -> Dict[str, str]:
    try:
        ws = _ensure_ws(orders_sh, SURVEY_SETTINGS_WS, SURVEY_SETTINGS_HEADERS)
        rows = read_records_manual(ws, required_headers=SURVEY_SETTINGS_HEADERS)

        out: Dict[str, str] = {}
        for r in rows:
            key = normalize(r.get("key", ""))
            value = _safe_str(r.get("value"))
            active = to_bool(r.get("active", ""))

            if not key:
                continue
            if not active:
                continue

            out[key] = value

        return out

    except Exception as e:
        log_event(
            "survey_load_settings_error",
            error_type=type(e).__name__,
            error=str(e),
        )
        alert_system_error(error=str(e), module="survey.load_survey_settings")
        raise


def survey_is_enabled(orders_sh) -> bool:
    try:
        settings = load_survey_settings(orders_sh)
        raw = _safe_str(settings.get("survey_enabled", ""))
        if not raw:
            return False
        return to_bool(raw)
    except Exception:
        return False


def get_survey_reward_text(orders_sh) -> str:
    try:
        settings = load_survey_settings(orders_sh)
        return _safe_str(settings.get("survey_reward", ""))
    except Exception:
        return ""


def get_survey_password(orders_sh) -> str:
    try:
        settings = load_survey_settings(orders_sh)
        return _safe_str(settings.get("survey_password", ""))
    except Exception:
        return ""


def validate_survey_password(orders_sh, plain_text: str) -> bool:
    try:
        expected = get_survey_password(orders_sh)
        if not expected:
            return False
        return _safe_str(plain_text) == expected
    except Exception:
        return False


def set_survey_setting(orders_sh, key: str, value: str, active: bool = True) -> bool:
    """
    Estrategia simple:
    - append-only
    - la lectura toma el último activo por key, porque out[key] = value pisa el anterior
    """
    try:
        ws = _ensure_ws(orders_sh, SURVEY_SETTINGS_WS, SURVEY_SETTINGS_HEADERS)
        ws.append_row(
            [
                _safe_str(key),
                _safe_str(value),
                "TRUE" if active else "FALSE",
            ],
            value_input_option="USER_ENTERED",
        )
        return True
    except Exception as e:
        log_event(
            "survey_set_setting_error",
            key=_safe_str(key),
            error_type=type(e).__name__,
            error=str(e),
        )
        alert_sheet_error(
            tenant_id="",
            error=f"survey set setting failed: {e}",
            extra_key="survey.set_survey_setting",
        )
        return False


def save_survey_enabled(orders_sh, enabled: bool) -> bool:
    return set_survey_setting(orders_sh, "survey_enabled", "TRUE" if enabled else "FALSE", active=True)


def save_survey_reward(orders_sh, reward_text: str) -> bool:
    return set_survey_setting(orders_sh, "survey_reward", reward_text, active=True)


def save_survey_password(orders_sh, password: str) -> bool:
    return set_survey_setting(orders_sh, "survey_password", password, active=True)


# ---------------------------------------------------------
# Questions
# ---------------------------------------------------------

def load_survey_questions(orders_sh) -> List[Dict[str, Any]]:
    try:
        ws = _ensure_ws(orders_sh, SURVEY_CONFIG_WS, SURVEY_CONFIG_HEADERS)
        rows = read_records_manual(ws, required_headers=SURVEY_CONFIG_HEADERS)

        questions: List[Dict[str, Any]] = []
        for r in rows:
            question_id = _safe_str(r.get("question_id"))
            question_text = _safe_str(r.get("question_text"))
            qtype = normalize(r.get("type", ""))
            active = to_bool(r.get("active", ""))

            try:
                order = int(_safe_str(r.get("order")) or "0")
            except Exception:
                order = 0

            if not active:
                continue
            if not question_id or not question_text:
                continue
            if qtype not in SURVEY_ALLOWED_TYPES:
                continue

            questions.append({
                "question_id": question_id,
                "order": order,
                "question_text": question_text,
                "type": qtype,
                "active": True,
            })

        questions.sort(key=lambda x: (int(x.get("order", 0)), _safe_str(x.get("question_id"))))
        return questions

    except Exception as e:
        log_event(
            "survey_load_questions_error",
            error_type=type(e).__name__,
            error=str(e),
        )
        alert_system_error(error=str(e), module="survey.load_survey_questions")
        raise


def _next_question_order(orders_sh) -> int:
    questions = load_survey_questions(orders_sh)
    if not questions:
        return 1
    return max(int(q.get("order", 0)) for q in questions) + 1


def _next_question_id(orders_sh) -> str:
    questions = load_survey_questions(orders_sh)
    max_n = 0
    for q in questions:
        qid = _safe_str(q.get("question_id"))
        if qid.lower().startswith("q"):
            try:
                n = int(qid[1:])
                if n > max_n:
                    max_n = n
            except Exception:
                continue
    return f"q{max_n + 1}"


def add_survey_question(orders_sh, question_text: str, question_type: str) -> Dict[str, Any]:
    try:
        qtext = _safe_str(question_text)
        qtype = normalize(question_type)

        if not qtext:
            return {"ok": False, "error": "question_text_empty"}

        if qtype not in SURVEY_ALLOWED_TYPES:
            return {"ok": False, "error": "invalid_question_type"}

        ws = _ensure_ws(orders_sh, SURVEY_CONFIG_WS, SURVEY_CONFIG_HEADERS)
        question_id = _next_question_id(orders_sh)
        order = _next_question_order(orders_sh)

        ws.append_row(
            [
                question_id,
                str(order),
                qtext,
                qtype,
                "TRUE",
            ],
            value_input_option="USER_ENTERED",
        )

        return {
            "ok": True,
            "question_id": question_id,
            "order": order,
            "question_text": qtext,
            "type": qtype,
        }

    except Exception as e:
        log_event(
            "survey_add_question_error",
            error_type=type(e).__name__,
            error=str(e),
        )
        alert_sheet_error(
            tenant_id="",
            error=f"survey add question failed: {e}",
            extra_key="survey.add_survey_question",
        )
        return {"ok": False, "error": str(e)}


def disable_survey_question(orders_sh, question_id: str) -> Dict[str, Any]:
    """
    Estrategia simple y segura:
    - appendamos una nueva versión del registro con active=FALSE
    - load_survey_questions solo lee activas
    """
    try:
        qid = _safe_str(question_id)
        if not qid:
            return {"ok": False, "error": "missing_question_id"}

        questions = load_survey_questions(orders_sh)
        target = None
        for q in questions:
            if _safe_str(q.get("question_id")) == qid:
                target = q
                break

        if not target:
            return {"ok": False, "error": "question_not_found"}

        ws = _ensure_ws(orders_sh, SURVEY_CONFIG_WS, SURVEY_CONFIG_HEADERS)
        ws.append_row(
            [
                qid,
                str(int(target.get("order", 0))),
                _safe_str(target.get("question_text")),
                _safe_str(target.get("type")),
                "FALSE",
            ],
            value_input_option="USER_ENTERED",
        )

        return {"ok": True, "question_id": qid}

    except Exception as e:
        log_event(
            "survey_disable_question_error",
            question_id=_safe_str(question_id),
            error_type=type(e).__name__,
            error=str(e),
        )
        alert_sheet_error(
            tenant_id="",
            error=f"survey disable question failed: {e}",
            extra_key="survey.disable_survey_question",
        )
        return {"ok": False, "error": str(e)}


# ---------------------------------------------------------
# Coupon + limits
# ---------------------------------------------------------

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
    fallback = f"{phone_norm}{datetime.utcnow().strftime('%H%M%S')}"
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


# ---------------------------------------------------------
# Responses
# ---------------------------------------------------------

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
        phone_norm = _normalize_phone(customer_phone)
        if not _is_valid_phone(phone_norm):
            return {"ok": False, "error": "invalid_phone"}

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


# ---------------------------------------------------------
# Runtime helpers para flujo cliente
# ---------------------------------------------------------

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


# ---------------------------------------------------------
# Analítica
# ---------------------------------------------------------

def load_survey_response_rows(orders_sh) -> List[Dict[str, Any]]:
    try:
        ws = _ensure_ws(orders_sh, SURVEY_RESPONSES_WS, SURVEY_RESPONSES_HEADERS)
        return read_records_manual(ws, required_headers=SURVEY_RESPONSES_HEADERS)
    except Exception as e:
        log_event(
            "survey_load_response_rows_error",
            error_type=type(e).__name__,
            error=str(e),
        )
        return []


def build_survey_analytics(orders_sh) -> Dict[str, Any]:
    """
    Devuelve estructura simple para luego renderizar en admin.
    """
    rows = load_survey_response_rows(orders_sh)
    if not rows:
        return {
            "total_answers": 0,
            "total_unique_responses": 0,
            "general_stars_avg": 0.0,
            "general_stars_hist": {1: 0, 2: 0, 3: 0, 4: 0, 5: 0},
            "by_question": [],
        }

    response_ids = set()
    general_stars_values: List[int] = []
    general_hist = {1: 0, 2: 0, 3: 0, 4: 0, 5: 0}
    by_question_map: Dict[str, Dict[str, Any]] = {}

    for r in rows:
        response_id = _safe_str(r.get("response_id"))
        if response_id:
            response_ids.add(response_id)

        qid = _safe_str(r.get("question_id"))
        qtext = _safe_str(r.get("question_text"))
        atype = normalize(r.get("answer_type", ""))
        avalue = _safe_str(r.get("answer_value"))

        if qid not in by_question_map:
            by_question_map[qid] = {
                "question_id": qid,
                "question_text": qtext,
                "answer_type": atype,
                "count": 0,
                "stars_values": [],
                "stars_hist": {1: 0, 2: 0, 3: 0, 4: 0, 5: 0},
                "text_answers": [],
            }

        q = by_question_map[qid]
        q["count"] += 1

        if atype == "stars":
            try:
                n = int(avalue)
            except Exception:
                n = 0

            if n in general_hist:
                general_hist[n] += 1
                general_stars_values.append(n)

            if n in q["stars_hist"]:
                q["stars_hist"][n] += 1
                q["stars_values"].append(n)

        elif atype == "text":
            if avalue:
                q["text_answers"].append(avalue)

    by_question: List[Dict[str, Any]] = []
    for _, q in by_question_map.items():
        stars_vals = q["stars_values"]
        stars_avg = round(sum(stars_vals) / len(stars_vals), 2) if stars_vals else 0.0

        by_question.append({
            "question_id": q["question_id"],
            "question_text": q["question_text"],
            "answer_type": q["answer_type"],
            "count": q["count"],
            "stars_avg": stars_avg,
            "stars_hist": q["stars_hist"],
            "text_answers": q["text_answers"][:20],  # límite defensivo para reporte simple
        })

    by_question.sort(key=lambda x: (_safe_str(x["question_id"])))

    general_avg = round(sum(general_stars_values) / len(general_stars_values), 2) if general_stars_values else 0.0

    return {
        "total_answers": len(rows),
        "total_unique_responses": len(response_ids),
        "general_stars_avg": general_avg,
        "general_stars_hist": general_hist,
        "by_question": by_question,
    }


# ---------------------------------------------------------
# Reporte admin simple
# ---------------------------------------------------------

def _stars_hist_text(hist: Dict[int, int]) -> str:
    parts = []
    for n in [1, 2, 3, 4, 5]:
        parts.append(f"{n}⭐: {int(hist.get(n, 0))}")
    return " | ".join(parts)


def build_survey_analytics_text(orders_sh) -> str:
    data = build_survey_analytics(orders_sh)

    lines: List[str] = []
    lines.append("🌟 ENCUESTAS")
    lines.append("")
    lines.append(f"Total respuestas guardadas: {int(data.get('total_answers', 0))}")
    lines.append(f"Encuestas completadas: {int(data.get('total_unique_responses', 0))}")
    lines.append(f"Promedio general estrellas: {float(data.get('general_stars_avg', 0.0)):.2f}")
    lines.append(f"Gráfico general estrellas: {_stars_hist_text(data.get('general_stars_hist', {}))}")
    lines.append("")

    by_question = data.get("by_question") or []
    if not by_question:
        lines.append("No hay resultados todavía.")
        return "\n".join(lines)

    lines.append("Detalle por pregunta:")
    for q in by_question:
        lines.append("")
        lines.append(f"• {q.get('question_text', '')}")
        lines.append(f"  Tipo: {q.get('answer_type', '')}")
        lines.append(f"  Respuestas: {int(q.get('count', 0))}")

        if q.get("answer_type") == "stars":
            lines.append(f"  Promedio: {float(q.get('stars_avg', 0.0)):.2f}")
            lines.append(f"  Gráfico: {_stars_hist_text(q.get('stars_hist', {}))}")
        else:
            text_answers = q.get("text_answers") or []
            if text_answers:
                lines.append("  Últimas respuestas:")
                for ans in text_answers[:5]:
                    lines.append(f"   - {ans}")
            else:
                lines.append("  Sin respuestas textuales.")

    return "\n".join(lines)
