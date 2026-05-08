from typing import Any, Dict, List, Tuple

from app.menu import (
    load_menu_admin_index,
    group_menu_admin_by_category,
    get_menu_product_or_404,
)
from app.telegram_api import telegram_send_text
from app.telegram_keyboard import kb
from app.webhook_helpers import fmt_price_short


PRICE_STEP_OPTIONS: List[Tuple[str, float]] = [
    ("-10", -10.0),
    ("-5", -5.0),
    ("-1", -1.0),
    ("+1", 1.0),
    ("+5", 5.0),
    ("+10", 10.0),
]


def _get_menu_cached(sess: Dict[str, Any], orders_sh):
    tmp = sess.setdefault("tmp", {})

    cached = tmp.get("admin_menu_cache")
    if cached is not None:
        return cached

    menu_idx = load_menu_admin_index(orders_sh, force=False)
    cats = group_menu_admin_by_category(menu_idx, orders_sh=orders_sh)
    cat_names = list(cats.keys())

    tmp["admin_menu_cache"] = (menu_idx, cats, cat_names)
    return tmp["admin_menu_cache"]


def _clear_menu_cache(sess: Dict[str, Any]) -> None:
    sess.setdefault("tmp", {}).pop("admin_menu_cache", None)


def _rebuild_ordered_cats(
    cats: Dict[str, List[Dict[str, Any]]],
    ordered_names: List[str],
) -> Dict[str, List[Dict[str, Any]]]:
    ordered: Dict[str, List[Dict[str, Any]]] = {}

    for cat_name in ordered_names or []:
        if cat_name in cats:
            ordered[cat_name] = cats[cat_name]

    for cat_name, items in cats.items():
        if cat_name not in ordered:
            ordered[cat_name] = items

    return ordered


def admin_menu_home_kb(tenant_id: str, cats: Dict[str, List[Dict[str, Any]]]) -> Dict[str, Any]:
    cat_names = list(cats.keys())
    rows: List[List[Tuple[str, str]]] = []

    for idx, cat in enumerate(cat_names):
        items = cats.get(cat, [])
        total_n = len(items)
        active_n = sum(1 for it in items if bool(it.get("active", False)))
        rows.append([(f"📂 {cat} ({active_n}/{total_n})", f"admmenu|{tenant_id}|cat|{idx}")])

    rows.append([("↕️ Orden de categorías en app de pedidos", f"admmenu|{tenant_id}|catorder")])
    rows.append([("➕ Crear producto", f"admmenu|{tenant_id}|create_product")])
    rows.append([("⬅️ Volver al panel", f"admmenu|{tenant_id}|panel")])
    return kb(rows)


def send_admin_menu_home(bot_token: str, chat_id: int, tenant_id: str, orders_sh, sess: Dict[str, Any]) -> bool:
    menu_idx, cats, cat_names = _get_menu_cached(sess, orders_sh)

    tmp = sess.setdefault("tmp", {})
    tmp["admin_menu_categories"] = cat_names
    tmp.pop("admin_menu_current_category", None)
    tmp.pop("admin_menu_price_sku", None)
    tmp.pop("admin_menu_price_work", None)
    tmp.pop("admin_menu_input_mode", None)

    total_products = len(menu_idx)
    total_active = sum(1 for v in menu_idx.values() if bool(v.get("active", False)))
    total_categories = len(cats)

    msg = (
        "⚙️ CONFIG MENÚ Y PRECIOS\n\n"
        f"Productos totales: {total_products}\n"
        f"Productos activos: {total_active}\n"
        f"Categorías: {total_categories}\n\n"
        "Elige una categoría o crea un producto nuevo:"
    )

    return telegram_send_text(
        bot_token,
        chat_id,
        msg,
        reply_markup=admin_menu_home_kb(tenant_id, cats),
    )


def _build_category_order_lines(cat_names: List[str], selected_idx: int = -1) -> List[str]:
    lines: List[str] = []

    for idx, cat_name in enumerate(cat_names or [], start=1):
        marker = "➡️ " if (idx - 1) == selected_idx else ""
        lines.append(f"{idx}. {marker}{cat_name}")

    return lines


