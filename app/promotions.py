# app/promotions.py — lectura y gestión robusta de promociones (v2)

from typing import Any, Dict, List, Optional
import json
import time

from fastapi import HTTPException

from app.sheets import get_ws, read_records_manual
from app.utils import normalize, to_bool, log_event
from app.alerts import alert_system_error


REQUIRED_PROMO_HEADERS = [
    "promo_id",
    "name",
    "type",
    "active",
    "product_sku",
    "combo_items_json",
    "original_price",
    "promo_price",
    "description",
    "sort_order",
    "created_at",
]

ALLOWED_PROMO_TYPES = {
    "discount",
    "combo",
}

_PROMO_CACHE: Dict[str, tuple] = {}
PROMO_CACHE_TTL_SECONDS = 60


# -------------------------
# cache helpers
# -------------------------

def _cache_key(orders_sh) -> str:
    try:
        return str(getattr(orders_sh, "id", None) or id(orders_sh))
    except Exception:
        return str(id(orders_sh))


def _cache_get(cache_key: str):
    v = _PROMO_CACHE.get(cache_key)
    if not v:
        return None

    ts, data = v
    if (time.time() - ts) <= PROMO_CACHE_TTL_SECONDS:
        return data

    return None


def _cache_set(cache_key: str, data):
    _PROMO_CACHE[cache_key] = (time.time(), data)


def invalidate_promotions_cache(orders_sh):
    ck = _cache_key(orders_sh)
    _PROMO_CACHE.pop(ck, None)


def invalidate_all_promotions_cache():
    _PROMO_CACHE.clear()


# -------------------------
# worksheet helpers
# -------------------------

def _get_promotions_ws(orders_sh):
    try:
        return get_ws(orders_sh, "Promotions")
    except Exception:
        alert_system_error(error="Promotions sheet not found", module="promotions")
        raise HTTPException(status_code=500, detail="Promotions sheet not found")


def _get_header(ws) -> List[str]:
    try:
        hdr = ws.row_values(1)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Cannot read Promotions header: {e}")
    return [str(h).strip() for h in hdr if str(h).strip()]


def _find_col_idx(header: List[str], col_name: str) -> Optional[int]:
    wanted = str(col_name or "").strip()
    for i, h in enumerate(header):
        if str(h).strip() == wanted:
            return i
    return None


def _build_row_by_header(header: List[str], data: Dict[str, Any]) -> List[str]:
    row: List[str] = [""] * len(header)

    for key, value in (data or {}).items():
        idx = _find_col_idx(header, key)
        if idx is None:
            continue

        if isinstance(value, (dict, list)):
            row[idx] = json.dumps(value, ensure_ascii=False)
        elif value is None:
            row[idx] = ""
        else:
            row[idx] = str(value)

    return row


def _safe_row_values(ws, row_index: int) -> List[Any]:
    try:
        return ws.row_values(row_index)
    except Exception:
        return []


def _safe_col_values(ws, col_index_1based: int) -> List[Any]:
    try:
        return ws.col_values(col_index_1based)
    except Exception:
        return []


def _col_to_a1(col_1based: int) -> str:
    result = ""
    n = int(col_1based)
    while n > 0:
        n, rem = divmod(n - 1, 26)
        result = chr(65 + rem) + result
    return result


def _cell_a1(row_1based: int, col_1based: int) -> str:
    return f"{_col_to_a1(col_1based)}{row_1based}"


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


def _batch_write_cells(ws, updates: List[Dict[str, Any]]) -> None:
    if not updates:
        return

    data = []
    for u in updates:
        row = int(u["row"])
        col = int(u["col"])
        value = str(u.get("value", ""))
        data.append({
            "range": _cell_a1(row, col),
            "values": [[value]],
        })

    ws.batch_update(data, value_input_option="RAW")


def _find_next_empty_row(ws, header_len: int) -> int:
    if header_len <= 0:
        return 2

    try:
        values = ws.get_all_values()
    except Exception:
        values = []

    if not values or len(values) == 1:
        return 2

    for idx_0based, row in enumerate(values[1:], start=2):
        slice_row = row[:header_len]
        if not any(str(cell).strip() for cell in slice_row):
            return idx_0based

    return len(values) + 1


# -------------------------
# parsing / validation helpers
# -------------------------

def _safe_float(v: Any) -> float:
    try:
        return float(v)
    except Exception:
        return 0.0


def _safe_int(v: Any) -> int:
    try:
        return int(v)
    except Exception:
        return 0


