# app/admin_manual_order.py — versión completa compatible, optimizada y con carrito editable

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


def _get_menu_cached(sess: Dict[str, Any], orders_sh):
    tmp = sess.setdefault("tmp", {})

    cached = tmp.get("admin_order_menu_cache")
    if cached is not None:
        return cached

    menu_idx = load_menu_admin_index(orders_sh, force=False)
    cats_raw = group_menu_admin_by_category(menu_idx)

    cats_active: Dict[str, List[Dict[str, Any]]] = {}
    for cat, items in cats_raw.items():
        only_active = [it for it in items if bool(it.get("active", False))]
        if only_active:
            cats_active[cat] = only_active

    cat_names = sorted(cats_active.keys(), key=lambda x: normalize(x))

    tmp["admin_order_menu_cache"] = (menu_idx, cats_active, cat_names)
    return tmp["admin_order_menu_cache"]


def _clear_menu_cache(sess: Dict[str, Any]) -> None:
    sess.setdefault("tmp", {}).pop("admin_order_menu_cache", None)


def _admin_order_reset(tmp: Dict[str, Any]) -> None:
    tmp.pop("admin_order_cart", None)
    tmp.pop("admin_order_step", None)
    tmp.pop("admin_order_name", None)
    tmp.pop("admin_order_contact", None)
    tmp.pop("admin_order_requested_time", None)
    tmp.pop("admin_order_categories", None)
    tmp.pop("admin_order_current_category", None)
    tmp.pop("admin_order_menu_cache", None)


def _admin_order_get_active_categories(orders_sh) -> Tuple[Dict[str, Any], Dict[str, List[Dict[str, Any]]], List[str]]:
    menu_idx = load_menu_admin_index(orders_sh, force=False)
    cats_raw = group_menu_admin_by_category(menu_idx)

    cats_active: Dict[str, List[Dict[str, Any]]] = {}
    for cat, items in cats_raw.items():
        only_active = [it for it in items if bool(it.get("active", False))]
        if only_active:
            cats_active[cat] = only_active

    cat_names = sorted(cats_active.keys(), key=lambda x: normalize(x))
    return menu_idx, cats_active, cat_names


def _admin_order_home_kb(tenant_id: str, cat_names: List[str], cats: Dict[str, List[Dict[str, Any]]]) -> Dict[str, Any]:
    rows: List[List[Tuple[str, str]]] = []

    for idx, cat in enumerate(cat_names):
        total_n = len(cats.get(cat, []))
        rows.append([(f"📂 {cat} ({total_n})", f"admord|{tenant_id}|cat|{idx}")])

    rows.append([("🛒 Ver carrito", f"admord|{tenant_id}|cart")])
    rows.append([("❌ Cancelar", f"admord|{tenant_id}|panel")])
    return kb(rows)


def _send_admin_order_home(
    bot_token: str,
    chat_id: int,
    tenant_id: str,
    orders_sh,
    sess: Dict[str, Any],
) -> bool:
    _, cats, cat_names = _get_menu_cached(sess, orders_sh)
    tmp = sess.setdefault("tmp", {})
    tmp["admin_order_categories"] = cat_names
    tmp.pop("admin_order_current_category", None)

    msg = (
        "➕ CREAR PEDIDO MANUAL\n\n"
        "Elige una categoría para agregar productos al carrito:"
    )

    return telegram_send_text(
        bot_token,
        chat_id,
        msg,
        reply_markup=_admin_order_home_kb(tenant_id, cat_names, cats),
    )


def _admin_order_category_kb(tenant_id: str, category: str, items: List[Dict[str, Any]]) -> Dict[str, Any]:
    rows: List[List[Tuple[str, str]]] = []

    for it in items[:25]:
        sku = str(it.get("sku") or "").strip()
        price_txt = fmt_price_short(it.get("price", 0))
        rows.append([(f"{it.get('name','')} — Bs {price_txt}", f"admord|{tenant_id}|prd|{sku}")])

    rows.append([("🛒 Ver carrito", f"admord|{tenant_id}|cart")])
    rows.append([("⬅️ Categorías", f"admord|{tenant_id}|home")])
    rows.append([("❌ Cancelar", f"admord|{tenant_id}|panel")])

    return kb(rows)


def _send_admin_order_category(
    bot_token: str,
    chat_id: int,
    tenant_id: str,
    orders_sh,
    sess: Dict[str, Any],
    category: str,
) -> bool:
    _, cats, _ = _get_menu_cached(sess, orders_sh)
    items = cats.get(category, [])

    tmp = sess.setdefault("tmp", {})
    tmp["admin_order_current_category"] = category

    msg = (
        f"📂 CATEGORÍA: {category}\n\n"
        "Elige un producto:"
    )

    return telegram_send_text(
        bot_token,
        chat_id,
        msg,
        reply_markup=_admin_order_category_kb(tenant_id, category, items),
    )