def admin_menu_category_order_kb(
    tenant_id: str,
    cat_names: List[str],
    *,
    selected_idx: int = -1,
) -> Dict[str, Any]:
    rows: List[List[Tuple[str, str]]] = []

    for idx, cat_name in enumerate(cat_names[:20]):
        label = f"{idx + 1}. {cat_name}"
        if idx == selected_idx:
            label = f"➡️ {label}"
        rows.append([(label, f"admmenu|{tenant_id}|catorder_pick|{idx}")])

    rows.append([
        ("⬆️ Subir", f"admmenu|{tenant_id}|catorder_up"),
        ("⬇️ Bajar", f"admmenu|{tenant_id}|catorder_down"),
    ])
    rows.append([("✅ Guardar orden", f"admmenu|{tenant_id}|catorder_save")])
    rows.append([("🔄 Resetear", f"admmenu|{tenant_id}|catorder_reset")])
    rows.append([("⬅️ Volver", f"admmenu|{tenant_id}|home")])
    return kb(rows)


def send_admin_menu_category_order(
    bot_token: str,
    chat_id: int,
    tenant_id: str,
    sess: Dict[str, Any],
) -> bool:
    tmp = sess.setdefault("tmp", {})
    cat_names = list(tmp.get("admin_menu_order_categories") or [])
    selected_idx_raw = tmp.get("admin_menu_order_selected_idx")
    try:
        selected_idx = int(selected_idx_raw)
    except Exception:
        selected_idx = -1

    if not cat_names:
        return telegram_send_text(
            bot_token,
            chat_id,
            "No hay categorías disponibles para ordenar.",
            reply_markup=kb([
                [("⬅️ Volver al menú", f"admmenu|{tenant_id}|home")],
            ]),
        )

    selected_line = ""
    if 0 <= selected_idx < len(cat_names):
        selected_line = f"\nCategoría seleccionada: {cat_names[selected_idx]}"

    msg = (
        "↕️ ORDEN DE CATEGORÍAS\n\n"
        "Orden actual:\n"
        + "\n".join(_build_category_order_lines(cat_names, selected_idx))
        + f"{selected_line}\n\n"
        "Elige una categoría y usa los botones para subirla o bajarla."
    )

    return telegram_send_text(
        bot_token,
        chat_id,
        msg,
        reply_markup=admin_menu_category_order_kb(
            tenant_id,
            cat_names,
            selected_idx=selected_idx,
        ),
    )


def admin_menu_category_kb(tenant_id: str, category: str, items: List[Dict[str, Any]]) -> Dict[str, Any]:
    rows: List[List[Tuple[str, str]]] = []

    for it in items[:25]:
        emoji = "🟢" if bool(it.get("active", False)) else "🔴"
        price_txt = fmt_price_short(it.get("price", 0))
        sku = str(it.get("sku") or "").strip()
        rows.append([(f"{emoji} {it.get('name', '')} — Bs {price_txt}", f"admmenu|{tenant_id}|prd|{sku}")])

    rows.append([("🔄 Refrescar categoría", f"admmenu|{tenant_id}|catrefresh")])
    rows.append([("⬅️ Categorías", f"admmenu|{tenant_id}|home")])

    return kb(rows)


def send_admin_menu_category(
    bot_token: str,
    chat_id: int,
    tenant_id: str,
    orders_sh,
    sess: Dict[str, Any],
    category: str,
) -> bool:
    menu_idx, cats, _ = _get_menu_cached(sess, orders_sh)
    items = cats.get(category, [])

    tmp = sess.setdefault("tmp", {})
    tmp["admin_menu_current_category"] = category
    tmp.pop("admin_menu_price_sku", None)
    tmp.pop("admin_menu_price_work", None)
    tmp.pop("admin_menu_input_mode", None)

    total_n = len(items)
    active_n = sum(1 for it in items if bool(it.get("active", False)))

    msg = (
        f"📂 CATEGORÍA: {category}\n\n"
        f"Productos: {total_n}\n"
        f"Activos: {active_n}\n"
        f"Inactivos: {max(0, total_n - active_n)}\n\n"
        "Elige un producto:"
    )

    return telegram_send_text(
        bot_token,
        chat_id,
        msg,
        reply_markup=admin_menu_category_kb(tenant_id, category, items),
    )


def admin_menu_product_kb(tenant_id: str, sku: str, active: bool) -> Dict[str, Any]:
    toggle_label = "⛔ Desactivar" if active else "✅ Activar"

    return kb([
        [(toggle_label, f"admmenu|{tenant_id}|toggle|{sku}")],
        [("💲 Modificar precio", f"admmenu|{tenant_id}|price|{sku}")],
        [("✏️ Editar nombre", f"admmenu|{tenant_id}|edit_name|{sku}")],
        [("📂 Cambiar categoría", f"admmenu|{tenant_id}|edit_category|{sku}")],
        [("🖼 Subir foto producto", f"admmenu|{tenant_id}|photo|{sku}")],
        [("⬅️ Volver a categoría", f"admmenu|{tenant_id}|catback")],
        [("🏠 Categorías", f"admmenu|{tenant_id}|home")],
    ])


