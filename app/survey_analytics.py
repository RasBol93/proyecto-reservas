# app/survey_analytics.py

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple

from zoneinfo import ZoneInfo

from app.sheets import read_records_manual
from app.utils import normalize, log_event

from app.survey_core import (
    SURVEY_RESPONSES_WS,
    SURVEY_RESPONSES_HEADERS,
    _ensure_ws,
    _safe_str,
)


# ---------------------------------------------------------
# Períodos / temporalidad
# ---------------------------------------------------------

@dataclass
class SurveyPeriod:
    key: str
    label: str
    start_local: datetime
    end_local: datetime


def _month_name_es(month: int) -> str:
    names = ["Enero", "Febrero", "Marzo", "Abril", "Mayo", "Junio", "Julio", "Agosto", "Septiembre", "Octubre", "Noviembre", "Diciembre"]
    try:
        return names[month - 1]
    except Exception:
        return str(month)


def _quarter_number(month: int) -> int:
    if month in (1, 2, 3):
        return 1
    if month in (4, 5, 6):
        return 2
    if month in (7, 8, 9):
        return 3
    return 4


def _shift_year_month(year: int, month: int, delta_months: int) -> Tuple[int, int]:
    total = (year * 12 + (month - 1)) + delta_months
    new_year = total // 12
    new_month = (total % 12) + 1
    return new_year, new_month


def _start_of_day(dt: datetime) -> datetime:
    return datetime(dt.year, dt.month, dt.day, 0, 0, 0, tzinfo=dt.tzinfo)


def _start_of_week(dt: datetime) -> datetime:
    day_start = _start_of_day(dt)
    return day_start - timedelta(days=day_start.weekday())


def _start_of_month(dt: datetime) -> datetime:
    return datetime(dt.year, dt.month, 1, 0, 0, 0, tzinfo=dt.tzinfo)


def _start_of_quarter(dt: datetime) -> datetime:
    q = _quarter_number(dt.month)
    if q == 1:
        m = 1
    elif q == 2:
        m = 4
    elif q == 3:
        m = 7
    else:
        m = 10
    return datetime(dt.year, m, 1, 0, 0, 0, tzinfo=dt.tzinfo)


def _start_of_year(dt: datetime) -> datetime:
    return datetime(dt.year, 1, 1, 0, 0, 0, tzinfo=dt.tzinfo)


def survey_period_options(tenant_tz: str) -> List[Tuple[str, str]]:
    tz = ZoneInfo(tenant_tz)
    now_local = datetime.now(tz)

    year = now_local.year
    month = now_local.month

    y1, m1 = _shift_year_month(year, month, -1)
    y2, m2 = _shift_year_month(year, month, -2)
    y3, m3 = _shift_year_month(year, month, -3)

    return [
        ("Hoy", "today"),
        ("Ayer", "yesterday"),
        ("Esta semana", "this_week"),
        ("Semana pasada", "last_week"),
        ("Mes en curso", "month_to_date"),
        ("Mes anterior", "last_month"),
        (f"{_month_name_es(m1)} {y1}", "month_1_ago"),
        (f"{_month_name_es(m2)} {y2}", "month_2_ago"),
        (f"{_month_name_es(m3)} {y3}", "month_3_ago"),
        ("Trimestre en curso", "quarter_to_date"),
        ("Último trimestre", "last_quarter"),
        ("Año en curso", "year_to_date"),
    ]


