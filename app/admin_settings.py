# app/admin_settings.py

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Dict, List, Optional, Set

try:
    from zoneinfo import ZoneInfo
except Exception:
    ZoneInfo = None  # type: ignore

from fastapi import HTTPException

from app.sheets import get_ws, read_records_manual
from app.utils import normalize, to_bool, log_event


ADMIN_SETTINGS_SHEET_NAME = "AdminSettings"
REQUIRED_ADMIN_SETTINGS_HEADERS = ["key", "value", "active", "scope"]


# =========================================================
# Modelos
# =========================================================

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

    today_closed: bool
    has_open_override: bool
    has_close_override: bool
    has_last_order_override: bool

    public_message: str


# =========================================================
# Time helpers
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
    # Monday=0 ... Sunday=6
    codes = ["lun", "mar", "mie", "jue", "vie", "sab", "dom"]
    return codes[dt_local.weekday()]


def _parse_hhmm(v: Any) -> Optional[str]:
    """
    Normaliza horas a HH:MM.
    Acepta:
      - 11:00
      - 9:00
      - 09:00
      - 21:30
      - 11
      - 11.00
      - 11h
    Devuelve None si no es válida.
    """
    s = str(v or "").strip().lower()
    if not s:
        return None

    s = s.replace("h", ":")
    s = s.replace(".", ":")

    if ":" not in s:
        if s.isdigit():
            hour = int(s)
            if 0 <= hour <= 23:
                return f"{hour:02d}:00"
            return None
        return None

    parts = s.split(":")
    if len(parts) < 2:
        return None

    try:
        hh = int(parts[0].strip())
        mm = int(parts[1].strip())
    except Exception:
        return None

    if not (0 <= hh <= 23 and 0 <= mm <= 59):
        return None

    return f"{hh:02d}:{mm:02d}"


def _time_to_minutes(hhmm: str) -> Optional[int]:
    t = _parse_hhmm(hhmm)
    if not t:
        return None
    hh, mm = t.split(":")
    return int(hh) * 60 + int(mm)


def _now_minutes(dt_local: datetime) -> int:
    return int(dt_local.hour) * 60 + int(dt_local.minute)


# =========================================================
# Parse helpers
# =========================================================

def _parse_days_csv(v: Any) -> List[str]:
    """
    Espera algo como: mon,tue,wed,thu,fri,sat,sun
    o variantes en español que normalizamos a lun,mar,mie,jue,vie,sab,dom
    """
    raw = str(v or "").strip()
    if not raw:
        return []

    parts = [normalize(x).replace(" ", "") for x in raw.split(",") if str(x).strip()]
    out: List[str] = []

    mapping = {
        # español
        "lun": "lun", "lunes": "lun",
        "mar": "mar", "martes": "mar",
        "mie": "mie", "miercoles": "mie", "miércoles": "mie",
        "jue": "jue", "jueves": "jue",
        "vie": "vie", "viernes": "vie",
        "sab": "sab", "sabado": "sab", "sábado": "sab",
        "dom": "dom", "domingo": "dom",
        # inglés
        "mon": "lun", "monday": "lun",
        "tue": "mar", "tuesday": "mar",
        "wed": "mie", "wednesday": "mie",
        "thu": "jue", "thursday": "jue",
        "fri": "vie", "friday": "vie",
        "sat": "sab", "saturday": "sab",
        "sun": "dom", "sunday": "dom",
    }

    seen: Set[str] = set()
    for p in parts:
        canon = mapping.get(p)
        if canon and canon not in seen:
            seen.add(canon)
            out.append(canon)

    return out