def send_admin_menu_product_detail(
    bot_token: str,
    chat_id: int,
    tenant_id: str,
    orders_sh,
    sess: Dict[str, Any],
    sku: str,
) -> bool:
    item = get_menu_product_or_404(orders_sh, sku)

    tmp = sess.setdefault("tmp", {})
    tmp["admin_menu_last_sku"] = sku
    tmp.pop("admin_menu_price_sku", None)
    tmp.pop("admin_menu_price_work", None)
    tmp.pop("admin_menu_input_mode", None)

    active_txt = "Sí" if bool(item.get("active", False)) else "No"
    price_txt = fmt_price_short(item.get("price", 0))
    photo_url = str(item.get("photo_url") or "").strip()
    photo_file_id = str(item.get("photo_file_id") or "").strip()
    has_photo = bool(photo_url or photo_file_id)

    msg = (
        "🧾 DETALLE DE PRODUCTO\n\n"
        f"Nombre: {item.get('name', '')}\n"
        f"Categoría: {item.get('category', '')}\n"
        f"Activo: {active_txt}\n"
        f"Precio actual: Bs {price_txt}\n"
        f"Foto de producto: {'Sí' if has_photo else 'No'}\n"
    )

    return telegram_send_text(
        bot_token,
        chat_id,
        msg,
        reply_markup=admin_menu_product_kb(tenant_id, sku, bool(item.get("active", False))),
    )


def admin_menu_price_kb(tenant_id: str, sku: str) -> Dict[str, Any]:
    row1: List[Tuple[str, str]] = []
    row2: List[Tuple[str, str]] = []

    for label, delta in PRICE_STEP_OPTIONS[:3]:
        token = f"m{int(abs(delta))}" if delta < 0 else f"p{int(delta)}"
        row1.append((label, f"admmenu|{tenant_id}|padj|{sku}|{token}"))

    for label, delta in PRICE_STEP_OPTIONS[3:]:
        token = f"m{int(abs(delta))}" if delta < 0 else f"p{int(delta)}"
        row2.append((label, f"admmenu|{tenant_id}|padj|{sku}|{token}"))

    return kb([
        row1,
        row2,
        [("💾 Guardar precio", f"admmenu|{tenant_id}|psave|{sku}")],
        [("↩️ Cancelar", f"admmenu|{tenant_id}|pback|{sku}")],
    ])


def send_admin_menu_price_editor(
    bot_token: str,
    chat_id: int,
    tenant_id: str,
    orders_sh,
    sess: Dict[str, Any],
    sku: str,
) -> bool:
    item = get_menu_product_or_404(orders_sh, sku)
    tmp = sess.setdefault("tmp", {})

    current_sku = str(tmp.get("admin_menu_price_sku") or "").strip()
    if current_sku != sku:
        tmp["admin_menu_price_sku"] = sku
        tmp["admin_menu_price_work"] = float(item.get("price", 0.0))

    work_price = float(tmp.get("admin_menu_price_work") or 0.0)

    msg = (
        "💲 MODIFICAR PRECIO\n\n"
        f"Producto: {item.get('name', '')}\n"
        f"Precio guardado: Bs {fmt_price_short(item.get('price', 0))}\n"
        f"Precio en edición: Bs {fmt_price_short(work_price)}\n\n"
        "Usa los botones para subir o bajar el precio.\n"
        "Luego presiona 'Guardar precio'."
    )

    return telegram_send_text(
        bot_token,
        chat_id,
        msg,
        reply_markup=admin_menu_price_kb(tenant_id, sku),
    )


def apply_price_delta(current_value: float, token: str) -> float:
    token = str(token or "").strip().lower()
    if not token or len(token) < 2:
        return current_value

    sign = token[0]
    num = token[1:]

    try:
        amount = float(num)
    except Exception:
        return current_value

    if sign == "m":
        new_value = current_value - amount
    elif sign == "p":
        new_value = current_value + amount
    else:
        return current_value

    if new_value < 0:
        new_value = 0.0

    return round(new_value, 2)
