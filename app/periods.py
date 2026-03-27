# app/periods.py
#
# Fuente única de verdad para periodos del sistema.
#
# Objetivo:
# - centralizar definición de opciones de temporalidad
# - centralizar resolución de rangos
# - reutilizar en stats, consumers y surveys
# - mantener compatibilidad conceptual con la lógica ya validada
#
# Convenciones:
# - rangos siempre semiabiertos: [start, end)
# - los labels visibles salen de aquí
# - se soportan periodos "hasta ahora" y periodos cerrados
# - timezone local por tenant, con fallback seguro a America/La_Paz
#
# Importante:
# - este archivo por sí solo no rompe nada
# - la migración de stats / consumers / surveys se hará después
# - se exponen tanto periodos UTC (para stats) como locales (para consumers/surveys)

from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import List, Optional, Tuple

try:
    from zoneinfo import ZoneInfo
except Exception:
    ZoneInfo = None  # type: ignore


DEFAULT_TENANT_TZ = "America/La_Paz"


# ---------------------------------------------------------
# Dataclasses
# ---------------------------------------------------------

@dataclass
class UTCPeriod:
    key: str
    label: str
    start_utc: datetime   # naive UTC
    end_utc: datetime     # naive UTC


@dataclass
class LocalPeriod:
    key: str
    label: str
    start_local: datetime  # aware local dt
    end_local: datetime    # aware local dt


# ---------------------------------------------------------
# TZ helpers
# ---------------------------------------------------------

def _tz(tenant_tz: str):
    if ZoneInfo is None:
        return None
    try:
        return ZoneInfo(tenant_tz or DEFAULT_TENANT_TZ)
    except Exception:
        return ZoneInfo(DEFAULT_TENANT_TZ)


def now_utc_naive() -> datetime:
    return datetime.utcnow().replace(tzinfo=None)


def now_local(tenant_tz: str, now_utc: Optional[datetime] = None) -> datetime:
    if now_utc is None:
        now_utc = now_utc_naive()

    tz = _tz(tenant_tz)
    if tz is None:
        return now_utc

    return now_utc.replace(tzinfo=ZoneInfo("UTC")).astimezone(tz)


def utc_naive_to_local(dt_utc_naive: datetime, tenant_tz: str) -> datetime:
    tz = _tz(tenant_tz)
    if tz is None:
        return dt_utc_naive
    return dt_utc_naive.replace(tzinfo=ZoneInfo("UTC")).astimezone(tz)


def local_aware_to_utc_naive(dt_local: datetime) -> datetime:
    if dt_local.tzinfo is None:
        return dt_local
    return dt_local.astimezone(ZoneInfo("UTC")).replace(tzinfo=None)


# ---------------------------------------------------------
# Date / formatting helpers
# ---------------------------------------------------------

def month_name_es(month: int) -> str:
    names = [
        "Enero", "Febrero", "Marzo", "Abril", "Mayo", "Junio",
        "Julio", "Agosto", "Septiembre", "Octubre", "Noviembre", "Diciembre",
    ]
    try:
        return names[month - 1]
    except Exception:
        return str(month)


def quarter_number(month: int) -> int:
    if month in (1, 2, 3):
        return 1
    if month in (4, 5, 6):
        return 2
    if month in (7, 8, 9):
        return 3
    return 4


def quarter_start_month(month: int) -> int:
    q = quarter_number(month)
    if q == 1:
        return 1
    if q == 2:
        return 4
    if q == 3:
        return 7
    return 10


def quarter_label(year: int, quarter: int) -> str:
    return f"T{quarter} {year}"


def shift_year_month(year: int, month: int, delta_months: int) -> Tuple[int, int]:
    total = (year * 12 + (month - 1)) + delta_months
    new_year = total // 12
    new_month = (total % 12) + 1
    return new_year, new_month


def start_of_day_local(dt_local: datetime) -> datetime:
    return datetime(dt_local.year, dt_local.month, dt_local.day, 0, 0, 0, tzinfo=dt_local.tzinfo)


def start_of_week_local(dt_local: datetime) -> datetime:
    return start_of_day_local(dt_local) - timedelta(days=dt_local.weekday())


def start_of_month_local(dt_local: datetime) -> datetime:
    return datetime(dt_local.year, dt_local.month, 1, 0, 0, 0, tzinfo=dt_local.tzinfo)


def start_of_quarter_local(dt_local: datetime) -> datetime:
    m = quarter_start_month(dt_local.month)
    return datetime(dt_local.year, m, 1, 0, 0, 0, tzinfo=dt_local.tzinfo)


def start_of_year_local(dt_local: datetime) -> datetime:
    return datetime(dt_local.year, 1, 1, 0, 0, 0, tzinfo=dt_local.tzinfo)