def _safe_json(v: Any) -> Any:
    if not v:
        return []
    if isinstance(v, (list, dict)):
        return v
    try:
        return json.loads(v)
    except Exception:
        return []


def _promo_type(value: Any) -> str:
    return str(value or "").strip().lower()


def _validate_promo_type(value: Any) -> str:
    t = _promo_type(value)
    if t not in ALLOWED_PROMO_TYPES:
        raise HTTPException(status_code=422, detail=f"Invalid promotion type: {t}")
    return t


def _validate_name(value: Any) -> str:
    name = str(value or "").strip()
    if not name:
        raise HTTPException(status_code=422, detail="Promotion name is required")
    return name


def _validate_price(value: Any, field_name: str) -> float:
    try:
        n = float(value)
    except Exception:
        raise HTTPException(status_code=422, detail=f"Invalid {field_name}")

    if n < 0:
        raise HTTPException(status_code=422, detail=f"{field_name} must be >= 0")

    return round(n, 2)


def _format_price_for_sheet(value: float) -> str:
    n = round(float(value), 2)
    if n < 0:
        raise HTTPException(status_code=422, detail="price must be >= 0")

    s = f"{n:.2f}"
    if s.endswith("00"):
        return str(int(round(n)))
    if s.endswith("0"):
        return s[:-1]
    return s


def _build_discount_description(name: str, original_price: float, promo_price: float) -> str:
    return f"{name} de Bs {_format_price_for_sheet(original_price)} a Bs {_format_price_for_sheet(promo_price)}"


def _build_combo_description(combo_items: List[Dict[str, Any]], original_price: float, promo_price: float) -> str:
    names = []
    for it in combo_items[:3]:
        nm = str(it.get("name") or it.get("sku") or "").strip()
        if nm:
            names.append(nm)

    items_txt = " + ".join(names) if names else "Combo"
    return f"{items_txt} de Bs {_format_price_for_sheet(original_price)} a Bs {_format_price_for_sheet(promo_price)}"


def _promo_sort_key(p: Dict[str, Any]):
    return (
        int(p.get("sort_order") or 0),
        normalize(str(p.get("name") or "")),
    )


# -------------------------
# row / lookup helpers
# -------------------------

def _find_row_index_by_promo_id(ws, promo_id: str) -> Optional[int]:
    header = _get_header(ws)
    if not header:
        return None

    promo_col = _find_col_idx(header, "promo_id")
    if promo_col is None:
        return None

    values = _safe_col_values(ws, promo_col + 1)
    target = str(promo_id or "").strip()
    if not target:
        return None

    for i in range(1, len(values)):
        if str(values[i]).strip() == target:
            return i + 1

    return None


def _row_to_dict(header: List[str], row: List[Any]) -> Dict[str, Any]:
    d: Dict[str, Any] = {}
    for i, h in enumerate(header):
        d[h] = row[i] if i < len(row) else ""
    return d


def get_promotion_by_id(orders_sh, promo_id: str) -> Optional[Dict[str, Any]]:
    ws = _get_promotions_ws(orders_sh)
    header = _get_header(ws)
    if not header:
        return None

    ridx = _find_row_index_by_promo_id(ws, promo_id)
    if ridx is None:
        return None

    row = _safe_row_values(ws, ridx)
    if not row:
        return None

    raw = _row_to_dict(header, row)
    return {
        "promo_id": str(raw.get("promo_id") or "").strip(),
        "name": str(raw.get("name") or "").strip(),
        "type": _promo_type(raw.get("type")),
        "active": to_bool(raw.get("active")),
        "product_sku": str(raw.get("product_sku") or "").strip(),
        "combo_items": _safe_json(raw.get("combo_items_json")),
        "original_price": _safe_float(raw.get("original_price")),
        "promo_price": _safe_float(raw.get("promo_price")),
        "description": str(raw.get("description") or "").strip(),
        "sort_order": _safe_int(raw.get("sort_order")),
        "created_at": str(raw.get("created_at") or "").strip(),
        "row_index": ridx,
    }


# -------------------------
# core loader
# -------------------------

