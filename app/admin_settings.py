# app/admin_settings.py — modelo B con today_mode y helpers compatibles

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
import time
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
ADMIN_SETTINGS_CACHE_TTL_SECONDS = 90

_ADMIN_SETTINGS_CACHE: Dict[str, Tuple[float, Dict[str, Dict[str, Any]]]] = {}


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


def _cache_key(orders_sh) -> str:
    try:
        sid = getattr(orders_sh, "id", None)
        if sid:
            return str(sid)
    except Exception:
        pass
    return str(id(orders_sh))


def _cache_get(cache_key: str) -> Optional[Dict[str, Dict[str, Any]]]:
    cached = _ADMIN_SETTINGS_CACHE.get(cache_key)
    if not cached:
        return None

    ts, data = cached
    if (time.time() - ts) <= ADMIN_SETTINGS_CACHE_TTL_SECONDS:
        return data

    return None


def _cache_set(cache_key: str, data: Dict[str, Dict[str, Any]]) -> None:
    _ADMIN_SETTINGS_CACHE[cache_key] = (time.time(), data)


def invalidate_admin_settings_cache(orders_sh) -> None:
    _ADMIN_SETTINGS_CACHE.pop(_cache_key(orders_sh), None)


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
    codes = ["lun", "mar", "mie", "jue", "vie", "sab", "dom"]
    return codes[dt_local.weekday()]


def _parse_hhmm(v: Any) -> Optional[str]:
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