def _settings_rows_to_map(rows: List[Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    """
    Convierte filas a mapa por key, quedándose con activas.
    Si hay duplicados activos, la última fila gana.
    """
    out: Dict[str, Dict[str, Any]] = {}
    for r in rows:
        key = normalize(r.get("key", "")).replace(" ", "_")
        if not key:
            continue

        active = to_bool(r.get("active"))
        if not active:
            continue

        out[key] = {
            "key": key,
            "value": r.get("value", ""),
            "active": active,
            "scope": normalize(r.get("scope", "")),
            "updated_at": r.get("updated_at", ""),
            "updated_by": r.get("updated_by", ""),
            "notes": r.get("notes", ""),
        }
    return out


# =========================================================
# Worksheet load
# =========================================================

def load_admin_settings(orders_sh) -> Dict[str, Dict[str, Any]]:
    """
    Lee la sheet AdminSettings del spreadsheet del tenant.
    """
    ws = None

    try:
        ws = get_ws(orders_sh, ADMIN_SETTINGS_SHEET_NAME)
    except Exception:
        ws = None

    if ws is None:
        raise HTTPException(
            status_code=500,
            detail=(
                "AdminSettings worksheet not found. "
                "Expected a tab named 'AdminSettings' with headers: key, value, active, scope"
            ),
        )

    rows = read_records_manual(ws, required_headers=REQUIRED_ADMIN_SETTINGS_HEADERS)
    cfg = _settings_rows_to_map(rows)

    try:
        log_event(
            "admin_settings_loaded",
            worksheet_title=getattr(ws, "title", "unknown"),
            active_keys=len(cfg),
            keys=sorted(list(cfg.keys()))[:50],
        )
    except Exception:
        pass

    return cfg


# =========================================================
# Getters simples
# =========================================================

def get_admin_setting_value(settings_map: Dict[str, Dict[str, Any]], key: str, default: str = "") -> str:
    k = normalize(key).replace(" ", "_")
    row = settings_map.get(k)
    if not row:
        return default
    return str(row.get("value") or "").strip()


# =========================================================
# Resolución operativa real
# =========================================================

def resolve_business_status(orders_sh, tenant_tz: str = "America/La_Paz") -> BusinessStatus:
    """
    Regla:
    1) weekly_open_days define si el negocio abre hoy
    2) today_closed=TRUE fuerza cerrado
    3) today_open_time_override / today_close_time_override / today_last_order_time_override
       reemplazan los valores globales de hoy
    4) accepts_orders_now depende de:
       - negocio abierto hoy
       - now >= open_time
       - now <= last_order_time
    """
    settings = load_admin_settings(orders_sh)
    now_local = _now_local(tenant_tz)
    weekday_code = _weekday_code_es(now_local)

    weekly_open_days = _parse_days_csv(get_admin_setting_value(settings, "weekly_open_days", ""))
    weekly_open_time = _parse_hhmm(get_admin_setting_value(settings, "weekly_open_time", "11:00")) or "11:00"
    weekly_close_time = _parse_hhmm(get_admin_setting_value(settings, "weekly_close_time", "23:00")) or "23:00"
    weekly_last_order_time = _parse_hhmm(get_admin_setting_value(settings, "weekly_last_order_time", "21:30")) or "21:30"

    today_closed = to_bool(get_admin_setting_value(settings, "today_closed", "FALSE"))

    today_open_override = _parse_hhmm(get_admin_setting_value(settings, "today_open_time_override", ""))
    today_close_override = _parse_hhmm(get_admin_setting_value(settings, "today_close_time_override", ""))
    today_last_order_override = _parse_hhmm(get_admin_setting_value(settings, "today_last_order_time_override", ""))

    today_closed_message = get_admin_setting_value(
        settings,
        "today_closed_message",
        "Hoy el negocio se encuentra cerrado.",
    )
    today_early_close_message = get_admin_setting_value(
        settings,
        "today_early_close_message",
        "Hoy cerraremos más temprano de lo habitual.",
    )

    is_scheduled_open_today = weekday_code in set(weekly_open_days)
    is_open_today = bool(is_scheduled_open_today and not today_closed)

    effective_open = today_open_override or weekly_open_time
    effective_close = today_close_override or weekly_close_time
    effective_last_order = today_last_order_override or weekly_last_order_time

    open_min = _time_to_minutes(effective_open)
    close_min = _time_to_minutes(effective_close)
    last_order_min = _time_to_minutes(effective_last_order)
    now_min = _now_minutes(now_local)

    accepts_orders_now = False
    if is_open_today and open_min is not None and last_order_min is not None:
        accepts_orders_now = open_min <= now_min <= last_order_min

    has_open_override = bool(today_open_override)
    has_close_override = bool(today_close_override)
    has_last_order_override = bool(today_last_order_override)

    public_message = ""
    if today_closed:
        public_message = today_closed_message
    elif has_close_override or has_last_order_override:
        public_message = today_early_close_message

    return BusinessStatus(
        tenant_tz=tenant_tz,
        now_local_iso=now_local.isoformat(),
        today_weekday_code=weekday_code,

        is_open_today=is_open_today,
        accepts_orders_now=accepts_orders_now,

        open_time=effective_open,
        close_time=effective_close,
        last_order_time=effective_last_order,

        weekly_open_days=weekly_open_days,

        today_closed=today_closed,
        has_open_override=has_open_override,
        has_close_override=has_close_override,
        has_last_order_override=has_last_order_override,

        public_message=public_message,
    )


# =========================================================
# Payloads utilitarios para debug / APIs futuras
# =========================================================

def resolve_business_status_dict(orders_sh, tenant_tz: str = "America/La_Paz") -> Dict[str, Any]:
    s = resolve_business_status(orders_sh=orders_sh, tenant_tz=tenant_tz)
    return {
        "tenant_tz": s.tenant_tz,
        "now_local_iso": s.now_local_iso,
        "today_weekday_code": s.today_weekday_code,

        "is_open_today": s.is_open_today,
        "accepts_orders_now": s.accepts_orders_now,

        "open_time": s.open_time,
        "close_time": s.close_time,
        "last_order_time": s.last_order_time,

        "weekly_open_days": s.weekly_open_days,

        "today_closed": s.today_closed,
        "has_open_override": s.has_open_override,
        "has_close_override": s.has_close_override,
        "has_last_order_override": s.has_last_order_override,

        "public_message": s.public_message,
    }
