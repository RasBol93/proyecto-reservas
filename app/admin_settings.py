# app/admin_settings.py

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Dict, List, Optional, Set, Tuple

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
    today_open_force: bool

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


def _days_to_csv(days: List[str]) -> str:
    order = ["lun", "mar", "mie", "jue", "vie", "sab", "dom"]
    days_norm = []
    seen: Set[str] = set()

    for d in days or []:
        dn = normalize(d).replace(" ", "")
        if dn in order and dn not in seen:
            seen.add(dn)
            days_norm.append(dn)

    days_sorted = [d for d in order if d in set(days_norm)]
    return ",".join(days_sorted)


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


def get_admin_settings_ws(orders_sh):
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
    return ws


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
# Helpers de escritura
# =========================================================

def _get_header(ws) -> List[str]:
    values = ws.get_all_values()
    if not values:
        return []
    return [str(x or "").strip() for x in values[0]]


def _find_col_idx(header: List[str], col_name: str) -> Optional[int]:
    target = normalize(col_name)
    for i, h in enumerate(header):
        if normalize(h) == target:
            return i
    return None


def _find_row_idx_by_key(ws, key: str) -> Optional[int]:
    values = ws.get_all_values()
    if not values or len(values) < 2:
        return None

    header = [str(x or "").strip() for x in values[0]]
    key_col = _find_col_idx(header, "key")
    if key_col is None:
        raise RuntimeError("Missing 'key' column in AdminSettings header")

    target = normalize(key).replace(" ", "_")

    for i in range(1, len(values)):
        row = values[i]
        current = row[key_col] if key_col < len(row) else ""
        if normalize(current).replace(" ", "_") == target:
            return i + 1  # 1-based sheet row index

    return None


def _update_admin_setting_cells(
    ws,
    row_idx: int,
    value: Optional[str] = None,
    active: Optional[bool] = None,
    scope: Optional[str] = None,
    updated_by: Optional[str] = None,
) -> None:
    header = _get_header(ws)
    if not header:
        raise RuntimeError("AdminSettings header row missing")

    value_col = _find_col_idx(header, "value")
    active_col = _find_col_idx(header, "active")
    scope_col = _find_col_idx(header, "scope")
    updated_at_col = _find_col_idx(header, "updated_at")
    updated_by_col = _find_col_idx(header, "updated_by")

    if value is not None and value_col is not None:
        ws.update_cell(row_idx, value_col + 1, str(value))

    if active is not None and active_col is not None:
        ws.update_cell(row_idx, active_col + 1, "TRUE" if active else "FALSE")

    if scope is not None and scope_col is not None:
        ws.update_cell(row_idx, scope_col + 1, str(scope))

    if updated_at_col is not None:
        ws.update_cell(row_idx, updated_at_col + 1, datetime.utcnow().isoformat())

    if updated_by is not None and updated_by_col is not None:
        ws.update_cell(row_idx, updated_by_col + 1, str(updated_by))


def set_admin_setting_value(
    orders_sh,
    key: str,
    value: str,
    updated_by: str = "admin_bot",
) -> Dict[str, Any]:
    ws = get_admin_settings_ws(orders_sh)
    row_idx = _find_row_idx_by_key(ws, key)
    if row_idx is None:
        raise HTTPException(status_code=500, detail=f"AdminSettings key not found: {key}")

    _update_admin_setting_cells(
        ws=ws,
        row_idx=row_idx,
        value=value,
        updated_by=updated_by,
    )

    try:
        log_event("admin_setting_updated", key=key, value=value, updated_by=updated_by)
    except Exception:
        pass

    return {"ok": True, "key": normalize(key).replace(" ", "_"), "value": value}


def set_admin_setting_bool(
    orders_sh,
    key: str,
    value: bool,
    updated_by: str = "admin_bot",
) -> Dict[str, Any]:
    return set_admin_setting_value(
        orders_sh=orders_sh,
        key=key,
        value="TRUE" if value else "FALSE",
        updated_by=updated_by,
    )


def set_admin_setting_time(
    orders_sh,
    key: str,
    hhmm: str,
    updated_by: str = "admin_bot",
) -> Dict[str, Any]:
    hhmm_norm = _parse_hhmm(hhmm)
    if not hhmm_norm:
        raise HTTPException(status_code=400, detail=f"Invalid time for {key}: {hhmm}")

    return set_admin_setting_value(
        orders_sh=orders_sh,
        key=key,
        value=hhmm_norm,
        updated_by=updated_by,
    )


def set_admin_setting_days(
    orders_sh,
    key: str,
    days: List[str],
    updated_by: str = "admin_bot",
) -> Dict[str, Any]:
    value = _days_to_csv(days)
    return set_admin_setting_value(
        orders_sh=orders_sh,
        key=key,
        value=value,
        updated_by=updated_by,
    )


# =========================================================
# Acciones de negocio (listas para bot)
# =========================================================

def action_set_today_closed(
    orders_sh,
    is_closed: bool,
    updated_by: str = "admin_bot",
) -> Dict[str, Any]:
    return set_admin_setting_bool(
        orders_sh=orders_sh,
        key="today_closed",
        value=is_closed,
        updated_by=updated_by,
    )


def action_set_today_open_force(
    orders_sh,
    enabled: bool,
    updated_by: str = "admin_bot",
) -> Dict[str, Any]:
    return set_admin_setting_bool(
        orders_sh=orders_sh,
        key="today_open_force",
        value=enabled,
        updated_by=updated_by,
    )


def action_set_weekly_open_days(
    orders_sh,
    days: List[str],
    updated_by: str = "admin_bot",
) -> Dict[str, Any]:
    return set_admin_setting_days(
        orders_sh=orders_sh,
        key="weekly_open_days",
        days=days,
        updated_by=updated_by,
    )


