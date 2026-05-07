from typing import Any, Dict, List, Optional, Tuple
import json
from pathlib import Path
import re
import time

from fastapi import HTTPException

from app.content import load_content_map
from app.sheets import get_ws, read_records_manual, detect_header_row, note_sheets_serving_source
from app.utils import to_bool, normalize, log_event
from app.alerts import alert_system_error, alert_tenant_error
from app.promotions import get_active_promotions


REQUIRED_MENU_HEADERS = ["sku", "name", "price", "active", "category"]

# Cache simple por spreadsheet
_MENU_CACHE: Dict[str, Tuple[float, Dict[str, Dict[str, Any]]]] = {}
_MENU_ADMIN_CACHE: Dict[str, Tuple[float, Dict[str, Dict[str, Any]]]] = {}
_MENU_READ_COOLDOWN_UNTIL: Dict[str, float] = {}
_MENU_TRANSIENT_ALERT_LAST_AT: Dict[str, float] = {}
_MENU_LAST_SERVE_SOURCE: Dict[str, str] = {}

MENU_CACHE_TTL_SECONDS = 900
MENU_CACHE_STALE_WINDOW_SECONDS = 900
MENU_SNAPSHOT_MAX_AGE_SECONDS = 86400
MENU_READ_FAILURE_COOLDOWN_SECONDS = 60
MENU_TRANSIENT_ALERT_COOLDOWN_SECONDS = 180
MENU_SNAPSHOT_VERSION = 1
MENU_SNAPSHOT_DIRNAME = ".menu_snapshots"

# Retry simple y corto para operaciones de red/Sheets
_MENU_RETRY_ATTEMPTS = 3
_MENU_RETRY_SLEEP_SECONDS = 0.30

PROMOTIONS_CATEGORY_NAME = "🎁 Promociones"


# -------------------------
# Retry helpers
# -------------------------

def _should_retry_exception(exc: Exception) -> bool:
    msg = str(exc or "").lower()
    retry_signals = (
        "timed out",
        "timeout",
        "temporarily unavailable",
        "connection reset",
        "connection aborted",
        "connection refused",
        "remote end closed connection",
        "service unavailable",
        "internal error",
        "bad gateway",
        "gateway timeout",
        "rate limit",
        "quota",
        "429",
        "500",
        "502",
        "503",
        "504",
    )
    return any(signal in msg for signal in retry_signals)


def _sleep_before_retry(attempt_index: int) -> None:
    try:
        time.sleep(_MENU_RETRY_SLEEP_SECONDS * max(1, attempt_index))
    except Exception:
        pass


def _call_with_retry(fn, *, op_name: str, log_fields: Optional[Dict[str, Any]] = None):
    last_exc: Exception | None = None
    extra = dict(log_fields or {})

    for attempt in range(1, _MENU_RETRY_ATTEMPTS + 1):
        try:
            return fn()
        except Exception as e:
            last_exc = e

            try:
                log_event(
                    "menu_retryable_error",
                    op_name=op_name,
                    attempt=attempt,
                    max_attempts=_MENU_RETRY_ATTEMPTS,
                    retry=bool(attempt < _MENU_RETRY_ATTEMPTS and _should_retry_exception(e)),
                    error_type=type(e).__name__,
                    error=str(e),
                    **extra,
                )
            except Exception:
                pass

            if attempt >= _MENU_RETRY_ATTEMPTS or not _should_retry_exception(e):
                break

            _sleep_before_retry(attempt)

    if last_exc is not None:
        raise last_exc

    raise RuntimeError(f"{op_name} failed without exception")


# -------------------------
# Internals
# -------------------------

def _ws_has_required_headers(ws, required_headers: List[str], max_scan_rows: int = 10) -> bool:
    try:
        values = _call_with_retry(
            lambda: ws.get_all_values(),
            op_name="menu._ws_has_required_headers.get_all_values",
            log_fields={"worksheet_title": getattr(ws, "title", "")},
        )
    except Exception as e:
        try:
            log_event(
                "menu_ws_headers_probe_failed",
                worksheet_title=getattr(ws, "title", ""),
                error_type=type(e).__name__,
                error=str(e),
                transient=_should_retry_exception(e),
            )
        except Exception:
            pass
        return False

    if not values:
        return False

    req = [normalize(h) for h in required_headers if str(h or "").strip()]
    scan = values[:max_scan_rows] if max_scan_rows > 0 else values

    for row in scan:
        row_norm = [normalize(x) for x in row]
        if all(h in row_norm for h in req):
            return True

    return False


def _find_menu_ws_by_headers(orders_sh) -> Optional[Any]:
    try:
        worksheets = _call_with_retry(
            lambda: orders_sh.worksheets(),
            op_name="menu._find_menu_ws_by_headers.worksheets",
        )
        for ws in worksheets:
            if _ws_has_required_headers(ws, REQUIRED_MENU_HEADERS):
                return ws
    except Exception as e:
        try:
            log_event(
                "menu_ws_autodetect_failed",
                error_type=type(e).__name__,
                error=str(e),
                transient=_should_retry_exception(e),
            )
        except Exception:
            pass
        return None
    return None


def _parse_price(value: Any) -> Optional[float]:
    s = str(value or "").strip()
    if not s:
        return None

    s = s.replace(",", ".")
    m = re.search(r"(\d+(?:\.\d+)?)", s)
    if not m:
        return None

    try:
        return float(m.group(1))
    except Exception:
        return None


def _format_price_for_sheet(value: float) -> str:
    try:
        n = round(float(value), 2)
    except Exception:
        raise HTTPException(status_code=422, detail="Invalid price")

    if n < 0:
        raise HTTPException(status_code=422, detail="price must be >= 0")

    s = f"{n:.2f}"
    if s.endswith("00"):
        return str(int(round(n)))
    if s.endswith("0"):
        return s[:-1]
    return s


def _cache_key_for_orders_sh(orders_sh) -> str:
    try:
        sid = getattr(orders_sh, "id", None)
        if sid:
            return str(sid)
    except Exception:
        pass
    return str(id(orders_sh))


def _cache_get(
    cache: Dict[str, Tuple[float, Dict[str, Dict[str, Any]]]],
    cache_key: str,
) -> Optional[Dict[str, Dict[str, Any]]]:
    now = time.time()
    v = cache.get(cache_key)
    if not v:
        return None
    ts, idx = v
    if MENU_CACHE_TTL_SECONDS > 0 and (now - ts) <= MENU_CACHE_TTL_SECONDS:
        return idx
    return None


