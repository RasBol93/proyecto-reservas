# app/menu.py

from typing import Any, Dict, List, Optional, Tuple
import re
import time

from fastapi import HTTPException

from app.sheets import get_ws, read_records_manual, detect_header_row
from app.utils import to_bool, normalize, log_event
from app.alerts import alert_system_error, alert_tenant_error


REQUIRED_MENU_HEADERS = ["sku", "name", "price", "active", "category"]

_MENU_CACHE: Dict[str, Tuple[float, Dict[str, Dict[str, Any]]]] = {}
_MENU_ADMIN_CACHE: Dict[str, Tuple[float, Dict[str, Dict[str, Any]]]] = {}

MENU_CACHE_TTL_SECONDS = 90


def _ws_has_required_headers(ws, required_headers: List[str], max_scan_rows: int = 10) -> bool:
    try:
        values = ws.get_all_values()
    except Exception as e:
        alert_system_error(error=str(e), module="menu.ws_headers")
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
    except Exception as e:
        alert_system_error(error=str(e), module="menu.find_ws")
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


def _cache_get(cache, cache_key):
    now = time.time()
    v = cache.get(cache_key)
    if not v:
        return None
    ts, idx = v
    if MENU_CACHE_TTL_SECONDS > 0 and (now - ts) <= MENU_CACHE_TTL_SECONDS:
        return idx
    return None


def _cache_set(cache, cache_key, idx):
    cache[cache_key] = (time.time(), idx)


def _cache_invalidate(cache, cache_key):
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

    if ws is None:
        alert_tenant_error(
            error="Menu worksheet not found",
        )
        raise HTTPException(status_code=500, detail="Menu worksheet not found")

    return ws


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

        for r in rows:
            sku = str(r.get("sku", "") or "").strip()
            name = str(r.get("name", "") or "").strip()
            price_raw = str(r.get("price", "") or "").strip()

            if not sku:
                continue

            if not to_bool(r.get("active", "")):
                continue

            price = _parse_price(price_raw)
            if price is None:
                alert_tenant_error(
                    error=f"Invalid price for SKU {sku}",
                )
                continue

            idx[sku] = {
                "sku": sku,
                "name": name,
                "price": float(price),
                "category": str(r.get("category", "") or "Otros"),
                "photo_file_id": str(r.get("photo_file_id", "") or ""),
                "photo_url": str(r.get("photo_url", "") or ""),
            }

        _cache_set(_MENU_CACHE, ck, idx)
        return idx

    except Exception as e:
        alert_system_error(
            error=str(e),
            module="menu.load_menu_index",
        )
        raise


def calc_total_amount(items, menu_idx):
    total = 0.0

    for it in items:
        sku = str(it.get("sku", "")).strip()
        qty = it.get("qty", 0)

        if sku not in menu_idx:
            alert_tenant_error(
                error=f"Unknown SKU {sku} in order",
            )
            raise HTTPException(status_code=422, detail=f"Unknown sku: {sku}")

        try:
            qty_i = int(qty)
        except Exception:
            raise HTTPException(status_code=422, detail=f"Invalid qty for {sku}")

        if qty_i <= 0:
            raise HTTPException(status_code=422, detail=f"qty must be >=1 for {sku}")

        total += float(menu_idx[sku]["price"]) * qty_i

    return round(total, 2)
