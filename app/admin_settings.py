# app/admin_settings.py — V2 SIMPLIFICADO (today_mode)

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Dict, List, Optional, Set

try:
    from zoneinfo import ZoneInfo
except Exception:
    ZoneInfo = None

from fastapi import HTTPException

from app.sheets import get_ws, read_records_manual
from app.utils import normalize, to_bool, log_event


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


# =========================================================
# Helpers
# =========================================================

def _tz(tenant_tz: str):
    if ZoneInfo is None:
        return None
    try:
        return ZoneInfo((tenant_tz or "America/La_Paz").strip())
    except Exception:
        return ZoneInfo("America/La_Paz")


def _now_local(tenant_tz: str) -> datetime:
    tz = _tz(tenant_tz)
    if tz is None:
        return datetime.utcnow()
    return datetime.now(tz)


def _weekday_code_es(dt_local: datetime) -> str:
    return ["lun","mar","mie","jue","vie","sab","dom"][dt_local.weekday()]


def _parse_hhmm(v: Any) -> Optional[str]:
    s = str(v or "").strip()
    if not s:
        return None
    try:
        hh, mm = s.split(":")
        return f"{int(hh):02d}:{int(mm):02d}"
    except:
        return None


def _time_to_minutes(hhmm: str) -> Optional[int]:
    t = _parse_hhmm(hhmm)
    if not t:
        return None
    hh, mm = t.split(":")
    return int(hh)*60 + int(mm)


def _now_minutes(dt_local: datetime) -> int:
    return dt_local.hour*60 + dt_local.minute


def _parse_days_csv(v: Any) -> List[str]:
    raw = str(v or "").strip()
    if not raw:
        return []
    return [normalize(x) for x in raw.split(",") if x.strip()]


def load_admin_settings(orders_sh) -> Dict[str, Any]:
    ws = get_ws(orders_sh, ADMIN_SETTINGS_SHEET_NAME)
    rows = read_records_manual(ws, required_headers=REQUIRED_ADMIN_SETTINGS_HEADERS)

    out = {}
    for r in rows:
        if not to_bool(r.get("active")):
            continue
        k = normalize(r.get("key")).replace(" ","_")
        out[k] = str(r.get("value") or "").strip()

    return out


# =========================================================
# NUEVA RESOLUCIÓN
# =========================================================

def resolve_business_status(orders_sh, tenant_tz="America/La_Paz") -> BusinessStatus:

    settings = load_admin_settings(orders_sh)

    now = _now_local(tenant_tz)
    today_code = _weekday_code_es(now)

    # Base
    weekly_days = _parse_days_csv(settings.get("weekly_open_days",""))
    open_time = settings.get("weekly_open_time","11:00")
    close_time = settings.get("weekly_close_time","23:00")
    last_time = settings.get("weekly_last_order_time","21:30")

    # NUEVO MODELO
    today_mode = settings.get("today_mode","habitual")
    today_date = settings.get("today_date","")

    today_str = now.strftime("%Y-%m-%d")

    # Si no es hoy → ignorar override
    if today_date != today_str:
        today_mode = "habitual"

    # Determinar apertura base
    is_open_today = today_code in set(weekly_days)

    # ================================
    # APLICAR today_mode
    # ================================

    if today_mode == "closed_today":
        is_open_today = False

    elif today_mode == "open_now":
        is_open_today = True

    elif today_mode == "closed_now":
        is_open_today = True

    # ================================
    # LÓGICA DE HORARIO
    # ================================

    open_min = _time_to_minutes(open_time)
    last_min = _time_to_minutes(last_time)
    now_min = _now_minutes(now)

    accepts = False

    if today_mode == "closed_today":
        accepts = False

    elif today_mode == "closed_now":
        accepts = False

    elif today_mode == "open_now":
        accepts = True

    else:
        if is_open_today and open_min is not None and last_min is not None:
            accepts = open_min <= now_min <= last_min

    # ================================
    # MENSAJES
    # ================================

    public_message = ""

    if today_mode == "closed_today":
        public_message = "Hoy el negocio está cerrado."

    elif today_mode == "closed_now":
        public_message = "El negocio está cerrado temporalmente."

    # ================================

    return BusinessStatus(
        tenant_tz=tenant_tz,
        now_local_iso=now.isoformat(),
        today_weekday_code=today_code,

        is_open_today=is_open_today,
        accepts_orders_now=accepts,

        open_time=open_time,
        close_time=close_time,
        last_order_time=last_time,

        weekly_open_days=weekly_days,

        public_message=public_message,
    )
