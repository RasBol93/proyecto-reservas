# app/survey_analytics.py

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Dict, List

from zoneinfo import ZoneInfo

from app.sheets import read_records_manual
from app.utils import log_event

from app.survey_core import (
    SURVEY_RESPONSES_WS,
    SURVEY_RESPONSES_HEADERS,
    _ensure_ws,
    _safe_str,
    _normalize_phone,
)


# =========================================================
# 📅 PERIODOS
# =========================================================

@dataclass
class SurveyPeriod:
    key: str
    label: str
    start_local: datetime
    end_local: datetime


def survey_period_options(tenant_tz: str) -> List[SurveyPeriod]:
    now = datetime.now(ZoneInfo(tenant_tz))

    today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    week_start = today_start - timedelta(days=today_start.weekday())
    month_start = today_start.replace(day=1)

    return [
        SurveyPeriod(
            key="today",
            label="Hoy",
            start_local=today_start,
            end_local=now,
        ),
        SurveyPeriod(
            key="week",
            label="Esta semana",
            start_local=week_start,
            end_local=now,
        ),
        SurveyPeriod(
            key="month",
            label="Este mes",
            start_local=month_start,
            end_local=now,
        ),
    ]


def resolve_survey_period(tenant_tz: str, key: str) -> SurveyPeriod:
    for p in survey_period_options(tenant_tz):
        if p.key == key:
            return p
    return survey_period_options(tenant_tz)[0]


# =========================================================
# 📊 LECTURA DE RESPUESTAS
# =========================================================

def load_survey_response_rows(orders_sh) -> List[Dict[str, Any]]:
    ws = _ensure_ws(orders_sh, SURVEY_RESPONSES_WS, SURVEY_RESPONSES_HEADERS)
    rows = read_records_manual(ws, required_headers=SURVEY_RESPONSES_HEADERS)
    return rows


def _parse_dt_local(dt_str: str, tenant_tz: str) -> datetime:
    try:
        dt = datetime.strptime(dt_str, "%Y-%m-%dT%H:%M:%SZ")
        return dt.replace(tzinfo=ZoneInfo("UTC")).astimezone(ZoneInfo(tenant_tz))
    except Exception:
        return datetime.now(ZoneInfo(tenant_tz))


# =========================================================
# 📈 ANALÍTICA
# =========================================================

def build_survey_analytics(
    orders_sh,
    tenant_tz: str,
    period: SurveyPeriod,
) -> Dict[str, Any]:
    rows = load_survey_response_rows(orders_sh)

    filtered: List[Dict[str, Any]] = []

    for r in rows:
        dt_local = _parse_dt_local(_safe_str(r.get("created_at")), tenant_tz)
        if period.start_local <= dt_local <= period.end_local:
            filtered.append(r)

    total_answers = len(filtered)

    # únicos clientes
    phones = set()
    for r in filtered:
        phones.add(_normalize_phone(r.get("customer_phone")))

    unique_customers = len([p for p in phones if p])

    # ⭐ promedio estrellas
    star_values = []
    for r in filtered:
        if _safe_str(r.get("answer_type")) == "stars":
            try:
                star_values.append(float(r.get("answer_value")))
            except Exception:
                continue

    avg_stars = round(sum(star_values) / len(star_values), 2) if star_values else 0.0

    # conteo por pregunta
    questions: Dict[str, int] = {}
    for r in filtered:
        q = _safe_str(r.get("question_text"))
        if not q:
            continue
        questions[q] = questions.get(q, 0) + 1

    return {
        "total_answers": total_answers,
        "unique_customers": unique_customers,
        "avg_stars": avg_stars,
        "questions_count": questions,
    }


# =========================================================
# 🧾 TEXTO
# =========================================================

def build_survey_analytics_text(
    orders_sh,
    tenant_tz: str,
    period: SurveyPeriod,
) -> str:
    try:
        data = build_survey_analytics(orders_sh, tenant_tz, period)

        lines = [
            "📊 RESULTADOS ENCUESTAS",
            "",
            f"Período: {period.label}",
            "",
            f"Respuestas: {data['total_answers']}",
            f"Clientes únicos: {data['unique_customers']}",
            f"Promedio estrellas: {data['avg_stars']}",
            "",
        ]

        if data["questions_count"]:
            lines.append("Preguntas respondidas:")
            for q, count in data["questions_count"].items():
                lines.append(f"• {q}: {count}")

        return "\n".join(lines)

    except Exception as e:
        log_event(
            "survey_analytics_text_error",
            error_type=type(e).__name__,
            error=str(e),
        )
        return "⚠️ Error generando analítica de encuestas."
