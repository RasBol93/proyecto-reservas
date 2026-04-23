# app/survey_settings.py

import time
from typing import Dict, Optional, Tuple

from app.sheets import read_records_manual
from app.utils import normalize, to_bool, log_event
from app.alerts import alert_system_error

from app.survey_core import (
    SURVEY_SETTINGS_WS,
    SURVEY_SETTINGS_HEADERS,
    _ensure_ws,
    _safe_str,
)


SURVEY_SETTINGS_CACHE_TTL_SECONDS = 90
_SURVEY_SETTINGS_CACHE: Dict[str, Tuple[float, Dict[str, str]]] = {}


def _cache_key(orders_sh) -> str:
    try:
        sid = getattr(orders_sh, "id", None)
        if sid:
            return str(sid)
    except Exception:
        pass
    return str(id(orders_sh))


def _cache_get(cache_key: str) -> Optional[Dict[str, str]]:
    cached = _SURVEY_SETTINGS_CACHE.get(cache_key)
    if not cached:
        return None

    ts, data = cached
    if (time.time() - ts) <= SURVEY_SETTINGS_CACHE_TTL_SECONDS:
        return data

    return None


def _cache_set(cache_key: str, data: Dict[str, str]) -> None:
    _SURVEY_SETTINGS_CACHE[cache_key] = (time.time(), data)


def invalidate_survey_settings_cache(orders_sh) -> None:
    _SURVEY_SETTINGS_CACHE.pop(_cache_key(orders_sh), None)


def load_survey_settings(orders_sh, force: bool = False) -> Dict[str, str]:
    try:
        cache_key = _cache_key(orders_sh)
        if force:
            invalidate_survey_settings_cache(orders_sh)
        else:
            cached = _cache_get(cache_key)
            if cached is not None:
                return cached

        ws = _ensure_ws(orders_sh, SURVEY_SETTINGS_WS, SURVEY_SETTINGS_HEADERS)
        rows = read_records_manual(ws, required_headers=SURVEY_SETTINGS_HEADERS)

        out: Dict[str, str] = {}
        for r in rows:
            key = normalize(r.get("key", ""))
            value = _safe_str(r.get("value"))
            active = to_bool(r.get("active", ""))

            if not key:
                continue
            if not active:
                continue

            out[key] = value
        _cache_set(cache_key, out)

        return out

    except Exception as e:
        log_event(
            "survey_load_settings_error",
            error_type=type(e).__name__,
            error=str(e),
        )
        alert_system_error(error=str(e), module="survey.load_survey_settings")
        raise


def survey_is_enabled(orders_sh) -> bool:
    try:
        settings = load_survey_settings(orders_sh)
        raw = _safe_str(settings.get("survey_enabled", ""))
        if not raw:
            return False
        return to_bool(raw)
    except Exception:
        return False


def get_survey_reward_text(orders_sh) -> str:
    try:
        settings = load_survey_settings(orders_sh)
        return _safe_str(settings.get("survey_reward", ""))
    except Exception:
        return ""


def get_survey_password(orders_sh) -> str:
    try:
        settings = load_survey_settings(orders_sh)
        return _safe_str(settings.get("survey_password", ""))
    except Exception:
        return ""


def validate_survey_password(orders_sh, plain_text: str) -> bool:
    try:
        expected = get_survey_password(orders_sh)
        if not expected:
            return False
        return _safe_str(plain_text) == expected
    except Exception:
        return False


def set_survey_setting(orders_sh, key: str, value: str, active: bool = True) -> bool:
    """
    Estrategia simple:
    - append-only
    - la lectura toma el último activo por key, porque out[key] = value pisa el anterior
    """
    try:
        ws = _ensure_ws(orders_sh, SURVEY_SETTINGS_WS, SURVEY_SETTINGS_HEADERS)
        ws.append_row(
            [
                _safe_str(key),
                _safe_str(value),
                "TRUE" if active else "FALSE",
            ],
            value_input_option="USER_ENTERED",
        )
        invalidate_survey_settings_cache(orders_sh)
        return True
    except Exception as e:
        log_event(
            "survey_set_setting_error",
            key=_safe_str(key),
            error_type=type(e).__name__,
            error=str(e),
        )
        from app.alerts import alert_sheet_error

        alert_sheet_error(
            tenant_id="",
            error=f"survey set setting failed: {e}",
            extra_key="survey.set_survey_setting",
        )
        return False


def save_survey_enabled(orders_sh, enabled: bool) -> bool:
    return set_survey_setting(orders_sh, "survey_enabled", "TRUE" if enabled else "FALSE", active=True)


def save_survey_reward(orders_sh, reward_text: str) -> bool:
    return set_survey_setting(orders_sh, "survey_reward", reward_text, active=True)


def save_survey_password(orders_sh, password: str) -> bool:
    return set_survey_setting(orders_sh, "survey_password", password, active=True)