def _admin_order_qty_kb(tenant_id: str, sku: str) -> Dict[str, Any]:
    return kb([
        [("1", f"admord|{tenant_id}|qty|{sku}|1"), ("2", f"admord|{tenant_id}|qty|{sku}|2"),
         ("3", f"admord|{tenant_id}|qty|{sku}|3"), ("4", f"admord|{tenant_id}|qty|{sku}|4")],
        [("🛒 Ver carrito", f"admord|{tenant_id}|cart")],
        [("⬅️ Volver", f"admord|{tenant_id}|catback")],
        [("❌ Cancelar", f"admord|{tenant_id}|panel")],
    ])


def _send_admin_order_product_qty(
    bot_token: str,
    chat_id: int,
    tenant_id: str,
    orders_sh,
    sku: str,
) -> bool:
    item = get_menu_product_or_404(orders_sh, sku)
    msg = (
        "➕ AGREGAR AL PEDIDO\n\n"
        f"Producto: {item.get('name','')}\n"
        f"Precio: Bs {fmt_price_short(item.get('price', 0))}\n\n"
        "Selecciona cantidad:"
    )
    return telegram_send_text(
        bot_token,
        chat_id,
        msg,
        reply_markup=_admin_order_qty_kb(tenant_id, sku),
    )


def _admin_order_add_to_cart(tmp: Dict[str, Any], sku: str, qty: int) -> None:
    cart = tmp.get("admin_order_cart") or []
    found = False
    for it in cart:
        if str(it.get("sku") or "").strip() == sku:
            it["qty"] = int(it.get("qty") or 0) + qty
            found = True
            break
    if not found:
        cart.append({"sku": sku, "qty": qty})
    tmp["admin_order_cart"] = cart


def _admin_order_inc_item(tmp: Dict[str, Any], sku: str) -> None:
    cart = tmp.get("admin_order_cart") or []
    for it in cart:
        if str(it.get("sku") or "").strip() == sku:
            it["qty"] = max(1, int(it.get("qty") or 1) + 1)
            break
    tmp["admin_order_cart"] = cart


def _admin_order_dec_item(tmp: Dict[str, Any], sku: str) -> None:
    cart = tmp.get("admin_order_cart") or []
    new_cart = []
    for it in cart:
        if str(it.get("sku") or "").strip() == sku:
            new_qty = int(it.get("qty") or 1) - 1
            if new_qty > 0:
                it["qty"] = new_qty
                new_cart.append(it)
        else:
            new_cart.append(it)
    tmp["admin_order_cart"] = new_cart


def _admin_order_remove_item(tmp: Dict[str, Any], sku: str) -> None:
    cart = tmp.get("admin_order_cart") or []
    tmp["admin_order_cart"] = [it for it in cart if str(it.get("sku") or "").strip() != sku]


def _admin_order_time_choice_kb(tenant_id: str) -> Dict[str, Any]:
    return kb([
        [("🕒 Ahora", f"admord|{tenant_id}|timenow")],
        [("⏰ Más tarde", f"admord|{tenant_id}|timelater")],
        [("🛒 Volver al carrito", f"admord|{tenant_id}|cart")],
        [("❌ Cancelar", f"admord|{tenant_id}|panel")],
    ])


def _admin_order_cart_kb(tenant_id: str, items_snapshot: List[Dict[str, Any]], has_items: bool) -> Dict[str, Any]:
    rows: List[List[Tuple[str, str]]] = []

    for it in items_snapshot:
        sku = str(it.get("sku") or "").strip()
        name = str(it.get("name") or sku).strip()
        try:
            qty = int(it.get("qty") or 1)
        except Exception:
            qty = 1

        rows.append([(f"{name} x{qty}", f"admord|{tenant_id}|noop")])
        rows.append([
            ("➖", f"admord|{tenant_id}|dec|{sku}"),
            ("➕", f"admord|{tenant_id}|inc|{sku}"),
            ("🗑", f"admord|{tenant_id}|rem|{sku}"),
        ])

    if has_items:
        rows.append([("✅ Confirmar pedido", f"admord|{tenant_id}|confirm")])
        rows.append([("🧹 Vaciar carrito", f"admord|{tenant_id}|clear")])

    rows.append([("⬅️ Seguir agregando", f"admord|{tenant_id}|home")])
    rows.append([("❌ Cancelar", f"admord|{tenant_id}|panel")])
    return kb(rows)


def _send_admin_order_cart(
    bot_token: str,
    chat_id: int,
    tenant_id: str,
    orders_sh,
    sess: Dict[str, Any],
) -> bool:
    tmp = sess.setdefault("tmp", {})
    cart = tmp.get("admin_order_cart") or []

    menu_idx, _, _ = _get_menu_cached(sess, orders_sh)
    items_snapshot = build_items_snapshot(cart, menu_idx)
    lines_txt, total, total_qty = fmt_snapshot_lines(items_snapshot)

    msg = (
        "🛒 PEDIDO MANUAL\n\n"
        f"{lines_txt}\n\n"
        f"Resumen: {total_qty}\n"
        f"Total: Bs {total:.2f}"
    )

    return telegram_send_text(
        bot_token,
        chat_id,
        msg,
        reply_markup=_admin_order_cart_kb(tenant_id, items_snapshot, total_qty > 0),
    )
