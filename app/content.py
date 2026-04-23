# app/content.py

import time
from typing import Any, Dict, List, Optional, Tuple

from fastapi import HTTPException

from app.sheets import get_ws, read_records_manual
from app.utils import normalize, to_bool, log_event
from app.alerts import alert_system_error, alert_tenant_error


REQUIRED_CONTENT_HEADERS = ["key", "value", "active"]
CONTENT_CACHE_TTL_SECONDS = 90

_CONTENT_CACHE: Dict[str, Tuple[float, Dict[str, str]]] = {}


def _safe_str(v: Any) -> str:
    return str(v or "").strip()


def _cache_key(orders_sh) -> str:
    try:
        sid = getattr(orders_sh, "id", None)
        if sid:
            return str(sid)
    except Exception:
        pass
    return str(id(orders_sh))


def _cache_get(cache_key: str) -> Optional[Dict[str, str]]:
    cached = _CONTENT_CACHE.get(cache_key)
    if not cached:
        return None

    ts, data = cached
    if (time.time() - ts) <= CONTENT_CACHE_TTL_SECONDS:
        return data

    return None


def _cache_set(cache_key: str, data: Dict[str, str]) -> None:
    _CONTENT_CACHE[cache_key] = (time.time(), data)


def _find_content_ws(orders_sh):
    try:
        return get_ws(orders_sh, "Content")
    except Exception as e:
        alert_tenant_error(
            tenant_id="",
            error=f"Content worksheet not found: {e}",
        )
        raise HTTPException(status_code=500, detail="Content worksheet not found")


def load_content_map(orders_sh, force: bool = False) -> Dict[str, str]:
    try:
        cache_key = _cache_key(orders_sh)
        if not force:
            cached = _cache_get(cache_key)
            if cached is not None:
                return cached

        ws = _find_content_ws(orders_sh)
        rows = read_records_manual(ws, required_headers=REQUIRED_CONTENT_HEADERS)

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

    except HTTPException:
        raise
    except Exception as e:
        log_event(
            "content_load_error",
            error_type=type(e).__name__,
            error=str(e),
        )
        alert_system_error(
            error=str(e),
            module="content.load_content_map",
        )
        raise


def get_content_value(content_map: Dict[str, str], key: str, default: str = "") -> str:
    return _safe_str(content_map.get(normalize(key), default))


def build_start_text(orders_sh, content_map: Optional[Dict[str, str]] = None) -> str:
    content = content_map if content_map is not None else load_content_map(orders_sh)

    restaurant_name = get_content_value(content, "restaurant_name", "Bienvenido")
    welcome_text = get_content_value(content, "welcome_text", "")

    if welcome_text:
        return f"Bienvenido a {restaurant_name} 👋\n\n{welcome_text}"

    return f"Bienvenido a {restaurant_name} 👋"


def has_location(content_map: Dict[str, str]) -> bool:
    return bool(
        get_content_value(content_map, "location_text") or
        get_content_value(content_map, "location_link")
    )


def has_faq(content_map: Dict[str, str]) -> bool:
    return bool(get_content_value(content_map, "faq_text"))


def has_survey(content_map: Dict[str, str]) -> bool:
    return bool(get_content_value(content_map, "survey_text"))


def build_location_text(orders_sh) -> str:
    content = load_content_map(orders_sh)

    location_text = get_content_value(content, "location_text", "")
    location_link = get_content_value(content, "location_link", "")

    parts: List[str] = []
    if location_text:
        parts.append(f"📍 {location_text}")
    if location_link:
        parts.append(location_link)

    if not parts:
        return "No tenemos ubicación configurada."

    return "\n\n".join(parts)


def build_faq_text(orders_sh) -> str:
    content = load_content_map(orders_sh)
    faq_text = get_content_value(content, "faq_text", "")
    return faq_text or "No hay FAQ configurado."


def build_survey_text(orders_sh) -> str:
    content = load_content_map(orders_sh)
    survey_text = get_content_value(content, "survey_text", "")
    return survey_text or "Encuesta no configurada."
