# app/menu.py

from typing import Any, Dict, List

from fastapi import HTTPException

from app.sheets import get_ws, read_records_manual, to_bool, normalize


# -------------------------
# Menu index
# -------------------------

def load_menu_index(orders_sh) -> Dict[str, Dict[str, Any]]:
    """
    Lee la hoja 'Menu' del sheet del tenant.
    Espera headers técnicos:
      sku, name, price, active, category
    """
    ws = get_ws(orders_sh, "Menu")
    rows = read_records_manual(
        ws,
        required_headers=["sku", "name", "price", "active", "category"],
    )

    idx: Dict[str, Dict[str, Any]] = {}

    for r in rows:
        sku = str(r.get("sku", "")).strip()
        if not sku:
            continue

        if not to_bool(r.get("active", "")):
            continue

        price_raw = str(r.get("price", "")).strip()

        try:
            price = float(price_raw)
        except Exception:
            continue

        idx[sku] = {
            "sku": sku,
            "name": r.get("name", ""),
            "price": price,
            "category": r.get("category", "") or "Otros",
        }

    return idx


# -------------------------
# Agrupar por categoría
# -------------------------

def group_menu_by_category(menu_idx: Dict[str, Dict[str, Any]]) -> Dict[str, List[Dict[str, Any]]]:
    cats: Dict[str, List[Dict[str, Any]]] = {}

    for item in menu_idx.values():
        cat = item.get("category", "") or "Otros"

        cats.setdefault(cat, []).append({
            "sku": item["sku"],
            "name": item.get("name", ""),
            "price": item.get("price", 0),
            "category": cat,
        })

    # ordenar productos por nombre normalizado
    for cat in cats:
        cats[cat] = sorted(
            cats[cat],
            key=lambda x: normalize(x.get("name", ""))
        )

    return cats


# -------------------------
# Calcular total
# -------------------------

def calc_total_amount(items: List[Dict[str, Any]], menu_idx: Dict[str, Dict[str, Any]]) -> float:
    total = 0.0

    for it in items:
        sku = str(it.get("sku", "")).strip()
        qty = it.get("qty", 0)

        if sku not in menu_idx:
            raise HTTPException(status_code=422, detail=f"Unknown sku: {sku}")

        qty_i = int(qty)
        if qty_i <= 0:
            raise HTTPException(status_code=422, detail=f"qty must be >= 1 for sku={sku}")

        total += float(menu_idx[sku]["price"]) * qty_i

    return round(total, 2)