def local_day_range_utc(tenant_tz: str, day_local: date) -> Tuple[datetime, datetime]:
    tz = _tz(tenant_tz)
    if tz is None:
        start = datetime(day_local.year, day_local.month, day_local.day, 0, 0, 0)
        end = start + timedelta(days=1)
        return start, end

    start_local = datetime(day_local.year, day_local.month, day_local.day, 0, 0, 0, tzinfo=tz)
    end_local = start_local + timedelta(days=1)

    start_utc = start_local.astimezone(ZoneInfo("UTC")).replace(tzinfo=None)
    end_utc = end_local.astimezone(ZoneInfo("UTC")).replace(tzinfo=None)
    return start_utc, end_utc


def local_month_range_utc(tenant_tz: str, year: int, month: int) -> Tuple[datetime, datetime]:
    tz = _tz(tenant_tz)
    if tz is None:
        start = datetime(year, month, 1, 0, 0, 0)
        if month == 12:
            end = datetime(year + 1, 1, 1, 0, 0, 0)
        else:
            end = datetime(year, month + 1, 1, 0, 0, 0)
        return start, end

    start_local = datetime(year, month, 1, 0, 0, 0, tzinfo=tz)
    if month == 12:
        end_local = datetime(year + 1, 1, 1, 0, 0, 0, tzinfo=tz)
    else:
        end_local = datetime(year, month + 1, 1, 0, 0, 0, tzinfo=tz)

    start_utc = start_local.astimezone(ZoneInfo("UTC")).replace(tzinfo=None)
    end_utc = end_local.astimezone(ZoneInfo("UTC")).replace(tzinfo=None)
    return start_utc, end_utc


def format_date_local(dt_local: datetime) -> str:
    return dt_local.strftime("%d/%m/%Y")


def format_local_period_range(period: LocalPeriod) -> str:
    if period.end_local <= period.start_local:
        return f"{format_date_local(period.start_local)} – {format_date_local(period.start_local)}"
    end_inclusive = period.end_local - timedelta(seconds=1)
    return f"{format_date_local(period.start_local)} – {format_date_local(end_inclusive)}"


def format_utc_period_range_local(period: UTCPeriod, tenant_tz: str) -> str:
    start_local = utc_naive_to_local(period.start_utc, tenant_tz)
    end_local = utc_naive_to_local(period.end_utc - timedelta(seconds=1), tenant_tz)
    return f"{format_date_local(start_local)} – {format_date_local(end_local)}"


# ---------------------------------------------------------
# Public period options
# ---------------------------------------------------------

def get_period_options(tenant_tz: str, now_utc: Optional[datetime] = None) -> List[Tuple[str, str]]:
    """
    Devuelve lista de (label, key).

    Orden:
    - Hoy
    - Ayer
    - Esta semana
    - Semana pasada
    - Mes en curso
    - Mes anterior
    - 3 meses individuales
    - Trimestre en curso
    - Último trimestre
    - Año en curso
    """
    now_local_dt = now_local(tenant_tz, now_utc=now_utc)

    year = now_local_dt.year
    month = now_local_dt.month

    y1, m1 = shift_year_month(year, month, -1)
    y2, m2 = shift_year_month(year, month, -2)
    y3, m3 = shift_year_month(year, month, -3)

    return [
        ("Hoy", "today"),
        ("Ayer", "yesterday"),
        ("Esta semana", "this_week"),
        ("Semana pasada", "last_week"),
        ("Mes en curso", "month_to_date"),
        ("Mes anterior", "last_month"),
        (f"{month_name_es(m1)} {y1}", "month_1_ago"),
        (f"{month_name_es(m2)} {y2}", "month_2_ago"),
        (f"{month_name_es(m3)} {y3}", "month_3_ago"),
        ("Trimestre en curso", "quarter_to_date"),
        ("Último trimestre", "last_quarter"),
        ("Año en curso", "year_to_date"),
    ]


# Alias de compatibilidad semántica
build_periods = get_period_options


# ---------------------------------------------------------
# Resolve as local period
# ---------------------------------------------------------

