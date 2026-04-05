# app/survey.py
#
# Fachada de compatibilidad para el sistema de encuestas.
# Mantiene los imports históricos:
#   from app.survey import ...
#
# La lógica real vive ahora en:
# - app.survey_core
# - app.survey_settings
# - app.survey_questions
# - app.survey_runtime
# - app.survey_analytics

from app.survey_core import *
from app.survey_settings import *
from app.survey_questions import *
from app.survey_runtime import *
from app.survey_analytics import *

# Compatibilidad explícita para imports legacy con nombres privados
from app.survey_core import (
    _safe_str,
    _now_local,
    _now_utc_iso,
    _survey_date_local,
    _normalize_phone,
    _is_valid_phone,
    _random_letters,
    _make_response_id,
    _make_coupon_code,
    _ensure_ws,
    SURVEY_SETTINGS_WS,
    SURVEY_CONFIG_WS,
    SURVEY_RESPONSES_WS,
    SURVEY_COUPONS_WS,
    SURVEY_SETTINGS_HEADERS,
    SURVEY_CONFIG_HEADERS,
    SURVEY_RESPONSES_HEADERS,
    SURVEY_COUPONS_HEADERS,
    SURVEY_ALLOWED_TYPES,
    ensure_survey_worksheets,
)

from app.survey_analytics import (
    SurveyPeriod,
    _month_name_es,
    _quarter_number,
    _shift_year_month,
    _start_of_day,
    _start_of_week,
    _start_of_month,
    _start_of_quarter,
    _start_of_year,
    _parse_iso_dt_any,
    _to_local,
    _match_period,
    _fmt_local_date,
    _survey_period_range_text,
    _hist_total,
    _bar_blue,
    _build_hist_block,
)
