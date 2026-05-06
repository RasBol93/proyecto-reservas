# app/content.py

import time
from typing import Any, Dict, List, Optional, Tuple

from fastapi import HTTPException

from app.sheets import get_ws, read_records_manual, detect_header_row
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


def invalidate_content_cache(orders_sh) -> None:
    cache_key = _cache_key(orders_sh)
    _CONTENT_CACHE.pop(cache_key, None)
    try:
        from app.config_bundle import invalidate_config_bundle

        invalidate_config_bundle(orders_sh=orders_sh)
    except Exception as e:
        try:
            log_event(
                "content_cache_bundle_invalidation_failed",
                cache_key=cache_key,
                error_type=type(e).__name__,
                error=str(e),
            )
        except Exception:
            pass


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


def _get_content_context(orders_sh) -> Dict[str, Any]:
    ws = _find_content_ws(orders_sh)
    values = ws.get_all_values()
    if not values:
        raise HTTPException(status_code=500, detail="Content worksheet is empty")

    header_row_1based = detect_header_row(values, required_headers=REQUIRED_CONTENT_HEADERS, max_scan=10)
    if header_row_1based < 1 or header_row_1based > len(values):
        raise HTTPException(status_code=500, detail="Invalid Content header row")

    headers_raw = [str(x or "").strip() for x in values[header_row_1based - 1]]
    headers_norm = [normalize(h) for h in headers_raw]

    idx_map: Dict[str, int] = {}
    for i, h in enumerate(headers_norm):
        if h and h not in idx_map:
            idx_map[h] = i

    for req in REQUIRED_CONTENT_HEADERS:
        if normalize(req) not in idx_map:
            raise HTTPException(status_code=500, detail=f"Missing required Content header: {req}")

    return {
        "ws": ws,
        "values": values,
        "header_row_1based": header_row_1based,
        "headers_raw": headers_raw,
        "idx_map": idx_map,
    }


def _col_to_a1(col_1based: int) -> str:
    result = ""
    n = int(col_1based)
    while n > 0:
        n, rem = divmod(n - 1, 26)
        result = chr(65 + rem) + result
    return result


def _cell_a1(row_1based: int, col_1based: int) -> str:
    return f"{_col_to_a1(col_1based)}{int(row_1based)}"


def _range_a1(row_1based: int, start_col_1based: int, end_col_1based: int) -> str:
    return f"{_cell_a1(row_1based, start_col_1based)}:{_cell_a1(row_1based, end_col_1based)}"


def _write_full_row(ws, row_index_1based: int, row_values: List[str]) -> None:
    if not row_values:
        return

    ws.update(
        _range_a1(row_index_1based, 1, len(row_values)),
        [row_values],
        value_input_option="RAW",
    )


def _find_next_empty_row_from_values(
    values: List[List[Any]],
    header_row_1based: int,
    header_len: int,
) -> int:
    start_row = max(2, int(header_row_1based or 1) + 1)

    if header_len <= 0:
        return start_row

    if not values or len(values) < start_row:
        return start_row

    for idx_1based, row in enumerate(values[start_row - 1:], start=start_row):
        slice_row = row[:header_len]
        if not any(str(cell).strip() for cell in slice_row):
            return idx_1based

    return len(values) + 1


def _find_content_row_idx_by_key_with_values(
    values: List[List[Any]],
    header_row_1based: int,
    idx_map: Dict[str, int],
    key: str,
) -> Optional[int]:
    key_idx = idx_map.get("key")
    if key_idx is None:
        raise HTTPException(status_code=500, detail="Missing 'key' column in Content")

    target_key = normalize(key)
    for ridx in range(header_row_1based + 1, len(values) + 1):
        row = values[ridx - 1]
        row_key = row[key_idx] if key_idx < len(row) else ""
        if normalize(row_key) == target_key:
            return ridx

    return None


def upsert_content_entries(orders_sh, entries: List[Dict[str, Any]]) -> Dict[str, str]:
    valid_entries = []
    for entry in entries or []:
        if not isinstance(entry, dict):
            continue
        clean_key = normalize(entry.get("key"))
        if not clean_key:
            continue
        clean_value = _safe_str(entry.get("value"))
        active = bool(entry.get("active"))
        valid_entries.append({
            "key": clean_key,
            "value": clean_value,
            "active": active,
        })

    if not valid_entries:
        raise HTTPException(status_code=400, detail="No content entries to update")

    ctx = _get_content_context(orders_sh)
    ws = ctx["ws"]
    values = ctx["values"]
    header_row_1based = int(ctx["header_row_1based"])
    headers_raw = ctx["headers_raw"]
    idx_map = ctx["idx_map"]

    key_idx = idx_map.get("key")
    value_idx = idx_map.get("value")
    active_idx = idx_map.get("active")
    if key_idx is None or value_idx is None or active_idx is None:
        raise HTTPException(status_code=500, detail="Content headers are incomplete")

    applied: Dict[str, str] = {}

    for entry in valid_entries:
        row_idx = _find_content_row_idx_by_key_with_values(values, header_row_1based, idx_map, entry["key"])
        active_str = "TRUE" if entry["active"] else "FALSE"

        if row_idx is None:
            row_values = [""] * len(headers_raw)
            row_values[key_idx] = entry["key"]
            row_values[value_idx] = entry["value"]
            row_values[active_idx] = active_str
            next_row = _find_next_empty_row_from_values(values, header_row_1based, len(headers_raw))
            _write_full_row(ws, next_row, row_values)
            while len(values) < next_row:
                values.append([])
            values[next_row - 1] = list(row_values)
        else:
            while len(values) < row_idx:
                values.append([])
            current_row = list(values[row_idx - 1]) if row_idx - 1 < len(values) else []
            if len(current_row) < len(headers_raw):
                current_row.extend([""] * (len(headers_raw) - len(current_row)))
            current_row[key_idx] = entry["key"]
            current_row[value_idx] = entry["value"]
            current_row[active_idx] = active_str
            _write_full_row(ws, row_idx, current_row)
            values[row_idx - 1] = current_row

        applied[entry["key"]] = entry["value"]

    invalidate_content_cache(orders_sh)

    try:
        log_event(
            "content_entries_upserted",
            keys=sorted(list(applied.keys())),
            updated_count=len(applied),
        )
    except Exception:
        pass

    return applied


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
