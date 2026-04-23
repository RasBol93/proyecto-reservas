# app/survey_questions.py

import time
from typing import Any, Dict, List, Optional, Tuple

from app.sheets import read_records_manual
from app.utils import normalize, to_bool, log_event
from app.alerts import alert_system_error, alert_sheet_error

from app.survey_core import (
    SURVEY_CONFIG_WS,
    SURVEY_CONFIG_HEADERS,
    SURVEY_ALLOWED_TYPES,
    _ensure_ws,
    _safe_str,
)


SURVEY_QUESTIONS_CACHE_TTL_SECONDS = 90
_SURVEY_QUESTIONS_CACHE: Dict[str, Tuple[float, List[Dict[str, Any]]]] = {}


def _cache_key(orders_sh) -> str:
    try:
        sid = getattr(orders_sh, "id", None)
        if sid:
            return str(sid)
    except Exception:
        pass
    return str(id(orders_sh))


def _cache_get(cache_key: str) -> Optional[List[Dict[str, Any]]]:
    cached = _SURVEY_QUESTIONS_CACHE.get(cache_key)
    if not cached:
        return None

    ts, data = cached
    if (time.time() - ts) <= SURVEY_QUESTIONS_CACHE_TTL_SECONDS:
        return data

    return None


def _cache_set(cache_key: str, data: List[Dict[str, Any]]) -> None:
    _SURVEY_QUESTIONS_CACHE[cache_key] = (time.time(), data)


def invalidate_survey_questions_cache(orders_sh) -> None:
    _SURVEY_QUESTIONS_CACHE.pop(_cache_key(orders_sh), None)


def load_survey_questions(orders_sh, force: bool = False) -> List[Dict[str, Any]]:
    try:
        cache_key = _cache_key(orders_sh)
        if force:
            invalidate_survey_questions_cache(orders_sh)
        else:
            cached = _cache_get(cache_key)
            if cached is not None:
                return cached

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
        _cache_set(cache_key, questions)
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
        invalidate_survey_questions_cache(orders_sh)

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
        invalidate_survey_questions_cache(orders_sh)

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
