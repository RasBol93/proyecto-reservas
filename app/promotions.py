# app/promotions.py — lectura robusta de promociones (v1)

from typing import Any, Dict, List, Optional
import json
import time

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


_PROMO_CACHE: Dict[str, tuple] = {}
PROMO_CACHE_TTL_SECONDS = 60


# -------------------------
# helpers
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


def _get_promotions_ws(orders_sh):
    try:
        return get_ws(orders_sh, "Promotions")
    except Exception:
        alert_system_error(error="Promotions sheet not found", module="promotions")
        raise


def _safe_float(v: Any) -> float:
    try:
        return float(v)
    except Exception:
        return 0.0


def _safe_json(v: Any) -> Any:
    if not v:
        return []
    try:
        return json.loads(v)
    except Exception:
        return []


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
                    "type": str(r.get("type") or "").strip(),
                    "active": to_bool(r.get("active")),
                    "product_sku": str(r.get("product_sku") or "").strip(),
                    "combo_items": _safe_json(r.get("combo_items_json")),
                    "original_price": _safe_float(r.get("original_price")),
                    "promo_price": _safe_float(r.get("promo_price")),
                    "description": str(r.get("description") or "").strip(),
                    "sort_order": int(r.get("sort_order") or 0),
                }

                if not promo["promo_id"]:
                    continue

                promos.append(promo)

            except Exception as e:
                log_event(
                    "promo_parse_error",
                    error=str(e),
                    raw=r,
                )
                continue

        # ordenar
        promos = sorted(promos, key=lambda x: x.get("sort_order", 0))

        _cache_set(ck, promos)

        return promos

    except Exception as e:
        alert_system_error(error=str(e), module="promotions.load")
        return []


# -------------------------
# public helpers
# -------------------------

def get_active_promotions(orders_sh) -> List[Dict[str, Any]]:
    promos = load_promotions(orders_sh, force=False)
    return [p for p in promos if p.get("active")]


def build_promotions_display(promos: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """
    Devuelve promos listas para UI (texto bonito tipo app)
    """

    result: List[Dict[str, Any]] = []

    for p in promos:
        name = p.get("name", "")
        original = p.get("original_price", 0)
        promo = p.get("promo_price", 0)

        text = f"{name} — de Bs {int(original)} a Bs {int(promo)}"

        result.append({
            "promo_id": p["promo_id"],
            "text": text,
            "price": promo,
            "type": p.get("type"),
        })

    return result
