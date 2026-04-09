# app/admin_promotions.py — UI admin tipo app para promociones

from typing import Any, Dict, List, Tuple

from app.promotions import (
    load_promotions,
    get_active_promotions,
    get_inactive_promotions,
    get_promotion_by_id,
)
from app.telegram_api import telegram_send_text
from app.telegram_keyboard import kb
from app.utils import normalize
from app.webhook_helpers import fmt_price_short


PROMO_TYPE_LABELS = {
    "discount": "💸 Descuento",
    "combo": "🎁 Combo",
}


# -------------------------
# Cache simple por sesión
# -------------------------

def _get_promotions_cached(sess: Dict[str, Any], orders_sh):
    tmp = sess.setdefault("tmp", {})

    cached = tmp.get("admin_promotions_cache")
    if cached is not None:
        return cached

    all_promos = load_promotions(orders_sh, force=False)
    active_promos = [p for p in all_promos if bool(p.get("active", False))]
    inactive_promos = [p for p in all_promos if not bool(p.get("active", False))]

    tmp["admin_promotions_cache"] = (all_promos, active_promos, inactive_promos)
    return tmp["admin_promotions_cache"]


def _clear_promotions_cache(sess: Dict[str, Any]) -> None:
    sess.setdefault("tmp", {}).pop("admin_promotions_cache", None)


# -------------------------
# Helpers visuales
# -------------------------

def _promo_type_label(promo_type: str) -> str:
    return PROMO_TYPE_LABELS.get(str(promo_type or "").strip().lower(), "🏷️ Promo")


def _build_promo_status_label(is_active: bool) -> str:
    return "🟢 Activa" if bool(is_active) else "⚫ Inactiva"


def _build_discount_summary(promo: Dict[str, Any]) -> str:
    name = str(promo.get("name") or "").strip()
    product_sku = str(promo.get("product_sku") or "").strip()
    original_price = fmt_price_short(promo.get("original_price", 0))
    promo_price = fmt_price_short(promo.get("promo_price", 0))

    base = (
        f"{_promo_type_label('discount')}\n"
        f"Nombre: {name}\n"
        f"SKU producto: {product_sku or '-'}\n"
        f"Precio normal: Bs {original_price}\n"
        f"Precio promo: Bs {promo_price}"
    )
    return base


def _build_combo_summary(promo: Dict[str, Any]) -> str:
    name = str(promo.get("name") or "").strip()
    combo_items = promo.get("combo_items") or []
    original_price = fmt_price_short(promo.get("original_price", 0))
    promo_price = fmt_price_short(promo.get("promo_price", 0))

    lines = []
    for it in combo_items[:5]:
        sku = str(it.get("sku") or "").strip()
        qty = int(it.get("qty") or 1)
        item_name = str(it.get("name") or sku).strip()
        lines.append(f"• {qty} x {item_name}")

    combo_txt = "\n".join(lines) if lines else "• Sin items"

    base = (
        f"{_promo_type_label('combo')}\n"
        f"Nombre: {name}\n"
        f"Contenido:\n{combo_txt}\n"
        f"Precio normal: Bs {original_price}\n"
        f"Precio combo: Bs {promo_price}"
    )
    return base


def _build_promo_card_text(promo: Dict[str, Any]) -> str:
    promo_type = str(promo.get("type") or "").strip().lower()
    promo_id = str(promo.get("promo_id") or "").strip()
    active = bool(promo.get("active", False))
    description = str(promo.get("description") or "").strip()

    if promo_type == "combo":
        body = _build_combo_summary(promo)
    else:
        body = _build_discount_summary(promo)

    msg = (
        "🏷️ PROMOCIÓN\n\n"
        f"ID: {promo_id}\n"
        f"Estado: {_build_promo_status_label(active)}\n\n"
        f"{body}"
    )

    if description:
        msg += f"\n\nDescripción:\n{description}"

    return msg


def _build_promo_row_label(promo: Dict[str, Any]) -> str:
    active_dot = "🟢" if bool(promo.get("active", False)) else "⚫"
    promo_type = str(promo.get("type") or "").strip().lower()
    name = str(promo.get("name") or "").strip()

    original_price = fmt_price_short(promo.get("original_price", 0))
    promo_price = fmt_price_short(promo.get("promo_price", 0))

    type_emoji = "💸" if promo_type == "discount" else "🎁"
    return f"{active_dot} {type_emoji} {name} — {original_price}→{promo_price}"


# -------------------------
# Home
# -------------------------