def load_promotions(orders_sh, force: bool = False) -> List[Dict[str, Any]]:
    ck = _cache_key(orders_sh)

    if not force:
        cached = _cache_get(ck)
        if cached is not None:
            return cached

    try:
        ws = _get_promotions_ws(orders_sh)
        rows = read_records_manual(ws, required_headers=REQUIRED_PROMO_HEADERS)

        promos: List[Dict[str, Any]] = []

        for r in rows:
            try:
                promo = {
                    "promo_id": str(r.get("promo_id") or "").strip(),
                    "name": str(r.get("name") or "").strip(),
                    "type": _promo_type(r.get("type")),
                    "active": to_bool(r.get("active")),
                    "product_sku": str(r.get("product_sku") or "").strip(),
                    "combo_items": _safe_json(r.get("combo_items_json")),
                    "original_price": _safe_float(r.get("original_price")),
                    "promo_price": _safe_float(r.get("promo_price")),
                    "description": str(r.get("description") or "").strip(),
                    "sort_order": _safe_int(r.get("sort_order")),
                    "created_at": str(r.get("created_at") or "").strip(),
                }

                if not promo["promo_id"]:
                    continue

                if promo["type"] not in ALLOWED_PROMO_TYPES:
                    continue

                promos.append(promo)

            except Exception as e:
                log_event(
                    "promo_parse_error",
                    error=str(e),
                    raw=r,
                )
                continue

        promos = sorted(promos, key=_promo_sort_key)
        _cache_set(ck, promos)
        return promos

    except Exception as e:
        alert_system_error(error=str(e), module="promotions.load")
        return []


# -------------------------
# public read helpers
# -------------------------

def get_active_promotions(orders_sh) -> List[Dict[str, Any]]:
    promos = load_promotions(orders_sh, force=False)
    return [p for p in promos if p.get("active")]


def get_inactive_promotions(orders_sh) -> List[Dict[str, Any]]:
    promos = load_promotions(orders_sh, force=False)
    return [p for p in promos if not p.get("active")]


