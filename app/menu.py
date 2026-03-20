from typing import Any, Dict, List, Optional, Tuple
import re
import time

from fastapi import HTTPException

from app.sheets import get_ws, read_records_manual, detect_header_row
from app.utils import to_bool, normalize, log_event


REQUIRED_MENU_HEADERS = ["sku", "name", "price", "active", "category"]

# Cache simple por spreadsheet
_MENU_CACHE: Dict[str, Tuple[float, Dict[str, Dict[str, Any]]]] = {}
_MENU_ADMIN_CACHE: Dict[str, Tuple[float, Dict[str, Dict[str, Any]]]] = {}

MENU_CACHE_TTL_SECONDS = 90


# -------------------------
# Internals
# -------------------------

def _ws_has_required_headers(ws, required_headers: List[str], max_scan_rows: int = 10) -> bool:
    try:
        values = ws.get_all_values()
    except Exception:
        return False

    if not values:
        return False

    req = [normalize(h) for h in required_headers]
    scan = values[:max_scan_rows]

    for row in scan:
        row_norm = [normalize(x) for x in row]
        if all(h in row_norm for h in req):
            return True

    return False


def _find_menu_ws_by_headers(orders_sh) -> Optional[Any]:
    try:
        for ws in orders_sh.worksheets():
            if _ws_has_required_headers(ws, REQUIRED_MENU_HEADERS):
                return ws
    except Exception:
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


def _cache_get(cache: Dict[str, Tuple[float, Dict[str, Dict[str, Any]]]], cache_key: str) -> Optional[Dict[str, Dict[str, Any]]]:
    now = time.time()
    v = cache.get(cache_key)
    if not v:
        return None
    ts, idx = v
    if MENU_CACHE_TTL_SECONDS > 0 and (now - ts) <= MENU_CACHE_TTL_SECONDS:
        return idx
    return None


def _cache_set(cache: Dict[str, Tuple[float, Dict[str, Dict[str, Any]]]], cache_key: str, idx: Dict[str, Dict[str, Any]]) -> None:
    cache[cache_key] = (time.time(), idx)


def _cache_invalidate(cache: Dict[str, Tuple[float, Dict[str, Dict[str, Any]]]], cache_key: str) -> None:
    if cache_key in cache:
        del cache[cache_key]


def invalidate_menu_cache(orders_sh) -> None:
    ck = _cache_key_for_orders_sh(orders_sh)
    _cache_invalidate(_MENU_CACHE, ck)
    _cache_invalidate(_MENU_ADMIN_CACHE, ck)


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
        raise HTTPException(
            status_code=500,
            detail="Menu worksheet not found. Expected a tab named 'Menu' or any tab with headers: sku,name,price,active,category",
        )

    return ws


def _get_menu_context(orders_sh) -> Dict[str, Any]:
    ws = _get_menu_ws(orders_sh)

    try:
        values = ws.get_all_values()
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Cannot read Menu worksheet: {e}")

    if not values:
        raise HTTPException(status_code=500, detail="Menu worksheet is empty")

    header_row_1based = detect_header_row(values, required_headers=REQUIRED_MENU_HEADERS, max_scan=10)
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


def group_menu_by_category(menu_idx: Dict[str, Dict[str, Any]]) -> Dict[str, List[Dict[str, Any]]]:
    cats: Dict[str, List[Dict[str, Any]]] = {}

    for item in menu_idx.values():
        cat = item.get("category", "") or "Otros"
        cats.setdefault(cat, []).append(
            {
                "sku": item["sku"],
                "name": item.get("name", ""),
                "price": item.get("price", 0),
                "category": cat,
                "photo_file_id": item.get("photo_file_id", ""),
                "photo_url": item.get("photo_url", ""),
            }
        )

    for cat in cats:
        cats[cat] = sorted(cats[cat], key=lambda x: normalize(x.get("name", "")))

    return cats


def calc_total_amount(items: List[Dict[str, Any]], menu_idx: Dict[str, Dict[str, Any]]) -> float:
    total = 0.0

    for it in items:
        sku = str(it.get("sku", "")).strip()
        qty = it.get("qty", 0)

        if sku not in menu_idx:
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
    ck = _cache_key_for_orders_sh(orders_sh)

    if not force:
        cached = _cache_get(_MENU_ADMIN_CACHE, ck)
        if cached is not None:
            return cached
    else:
        _cache_invalidate(_MENU_ADMIN_CACHE, ck)

    ctx = _get_menu_context(orders_sh)
    ws = ctx["ws"]
    values = ctx["values"]
    header_row_1based = ctx["header_row_1based"]
    idx_map = ctx["idx_map"]

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

        if sku in idx:
            stats["duplicates"] += 1

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
    ws.update_cell(row_index, active_col_idx0 + 1, "TRUE" if is_active else "FALSE")

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
    ws.update_cell(row_index, price_col_idx0 + 1, price_str)

    invalidate_menu_cache(orders_sh)

    try:
        log_event("menu_product_price_updated", sku=sku, price=price_str, row_index=row_index)
    except Exception:
        pass

    updated = get_menu_product_or_404(orders_sh, sku)
    return {"ok": True, "sku": sku, "price": float(updated.get("price", 0.0))}
