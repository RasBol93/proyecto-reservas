# app/menu.py

from typing import Any, Dict, List, Optional, Tuple
import re
import time

from fastapi import HTTPException

from app.sheets import get_ws, read_records_manual
from app.utils import to_bool, normalize, log_event


REQUIRED_MENU_HEADERS = ["sku", "name", "price", "active", "category"]

# Cache simple por spreadsheet (reduce hits a Sheets)
# key -> (ts_epoch, idx)
_MENU_CACHE: Dict[str, Tuple[float, Dict[str, Dict[str, Any]]]] = {}
MENU_CACHE_TTL_SECONDS = 90  # 1.5 min


# -------------------------
# Internals
# -------------------------

def _ws_has_required_headers(ws, required_headers: List[str], max_scan_rows: int = 10) -> bool:
    """
    Verifica si una worksheet contiene TODOS los headers requeridos en alguna de las
    primeras filas (max_scan_rows). Robusto ante fila 2 traducción.
    """
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
    """
    Busca en todas las pestañas del spreadsheet una worksheet que tenga los headers del menú.
    """
    try:
        for ws in orders_sh.worksheets():
            if _ws_has_required_headers(ws, REQUIRED_MENU_HEADERS):
                return ws
    except Exception:
        return None
    return None


def _parse_price(value: Any) -> Optional[float]:
    """
    Soporta:
      - "12"
      - "12.5"
      - "12,5"
      - " 12,50 "
      - "12 Bs" / "Bs 12" / "12 BOB"
      - "12.000" (ojo: esto puede ser doce mil o doce; aquí se interpretará como 12.0 si no hay decimales)
    Regla: toma el PRIMER número encontrado.
    """
    s = str(value or "").strip()
    if not s:
        return None

    # normalizar separador decimal
    s = s.replace(",", ".")

    # extraer primer número tipo float
    m = re.search(r"(\d+(?:\.\d+)?)", s)
    if not m:
        return None

    try:
        return float(m.group(1))
    except Exception:
        return None


def _cache_key_for_orders_sh(orders_sh) -> str:
    """
    Usa id del spreadsheet si existe; si no, fallback a id(obj).
    """
    try:
        sid = getattr(orders_sh, "id", None)
        if sid:
            return str(sid)
    except Exception:
        pass
    return str(id(orders_sh))


def _cache_get(cache_key: str) -> Optional[Dict[str, Dict[str, Any]]]:
    now = time.time()
    v = _MENU_CACHE.get(cache_key)
    if not v:
        return None
    ts, idx = v
    if MENU_CACHE_TTL_SECONDS > 0 and (now - ts) <= MENU_CACHE_TTL_SECONDS:
        return idx
    return None


def _cache_set(cache_key: str, idx: Dict[str, Dict[str, Any]]) -> None:
    _MENU_CACHE[cache_key] = (time.time(), idx)


def _cache_invalidate(cache_key: str) -> None:
    if cache_key in _MENU_CACHE:
        del _MENU_CACHE[cache_key]


# -------------------------
# Public API
# -------------------------

def load_menu_index(orders_sh, force: bool = False) -> Dict[str, Dict[str, Any]]:
    """
    Lee el menú del sheet del tenant.

    Estrategia robusta:
    1) intenta abrir la pestaña "Menu"
    2) si no existe, busca cualquier pestaña que contenga los headers técnicos:
       sku, name, price, active, category

    Devuelve un índice: sku -> {sku, name, price, category}
    (solo active=TRUE)
    """
    ck = _cache_key_for_orders_sh(orders_sh)

    if not force:
        cached = _cache_get(ck)
        if cached is not None:
            return cached
    else:
        _cache_invalidate(ck)

    ws = None

    # 1) Camino rápido (nombre estándar)
    try:
        ws = get_ws(orders_sh, "Menu")
    except Exception:
        ws = None

    # 2) Fallback robusto (por headers)
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

    rows = read_records_manual(ws, required_headers=REQUIRED_MENU_HEADERS)

    idx: Dict[str, Dict[str, Any]] = {}
    stats = {
        "rows_in": len(rows),
        "active_in": 0,
        "skipped_no_sku": 0,
        "skipped_inactive": 0,
        "skipped_bad_price": 0,
        "duplicates": 0,
    }

    for r in rows:
        sku = str(r.get("sku", "") or "").strip()
        if not sku:
            stats["skipped_no_sku"] += 1
            continue

        if not to_bool(r.get("active", "")):
            stats["skipped_inactive"] += 1
            continue

        stats["active_in"] += 1

        price = _parse_price(r.get("price", ""))
        if price is None:
            stats["skipped_bad_price"] += 1
            continue

        name = str(r.get("name", "") or "").strip()
        category = str(r.get("category", "") or "").strip() or "Otros"

        if sku in idx:
            stats["duplicates"] += 1
            try:
                log_event(
                    "menu_duplicate_sku",
                    sku=sku,
                    prev=idx[sku],
                    new={"name": name, "price": float(price), "category": category},
                )
            except Exception:
                pass

        idx[sku] = {
            "sku": sku,
            "name": name,
            "price": float(price),
            "category": category,
        }

    _cache_set(ck, idx)

    # Log compacto para diagnóstico (no rompe si falla)
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