def admin_promotions_home_kb(tenant_id: str, active_n: int, inactive_n: int) -> Dict[str, Any]:
    return kb([
        [("➕ Crear promoción", f"admpromo|{tenant_id}|create")],
        [("🟢 Promociones activas", f"admpromo|{tenant_id}|list|active")],
        [("⚫ Promociones inactivas", f"admpromo|{tenant_id}|list|inactive")],
        [("🔄 Refrescar promociones", f"admpromo|{tenant_id}|refresh")],
        [("⬅️ Volver al menú", f"admpromo|{tenant_id}|menu")],
    ])


def send_admin_promotions_home(
    bot_token: str,
    chat_id: int,
    tenant_id: str,
    orders_sh,
    sess: Dict[str, Any],
) -> bool:
    all_promos, active_promos, inactive_promos = _get_promotions_cached(sess, orders_sh)

    tmp = sess.setdefault("tmp", {})
    tmp.pop("admin_promo_current_filter", None)
    tmp.pop("admin_promo_current_id", None)
    tmp.pop("admin_promo_create_step", None)
    tmp.pop("admin_promo_create_type", None)
    tmp.pop("admin_promo_create_name", None)
    tmp.pop("admin_promo_create_product_sku", None)
    tmp.pop("admin_promo_create_combo_items", None)
    tmp.pop("admin_promo_create_original_price", None)
    tmp.pop("admin_promo_create_promo_price", None)
    tmp.pop("admin_promo_create_description", None)

    msg = (
        "🎁 PROMOCIONES\n\n"
        f"Promociones totales: {len(all_promos)}\n"
        f"Activas: {len(active_promos)}\n"
        f"Inactivas: {len(inactive_promos)}\n\n"
        "Gestiona descuentos y combos desde aquí."
    )

    return telegram_send_text(
        bot_token,
        chat_id,
        msg,
        reply_markup=admin_promotions_home_kb(
            tenant_id=tenant_id,
            active_n=len(active_promos),
            inactive_n=len(inactive_promos),
        ),
    )


# -------------------------
# Listados
# -------------------------

def admin_promotions_list_kb(
    tenant_id: str,
    promos: List[Dict[str, Any]],
    list_mode: str,
) -> Dict[str, Any]:
    rows: List[List[Tuple[str, str]]] = []

    for promo in promos[:25]:
        promo_id = str(promo.get("promo_id") or "").strip()
        rows.append([(_build_promo_row_label(promo), f"admpromo|{tenant_id}|detail|{promo_id}")])

    if list_mode == "active":
        rows.append([("⚫ Ver inactivas", f"admpromo|{tenant_id}|list|inactive")])
    elif list_mode == "inactive":
        rows.append([("🟢 Ver activas", f"admpromo|{tenant_id}|list|active")])

    rows.append([("⬅️ Promociones", f"admpromo|{tenant_id}|home")])

    return kb(rows)


def send_admin_promotions_list(
    bot_token: str,
    chat_id: int,
    tenant_id: str,
    orders_sh,
    sess: Dict[str, Any],
    list_mode: str,
) -> bool:
    _all_promos, active_promos, inactive_promos = _get_promotions_cached(sess, orders_sh)

    if list_mode == "inactive":
        promos = inactive_promos
        title = "⚫ PROMOCIONES INACTIVAS"
    else:
        promos = active_promos
        title = "🟢 PROMOCIONES ACTIVAS"
        list_mode = "active"

    tmp = sess.setdefault("tmp", {})
    tmp["admin_promo_current_filter"] = list_mode
    tmp.pop("admin_promo_current_id", None)

    if not promos:
        msg = (
            f"{title}\n\n"
            "No hay promociones en este listado."
        )
    else:
        msg = (
            f"{title}\n\n"
            f"Total: {len(promos)}\n\n"
            "Elige una promoción:"
        )

    return telegram_send_text(
        bot_token,
        chat_id,
        msg,
        reply_markup=admin_promotions_list_kb(tenant_id, promos, list_mode),
    )


# -------------------------
# Detalle
# -------------------------

def admin_promotion_detail_kb(tenant_id: str, promo_id: str, active: bool) -> Dict[str, Any]:
    toggle_label = "⛔ Desactivar" if active else "✅ Activar"

    return kb([
        [(toggle_label, f"admpromo|{tenant_id}|toggle|{promo_id}")],
        [("🗑 Eliminar", f"admpromo|{tenant_id}|delete|{promo_id}")],
        [("⬅️ Volver al listado", f"admpromo|{tenant_id}|backlist")],
        [("🏠 Promociones", f"admpromo|{tenant_id}|home")],
    ])


