# app/admin_manual_order.py — optimizado (menos lecturas, más simple)

from typing import Any, Dict, List, Tuple

from app.menu import (
    load_menu_admin_index,
    group_menu_admin_by_category,
    get_menu_product_or_404,
)
from app.orders import build_items_snapshot
from app.telegram_api import telegram_send_text
from app.telegram_keyboard import kb
from app.utils import normalize
from app.webhook_helpers import fmt_price_short, fmt_snapshot_lines


# -------------------------
# CACHE
# -------------------------

def _get_menu_cached(sess, orders_sh):
    tmp = sess.setdefault("tmp", {})

    if "admin_order_menu_cache" in tmp:
        return tmp["admin_order_menu_cache"]

    menu_idx = load_menu_admin_index(orders_sh, force=False)
    cats_raw = group_menu_admin_by_category(menu_idx)

    cats_active = {
        cat: [it for it in items if it.get("active")]
        for cat, items in cats_raw.items()
        if any(it.get("active") for it in items)
    }

    cat_names = sorted(cats_active.keys(), key=lambda x: normalize(x))

    tmp["admin_order_menu_cache"] = (menu_idx, cats_active, cat_names)
    return tmp["admin_order_menu_cache"]


def _clear_menu_cache(sess):
    sess.setdefault("tmp", {}).pop("admin_order_menu_cache", None)


# -------------------------
# RESET
# -------------------------

def _admin_order_reset(tmp):
    for k in [
        "admin_order_cart",
        "admin_order_step",
        "admin_order_name",
        "admin_order_contact",
        "admin_order_requested_time",
        "admin_order_categories",
        "admin_order_current_category",
        "admin_order_menu_cache",
    ]:
        tmp.pop(k, None)


# -------------------------
# HOME
# -------------------------

def _send_admin_order_home(bot_token, chat_id, tenant_id, orders_sh, sess):
    _, cats, cat_names = _get_menu_cached(sess, orders_sh)

    tmp = sess.setdefault("tmp", {})
    tmp["admin_order_categories"] = cat_names

    msg = "➕ CREAR PEDIDO MANUAL\n\nElige una categoría:"

    rows = [[(f"📂 {c} ({len(cats[c])})", f"admord|{tenant_id}|cat|{i}")]
            for i, c in enumerate(cat_names)]

    rows += [
        [("🛒 Ver carrito", f"admord|{tenant_id}|cart")],
        [("❌ Cancelar", f"admord|{tenant_id}|panel")]
    ]

    return telegram_send_text(bot_token, chat_id, msg, reply_markup=kb(rows))


# -------------------------
# CATEGORY
# -------------------------

def _send_admin_order_category(bot_token, chat_id, tenant_id, orders_sh, sess, category):
    _, cats, _ = _get_menu_cached(sess, orders_sh)
    items = cats.get(category, [])

    msg = f"📂 {category}\n\nElige producto:"

    rows = [[(f"{it['name']} — Bs {fmt_price_short(it['price'])}",
              f"admord|{tenant_id}|prd|{it['sku']}")]
            for it in items[:25]]

    rows += [
        [("🛒 Ver carrito", f"admord|{tenant_id}|cart")],
        [("⬅️ Categorías", f"admord|{tenant_id}|home")]
    ]

    return telegram_send_text(bot_token, chat_id, msg, reply_markup=kb(rows))


# -------------------------
# PRODUCT QTY
# -------------------------

def _send_admin_order_product_qty(bot_token, chat_id, tenant_id, orders_sh, sku):
    item = get_menu_product_or_404(orders_sh, sku)

    msg = (
        f"{item['name']}\n"
        f"Bs {fmt_price_short(item['price'])}\n\n"
        "Cantidad:"
    )

    return telegram_send_text(
        bot_token,
        chat_id,
        msg,
        reply_markup=kb([
            [("1", f"admord|{tenant_id}|qty|{sku}|1"),
             ("2", f"admord|{tenant_id}|qty|{sku}|2"),
             ("3", f"admord|{tenant_id}|qty|{sku}|3")],
            [("🛒 Carrito", f"admord|{tenant_id}|cart")]
        ])
    )


# -------------------------
# CART
# -------------------------

def _admin_order_add_to_cart(tmp, sku, qty):
    cart = tmp.get("admin_order_cart") or []

    for it in cart:
        if it["sku"] == sku:
            it["qty"] += qty
            break
    else:
        cart.append({"sku": sku, "qty": qty})

    tmp["admin_order_cart"] = cart


def _send_admin_order_cart(bot_token, chat_id, tenant_id, orders_sh, sess):
    tmp = sess.setdefault("tmp", {})
    cart = tmp.get("admin_order_cart") or []

    menu_idx, _, _ = _get_menu_cached(sess, orders_sh)

    snapshot = build_items_snapshot(cart, menu_idx)
    lines_txt, total, total_qty = fmt_snapshot_lines(snapshot)

    msg = (
        f"🛒 PEDIDO\n\n"
        f"{lines_txt}\n\n"
        f"Total: Bs {total:.2f}"
    )

    return telegram_send_text(
        bot_token,
        chat_id,
        msg,
        reply_markup=kb([
            [("✅ Confirmar", f"admord|{tenant_id}|confirm")],
            [("⬅️ Seguir", f"admord|{tenant_id}|home")]
        ])
    )