def _cache_set(
    cache: Dict[str, Tuple[float, Dict[str, Dict[str, Any]]]],
    cache_key: str,
    idx: Dict[str, Dict[str, Any]],
    *,
    ts: Optional[float] = None,
) -> None:
    cache[cache_key] = (float(ts if ts is not None else time.time()), idx)


def _cache_get_stale(
    cache: Dict[str, Tuple[float, Dict[str, Dict[str, Any]]]],
    cache_key: str,
    *,
    max_age_seconds: int,
) -> Optional[Dict[str, Dict[str, Any]]]:
    now = time.time()
    cached = cache.get(cache_key)
    if not cached:
        return None

    ts, idx = cached
    if max_age_seconds > 0 and (now - ts) <= max_age_seconds:
        return idx

    return None


def _cache_age_seconds(
    cache: Dict[str, Tuple[float, Dict[str, Dict[str, Any]]]],
    cache_key: str,
) -> Optional[int]:
    cached = cache.get(cache_key)
    if not cached:
        return None

    ts, _ = cached
    try:
        return max(0, int(time.time() - ts))
    except Exception:
        return None


def _cache_invalidate(cache: Dict[str, Tuple[float, Dict[str, Dict[str, Any]]]], cache_key: str) -> None:
    if cache_key in cache:
        del cache[cache_key]


def _project_root() -> Path:
    return Path(__file__).resolve().parent.parent


def _snapshot_dir() -> Path:
    return _project_root() / MENU_SNAPSHOT_DIRNAME


def _snapshot_path(cache_key: str) -> Path:
    safe_key = re.sub(r"[^A-Za-z0-9_.-]", "_", str(cache_key or "").strip()) or "unknown"
    return _snapshot_dir() / f"{safe_key}.json"


def _read_snapshot_payload(path: Path) -> Optional[Dict[str, Any]]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None

    if not isinstance(payload, dict):
        return None

    return payload


def _load_menu_snapshot(cache_key: str) -> Optional[Tuple[float, Dict[str, Dict[str, Any]]]]:
    path = _snapshot_path(cache_key)
    if not path.exists():
        return None

    payload = _read_snapshot_payload(path)
    if payload is None:
        try:
            log_event(
                "menu_snapshot_read_failed",
                cache_key=cache_key,
                error_type="snapshot_payload_invalid",
                error="invalid snapshot payload",
            )
        except Exception:
            pass
        return None

    if int(payload.get("version") or 0) != MENU_SNAPSHOT_VERSION:
        return None

    snapshot_key = str(payload.get("spreadsheet_id") or "").strip()
    if snapshot_key != str(cache_key or "").strip():
        return None

    try:
        generated_at_ts = float(payload.get("generated_at_ts") or 0)
    except Exception:
        return None

    if generated_at_ts <= 0:
        return None

    age_seconds = max(0, int(time.time() - generated_at_ts))
    if age_seconds > MENU_SNAPSHOT_MAX_AGE_SECONDS:
        return None

    menu_idx = payload.get("menu_idx")
    if not isinstance(menu_idx, dict) or not menu_idx:
        return None

    return float(generated_at_ts), menu_idx


def _persist_menu_snapshot(cache_key: str, idx: Dict[str, Dict[str, Any]], *, ts: Optional[float] = None) -> None:
    snapshot_ts = float(ts if ts is not None else time.time())
    payload = {
        "version": MENU_SNAPSHOT_VERSION,
        "spreadsheet_id": str(cache_key or "").strip(),
        "generated_at_ts": snapshot_ts,
        "menu_idx": idx,
    }

    snapshot_dir = _snapshot_dir()
    snapshot_dir.mkdir(parents=True, exist_ok=True)

    path = _snapshot_path(cache_key)
    temp_path = path.with_suffix(path.suffix + ".tmp")
    temp_path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    temp_path.replace(path)


def _set_menu_read_cooldown(cache_key: str) -> None:
    _MENU_READ_COOLDOWN_UNTIL[cache_key] = time.time() + MENU_READ_FAILURE_COOLDOWN_SECONDS


def _clear_menu_read_cooldown(cache_key: str) -> None:
    _MENU_READ_COOLDOWN_UNTIL.pop(cache_key, None)


def _is_menu_read_cooldown_active(cache_key: str) -> bool:
    until = float(_MENU_READ_COOLDOWN_UNTIL.get(cache_key) or 0.0)
    if until <= 0:
        return False
    if time.time() >= until:
        _MENU_READ_COOLDOWN_UNTIL.pop(cache_key, None)
        return False
    return True


def _cooldown_remaining_seconds(cache_key: str) -> int:
    until = float(_MENU_READ_COOLDOWN_UNTIL.get(cache_key) or 0.0)
    if until <= 0:
        return 0
    return max(0, int(until - time.time()))


def _should_emit_transient_menu_alert(cache_key: str) -> bool:
    now = time.time()
    last = float(_MENU_TRANSIENT_ALERT_LAST_AT.get(cache_key) or 0.0)
    if last > 0 and (now - last) < MENU_TRANSIENT_ALERT_COOLDOWN_SECONDS:
        return False
    _MENU_TRANSIENT_ALERT_LAST_AT[cache_key] = now
    return True


def _maybe_alert_transient_menu_failure(cache_key: str, error: Exception, *, served_stale: bool) -> None:
    if not _should_emit_transient_menu_alert(cache_key):
        return

    try:
        log_event(
            "menu_transient_failure",
            cache_key=cache_key,
            error_type=type(error).__name__,
            error=str(error),
            served_fallback=bool(served_stale),
            cooldown_seconds=MENU_TRANSIENT_ALERT_COOLDOWN_SECONDS,
            module="menu.load_menu_index_transient_stale" if served_stale else "menu.load_menu_index_transient",
        )
    except Exception:
        pass


def _set_last_menu_serve_source(cache_key: str, source: str) -> None:
    clean_source = str(source or "").strip()
    _MENU_LAST_SERVE_SOURCE[cache_key] = clean_source
    note_sheets_serving_source(clean_source)


def _get_last_menu_serve_source(cache_key: str) -> str:
    return str(_MENU_LAST_SERVE_SOURCE.get(cache_key) or "").strip()


def invalidate_menu_cache(orders_sh) -> None:
    ck = _cache_key_for_orders_sh(orders_sh)
    _cache_invalidate(_MENU_CACHE, ck)
    _cache_invalidate(_MENU_ADMIN_CACHE, ck)
    _clear_menu_read_cooldown(ck)
    _MENU_TRANSIENT_ALERT_LAST_AT.pop(ck, None)
    _MENU_LAST_SERVE_SOURCE.pop(ck, None)

    try:
        from app.config_bundle import invalidate_config_bundle

        invalidate_config_bundle(orders_sh=orders_sh)
    except Exception as e:
        try:
            log_event(
                "menu_cache_bundle_invalidation_failed",
                cache_key=ck,
                error_type=type(e).__name__,
                error=str(e),
            )
        except Exception:
            pass