def send_admin_promotion_detail(
    bot_token: str,
    chat_id: int,
    tenant_id: str,
    orders_sh,
    sess: Dict[str, Any],
    promo_id: str,
) -> bool:
    promo = get_promotion_by_id(orders_sh, promo_id)
    if not promo:
        return telegram_send_text(
            bot_token,
            chat_id,
            "⚠️ No encontré esa promoción.",
            reply_markup=kb([
                [("🏠 Promociones", f"admpromo|{tenant_id}|home")],
            ]),
        )

    tmp = sess.setdefault("tmp", {})
    tmp["admin_promo_current_id"] = promo_id

    msg = _build_promo_card_text(promo)

    return telegram_send_text(
        bot_token,
        chat_id,
        msg,
        reply_markup=admin_promotion_detail_kb(tenant_id, promo_id, bool(promo.get("active", False))),
    )


# -------------------------
# Crear promoción
# -------------------------

def admin_promotions_create_type_kb(tenant_id: str) -> Dict[str, Any]:
    return kb([
        [("💸 Descuento de producto", f"admpromo|{tenant_id}|create_type|discount")],
        [("🎁 Combo", f"admpromo|{tenant_id}|create_type|combo")],
        [("⬅️ Promociones", f"admpromo|{tenant_id}|home")],
    ])


def send_admin_promotions_create_home(
    bot_token: str,
    chat_id: int,
    tenant_id: str,
    sess: Dict[str, Any],
) -> bool:
    tmp = sess.setdefault("tmp", {})

    tmp["admin_promo_create_step"] = "type"
    tmp.pop("admin_promo_create_type", None)
    tmp.pop("admin_promo_create_name", None)
    tmp.pop("admin_promo_create_product_sku", None)
    tmp.pop("admin_promo_create_combo_items", None)
    tmp.pop("admin_promo_create_original_price", None)
    tmp.pop("admin_promo_create_promo_price", None)
    tmp.pop("admin_promo_create_description", None)

    msg = (
        "➕ CREAR PROMOCIÓN\n\n"
        "Elige el tipo de promoción que quieres crear."
    )

    return telegram_send_text(
        bot_token,
        chat_id,
        msg,
        reply_markup=admin_promotions_create_type_kb(tenant_id),
    )


def send_admin_promotions_ask_name(
    bot_token: str,
    chat_id: int,
    tenant_id: str,
    promo_type: str,
    sess: Dict[str, Any],
) -> bool:
    tmp = sess.setdefault("tmp", {})
    tmp["admin_promo_create_step"] = "name"
    tmp["admin_promo_create_type"] = str(promo_type or "").strip().lower()

    promo_type_label = _promo_type_label(promo_type)

    msg = (
        f"➕ CREAR PROMOCIÓN\n\n"
        f"Tipo: {promo_type_label}\n\n"
        "Ahora escribe el nombre visible de la promoción.\n"
        "Ejemplo:\n"
        "Papas 2x1"
    )

    return telegram_send_text(
        bot_token,
        chat_id,
        msg,
        reply_markup=kb([
            [("⬅️ Volver", f"admpromo|{tenant_id}|create")],
        ]),
    )


def build_admin_promo_create_summary(tmp: Dict[str, Any]) -> str:
    promo_type = str(tmp.get("admin_promo_create_type") or "").strip().lower()
    name = str(tmp.get("admin_promo_create_name") or "").strip()
    product_sku = str(tmp.get("admin_promo_create_product_sku") or "").strip()
    combo_items = tmp.get("admin_promo_create_combo_items") or []
    original_price = tmp.get("admin_promo_create_original_price")
    promo_price = tmp.get("admin_promo_create_promo_price")
    description = str(tmp.get("admin_promo_create_description") or "").strip()

    msg = (
        "🧾 RESUMEN PROMOCIÓN\n\n"
        f"Tipo: {_promo_type_label(promo_type)}\n"
        f"Nombre: {name or '-'}\n"
    )

    if promo_type == "discount":
        msg += f"SKU producto: {product_sku or '-'}\n"
    elif promo_type == "combo":
        if combo_items:
            combo_txt = "\n".join([
                f"• {int(it.get('qty') or 1)} x {str(it.get('name') or it.get('sku') or '').strip()}"
                for it in combo_items
            ])
        else:
            combo_txt = "• Sin items"
        msg += f"Items combo:\n{combo_txt}\n"

    msg += (
        f"Precio normal: Bs {fmt_price_short(original_price or 0)}\n"
        f"Precio promo: Bs {fmt_price_short(promo_price or 0)}\n"
    )

    if description:
        msg += f"Descripción:\n{description}\n"

    return msg.strip()


def admin_promotions_confirm_kb(tenant_id: str) -> Dict[str, Any]:
    return kb([
        [("✅ Confirmar creación", f"admpromo|{tenant_id}|create_confirm")],
        [("❌ Cancelar", f"admpromo|{tenant_id}|home")],
    ])