def resolve_period_local(period_key: str, tenant_tz: str, now_utc: Optional[datetime] = None) -> LocalPeriod:
    now_local_dt = now_local(tenant_tz, now_utc=now_utc)
    tzinfo = now_local_dt.tzinfo

    if period_key == "today":
        start_local = start_of_day_local(now_local_dt)
        return LocalPeriod("today", "Hoy", start_local, now_local_dt)

    if period_key == "yesterday":
        today_start = start_of_day_local(now_local_dt)
        yesterday_start = today_start - timedelta(days=1)
        return LocalPeriod("yesterday", "Ayer", yesterday_start, today_start)

    if period_key == "this_week":
        start_local = start_of_week_local(now_local_dt)
        return LocalPeriod("this_week", "Esta semana", start_local, now_local_dt)

    if period_key == "last_week":
        current_week_start = start_of_week_local(now_local_dt)
        last_week_start = current_week_start - timedelta(days=7)
        return LocalPeriod("last_week", "Semana pasada", last_week_start, current_week_start)

    if period_key == "month_to_date":
        start_local = start_of_month_local(now_local_dt)
        return LocalPeriod("month_to_date", "Mes en curso", start_local, now_local_dt)

    if period_key == "last_month":
        y, m = shift_year_month(now_local_dt.year, now_local_dt.month, -1)
        start_local = datetime(y, m, 1, 0, 0, 0, tzinfo=tzinfo)
        end_local = datetime(now_local_dt.year, now_local_dt.month, 1, 0, 0, 0, tzinfo=tzinfo)
        return LocalPeriod("last_month", "Mes anterior", start_local, end_local)

    if period_key == "month_1_ago":
        y, m = shift_year_month(now_local_dt.year, now_local_dt.month, -1)
        start_local = datetime(y, m, 1, 0, 0, 0, tzinfo=tzinfo)
        y_next, m_next = shift_year_month(y, m, 1)
        end_local = datetime(y_next, m_next, 1, 0, 0, 0, tzinfo=tzinfo)
        return LocalPeriod("month_1_ago", f"{month_name_es(m)} {y}", start_local, end_local)

    if period_key == "month_2_ago":
        y, m = shift_year_month(now_local_dt.year, now_local_dt.month, -2)
        start_local = datetime(y, m, 1, 0, 0, 0, tzinfo=tzinfo)
        y_next, m_next = shift_year_month(y, m, 1)
        end_local = datetime(y_next, m_next, 1, 0, 0, 0, tzinfo=tzinfo)
        return LocalPeriod("month_2_ago", f"{month_name_es(m)} {y}", start_local, end_local)

    if period_key == "month_3_ago":
        y, m = shift_year_month(now_local_dt.year, now_local_dt.month, -3)
        start_local = datetime(y, m, 1, 0, 0, 0, tzinfo=tzinfo)
        y_next, m_next = shift_year_month(y, m, 1)
        end_local = datetime(y_next, m_next, 1, 0, 0, 0, tzinfo=tzinfo)
        return LocalPeriod("month_3_ago", f"{month_name_es(m)} {y}", start_local, end_local)

    if period_key == "quarter_to_date":
        start_local = start_of_quarter_local(now_local_dt)
        return LocalPeriod("quarter_to_date", "Trimestre en curso", start_local, now_local_dt)

    if period_key == "last_quarter":
        current_q_start = start_of_quarter_local(now_local_dt)
        prev_q_end = current_q_start
        prev_q_start_year, prev_q_start_month = shift_year_month(current_q_start.year, current_q_start.month, -3)
        prev_q_start = datetime(prev_q_start_year, prev_q_start_month, 1, 0, 0, 0, tzinfo=tzinfo)
        prev_q_num = quarter_number(prev_q_start_month)
        return LocalPeriod("last_quarter", f"T{prev_q_num} {prev_q_start_year}", prev_q_start, prev_q_end)

    if period_key == "year_to_date":
        start_local = start_of_year_local(now_local_dt)
        return LocalPeriod("year_to_date", "Año en curso", start_local, now_local_dt)

    raise ValueError(f"Unknown period key: {period_key}")


# ---------------------------------------------------------
# Resolve as UTC period
# ---------------------------------------------------------

def resolve_period_utc(tenant_tz: str, period_key: str, now_utc: Optional[datetime] = None) -> UTCPeriod:
    if now_utc is None:
        now_utc = now_utc_naive()

    local_period = resolve_period_local(period_key=period_key, tenant_tz=tenant_tz, now_utc=now_utc)

    # Para períodos "hasta ahora", el end_utc debe ser exactamente now_utc
    # para conservar compatibilidad con stats.
    if period_key in {"today", "this_week", "month_to_date", "quarter_to_date", "year_to_date"}:
        return UTCPeriod(
            key=local_period.key,
            label=local_period.label,
            start_utc=local_aware_to_utc_naive(local_period.start_local),
            end_utc=now_utc,
        )

    return UTCPeriod(
        key=local_period.key,
        label=local_period.label,
        start_utc=local_aware_to_utc_naive(local_period.start_local),
        end_utc=local_aware_to_utc_naive(local_period.end_local),
    )


# Alias de compatibilidad para stats
def resolve_period(tenant_tz: str, period_key: str, now_utc: Optional[datetime] = None) -> UTCPeriod:
    return resolve_period_utc(tenant_tz=tenant_tz, period_key=period_key, now_utc=now_utc)


# ---------------------------------------------------------
# Matching helpers
# ---------------------------------------------------------

def match_local_datetime_to_period(dt_local: Optional[datetime], period: LocalPeriod) -> bool:
    if not dt_local:
        return False
    return period.start_local <= dt_local < period.end_local


def match_utc_naive_datetime_to_period(dt_utc_naive: Optional[datetime], period: UTCPeriod) -> bool:
    if not dt_utc_naive:
        return False
    return period.start_utc <= dt_utc_naive < period.end_utc


def parse_iso_to_utc_naive(value: str) -> Optional[datetime]:
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
                # si viene naive, lo tratamos como UTC
                return dt
            return dt.astimezone(ZoneInfo("UTC")).replace(tzinfo=None)
        except Exception:
            continue

    return None
