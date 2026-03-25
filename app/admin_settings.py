# app/admin_settings.py — modelo B con mensaje mejorado de cierre temporal

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, List, Optional, Set, Tuple

try:
    from zoneinfo import ZoneInfo
except Exception:
    ZoneInfo = None

from fastapi import HTTPException

from app.sheets import get_ws, read_records_manual
from app.utils import normalize, to_bool


ADMIN_SETTINGS_SHEET_NAME = "AdminSettings"
REQUIRED_ADMIN_SETTINGS_HEADERS = ["key", "value", "active", "scope"]


@dataclass
class BusinessStatus:
    tenant_tz: str
    now_local_iso: str
    today_weekday_code: str

    is_open_today: bool
    accepts_orders_now: bool

    open_time: str
    close_time: str
    last_order_time: str

    weekly_open_days: List[str]

    public_message: str

    today_mode: str = "habitual"
    today_date: str = ""
    today_slots: List[Tuple[str, str]] = field(default_factory=list)
    has_two_slots: bool = False


# =========================
# Helpers
# =========================

def _tz(tenant_tz: str):
    if ZoneInfo is None:
        return None
    try:
        return ZoneInfo(tenant_tz or "America/La_Paz")
    except Exception:
        return ZoneInfo("America/La_Paz")


def _now_local(tenant_tz: str) -> datetime:
    tz = _tz(tenant_tz)
    if tz is None:
        return datetime.utcnow()
    return datetime.now(tz)


def _weekday_code_es(dt: datetime) -> str:
    return ["lun","mar","mie","jue","vie","sab","dom"][dt.weekday()]


def _parse_hhmm(v: Any) -> Optional[str]:
    s = str(v or "").strip()
    if not s:
        return None
    try:
        hh, mm = s.split(":")
        hh = int(hh)
        mm = int(mm)
        if 0 <= hh <= 23 and 0 <= mm <= 59:
            return f"{hh:02d}:{mm:02d}"
    except:
        return None
    return None


def _time_to_minutes(hhmm: str) -> Optional[int]:
    t = _parse_hhmm(hhmm)
    if not t:
        return None
    hh, mm = t.split(":")
    return int(hh)*60 + int(mm)


def _minutes_to_hhmm(m: int) -> str:
    return f"{m//60:02d}:{m%60:02d}"


def _now_minutes(dt: datetime) -> int:
    return dt.hour*60 + dt.minute


def _parse_days(v: str) -> List[str]:
    if not v:
        return []
    return [normalize(x) for x in v.split(",") if x.strip()]


def load_admin_settings(orders_sh) -> Dict[str, str]:
    ws = get_ws(orders_sh, ADMIN_SETTINGS_SHEET_NAME)
    rows = read_records_manual(ws, REQUIRED_ADMIN_SETTINGS_HEADERS)

    out = {}
    for r in rows:
        if not to_bool(r.get("active")):
            continue
        key = normalize(r.get("key")).replace(" ", "_")
        out[key] = str(r.get("value") or "").strip()
    return out


# =========================
# NUEVO MOTOR
# =========================

def resolve_business_status(orders_sh, tenant_tz="America/La_Paz") -> BusinessStatus:

    settings = load_admin_settings(orders_sh)
    now = _now_local(tenant_tz)
    today_code = _weekday_code_es(now)
    now_min = _now_minutes(now)

    weekly_days = _parse_days(settings.get("weekly_open_days",""))

    slot_mode = settings.get("weekly_slot_mode","1")

    s1o = settings.get("weekly_slot1_open","")
    s1c = settings.get("weekly_slot1_close","")
    s2o = settings.get("weekly_slot2_open","")
    s2c = settings.get("weekly_slot2_close","")

    today_mode = settings.get("today_mode","habitual")
    today_date = settings.get("today_date","")

    today_str = now.strftime("%Y-%m-%d")
    if today_date != today_str:
        today_mode = "habitual"

    # =========================
    # Slots habituales
    # =========================

    slots = []

    if today_code in weekly_days:
        if _time_to_minutes(s1o) and _time_to_minutes(s1c):
            slots.append((s1o, s1c))

        if slot_mode == "2":
            if _time_to_minutes(s2o) and _time_to_minutes(s2c):
                slots.append((s2o, s2c))

    # =========================
    # Overrides
    # =========================

    if today_mode == "closed_today":
        slots = []

    elif today_mode == "closed_now":
        slots = []

    elif today_mode == "open_now":
        if slots:
            close = slots[-1][1]
        else:
            close = "23:59"

        slots = [(_minutes_to_hhmm(now_min), close)]

    # =========================
    # Estado actual
    # =========================

    current_slot = None
    for s in slots:
        o = _time_to_minutes(s[0])
        c = _time_to_minutes(s[1])
        if o is None or c is None:
            continue
        if o <= now_min <= c:
            current_slot = s
            break

    accepts = current_slot is not None

    open_time = current_slot[0] if current_slot else ""
    close_time = current_slot[1] if current_slot else ""
    last_time = close_time

    # =========================
    # MENSAJES NUEVOS
    # =========================

    public_message = ""

    # Formateo días
    days_txt = ", ".join([d.capitalize() for d in weekly_days])

    # Formateo horarios
    slots_txt = "\n".join([f"{s[0]}–{s[1]}" for s in slots]) if slots else ""

    if today_mode == "closed_today":
        public_message = "Hoy el negocio se encuentra cerrado."

    elif today_mode == "closed_now":
        public_message = (
            f"Nuestros días y horarios habituales son:\n\n"
            f"{days_txt}\n{slots_txt}\n\n"
            f"Pero en este momento nos encontramos excepcionalmente cerrados."
        )

    # =========================

    return BusinessStatus(
        tenant_tz=tenant_tz,
        now_local_iso=now.isoformat(),
        today_weekday_code=today_code,

        is_open_today=bool(slots),
        accepts_orders_now=accepts,

        open_time=open_time,
        close_time=close_time,
        last_order_time=last_time,

        weekly_open_days=weekly_days,
        public_message=public_message,

        today_mode=today_mode,
        today_date=today_date,
        today_slots=slots,
        has_two_slots=(slot_mode == "2"),
    )