def invalidate_all_menu_caches() -> None:
    _MENU_CACHE.clear()
    _MENU_ADMIN_CACHE.clear()
    _MENU_READ_COOLDOWN_UNTIL.clear()
    _MENU_TRANSIENT_ALERT_LAST_AT.clear()
    _MENU_LAST_SERVE_SOURCE.clear()


def get_menu_runtime_status(orders_sh) -> Dict[str, Any]:
    ck = _cache_key_for_orders_sh(orders_sh)
    path = _snapshot_path(ck)
    payload = _read_snapshot_payload(path) if path.exists() else None
    snapshot_valid = _load_menu_snapshot(ck) is not None

    age_seconds: Optional[int] = None
    snapshot_spreadsheet_id = ""
    generated_at_ts: Optional[float] = None
    rejection_reason = ""

    if payload is None:
        if path.exists():
            rejection_reason = "invalid_payload"
    else:
        snapshot_spreadsheet_id = str(payload.get("spreadsheet_id") or "").strip()
        try:
            generated_at_ts = float(payload.get("generated_at_ts") or 0)
        except Exception:
            generated_at_ts = None

        if generated_at_ts and generated_at_ts > 0:
            try:
                age_seconds = max(0, int(time.time() - generated_at_ts))
            except Exception:
                age_seconds = None

        if not snapshot_valid:
            if int(payload.get("version") or 0) != MENU_SNAPSHOT_VERSION:
                rejection_reason = "version_mismatch"
            elif snapshot_spreadsheet_id != str(ck or "").strip():
                rejection_reason = "spreadsheet_id_mismatch"
            elif not generated_at_ts or generated_at_ts <= 0:
                rejection_reason = "generated_at_invalid"
            elif age_seconds is not None and age_seconds > MENU_SNAPSHOT_MAX_AGE_SECONDS:
                rejection_reason = "snapshot_too_old"
            elif not isinstance(payload.get("menu_idx"), dict) or not payload.get("menu_idx"):
                rejection_reason = "menu_idx_invalid"

    return {
        "spreadsheet_id": ck,
        "snapshot_path": str(path),
        "snapshot_exists": bool(path.exists()),
        "snapshot_valid": bool(snapshot_valid),
        "snapshot_age_seconds": age_seconds,
        "snapshot_spreadsheet_id": snapshot_spreadsheet_id,
        "last_served_from": _get_last_menu_serve_source(ck),
        "memory_cache_age_seconds": _cache_age_seconds(_MENU_CACHE, ck),
        "memory_cache_fresh": _cache_get(_MENU_CACHE, ck) is not None,
        "cooldown_active": _is_menu_read_cooldown_active(ck),
        "cooldown_remaining_seconds": _cooldown_remaining_seconds(ck),
        "snapshot_rejection_reason": rejection_reason,
    }


def _get_menu_ws(orders_sh):
    ws = None

    try:
        ws = get_ws(orders_sh, "Menu")
    except Exception:
        ws = None

    if ws is None:
        ws = _find_menu_ws_by_headers(orders_sh)
        if ws is not None:
            try:
                log_event("menu_ws_autodetected", worksheet_title=getattr(ws, "title", "unknown"))
            except Exception:
                pass

    if ws is None:
        alert_tenant_error(tenant_id="", error="Menu worksheet not found")
        raise HTTPException(
            status_code=500,
            detail="Menu worksheet not found. Expected a tab named 'Menu' or any tab with headers: sku,name,price,active,category",
        )

    return ws