def resolve_survey_period(period_key: str, tenant_tz: str) -> SurveyPeriod:
    tz = ZoneInfo(tenant_tz)
    now_local = datetime.now(tz)

    if period_key == "today":
        start_local = _start_of_day(now_local)
        return SurveyPeriod("today", "Hoy", start_local, now_local)

    if period_key == "yesterday":
        current_day_start = _start_of_day(now_local)
        yesterday_start = current_day_start - timedelta(days=1)
        return SurveyPeriod("yesterday", "Ayer", yesterday_start, current_day_start)

    if period_key == "this_week":
        start_local = _start_of_week(now_local)
        return SurveyPeriod("this_week", "Esta semana", start_local, now_local)

    if period_key == "last_week":
        current_week_start = _start_of_week(now_local)
        last_week_start = current_week_start - timedelta(days=7)
        return SurveyPeriod("last_week", "Semana pasada", last_week_start, current_week_start)

    if period_key == "month_to_date":
        start_local = _start_of_month(now_local)
        return SurveyPeriod("month_to_date", "Mes en curso", start_local, now_local)

    if period_key == "last_month":
        y, m = _shift_year_month(now_local.year, now_local.month, -1)
        start_local = datetime(y, m, 1, 0, 0, 0, tzinfo=tz)
        end_local = datetime(now_local.year, now_local.month, 1, 0, 0, 0, tzinfo=tz)
        return SurveyPeriod("last_month", "Mes anterior", start_local, end_local)

    if period_key == "month_1_ago":
        y, m = _shift_year_month(now_local.year, now_local.month, -1)
        start_local = datetime(y, m, 1, 0, 0, 0, tzinfo=tz)
        y_next, m_next = _shift_year_month(y, m, 1)
        end_local = datetime(y_next, m_next, 1, 0, 0, 0, tzinfo=tz)
        return SurveyPeriod("month_1_ago", f"{_month_name_es(m)} {y}", start_local, end_local)

    if period_key == "month_2_ago":
        y, m = _shift_year_month(now_local.year, now_local.month, -2)
        start_local = datetime(y, m, 1, 0, 0, 0, tzinfo=tz)
        y_next, m_next = _shift_year_month(y, m, 1)
        end_local = datetime(y_next, m_next, 1, 0, 0, 0, tzinfo=tz)
        return SurveyPeriod("month_2_ago", f"{_month_name_es(m)} {y}", start_local, end_local)

    if period_key == "month_3_ago":
        y, m = _shift_year_month(now_local.year, now_local.month, -3)
        start_local = datetime(y, m, 1, 0, 0, 0, tzinfo=tz)
        y_next, m_next = _shift_year_month(y, m, 1)
        end_local = datetime(y_next, m_next, 1, 0, 0, 0, tzinfo=tz)
        return SurveyPeriod("month_3_ago", f"{_month_name_es(m)} {y}", start_local, end_local)

    if period_key == "quarter_to_date":
        start_local = _start_of_quarter(now_local)
        return SurveyPeriod("quarter_to_date", "Trimestre en curso", start_local, now_local)

    if period_key == "last_quarter":
        current_q_start = _start_of_quarter(now_local)
        prev_q_end = current_q_start
        prev_q_start_year, prev_q_start_month = _shift_year_month(current_q_start.year, current_q_start.month, -3)
        prev_q_start = datetime(prev_q_start_year, prev_q_start_month, 1, 0, 0, 0, tzinfo=tz)
        prev_q_num = _quarter_number(prev_q_start_month)
        return SurveyPeriod("last_quarter", f"T{prev_q_num} {prev_q_start_year}", prev_q_start, prev_q_end)

    if period_key == "year_to_date":
        start_local = _start_of_year(now_local)
        return SurveyPeriod("year_to_date", "Año en curso", start_local, now_local)

    raise ValueError(f"Unknown survey period: {period_key}")


def _parse_iso_dt_any(value: Any) -> Optional[datetime]:
    s = str(value or "").strip()
    if not s:
        return None

    candidates = [
        s,
        s.replace("Z", "+00:00"),
        s.replace(" ", "T"),
        s.replace(" ", "T").replace("Z", "+00:00"),
    ]

    for c in candidates:
        try:
            dt = datetime.fromisoformat(c)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=ZoneInfo("UTC"))
            return dt
        except Exception:
            continue

    return None


def _to_local(dt: datetime, tenant_tz: str) -> datetime:
    tz = ZoneInfo(tenant_tz)
    return dt.astimezone(tz)


def _match_period(created_local: Optional[datetime], period: SurveyPeriod) -> bool:
    if not created_local:
        return False
    return period.start_local <= created_local < period.end_local


def _fmt_local_date(dt: datetime) -> str:
    return dt.strftime("%d/%m/%Y")


def _survey_period_range_text(period: SurveyPeriod) -> str:
    if period.end_local <= period.start_local:
        return f"{_fmt_local_date(period.start_local)} – {_fmt_local_date(period.start_local)}"
    end_inclusive = period.end_local - timedelta(seconds=1)
    return f"{_fmt_local_date(period.start_local)} – {_fmt_local_date(end_inclusive)}"


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


