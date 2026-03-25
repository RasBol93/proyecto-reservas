# app/admin_menu.py — optimizado (menos lecturas, mismo comportamiento)

from typing import Any, Dict, List, Tuple

from app.menu import (
    load_menu_admin_index,
    group_menu_admin_by_category,
    get_menu_product_or_404,
)
from app.telegram_api import telegram_send_text
from app.telegram_keyboard import kb
from app.utils import normalize
from app.webhook_helpers import fmt_price_short


PRICE_STEP_OPTIONS: List[Tuple[str, float]] = [
    ("-10", -10.0),
    ("-5", -5.0),
    ("-1", -1.0),
    ("+1", 1.0),
    ("+5", 5.0),
    ("+10", 10.0),
]


# -------------------------
# CORE CACHE (por request)
# -------------------------

def _get_menu_cached(sess: Dict[str, Any], orders_sh):
    tmp = sess.setdefault("tmp", {})

    if "admin_menu_cache" in tmp:
        return tmp["admin_menu_cache"]

    menu_idx = load_menu_admin_index(orders_sh, force=False)
    cats = group_menu_admin_by_category(menu_idx)

    tmp["admin_menu_cache"] = (menu_idx, cats)
    return menu_idx, cats


def _clear_menu_cache(sess: Dict[str, Any]):
    sess.setdefault("tmp", {}).pop("admin_menu_cache", None)


# -------------------------
# HOME
# -------------------------

def admin_menu_home_kb(tenant_id: str, cats: Dict[str, List[Dict[str, Any]]]) -> Dict[str, Any]:
    cat_names = sorted(cats.keys(), key=lambda x: normalize(x))
    rows: List[List[Tuple[str, str]]] = []

    for idx, cat in enumerate(cat_names):
        items = cats.get(cat, [])
        total_n = len(items)
        active_n = sum(1 for it in items if bool(it.get("active", False)))
        rows.append([(f"📂 {cat} ({active_n}/{total_n})", f"admmenu|{tenant_id}|cat|{idx}")])

    rows.append([("🔄 Refrescar menú", f"admmenu|{tenant_id}|refresh")])
    rows.append([("⬅️ Volver al panel", f"admmenu|{tenant_id}|panel")])
    return kb(rows)


def send_admin_menu_home(bot_token, chat_id, tenant_id, orders_sh, sess):
    menu_idx, cats = _get_menu_cached(sess, orders_sh)
    cat_names = sorted(cats.keys(), key=lambda x: normalize(x))

    tmp = sess.setdefault("tmp", {})
    tmp["admin_menu_categories"] = cat_names

    for k in [
        "admin_menu_current_category",
        "admin_menu_price_sku",
        "admin_menu_price_work",
        "admin_menu_input_mode",
    ]:
        tmp.pop(k, None)

    msg = (
        "⚙️ CONFIG MENÚ Y PRECIOS\n\n"
        f"Productos totales: {len(menu_idx)}\n"
        f"Productos activos: {sum(1 for v in menu_idx.values() if v.get('active'))}\n"
        f"Categorías: {len(cats)}\n\n"
        "Elige una categoría:"
    )

    return telegram_send_text(bot_token, chat_id, msg, reply_markup=admin_menu_home_kb(tenant_id, cats))


# -------------------------
# CATEGORY
# -------------------------

def send_admin_menu_category(bot_token, chat_id, tenant_id, orders_sh, sess, category):
    menu_idx, cats = _get_menu_cached(sess, orders_sh)
    items = cats.get(category, [])

    tmp = sess.setdefault("tmp", {})
    tmp["admin_menu_current_category"] = category

    for k in [
        "admin_menu_price_sku",
        "admin_menu_price_work",
        "admin_menu_input_mode",
    ]:
        tmp.pop(k, None)

    msg = (
        f"📂 CATEGORÍA: {category}\n\n"
        f"Productos: {len(items)}\n"
        f"Activos: {sum(1 for it in items if it.get('active'))}\n\n"
        "Elige un producto:"
    )

    return telegram_send_text(
        bot_token,
        chat_id,
        msg,
        reply_markup=admin_menu_category_kb(tenant_id, category, items),
    )


def admin_menu_category_kb(tenant_id, category, items):
    rows = []

    for it in items[:25]:
        emoji = "🟢" if it.get("active") else "🔴"
        rows.append([(f"{emoji} {it['name']} — Bs {fmt_price_short(it['price'])}", f"admmenu|{tenant_id}|prd|{it['sku']}")])

    rows.append([("🔄 Refrescar categoría", f"admmenu|{tenant_id}|catrefresh")])
    rows.append([("⬅️ Categorías", f"admmenu|{tenant_id}|home")])
    return kb(rows)


# -------------------------
# PRODUCT
# -------------------------

def send_admin_menu_product_detail(bot_token, chat_id, tenant_id, orders_sh, sess, sku):
    item = get_menu_product_or_404(orders_sh, sku)

    tmp = sess.setdefault("tmp", {})
    tmp["admin_menu_last_sku"] = sku

    for k in ["admin_menu_price_sku", "admin_menu_price_work", "admin_menu_input_mode"]:
        tmp.pop(k, None)

    msg = (
        "🧾 DETALLE DE PRODUCTO\n\n"
        f"{item['name']}\n"
        f"Precio: Bs {fmt_price_short(item['price'])}\n"
        f"Activo: {'Sí' if item['active'] else 'No'}\n"
    )

    return telegram_send_text(
        bot_token,
        chat_id,
        msg,
        reply_markup=admin_menu_product_kb(tenant_id, sku, item["active"]),
    )


def admin_menu_product_kb(tenant_id, sku, active):
    toggle_label = "⛔ Desactivar" if active else "✅ Activar"

    return kb([
        [(toggle_label, f"admmenu|{tenant_id}|toggle|{sku}")],
        [("💲 Ajustar precio", f"admmenu|{tenant_id}|price|{sku}")],
        [("⬅️ Volver", f"admmenu|{tenant_id}|catback")],
    ])