def _get_menu_context(orders_sh) -> Dict[str, Any]:
    ws = _get_menu_ws(orders_sh)

    try:
        values = _call_with_retry(
            lambda: ws.get_all_values(),
            op_name="menu._get_menu_context.get_all_values",
            log_fields={"worksheet_title": getattr(ws, "title", "")},
        )
    except Exception as e:
        try:
            log_event(
                "menu_context_read_failed",
                worksheet_title=getattr(ws, "title", ""),
                error_type=type(e).__name__,
                error=str(e),
                transient=_should_retry_exception(e),
            )
        except Exception:
            pass
        raise HTTPException(status_code=500, detail=f"Cannot read Menu worksheet: {e}")

    if not values:
        raise HTTPException(status_code=500, detail="Menu worksheet is empty")

    header_row_1based = detect_header_row(values, required_headers=REQUIRED_MENU_HEADERS, max_scan=10)
    if header_row_1based < 1 or header_row_1based > len(values):
        raise HTTPException(status_code=500, detail="Invalid Menu header row")

    headers_raw = values[header_row_1based - 1]
    headers_norm = [normalize(h) for h in headers_raw]

    idx_map: Dict[str, int] = {}
    for i, h in enumerate(headers_norm):
        if h and h not in idx_map:
            idx_map[h] = i

    for req in REQUIRED_MENU_HEADERS:
        if normalize(req) not in idx_map:
            raise HTTPException(status_code=500, detail=f"Missing required Menu header: {req}")

    return {
        "ws": ws,
        "values": values,
        "header_row_1based": header_row_1based,
        "headers_raw": headers_raw,
        "headers_norm": headers_norm,
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

    end_col = len(row_values)
    ws.update(
        _range_a1(row_index_1based, 1, end_col),
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


def _find_next_empty_row(ws, header_row_1based: int, header_len: int) -> int:
    try:
        values = _call_with_retry(
            lambda: ws.get_all_values(),
            op_name="menu._find_next_empty_row.get_all_values",
            log_fields={"worksheet_title": getattr(ws, "title", "")},
        )
    except Exception:
        values = []

    return _find_next_empty_row_from_values(
        values=values,
        header_row_1based=header_row_1based,
        header_len=header_len,
    )


def _looks_like_headerish_menu_row(sku: str, name: str, price_raw: str, active_raw: str, category: str) -> bool:
    sku_n = normalize(sku)
    name_n = normalize(name)
    price_n = normalize(price_raw)
    active_n = normalize(active_raw)
    category_n = normalize(category)

    headerish_values = {
        "sku", "codigo", "codigo sku", "identificador",
        "name", "nombre",
        "price", "precio",
        "active", "activo",
        "category", "categoria",
    }

    matches = 0
    for v in [sku_n, name_n, price_n, active_n, category_n]:
        if v in headerish_values:
            matches += 1

    return matches >= 3


def _build_discount_promo_virtual_item(promo: Dict[str, Any], menu_idx: Dict[str, Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    product_sku = str(promo.get("product_sku") or "").strip()
    if not product_sku:
        return None

    base_item = menu_idx.get(product_sku)
    if not base_item:
        return None

    promo_id = str(promo.get("promo_id") or "").strip()
    promo_name = str(promo.get("name") or "").strip()
    description = str(promo.get("description") or "").strip()

    original_price = float(promo.get("original_price") or 0.0)
    promo_price = float(promo.get("promo_price") or 0.0)

    if original_price <= 0:
        original_price = float(base_item.get("price") or 0.0)

    if promo_price <= 0:
        return None

    if not promo_name:
        promo_name = str(base_item.get("name") or product_sku).strip()

    if not description:
        description = f"De Bs {int(original_price) if float(original_price).is_integer() else original_price} a Bs {int(promo_price) if float(promo_price).is_integer() else promo_price}"

    return {
        "sku": f"promo::{promo_id}",
        "name": promo_name,
        "price": float(promo_price),
        "category": PROMOTIONS_CATEGORY_NAME,
        "photo_file_id": str(base_item.get("photo_file_id") or "").strip(),
        "photo_url": str(base_item.get("photo_url") or "").strip(),
        "is_promo": True,
        "promo_id": promo_id,
        "promo_type": "discount",
        "promo_description": description,
        "promo_original_price": float(original_price),
        "base_product_sku": product_sku,
    }


def _build_combo_promo_virtual_item(promo: Dict[str, Any], menu_idx: Dict[str, Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    promo_id = str(promo.get("promo_id") or "").strip()
    promo_name = str(promo.get("name") or "").strip()
    combo_items = promo.get("combo_items") or []
    promo_price = float(promo.get("promo_price") or 0.0)
    original_price = float(promo.get("original_price") or 0.0)
    description = str(promo.get("description") or "").strip()

    if not promo_id or not promo_name or not isinstance(combo_items, list) or not combo_items:
        return None

    resolved_items: List[Dict[str, Any]] = []
    first_photo_url = ""
    first_photo_file_id = ""

    computed_original = 0.0
    for it in combo_items:
        sku = str(it.get("sku") or "").strip()
        if not sku or sku not in menu_idx:
            return None

        try:
            qty = int(it.get("qty") or 1)
        except Exception:
            qty = 1
        qty = max(1, qty)

        base_item = menu_idx[sku]
        base_price = float(base_item.get("price") or 0.0)
        computed_original += base_price * qty

        if not first_photo_url:
            first_photo_url = str(base_item.get("photo_url") or "").strip()
        if not first_photo_file_id:
            first_photo_file_id = str(base_item.get("photo_file_id") or "").strip()

        resolved_items.append({
            "sku": sku,
            "qty": qty,
            "name": str(base_item.get("name") or sku).strip(),
            "unit_price": base_price,
        })

    if original_price <= 0:
        original_price = computed_original

    if promo_price <= 0:
        return None

    if not description:
        item_names = " + ".join([str(x.get("name") or "").strip() for x in resolved_items[:3] if str(x.get("name") or "").strip()])
        if item_names:
            description = item_names

    return {
        "sku": f"promo::{promo_id}",
        "name": promo_name,
        "price": float(promo_price),
        "category": PROMOTIONS_CATEGORY_NAME,
        "photo_file_id": first_photo_file_id,
        "photo_url": first_photo_url,
        "is_promo": True,
        "promo_id": promo_id,
        "promo_type": "combo",
        "promo_description": description,
        "promo_original_price": float(original_price),
        "combo_items": resolved_items,
    }


def _build_virtual_promotions_index(orders_sh, menu_idx: Dict[str, Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    result: Dict[str, Dict[str, Any]] = {}

    try:
        promos = get_active_promotions(orders_sh)
    except Exception as e:
        try:
            log_event("menu_promotions_load_failed", error=str(e))
        except Exception:
            pass
        return result

    for promo in promos:
        promo_type = str(promo.get("type") or "").strip().lower()
        item: Optional[Dict[str, Any]] = None

        if promo_type == "discount":
            item = _build_discount_promo_virtual_item(promo, menu_idx)
        elif promo_type == "combo":
            item = _build_combo_promo_virtual_item(promo, menu_idx)

        if not item:
            continue

        sku = str(item.get("sku") or "").strip()
        if not sku:
            continue

        result[sku] = item

    return result


def _load_menu_category_order_config(
    orders_sh=None,
    *,
    content_map: Optional[Dict[str, str]] = None,
) -> List[str]:
    resolved_content_map = content_map if isinstance(content_map, dict) else None

    if resolved_content_map is None and orders_sh is not None:
        try:
            resolved_content_map = load_content_map(orders_sh, force=False)
        except Exception:
            resolved_content_map = {}

    raw_value = str((resolved_content_map or {}).get(normalize("menu_category_order_json")) or "").strip()
    if not raw_value:
        return []

    try:
        parsed = json.loads(raw_value)
    except Exception:
        return []

    if not isinstance(parsed, list):
        return []

    ordered_names: List[str] = []
    seen = set()

    for item in parsed:
        clean_name = str(item or "").strip()
        clean_key = normalize(clean_name)
        if not clean_key or clean_key in seen:
            continue
        seen.add(clean_key)
        ordered_names.append(clean_name)

    return ordered_names


def resolve_effective_category_order(
    category_names: List[str],
    orders_sh=None,
    *,
    content_map: Optional[Dict[str, str]] = None,
) -> List[str]:
    existing_by_key: Dict[str, str] = {}
    existing_keys_in_order: List[str] = []

    for category_name in category_names or []:
        clean_name = str(category_name or "").strip()
        clean_key = normalize(clean_name)
        if not clean_key or clean_key in existing_by_key:
            continue
        existing_by_key[clean_key] = clean_name
        existing_keys_in_order.append(clean_key)

    if not existing_keys_in_order:
        return []

    configured_order = _load_menu_category_order_config(orders_sh, content_map=content_map)
    ordered_keys: List[str] = []
    seen = set()
    promotions_key = normalize(PROMOTIONS_CATEGORY_NAME)

    for configured_name in configured_order:
        configured_key = normalize(configured_name)
        if configured_key not in existing_by_key or configured_key in seen:
            continue
        ordered_keys.append(configured_key)
        seen.add(configured_key)

    for existing_key in existing_keys_in_order:
        if existing_key == promotions_key:
            continue
        if existing_key in seen:
            continue
        ordered_keys.append(existing_key)
        seen.add(existing_key)

    if promotions_key in existing_by_key and promotions_key not in seen:
        ordered_keys.append(promotions_key)
        seen.add(promotions_key)

    for existing_key in existing_keys_in_order:
        if existing_key in seen:
            continue
        ordered_keys.append(existing_key)
        seen.add(existing_key)

    return [existing_by_key[key] for key in ordered_keys if key in existing_by_key]


# -------------------------
# Public API (cliente)
# -------------------------

def load_menu_index(orders_sh, force: bool = False) -> Dict[str, Dict[str, Any]]:
    ck = _cache_key_for_orders_sh(orders_sh)
    max_stale_age = MENU_CACHE_TTL_SECONDS + MENU_CACHE_STALE_WINDOW_SECONDS
    stale_cached = _cache_get_stale(_MENU_CACHE, ck, max_age_seconds=max_stale_age)
    stale_cache_age_seconds = _cache_age_seconds(_MENU_CACHE, ck) if stale_cached is not None else None
    snapshot_cached = _load_menu_snapshot(ck)

    if not force:
        cached = _cache_get(_MENU_CACHE, ck)
        if cached is not None:
            _set_last_menu_serve_source(ck, "memory")
            return cached
    else:
        _cache_invalidate(_MENU_CACHE, ck)

    if not force and snapshot_cached is not None:
        snapshot_ts, snapshot_idx = snapshot_cached
        _cache_set(_MENU_CACHE, ck, snapshot_idx, ts=snapshot_ts)
        _set_last_menu_serve_source(ck, "snapshot")
        try:
            log_event(
                "menu_loaded_from_snapshot",
                cache_key=ck,
                cache_age_seconds=max(0, int(time.time() - snapshot_ts)),
                snapshot_max_age_seconds=MENU_SNAPSHOT_MAX_AGE_SECONDS,
            )
        except Exception:
            pass
        return snapshot_idx

    if not force and stale_cached is not None:
        try:
            log_event(
                "menu_served_from_stale_memory",
                cache_key=ck,
                cache_age_seconds=_cache_age_seconds(_MENU_CACHE, ck),
                fresh_ttl_seconds=MENU_CACHE_TTL_SECONDS,
                stale_window_seconds=MENU_CACHE_STALE_WINDOW_SECONDS,
            )
        except Exception:
            pass
        _set_last_menu_serve_source(ck, "memory")
        return stale_cached

    if not force and _is_menu_read_cooldown_active(ck):
        if stale_cached is not None:
            try:
                log_event(
                    "menu_served_stale_during_cooldown",
                    cache_key=ck,
                    cache_age_seconds=_cache_age_seconds(_MENU_CACHE, ck),
                    cooldown_remaining_seconds=_cooldown_remaining_seconds(ck),
                    fresh_ttl_seconds=MENU_CACHE_TTL_SECONDS,
                    stale_window_seconds=MENU_CACHE_STALE_WINDOW_SECONDS,
                )
            except Exception:
                pass
            _set_last_menu_serve_source(ck, "memory")
            return stale_cached

        raise HTTPException(status_code=503, detail="Menu temporarily unavailable")

    try:
        ws = _get_menu_ws(orders_sh)
        rows = read_records_manual(ws, required_headers=REQUIRED_MENU_HEADERS)

        idx: Dict[str, Dict[str, Any]] = {}
        stats = {
            "rows_in": len(rows),
            "active_in": 0,
            "skipped_no_sku": 0,
            "skipped_inactive": 0,
            "skipped_bad_price": 0,
            "skipped_headerish": 0,
            "duplicates": 0,
            "virtual_promotions": 0,
        }

        for r in rows:
            sku = str(r.get("sku", "") or "").strip()
            name = str(r.get("name", "") or "").strip()
            price_raw = str(r.get("price", "") or "").strip()
            active_raw = str(r.get("active", "") or "").strip()
            category = str(r.get("category", "") or "").strip() or "Otros"
            photo_file_id = str(r.get("photo_file_id", "") or "").strip()
            photo_url = str(r.get("photo_url", "") or "").strip()

            if _looks_like_headerish_menu_row(sku, name, price_raw, active_raw, category):
                stats["skipped_headerish"] += 1
                continue

            if not sku:
                stats["skipped_no_sku"] += 1
                continue

            if not to_bool(r.get("active", "")):
                stats["skipped_inactive"] += 1
                continue

            stats["active_in"] += 1

            price = _parse_price(price_raw)
            if price is None:
                stats["skipped_bad_price"] += 1
                alert_tenant_error(tenant_id="", error=f"Invalid price for SKU {sku}")
                continue

            if sku in idx:
                stats["duplicates"] += 1
                try:
                    log_event(
                        "menu_duplicate_sku",
                        sku=sku,
                        prev=idx[sku],
                        new={
                            "name": name,
                            "price": float(price),
                            "category": category,
                            "photo_url": photo_url,
                            "photo_file_id": photo_file_id,
                        },
                    )
                except Exception:
                    pass

            idx[sku] = {
                "sku": sku,
                "name": name,
                "price": float(price),
                "category": category,
                "photo_file_id": photo_file_id,
                "photo_url": photo_url,
            }

        virtual_promos = _build_virtual_promotions_index(orders_sh, idx)
        if virtual_promos:
            idx.update(virtual_promos)
            stats["virtual_promotions"] = len(virtual_promos)

        now_ts = time.time()
        _cache_set(_MENU_CACHE, ck, idx, ts=now_ts)
        _set_last_menu_serve_source(ck, "sheets")
        try:
            _persist_menu_snapshot(ck, idx, ts=now_ts)
        except Exception as e:
            try:
                log_event(
                    "menu_snapshot_write_failed",
                    cache_key=ck,
                    error_type=type(e).__name__,
                    error=str(e),
                )
            except Exception:
                pass
        _clear_menu_read_cooldown(ck)

        try:
            log_event(
                "menu_loaded",
                worksheet_title=getattr(ws, "title", "unknown"),
                items=len(idx),
                stats=stats,
                fresh_ttl_seconds=MENU_CACHE_TTL_SECONDS,
                stale_window_seconds=MENU_CACHE_STALE_WINDOW_SECONDS,
                snapshot_max_age_seconds=MENU_SNAPSHOT_MAX_AGE_SECONDS,
            )
        except Exception:
            pass

        return idx

    except Exception as e:
        is_transient = _should_retry_exception(e)
        if is_transient:
            _set_menu_read_cooldown(ck)
            if stale_cached is not None:
                try:
                    log_event(
                        "menu_load_failed_serving_stale",
                        cache_key=ck,
                        error_type=type(e).__name__,
                        error=str(e),
                        cache_age_seconds=stale_cache_age_seconds,
                        cooldown_seconds=MENU_READ_FAILURE_COOLDOWN_SECONDS,
                        fresh_ttl_seconds=MENU_CACHE_TTL_SECONDS,
                        stale_window_seconds=MENU_CACHE_STALE_WINDOW_SECONDS,
                    )
                except Exception:
                    pass
                _set_last_menu_serve_source(ck, "memory")
                _maybe_alert_transient_menu_failure(ck, e, served_stale=True)
                return stale_cached

            if snapshot_cached is not None:
                snapshot_ts, snapshot_idx = snapshot_cached
                _cache_set(_MENU_CACHE, ck, snapshot_idx, ts=snapshot_ts)
                _set_last_menu_serve_source(ck, "snapshot")
                try:
                    log_event(
                        "menu_load_failed_serving_snapshot",
                        cache_key=ck,
                        error_type=type(e).__name__,
                        error=str(e),
                        snapshot_age_seconds=max(0, int(time.time() - snapshot_ts)),
                        snapshot_max_age_seconds=MENU_SNAPSHOT_MAX_AGE_SECONDS,
                        cooldown_seconds=MENU_READ_FAILURE_COOLDOWN_SECONDS,
                    )
                except Exception:
                    pass
                _maybe_alert_transient_menu_failure(ck, e, served_stale=True)
                return snapshot_idx

            _maybe_alert_transient_menu_failure(ck, e, served_stale=False)
        else:
            alert_system_error(error=str(e), module="menu.load_menu_index")
        raise


def group_menu_by_category(
    menu_idx: Dict[str, Dict[str, Any]],
    orders_sh=None,
    *,
    content_map: Optional[Dict[str, str]] = None,
) -> Dict[str, List[Dict[str, Any]]]:
    cats: Dict[str, List[Dict[str, Any]]] = {}

    for item in menu_idx.values():
        cat = item.get("category", "") or "Otros"

        row_item = {
            "sku": item["sku"],
            "name": item.get("name", ""),
            "price": item.get("price", 0),
            "category": cat,
            "photo_file_id": item.get("photo_file_id", ""),
            "photo_url": item.get("photo_url", ""),
        }

        if bool(item.get("is_promo")):
            row_item["is_promo"] = True
            row_item["promo_id"] = item.get("promo_id", "")
            row_item["promo_type"] = item.get("promo_type", "")
            row_item["promo_description"] = item.get("promo_description", "")
            row_item["promo_original_price"] = item.get("promo_original_price", 0)
            row_item["base_product_sku"] = item.get("base_product_sku", "")
            row_item["combo_items"] = item.get("combo_items", [])

        cats.setdefault(cat, []).append(row_item)

    for cat in cats:
        if cat == PROMOTIONS_CATEGORY_NAME:
            cats[cat] = sorted(cats[cat], key=lambda x: normalize(x.get("name", "")))
        else:
            cats[cat] = sorted(cats[cat], key=lambda x: normalize(x.get("name", "")))

    ordered_cat_names = resolve_effective_category_order(
        list(cats.keys()),
        orders_sh=orders_sh,
        content_map=content_map,
    )
    ordered_cats: Dict[str, List[Dict[str, Any]]] = {}

    for cat_name in ordered_cat_names:
        if cat_name in cats:
            ordered_cats[cat_name] = cats[cat_name]

    for cat_name, items in cats.items():
        if cat_name not in ordered_cats:
            ordered_cats[cat_name] = items

    return ordered_cats


def calc_total_amount(items: List[Dict[str, Any]], menu_idx: Dict[str, Dict[str, Any]]) -> float:
    total = 0.0

    for it in items:
        sku = str(it.get("sku", "")).strip()
        qty = it.get("qty", 0)

        if sku not in menu_idx:
            alert_tenant_error(tenant_id="", error=f"Unknown SKU {sku} in order")
            raise HTTPException(status_code=422, detail=f"Unknown sku: {sku}")

        try:
            qty_i = int(qty)
        except Exception:
            raise HTTPException(status_code=422, detail=f"qty must be integer for sku={sku}")

        if qty_i <= 0:
            raise HTTPException(status_code=422, detail=f"qty must be >= 1 for sku={sku}")

        total += float(menu_idx[sku]["price"]) * qty_i

    return round(total, 2)


# -------------------------
# Public API (admin)
# -------------------------

def load_menu_admin_index(orders_sh, force: bool = False) -> Dict[str, Dict[str, Any]]:
    """
    Versión admin consistente con la lectura base del menú.
    Diferencia principal vs load_menu_index:
    - NO filtra por active
    - sí devuelve row_index y active
    """
    ck = _cache_key_for_orders_sh(orders_sh)
    max_stale_age = MENU_CACHE_TTL_SECONDS + MENU_CACHE_STALE_WINDOW_SECONDS
    stale_cached = _cache_get_stale(_MENU_ADMIN_CACHE, ck, max_age_seconds=max_stale_age)
    stale_cache_age_seconds = _cache_age_seconds(_MENU_ADMIN_CACHE, ck) if stale_cached is not None else None

    if not force:
        cached = _cache_get(_MENU_ADMIN_CACHE, ck)
        if cached is not None:
            return cached
    else:
        _cache_invalidate(_MENU_ADMIN_CACHE, ck)

    try:
        ctx = _get_menu_context(orders_sh)
        values = ctx["values"]
        header_row_1based = ctx["header_row_1based"]
        idx_map = ctx["idx_map"]
        ws = ctx["ws"]

        idx: Dict[str, Dict[str, Any]] = {}
        stats = {
            "rows_in": 0,
            "skipped_no_sku": 0,
            "skipped_headerish": 0,
            "bad_price": 0,
            "duplicates": 0,
            "active_true": 0,
            "active_false": 0,
        }

        for ridx in range(header_row_1based + 1, len(values) + 1):
            row = values[ridx - 1]
            if not any(str(x).strip() for x in row):
                continue

            stats["rows_in"] += 1

            def g(col_name: str) -> str:
                i = idx_map.get(normalize(col_name))
                if i is None:
                    return ""
                return row[i] if i < len(row) else ""

            sku = str(g("sku") or "").strip()
            name = str(g("name") or "").strip()
            price_raw = str(g("price") or "").strip()
            active_raw = str(g("active") or "").strip()
            category = str(g("category") or "").strip() or "Otros"
            photo_file_id = str(g("photo_file_id") or "").strip()
            photo_url = str(g("photo_url") or "").strip()

            if _looks_like_headerish_menu_row(sku, name, price_raw, active_raw, category):
                stats["skipped_headerish"] += 1
                continue

            if not sku:
                stats["skipped_no_sku"] += 1
                continue

            active = to_bool(active_raw)
            if active:
                stats["active_true"] += 1
            else:
                stats["active_false"] += 1

            price = _parse_price(price_raw)
            if price is None:
                stats["bad_price"] += 1
                price = 0.0
                alert_tenant_error(tenant_id="", error=f"Invalid admin price for SKU {sku}")

            if sku in idx:
                stats["duplicates"] += 1
                try:
                    log_event(
                        "menu_admin_duplicate_sku",
                        sku=sku,
                        previous_row_index=idx[sku].get("row_index"),
                        new_row_index=ridx,
                    )
                except Exception:
                    pass

            idx[sku] = {
                "sku": sku,
                "name": name,
                "price": float(price),
                "category": category,
                "active": bool(active),
                "photo_file_id": photo_file_id,
                "photo_url": photo_url,
                "row_index": ridx,
            }

        _cache_set(_MENU_ADMIN_CACHE, ck, idx)

        try:
            log_event(
                "menu_admin_loaded",
                worksheet_title=getattr(ws, "title", "unknown"),
                items=len(idx),
                stats=stats,
                ttl_seconds=MENU_CACHE_TTL_SECONDS,
            )
        except Exception:
            pass

        return idx

    except Exception as e:
        if _should_retry_exception(e):
            if stale_cached is not None:
                try:
                    log_event(
                        "menu_admin_load_failed_serving_stale",
                        cache_key=ck,
                        error_type=type(e).__name__,
                        error=str(e),
                        cache_age_seconds=stale_cache_age_seconds,
                        fresh_ttl_seconds=MENU_CACHE_TTL_SECONDS,
                        stale_window_seconds=MENU_CACHE_STALE_WINDOW_SECONDS,
                        force=bool(force),
                    )
                except Exception:
                    pass
                return stale_cached
            try:
                log_event(
                    "menu_admin_load_transient_failure",
                    cache_key=ck,
                    error_type=type(e).__name__,
                    error=str(e),
                    force=bool(force),
                )
            except Exception:
                pass
        else:
            alert_system_error(error=str(e), module="menu.load_menu_admin_index")
        raise


def group_menu_admin_by_category(
    menu_idx: Dict[str, Dict[str, Any]],
    orders_sh=None,
    *,
    content_map: Optional[Dict[str, str]] = None,
) -> Dict[str, List[Dict[str, Any]]]:
    cats: Dict[str, List[Dict[str, Any]]] = {}

    for item in menu_idx.values():
        cat = item.get("category", "") or "Otros"
        cats.setdefault(cat, []).append(
            {
                "sku": item["sku"],
                "name": item.get("name", ""),
                "price": item.get("price", 0),
                "category": cat,
                "active": bool(item.get("active", False)),
                "photo_file_id": item.get("photo_file_id", ""),
                "photo_url": item.get("photo_url", ""),
                "row_index": item.get("row_index"),
            }
        )

    for cat in cats:
        cats[cat] = sorted(cats[cat], key=lambda x: normalize(x.get("name", "")))

    ordered_cat_names = resolve_effective_category_order(
        list(cats.keys()),
        orders_sh=orders_sh,
        content_map=content_map,
    )
    ordered_cats: Dict[str, List[Dict[str, Any]]] = {}

    for cat_name in ordered_cat_names:
        if cat_name in cats:
            ordered_cats[cat_name] = cats[cat_name]

    for cat_name, items in cats.items():
        if cat_name not in ordered_cats:
            ordered_cats[cat_name] = items

    return ordered_cats


def get_menu_product_or_404(orders_sh, sku: str) -> Dict[str, Any]:
    sku = str(sku or "").strip()
    if not sku:
        raise HTTPException(status_code=400, detail="sku is required")

    idx = load_menu_admin_index(orders_sh, force=False)
    item = idx.get(sku)

    if item:
        return item

    idx = load_menu_admin_index(orders_sh, force=True)
    item = idx.get(sku)
    if not item:
        raise HTTPException(status_code=404, detail=f"Product not found: {sku}")
    return item


def _get_menu_product_context_or_404(orders_sh, sku: str) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    clean_sku = str(sku or "").strip()
    if not clean_sku:
        raise HTTPException(status_code=400, detail="sku is required")

    ctx = _get_menu_context(orders_sh)
    values = ctx["values"]
    header_row_1based = int(ctx["header_row_1based"])
    idx_map = ctx["idx_map"]

    for ridx in range(header_row_1based + 1, len(values) + 1):
        row = values[ridx - 1]
        if not any(str(x).strip() for x in row):
            continue

        def g(col_name: str) -> str:
            i = idx_map.get(normalize(col_name))
            if i is None:
                return ""
            return row[i] if i < len(row) else ""

        row_sku = str(g("sku") or "").strip()
        name = str(g("name") or "").strip()
        price_raw = str(g("price") or "").strip()
        active_raw = str(g("active") or "").strip()
        category = str(g("category") or "").strip() or "Otros"
        photo_file_id = str(g("photo_file_id") or "").strip()
        photo_url = str(g("photo_url") or "").strip()

        if _looks_like_headerish_menu_row(row_sku, name, price_raw, active_raw, category):
            continue

        if row_sku != clean_sku:
            continue

        price = _parse_price(price_raw)
        if price is None:
            price = 0.0

        return {
            "sku": row_sku,
            "name": name,
            "price": float(price),
            "category": category,
            "active": bool(to_bool(active_raw)),
            "photo_file_id": photo_file_id,
            "photo_url": photo_url,
            "row_index": ridx,
        }, ctx

    raise HTTPException(status_code=404, detail=f"Product not found: {clean_sku}")


def set_menu_product_active(orders_sh, sku: str, is_active: bool) -> Dict[str, Any]:
    item, ctx = _get_menu_product_context_or_404(orders_sh, sku)
    ws = ctx["ws"]
    idx_map = ctx["idx_map"]

    active_col_idx0 = idx_map.get("active")
    if active_col_idx0 is None:
        raise HTTPException(status_code=500, detail="Missing 'active' column in Menu")

    row_index = int(item["row_index"])
    _call_with_retry(
        lambda: ws.update_cell(row_index, active_col_idx0 + 1, "TRUE" if is_active else "FALSE"),
        op_name="menu.set_menu_product_active.update_cell",
        log_fields={"sku": sku, "row_index": row_index},
    )

    invalidate_menu_cache(orders_sh)

    try:
        log_event("menu_product_active_updated", sku=sku, active=bool(is_active), row_index=row_index)
    except Exception:
        pass

    updated = get_menu_product_or_404(orders_sh, sku)
    return {"ok": True, "sku": sku, "active": bool(updated.get("active", False))}


def set_menu_product_price(orders_sh, sku: str, new_price: float) -> Dict[str, Any]:
    item, ctx = _get_menu_product_context_or_404(orders_sh, sku)
    ws = ctx["ws"]
    idx_map = ctx["idx_map"]

    price_col_idx0 = idx_map.get("price")
    if price_col_idx0 is None:
        raise HTTPException(status_code=500, detail="Missing 'price' column in Menu")

    price_str = _format_price_for_sheet(new_price)
    row_index = int(item["row_index"])
    _call_with_retry(
        lambda: ws.update_cell(row_index, price_col_idx0 + 1, price_str),
        op_name="menu.set_menu_product_price.update_cell",
        log_fields={"sku": sku, "row_index": row_index},
    )

    invalidate_menu_cache(orders_sh)

    try:
        log_event("menu_product_price_updated", sku=sku, price=price_str, row_index=row_index)
    except Exception:
        pass

    updated = get_menu_product_or_404(orders_sh, sku)
    return {"ok": True, "sku": sku, "price": float(updated.get("price", 0.0))}


# -------------------------
# Nuevas funciones admin
# -------------------------

def get_menu_categories(orders_sh) -> List[str]:
    idx = load_menu_admin_index(orders_sh, force=False)
    categories: List[str] = []
    seen = set()

    for item in idx.values():
        category = str(item.get("category") or "").strip()
        if not category:
            continue
        key = normalize(category)
        if key in seen:
            continue
        seen.add(key)
        categories.append(category)

    return resolve_effective_category_order(categories, orders_sh=orders_sh)


def _set_menu_product_text_field(orders_sh, sku: str, field_name: str, new_value: str) -> Dict[str, Any]:
    item, ctx = _get_menu_product_context_or_404(orders_sh, sku)
    ws = ctx["ws"]
    idx_map = ctx["idx_map"]

    field_col_idx0 = idx_map.get(normalize(field_name))
    if field_col_idx0 is None:
        raise HTTPException(status_code=500, detail=f"Missing '{field_name}' column in Menu")

    clean_value = str(new_value or "").strip()
    if not clean_value:
        raise HTTPException(status_code=422, detail=f"{field_name} cannot be empty")

    row_index = int(item["row_index"])
    _call_with_retry(
        lambda: ws.update_cell(row_index, field_col_idx0 + 1, clean_value),
        op_name="menu._set_menu_product_text_field.update_cell",
        log_fields={"sku": sku, "field_name": field_name, "row_index": row_index},
    )

    invalidate_menu_cache(orders_sh)

    updated = get_menu_product_or_404(orders_sh, sku)
    return {
        "ok": True,
        "sku": sku,
        field_name: str(updated.get(field_name, "") or "").strip(),
    }


def set_menu_product_name(orders_sh, sku: str, new_name: str) -> Dict[str, Any]:
    result = _set_menu_product_text_field(orders_sh, sku, "name", new_name)

    try:
        log_event("menu_product_name_updated", sku=sku, name=result.get("name", ""))
    except Exception:
        pass

    return result


def set_menu_product_category(orders_sh, sku: str, new_category: str) -> Dict[str, Any]:
    result = _set_menu_product_text_field(orders_sh, sku, "category", new_category)

    try:
        log_event("menu_product_category_updated", sku=sku, category=result.get("category", ""))
    except Exception:
        pass

    return result


def _generate_menu_product_sku(orders_sh) -> str:
    ctx = _get_menu_context(orders_sh)
    values = ctx["values"]
    header_row_1based = int(ctx["header_row_1based"])
    idx_map = ctx["idx_map"]
    existing_skus = set()

    sku_idx = idx_map.get(normalize("sku"))
    if sku_idx is not None:
        for row in values[header_row_1based:]:
            sku = row[sku_idx] if sku_idx < len(row) else ""
            clean_sku = str(sku or "").strip()
            if clean_sku:
                existing_skus.add(clean_sku)

    base = f"p_{int(time.time())}"
    if base not in existing_skus:
        return base

    for i in range(1, 1000):
        candidate = f"{base}_{i}"
        if candidate not in existing_skus:
            return candidate

    raise HTTPException(status_code=500, detail="Could not generate unique sku")


def create_menu_product(
    orders_sh,
    name: str,
    category: str,
    price: float,
    active: bool = True,
    photo_url: str = "",
) -> Dict[str, Any]:
    clean_name = str(name or "").strip()
    clean_category = str(category or "").strip()

    if not clean_name:
        raise HTTPException(status_code=422, detail="name cannot be empty")

    if not clean_category:
        raise HTTPException(status_code=422, detail="category cannot be empty")

    price_str = _format_price_for_sheet(price)
    sku = _generate_menu_product_sku(orders_sh)

    ctx = _get_menu_context(orders_sh)
    ws = ctx["ws"]
    values = ctx["values"]
    headers_raw = ctx["headers_raw"]
    header_row_1based = int(ctx["header_row_1based"])
    idx_map = ctx["idx_map"]

    row_values = [""] * len(headers_raw)

    def put(col_name: str, value: str) -> None:
        idx0 = idx_map.get(normalize(col_name))
        if idx0 is not None and idx0 < len(row_values):
            row_values[idx0] = value

    put("sku", sku)
    put("name", clean_name)
    put("price", price_str)
    put("active", "TRUE" if active else "FALSE")
    put("category", clean_category)

    if normalize("photo_url") in idx_map:
        put("photo_url", str(photo_url or "").strip())

    if normalize("photo_file_id") in idx_map:
        put("photo_file_id", "")

    try:
        next_row = _find_next_empty_row_from_values(
            values=values,
            header_row_1based=header_row_1based,
            header_len=len(headers_raw),
        )
        _call_with_retry(
            lambda: _write_full_row(ws, next_row, row_values),
            op_name="menu.create_menu_product.write_full_row",
            log_fields={"sku": sku, "name": clean_name, "row_index": next_row},
        )
    except Exception as e:
        alert_system_error(error=str(e), module="menu.create_menu_product")
        raise HTTPException(status_code=500, detail=f"Could not create product: {e}")

    invalidate_menu_cache(orders_sh)

    created = get_menu_product_or_404(orders_sh, sku)

    try:
        log_event(
            "menu_product_created",
            sku=sku,
            name=clean_name,
            category=clean_category,
            price=price_str,
            active=bool(active),
        )
    except Exception:
        pass

    return {
        "ok": True,
        "sku": sku,
        "name": str(created.get("name", "") or "").strip(),
        "category": str(created.get("category", "") or "").strip(),
        "price": float(created.get("price", 0.0)),
        "active": bool(created.get("active", False)),
        "photo_url": str(created.get("photo_url", "") or "").strip(),
    }
