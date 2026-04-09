from typing import Any, Dict, List, Optional, Tuple
import re
import time

from fastapi import HTTPException

from app.sheets import get_ws, read_records_manual, detect_header_row
from app.utils import to_bool, normalize, log_event
from app.alerts import alert_system_error, alert_tenant_error
from app.promotions import get_active_promotions


REQUIRED_MENU_HEADERS = ["sku", "name", "price", "active", "category"]

# Cache simple por spreadsheet
_MENU_CACHE: Dict[str, Tuple[float, Dict[str, Dict[str, Any]]]] = {}
_MENU_ADMIN_CACHE: Dict[str, Tuple[float, Dict[str, Dict[str, Any]]]] = {}

MENU_CACHE_TTL_SECONDS = 90

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
        alert_system_error(error=str(e), module="menu._ws_has_required_headers")
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
        alert_system_error(error=str(e), module="menu._find_menu_ws_by_headers")
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
) -> None:
    cache[cache_key] = (time.time(), idx)


def _cache_invalidate(cache: Dict[str, Tuple[float, Dict[str, Dict[str, Any]]]], cache_key: str) -> None:
    if cache_key in cache:
        del cache[cache_key]


def invalidate_menu_cache(orders_sh) -> None:
    ck = _cache_key_for_orders_sh(orders_sh)
    _cache_invalidate(_MENU_CACHE, ck)
    _cache_invalidate(_MENU_ADMIN_CACHE, ck)


def invalidate_all_menu_caches() -> None:
    _MENU_CACHE.clear()
    _MENU_ADMIN_CACHE.clear()


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
        alert_system_error(error=str(e), module="menu._get_menu_context")
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


# -------------------------
# Public API (cliente)
# -------------------------

def load_menu_index(orders_sh, force: bool = False) -> Dict[str, Dict[str, Any]]:
    ck = _cache_key_for_orders_sh(orders_sh)

    if not force:
        cached = _cache_get(_MENU_CACHE, ck)
        if cached is not None:
            return cached
    else:
        _cache_invalidate(_MENU_CACHE, ck)

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

        _cache_set(_MENU_CACHE, ck, idx)

        try:
            log_event(
                "menu_loaded",
                worksheet_title=getattr(ws, "title", "unknown"),
                items=len(idx),
                stats=stats,
                ttl_seconds=MENU_CACHE_TTL_SECONDS,
            )
        except Exception:
            pass

        return idx

    except Exception as e:
        alert_system_error(error=str(e), module="menu.load_menu_index")
        raise


def group_menu_by_category(menu_idx: Dict[str, Dict[str, Any]]) -> Dict[str, List[Dict[str, Any]]]:
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

    return cats


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
        alert_system_error(error=str(e), module="menu.load_menu_admin_index")
        raise


def group_menu_admin_by_category(menu_idx: Dict[str, Dict[str, Any]]) -> Dict[str, List[Dict[str, Any]]]:
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

    return cats


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


def set_menu_product_active(orders_sh, sku: str, is_active: bool) -> Dict[str, Any]:
    item = get_menu_product_or_404(orders_sh, sku)
    ctx = _get_menu_context(orders_sh)
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
    item = get_menu_product_or_404(orders_sh, sku)
    ctx = _get_menu_context(orders_sh)
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

    return sorted(categories, key=lambda x: normalize(x))


def _set_menu_product_text_field(orders_sh, sku: str, field_name: str, new_value: str) -> Dict[str, Any]:
    item = get_menu_product_or_404(orders_sh, sku)
    ctx = _get_menu_context(orders_sh)
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
    idx = load_menu_admin_index(orders_sh, force=True)
    existing_skus = set(idx.keys())

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
    headers_raw = ctx["headers_raw"]
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
        _call_with_retry(
            lambda: ws.append_row(row_values, value_input_option="USER_ENTERED"),
            op_name="menu.create_menu_product.append_row",
            log_fields={"sku": sku, "name": clean_name},
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