def build_promotions_display(promos: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """
    Devuelve promos listas para UI (texto bonito tipo app)
    """
    result: List[Dict[str, Any]] = []

    for p in promos:
        name = str(p.get("name") or "").strip()
        original = float(p.get("original_price") or 0)
        promo = float(p.get("promo_price") or 0)

        original_txt = _format_price_for_sheet(original)
        promo_txt = _format_price_for_sheet(promo)

        text = f"{name} — de Bs {original_txt} a Bs {promo_txt}"

        result.append({
            "promo_id": str(p.get("promo_id") or "").strip(),
            "text": text,
            "price": promo,
            "type": str(p.get("type") or "").strip(),
            "active": bool(p.get("active")),
        })

    return result


# -------------------------
# id / sort helpers
# -------------------------

def generate_promo_id() -> str:
    import secrets
    return f"promo_{secrets.token_hex(4)}"


def _get_next_sort_order(orders_sh) -> int:
    promos = load_promotions(orders_sh, force=True)
    if not promos:
        return 1
    return max(int(p.get("sort_order") or 0) for p in promos) + 1


# -------------------------
# create helpers
# -------------------------

def create_discount_promotion(
    orders_sh,
    name: str,
    product_sku: str,
    original_price: float,
    promo_price: float,
    description: str = "",
    active: bool = True,
) -> Dict[str, Any]:
    ws = _get_promotions_ws(orders_sh)
    header = _get_header(ws)
    if not header:
        raise HTTPException(status_code=500, detail="Promotions header row missing")

    clean_name = _validate_name(name)
    clean_type = _validate_promo_type("discount")
    clean_product_sku = str(product_sku or "").strip()
    if not clean_product_sku:
        raise HTTPException(status_code=422, detail="product_sku is required")

    original_price_num = _validate_price(original_price, "original_price")
    promo_price_num = _validate_price(promo_price, "promo_price")

    if promo_price_num > original_price_num:
        raise HTTPException(status_code=422, detail="promo_price cannot be greater than original_price")

    promo_id = generate_promo_id()
    sort_order = _get_next_sort_order(orders_sh)
    description_txt = str(description or "").strip()
    if not description_txt:
        description_txt = _build_discount_description(clean_name, original_price_num, promo_price_num)

    data = {
        "promo_id": promo_id,
        "name": clean_name,
        "type": clean_type,
        "active": "TRUE" if active else "FALSE",
        "product_sku": clean_product_sku,
        "combo_items_json": "",
        "original_price": _format_price_for_sheet(original_price_num),
        "promo_price": _format_price_for_sheet(promo_price_num),
        "description": description_txt,
        "sort_order": sort_order,
        "created_at": str(int(time.time())),
    }

    row = _build_row_by_header(header, data)
    next_row = _find_next_empty_row(ws, len(header))
    _write_full_row(ws, next_row, row)

    invalidate_promotions_cache(orders_sh)

    created = get_promotion_by_id(orders_sh, promo_id)
    if not created:
        raise HTTPException(status_code=500, detail="Promotion created but could not be reloaded")

    try:
        log_event(
            "promotion_created",
            promo_id=promo_id,
            promo_type="discount",
            product_sku=clean_product_sku,
            promo_price=promo_price_num,
        )
    except Exception:
        pass

    return created


def create_combo_promotion(
    orders_sh,
    name: str,
    combo_items: List[Dict[str, Any]],
    original_price: float,
    promo_price: float,
    description: str = "",
    active: bool = True,
) -> Dict[str, Any]:
    ws = _get_promotions_ws(orders_sh)
    header = _get_header(ws)
    if not header:
        raise HTTPException(status_code=500, detail="Promotions header row missing")

    clean_name = _validate_name(name)
    clean_type = _validate_promo_type("combo")

    if not isinstance(combo_items, list) or not combo_items:
        raise HTTPException(status_code=422, detail="combo_items must be a non-empty list")

    normalized_items: List[Dict[str, Any]] = []
    for it in combo_items:
        sku = str(it.get("sku") or "").strip()
        if not sku:
            raise HTTPException(status_code=422, detail="Each combo item must include sku")

        try:
            qty = int(it.get("qty") or 1)
        except Exception:
            qty = 1
        qty = max(1, qty)

        name_txt = str(it.get("name") or "").strip()

        normalized_items.append({
            "sku": sku,
            "qty": qty,
            "name": name_txt,
        })

    original_price_num = _validate_price(original_price, "original_price")
    promo_price_num = _validate_price(promo_price, "promo_price")

    if promo_price_num > original_price_num:
        raise HTTPException(status_code=422, detail="promo_price cannot be greater than original_price")

    promo_id = generate_promo_id()
    sort_order = _get_next_sort_order(orders_sh)
    description_txt = str(description or "").strip()
    if not description_txt:
        description_txt = _build_combo_description(normalized_items, original_price_num, promo_price_num)

    data = {
        "promo_id": promo_id,
        "name": clean_name,
        "type": clean_type,
        "active": "TRUE" if active else "FALSE",
        "product_sku": "",
        "combo_items_json": normalized_items,
        "original_price": _format_price_for_sheet(original_price_num),
        "promo_price": _format_price_for_sheet(promo_price_num),
        "description": description_txt,
        "sort_order": sort_order,
        "created_at": str(int(time.time())),
    }

    row = _build_row_by_header(header, data)
    next_row = _find_next_empty_row(ws, len(header))
    _write_full_row(ws, next_row, row)

    invalidate_promotions_cache(orders_sh)

    created = get_promotion_by_id(orders_sh, promo_id)
    if not created:
        raise HTTPException(status_code=500, detail="Promotion created but could not be reloaded")

    try:
        log_event(
            "promotion_created",
            promo_id=promo_id,
            promo_type="combo",
            promo_price=promo_price_num,
        )
    except Exception:
        pass

    return created


# -------------------------
# update helpers
# -------------------------

def set_promotion_active(orders_sh, promo_id: str, is_active: bool) -> Dict[str, Any]:
    ws = _get_promotions_ws(orders_sh)
    header = _get_header(ws)
    ridx = _find_row_index_by_promo_id(ws, promo_id)

    if ridx is None:
        raise HTTPException(status_code=404, detail=f"Promotion not found: {promo_id}")

    active_col = _find_col_idx(header, "active")
    if active_col is None:
        raise HTTPException(status_code=500, detail="Missing 'active' column in Promotions")

    _batch_write_cells(ws, [
        {"row": ridx, "col": active_col + 1, "value": "TRUE" if is_active else "FALSE"},
    ])

    invalidate_promotions_cache(orders_sh)

    updated = get_promotion_by_id(orders_sh, promo_id)
    if not updated:
        raise HTTPException(status_code=500, detail="Promotion updated but could not be reloaded")

    try:
        log_event(
            "promotion_active_updated",
            promo_id=promo_id,
            active=bool(is_active),
        )
    except Exception:
        pass

    return updated


def delete_promotion(orders_sh, promo_id: str) -> Dict[str, Any]:
    ws = _get_promotions_ws(orders_sh)
    ridx = _find_row_index_by_promo_id(ws, promo_id)

    if ridx is None:
        raise HTTPException(status_code=404, detail=f"Promotion not found: {promo_id}")

    try:
        ws.delete_rows(ridx)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Could not delete promotion: {e}")

    invalidate_promotions_cache(orders_sh)

    try:
        log_event("promotion_deleted", promo_id=promo_id)
    except Exception:
        pass

    return {"ok": True, "promo_id": promo_id}