def _minutes_to_hhmm(m: int) -> str:
    hh = max(0, min(23, m // 60))
    mm = max(0, min(59, m % 60))
    return f"{hh:02d}:{mm:02d}"


def _now_minutes(dt_local: datetime) -> int:
    return int(dt_local.hour) * 60 + int(dt_local.minute)


def _parse_days_csv(v: Any) -> List[str]:
    raw = str(v or "").strip()
    if not raw:
        return []

    parts = [normalize(x).replace(" ", "") for x in raw.split(",") if str(x).strip()]
    out: List[str] = []

    mapping = {
        "lun": "lun", "lunes": "lun", "mon": "lun", "monday": "lun",
        "mar": "mar", "martes": "mar", "tue": "mar", "tuesday": "mar",
        "mie": "mie", "miercoles": "mie", "miércoles": "mie", "wed": "mie", "wednesday": "mie",
        "jue": "jue", "jueves": "jue", "thu": "jue", "thursday": "jue",
        "vie": "vie", "viernes": "vie", "fri": "vie", "friday": "vie",
        "sab": "sab", "sabado": "sab", "sábado": "sab", "sat": "sab", "saturday": "sab",
        "dom": "dom", "domingo": "dom", "sun": "dom", "sunday": "dom",
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


def load_admin_settings(orders_sh, force: bool = False) -> Dict[str, Dict[str, Any]]:
    cache_key = _cache_key(orders_sh)
    if force:
        invalidate_admin_settings_cache(orders_sh)
    else:
        cached = _cache_get(cache_key)
        if cached is not None:
            return cached

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
    _cache_set(cache_key, cfg)

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


def get_admin_setting_value(settings_map: Dict[str, Dict[str, Any]], key: str, default: str = "") -> str:
    k = normalize(key).replace(" ", "_")
    row = settings_map.get(k)
    if not row:
        return default
    return str(row.get("value") or "").strip()


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
            return i + 1

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


def _append_admin_setting_row(
    ws,
    key: str,
    value: str,
    *,
    active: bool = True,
    scope: str = "global",
    updated_by: str = "admin_bot",
) -> None:
    header = _get_header(ws)
    if not header:
        raise RuntimeError("AdminSettings header row missing")

    row = [""] * len(header)

    key_col = _find_col_idx(header, "key")
    value_col = _find_col_idx(header, "value")
    active_col = _find_col_idx(header, "active")
    scope_col = _find_col_idx(header, "scope")
    updated_at_col = _find_col_idx(header, "updated_at")
    updated_by_col = _find_col_idx(header, "updated_by")

    if key_col is not None:
        row[key_col] = normalize(key).replace(" ", "_")
    if value_col is not None:
        row[value_col] = str(value)
    if active_col is not None:
        row[active_col] = "TRUE" if active else "FALSE"
    if scope_col is not None:
        row[scope_col] = str(scope)
    if updated_at_col is not None:
        row[updated_at_col] = datetime.utcnow().isoformat()
    if updated_by_col is not None:
        row[updated_by_col] = str(updated_by)

    ws.append_row(row, value_input_option="USER_ENTERED")


def set_admin_setting_value(
    orders_sh,
    key: str,
    value: str,
    updated_by: str = "admin_bot",
) -> Dict[str, Any]:
    ws = get_admin_settings_ws(orders_sh)
    row_idx = _find_row_idx_by_key(ws, key)

    if row_idx is None:
        _append_admin_setting_row(
            ws=ws,
            key=key,
            value=value,
            active=True,
            scope="global",
            updated_by=updated_by,
        )
        try:
            log_event("admin_setting_created", key=key, value=value, updated_by=updated_by)
        except Exception:
            pass
        invalidate_admin_settings_cache(orders_sh)
        return {"ok": True, "key": normalize(key).replace(" ", "_"), "value": value, "created": True}

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

    invalidate_admin_settings_cache(orders_sh)

    return {"ok": True, "key": normalize(key).replace(" ", "_"), "value": value, "created": False}


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


def set_today_mode(
    orders_sh,
    mode: str,
    tenant_tz: str,
    updated_by: str = "admin_bot",
) -> Dict[str, Any]:
    mode = str(mode or "").strip().lower()
    if mode not in {"habitual", "open_now", "closed_now", "closed_today"}:
        raise HTTPException(status_code=400, detail=f"Invalid today_mode: {mode}")

    today_str = _now_local(tenant_tz).strftime("%Y-%m-%d")
    set_admin_setting_value(orders_sh, "today_mode", mode, updated_by=updated_by)
    set_admin_setting_value(orders_sh, "today_date", today_str, updated_by=updated_by)
    return {"ok": True, "today_mode": mode, "today_date": today_str}


def resolve_business_status(orders_sh, tenant_tz: str = "America/La_Paz") -> BusinessStatus:
    settings = load_admin_settings(orders_sh)
    now_local = _now_local(tenant_tz)
    weekday_code = _weekday_code_es(now_local)
    now_min = _now_minutes(now_local)

    weekly_open_days = _parse_days_csv(get_admin_setting_value(settings, "weekly_open_days", ""))

    weekly_slot_mode_raw = get_admin_setting_value(settings, "weekly_slot_mode", "1")
    weekly_slot_mode = "2" if str(weekly_slot_mode_raw).strip() == "2" else "1"

    slot1_open = _parse_hhmm(get_admin_setting_value(settings, "weekly_slot1_open", "11:00")) or "11:00"
    slot1_close = _parse_hhmm(get_admin_setting_value(settings, "weekly_slot1_close", "23:00")) or "23:00"
    slot2_open = _parse_hhmm(get_admin_setting_value(settings, "weekly_slot2_open", "")) or ""
    slot2_close = _parse_hhmm(get_admin_setting_value(settings, "weekly_slot2_close", "")) or ""

    today_mode = get_admin_setting_value(settings, "today_mode", "habitual").strip().lower() or "habitual"
    today_date = get_admin_setting_value(settings, "today_date", "")

    today_closed_message = get_admin_setting_value(
        settings,
        "today_closed_message",
        "Hoy el negocio se encuentra cerrado.",
    )
    today_temporal_close_message = get_admin_setting_value(
        settings,
        "today_temporal_close_message",
        "El negocio se encuentra cerrado temporalmente.",
    )

    today_str = now_local.strftime("%Y-%m-%d")
    if today_date != today_str:
        today_mode = "habitual"

    scheduled_slots: List[Tuple[str, str]] = []
    if weekday_code in set(weekly_open_days):
        s1o = _time_to_minutes(slot1_open)
        s1c = _time_to_minutes(slot1_close)
        if s1o is not None and s1c is not None and s1o < s1c:
            scheduled_slots.append((slot1_open, slot1_close))

        if weekly_slot_mode == "2" and slot2_open and slot2_close:
            s2o = _time_to_minutes(slot2_open)
            s2c = _time_to_minutes(slot2_close)
            if s2o is not None and s2c is not None and s2o < s2c:
                scheduled_slots.append((slot2_open, slot2_close))

    today_slots: List[Tuple[str, str]] = list(scheduled_slots)
    public_message = ""

    if today_mode == "closed_today":
        today_slots = []
        public_message = today_closed_message

    elif today_mode == "closed_now":
        today_slots = []
        days_txt = ", ".join([d.capitalize() for d in weekly_open_days])
        habitual_slots_txt = "\n".join([f"{s[0]}–{s[1]}" for s in scheduled_slots]) if scheduled_slots else ""
        public_message = (
            f"Nuestros días y horarios habituales son:\n\n"
            f"{days_txt}\n{habitual_slots_txt}\n\n"
            f"Pero en este momento nos encontramos excepcionalmente cerrados."
        )

    elif today_mode == "open_now":
        if scheduled_slots:
            final_close = scheduled_slots[-1][1]
            final_close_min = _time_to_minutes(final_close)
            now_hhmm = _minutes_to_hhmm(now_min)

            if final_close_min is not None and now_min < final_close_min:
                today_slots = [(now_hhmm, final_close)]
            else:
                today_slots = [(now_hhmm, "23:59")]
        else:
            today_slots = [(_minutes_to_hhmm(now_min), "23:59")]

    is_open_today = bool(today_slots)

    current_slot: Optional[Tuple[str, str]] = None
    for start_hhmm, end_hhmm in today_slots:
        start_min = _time_to_minutes(start_hhmm)
        end_min = _time_to_minutes(end_hhmm)
        if start_min is None or end_min is None:
            continue
        if start_min <= now_min <= end_min:
            current_slot = (start_hhmm, end_hhmm)
            break

    accepts_orders_now = current_slot is not None

    open_time = current_slot[0] if current_slot else (today_slots[0][0] if today_slots else "")
    close_time = current_slot[1] if current_slot else (today_slots[-1][1] if today_slots else "")
    last_order_time = current_slot[1] if current_slot else (today_slots[-1][1] if today_slots else "")

    return BusinessStatus(
        tenant_tz=tenant_tz,
        now_local_iso=now_local.isoformat(),
        today_weekday_code=weekday_code,

        is_open_today=is_open_today,
        accepts_orders_now=accepts_orders_now,

        open_time=open_time,
        close_time=close_time,
        last_order_time=last_order_time,

        weekly_open_days=weekly_open_days,

        public_message=public_message,

        today_mode=today_mode,
        today_date=today_date,
        today_slots=today_slots,
        has_two_slots=(weekly_slot_mode == "2"),
    )


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
        "public_message": s.public_message,

        "today_mode": s.today_mode,
        "today_date": s.today_date,
        "today_slots": s.today_slots,
        "has_two_slots": s.has_two_slots,
    }