def action_set_weekly_normal_hours(
    orders_sh,
    open_time: str,
    close_time: str,
    last_order_time: str,
    updated_by: str = "admin_bot",
) -> Dict[str, Any]:
    open_norm = _parse_hhmm(open_time)
    close_norm = _parse_hhmm(close_time)
    last_norm = _parse_hhmm(last_order_time)

    if not open_norm or not close_norm or not last_norm:
        raise HTTPException(status_code=400, detail="Invalid weekly normal hours")

    open_min = _time_to_minutes(open_norm)
    close_min = _time_to_minutes(close_norm)
    last_min = _time_to_minutes(last_norm)

    if open_min is None or close_min is None or last_min is None:
        raise HTTPException(status_code=400, detail="Invalid weekly normal hours")

    if not (open_min < last_min <= close_min):
        raise HTTPException(
            status_code=400,
            detail="Expected open_time < last_order_time <= close_time",
        )

    set_admin_setting_time(orders_sh, "weekly_open_time", open_norm, updated_by=updated_by)
    set_admin_setting_time(orders_sh, "weekly_close_time", close_norm, updated_by=updated_by)
    set_admin_setting_time(orders_sh, "weekly_last_order_time", last_norm, updated_by=updated_by)

    return {
        "ok": True,
        "weekly_open_time": open_norm,
        "weekly_close_time": close_norm,
        "weekly_last_order_time": last_norm,
    }


def action_set_today_open_late(
    orders_sh,
    open_time: str,
    updated_by: str = "admin_bot",
) -> Dict[str, Any]:
    open_norm = _parse_hhmm(open_time)
    if not open_norm:
        raise HTTPException(status_code=400, detail="Invalid today_open_time_override")

    set_admin_setting_time(orders_sh, "today_open_time_override", open_norm, updated_by=updated_by)
    set_admin_setting_bool(orders_sh, "today_closed", False, updated_by=updated_by)

    return {"ok": True, "today_open_time_override": open_norm}


def action_set_today_close_early(
    orders_sh,
    close_time: str,
    last_order_time: str,
    updated_by: str = "admin_bot",
) -> Dict[str, Any]:
    close_norm = _parse_hhmm(close_time)
    last_norm = _parse_hhmm(last_order_time)

    if not close_norm or not last_norm:
        raise HTTPException(status_code=400, detail="Invalid today early close settings")

    close_min = _time_to_minutes(close_norm)
    last_min = _time_to_minutes(last_norm)

    if close_min is None or last_min is None:
        raise HTTPException(status_code=400, detail="Invalid today early close settings")

    if not (last_min <= close_min):
        raise HTTPException(
            status_code=400,
            detail="Expected today_last_order_time_override <= today_close_time_override",
        )

    set_admin_setting_time(orders_sh, "today_close_time_override", close_norm, updated_by=updated_by)
    set_admin_setting_time(orders_sh, "today_last_order_time_override", last_norm, updated_by=updated_by)
    set_admin_setting_bool(orders_sh, "today_closed", False, updated_by=updated_by)

    return {
        "ok": True,
        "today_close_time_override": close_norm,
        "today_last_order_time_override": last_norm,
    }


def action_clear_today_open_override(
    orders_sh,
    updated_by: str = "admin_bot",
) -> Dict[str, Any]:
    return set_admin_setting_value(
        orders_sh=orders_sh,
        key="today_open_time_override",
        value="",
        updated_by=updated_by,
    )


def action_clear_today_close_override(
    orders_sh,
    updated_by: str = "admin_bot",
) -> Dict[str, Any]:
    set_admin_setting_value(
        orders_sh=orders_sh,
        key="today_close_time_override",
        value="",
        updated_by=updated_by,
    )
    set_admin_setting_value(
        orders_sh=orders_sh,
        key="today_last_order_time_override",
        value="",
        updated_by=updated_by,
    )
    return {"ok": True}


def action_restore_today_normal(
    orders_sh,
    updated_by: str = "admin_bot",
) -> Dict[str, Any]:
    set_admin_setting_bool(orders_sh, "today_closed", False, updated_by=updated_by)
    set_admin_setting_bool(orders_sh, "today_open_force", False, updated_by=updated_by)
    set_admin_setting_value(orders_sh, "today_open_time_override", "", updated_by=updated_by)
    set_admin_setting_value(orders_sh, "today_close_time_override", "", updated_by=updated_by)
    set_admin_setting_value(orders_sh, "today_last_order_time_override", "", updated_by=updated_by)

    return {"ok": True}


# =========================================================
# Resolución operativa real
# =========================================================

def resolve_business_status(orders_sh, tenant_tz: str = "America/La_Paz") -> BusinessStatus:
    """
    Regla:
    1) weekly_open_days define si el negocio abre hoy
    2) today_open_force=TRUE permite abrir hoy aunque no esté en weekly_open_days
    3) today_closed=TRUE fuerza cerrado
    4) today_open_time_override / today_close_time_override / today_last_order_time_override
       reemplazan los valores globales de hoy
    5) accepts_orders_now depende de:
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
    today_open_force = to_bool(get_admin_setting_value(settings, "today_open_force", "FALSE"))

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
    is_open_today = bool((is_scheduled_open_today or today_open_force) and not today_closed)

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
        today_open_force=today_open_force,

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
        "today_open_force": s.today_open_force,

        "has_open_override": s.has_open_override,
        "has_close_override": s.has_close_override,
        "has_last_order_override": s.has_last_order_override,

        "public_message": s.public_message,
    }