def build_survey_analytics(
    orders_sh,
    tenant_tz: str = "America/La_Paz",
    period_key: Optional[str] = None,
) -> Dict[str, Any]:
    rows = load_survey_response_rows(orders_sh)

    selected_period: Optional[SurveyPeriod] = None
    if period_key:
        try:
            selected_period = resolve_survey_period(period_key, tenant_tz)
        except Exception as e:
            log_event(
                "survey_resolve_period_error",
                period_key=str(period_key),
                tenant_tz=str(tenant_tz),
                error_type=type(e).__name__,
                error=str(e),
            )
            selected_period = None

    if selected_period is not None:
        filtered_rows: List[Dict[str, Any]] = []
        for r in rows:
            created_dt = _parse_iso_dt_any(r.get("created_at"))
            if not created_dt:
                continue

            created_local = _to_local(created_dt, tenant_tz)
            if _match_period(created_local, selected_period):
                filtered_rows.append(r)

        rows = filtered_rows

    if not rows:
        return {
            "period_label": selected_period.label if selected_period else "",
            "period_range_text": _survey_period_range_text(selected_period) if selected_period else "",
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
                "order_hint": 999999,
            }

        q = by_question_map[qid]
        q["count"] += 1

        try:
            q_order = int(_safe_str(r.get("question_order")) or "999999")
        except Exception:
            q_order = 999999

        if q_order < q["order_hint"]:
            q["order_hint"] = q_order

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
            "text_answers": q["text_answers"][:20],
            "order_hint": q["order_hint"],
        })

    by_question.sort(key=lambda x: (int(x.get("order_hint", 999999)), _safe_str(x["question_id"])))

    general_avg = round(sum(general_stars_values) / len(general_stars_values), 2) if general_stars_values else 0.0

    return {
        "period_label": selected_period.label if selected_period else "",
        "period_range_text": _survey_period_range_text(selected_period) if selected_period else "",
        "total_answers": len(rows),
        "total_unique_responses": len(response_ids),
        "general_stars_avg": general_avg,
        "general_stars_hist": general_hist,
        "by_question": by_question,
    }


# ---------------------------------------------------------
# Render visual admin
# ---------------------------------------------------------

def _hist_total(hist: Dict[int, int]) -> int:
    return sum(int(hist.get(n, 0)) for n in [1, 2, 3, 4, 5])


def _bar_blue(count: int, max_count: int, width: int = 10) -> str:
    if max_count <= 0:
        return "▫️"
    if count <= 0:
        return "▫️"

    filled = round((count / max_count) * width)
    filled = max(1, filled)
    return "🟦" * filled


def _build_hist_block(hist: Dict[int, int], width: int = 10) -> List[str]:
    max_count = max([int(hist.get(n, 0)) for n in [1, 2, 3, 4, 5]] + [0])
    lines: List[str] = []

    for n in [1, 2, 3, 4, 5]:
        count = int(hist.get(n, 0))
        bar = _bar_blue(count, max_count, width=width)
        lines.append(f"{n}⭐  {bar}  {count}")

    return lines


def build_survey_analytics_text(
    orders_sh,
    tenant_tz: str = "America/La_Paz",
    period_key: Optional[str] = None,
) -> str:
    data = build_survey_analytics(orders_sh, tenant_tz=tenant_tz, period_key=period_key)

    total_completed = int(data.get("total_unique_responses", 0))
    general_avg = float(data.get("general_stars_avg", 0.0))
    general_hist = data.get("general_stars_hist", {}) or {}
    by_question = data.get("by_question", []) or []
    period_label = _safe_str(data.get("period_label"))
    period_range_text = _safe_str(data.get("period_range_text"))

    lines: List[str] = []

    lines.append("╔══════════════════════╗")
    lines.append("║   🌟  ENCUESTAS      ║")
    lines.append("╚══════════════════════╝")
    lines.append("")

    if period_label:
        lines.append(f"🗓️ Período: {period_label}")
        if period_range_text:
            lines.append(f"📅 Rango: {period_range_text}")
        lines.append("")

    lines.append("📌 RESUMEN GENERAL")
    lines.append(f"• Encuestas completadas: {total_completed}")
    lines.append(f"• Promedio general: {general_avg:.2f}")
    lines.append("")
    lines.append("🟦 Distribución general de estrellas")
    lines.extend(_build_hist_block(general_hist))
    lines.append("")

    if not by_question:
        lines.append("Aún no hay resultados guardados.")
        return "\n".join(lines)

    lines.append("━━━━━━━━━━━━━━━━━━━━━━")
    lines.append("🧩 DETALLE POR PREGUNTA")
    lines.append("━━━━━━━━━━━━━━━━━━━━━━")

    for idx, q in enumerate(by_question, start=1):
        qtext = _safe_str(q.get("question_text"))
        answer_type = normalize(q.get("answer_type", ""))
        count = int(q.get("count", 0))
        stars_avg = float(q.get("stars_avg", 0.0))
        stars_hist = q.get("stars_hist", {}) or {}
        text_answers = q.get("text_answers", []) or []

        lines.append("")
        lines.append(f"❓ Pregunta {idx}")
        lines.append(qtext)
        lines.append(f"• Respuestas: {count}")

        if answer_type == "stars":
            lines.append(f"• Promedio: {stars_avg:.2f}")
            lines.append("🟦 Distribución")
            lines.extend(_build_hist_block(stars_hist))
        else:
            lines.append("💬 Respuestas recientes")
            if text_answers:
                for ans in text_answers[:5]:
                    lines.append(f"• {ans}")
            else:
                lines.append("• Sin respuestas todavía.")

        lines.append("──────────────────────")

    if lines and lines[-1] == "──────────────────────":
        lines.pop()

    return "\n".join(lines)
