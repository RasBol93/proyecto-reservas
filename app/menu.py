# app/menu.py

from typing import Any, Dict, List, Optional, Tuple
import re
import time

from fastapi import HTTPException

from app.sheets import get_ws, read_records_manual, detect_header_row
from app.utils import to_bool, normalize, log_event


REQUIRED_MENU_HEADERS = ["sku", "name", "price", "active", "category"]

_MENU_CACHE: Dict[str, Tuple[float, Dict[str, Dict[str, Any]]]] = {}
_MENU_ADMIN_CACHE: Dict[str, Tuple[float, Dict[str, Dict[str, Any]]]] = {}

MENU_CACHE_TTL_SECONDS = 90


# ---------------------------------------------------
# PRICE PARSER
# ---------------------------------------------------

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


# ---------------------------------------------------
# WORKSHEET
# ---------------------------------------------------

def _get_menu_ws(orders_sh):

    try:
        return get_ws(orders_sh, "Menu")
    except Exception:

        for ws in orders_sh.worksheets():

            values = ws.get_all_values()
            if not values:
                continue

            header_row = detect_header_row(values, REQUIRED_MENU_HEADERS)

            headers = values[header_row - 1]
            headers_norm = [normalize(h) for h in headers]

            if all(normalize(h) in headers_norm for h in REQUIRED_MENU_HEADERS):
                return ws

    raise HTTPException(status_code=500, detail="Menu worksheet not found")


# ---------------------------------------------------
# CACHE
# ---------------------------------------------------

def _cache_key_for_orders_sh(orders_sh):

    try:
        return str(getattr(orders_sh, "id"))
    except Exception:
        return str(id(orders_sh))


def invalidate_menu_cache(orders_sh):

    ck = _cache_key_for_orders_sh(orders_sh)

    if ck in _MENU_CACHE:
        del _MENU_CACHE[ck]

    if ck in _MENU_ADMIN_CACHE:
        del _MENU_ADMIN_CACHE[ck]


# ---------------------------------------------------
# CLIENT MENU
# ---------------------------------------------------

def load_menu_index(orders_sh, force: bool = False):

    ck = _cache_key_for_orders_sh(orders_sh)

    if not force and ck in _MENU_CACHE:
        ts, idx = _MENU_CACHE[ck]

        if time.time() - ts <= MENU_CACHE_TTL_SECONDS:
            return idx

    ws = _get_menu_ws(orders_sh)

    rows = read_records_manual(ws, REQUIRED_MENU_HEADERS)

    idx: Dict[str, Dict[str, Any]] = {}

    for r in rows:

        sku = str(r.get("sku", "")).strip()

        if not sku:
            continue

        if not to_bool(r.get("active")):
            continue

        price = _parse_price(r.get("price"))

        if price is None:
            continue

        idx[sku] = {
            "sku": sku,
            "name": r.get("name", ""),
            "price": float(price),
            "category": r.get("category", "") or "Otros",
            "photo_file_id": r.get("photo_file_id", ""),
        }

    _MENU_CACHE[ck] = (time.time(), idx)

    return idx


# ---------------------------------------------------
# ADMIN MENU
# ---------------------------------------------------

def load_menu_admin_index(orders_sh):

    ck = _cache_key_for_orders_sh(orders_sh)

    if ck in _MENU_ADMIN_CACHE:

        ts, idx = _MENU_ADMIN_CACHE[ck]

        if time.time() - ts <= MENU_CACHE_TTL_SECONDS:
            return idx

    ws = _get_menu_ws(orders_sh)

    rows = read_records_manual(ws, REQUIRED_MENU_HEADERS)

    idx = {}

    values = ws.get_all_values()
    header_row = detect_header_row(values, REQUIRED_MENU_HEADERS)

    for i, r in enumerate(rows):

        sku = str(r.get("sku", "")).strip()

        if not sku:
            continue

        idx[sku] = {
            "sku": sku,
            "name": r.get("name", ""),
            "price": float(_parse_price(r.get("price")) or 0),
            "category": r.get("category", ""),
            "active": to_bool(r.get("active")),
            "photo_file_id": r.get("photo_file_id", ""),
            "row_index": header_row + 1 + i,
        }

    _MENU_ADMIN_CACHE[ck] = (time.time(), idx)

    return idx


# ---------------------------------------------------
# GET PRODUCT
# ---------------------------------------------------

def get_menu_product_or_404(orders_sh, sku):

    idx = load_menu_admin_index(orders_sh)

    if sku not in idx:
        raise HTTPException(status_code=404, detail="Product not found")

    return idx[sku]


# ---------------------------------------------------
# UPDATE PHOTO
# ---------------------------------------------------

def set_menu_product_photo(orders_sh, sku, file_id):

    item = get_menu_product_or_404(orders_sh, sku)

    ws = _get_menu_ws(orders_sh)

    values = ws.get_all_values()

    header_row = detect_header_row(values, REQUIRED_MENU_HEADERS)

    headers = values[header_row - 1]

    photo_col = None

    for i, h in enumerate(headers):

        if normalize(h) == "photo_file_id":
            photo_col = i + 1
            break

    if photo_col is None:
        raise HTTPException(status_code=500, detail="photo_file_id column missing")

    ws.update_cell(item["row_index"], photo_col, file_id)

    invalidate_menu_cache(orders_sh)

    log_event("menu_photo_updated", sku=sku)
