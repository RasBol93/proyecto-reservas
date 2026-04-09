# app/admin_messages_promotions.py — mensajes admin para creación de promociones

from typing import Any, Dict, List, Optional
import re

from fastapi import HTTPException

from app.telegram_api import telegram_send_text
from app.telegram_keyboard import kb
from app.utils import normalize
from app.webhook_helpers import fmt_price_short
from app.menu import (
    load_menu_admin_index,
    group_menu_admin_by_category,
    get_menu_product_or_404,
)
from app.admin_promotions import (
    build_admin_promo_create_summary,
    admin_promotions_confirm_kb,
)


def _safe_send_text(
    bot_token: str,
    chat_id: int,
    text: str,
    reply_markup: Optional[Dict[str, Any]] = None,
    parse_mode: Optional[str] = None,
) -> bool:
    try:
        return telegram_send_text(
            bot_token,
            chat_id,
            text,
            reply_markup=reply_markup,
            parse_mode=parse_mode,
        )
    except Exception:
        return False


def _parse_price_text(text: str) -> Optional[float]:
    s = str(text or "").strip().lower()
    if not s:
        return None

    s = s.replace(",", ".")
    m = re.search(r"(\d+(?:\.\d+)?)", s)
    if not m:
        return None

    try:
        value = float(m.group(1))
    except Exception:
        return None

    if value < 0:
        return None

    return round(value, 2)


def _parse_combo_items_text(text: str, menu_idx: Dict[str, Dict[str, Any]]) -> Optional[List[Dict[str, Any]]]:
    """
    Formato esperado:
    sku1 x2, sku2 x1
    sku1, sku2 x3
    """
    raw = str(text or "").strip()
    if not raw:
        return None

    parts = [p.strip() for p in raw.split(",") if p.strip()]
    if not parts:
        return None

    result: List[Dict[str, Any]] = []

    for part in parts:
        m = re.match(r"^\s*([A-Za-z0-9_\-:]+)\s*(?:x\s*(\d+))?\s*$", part, flags=re.IGNORECASE)
        if not m:
            return None

        sku = str(m.group(1) or "").strip()
        qty_raw = m.group(2)

        if sku not in menu_idx:
            return None

        try:
            qty = int(qty_raw) if qty_raw else 1
        except Exception:
            qty = 1
        qty = max(1, qty)

        item = menu_idx[sku]
        result.append({
            "sku": sku,
            "qty": qty,
            "name": str(item.get("name") or sku).strip(),
            "unit_price": float(item.get("price") or 0.0),
        })

    return result


def _build_categories_kb(tenant_id: str, cats: Dict[str, List[Dict[str, Any]]], callback_prefix: str) -> Dict[str, Any]:
    cat_names = sorted(cats.keys(), key=lambda x: normalize(x))
    rows = []

    for idx, cat in enumerate(cat_names[:30]):
        rows.append([(f"📂 {cat}", f"{callback_prefix}|cat|{idx}")])

    rows.append([("⬅️ Cancelar", f"admpromo|{tenant_id}|home")])
    return kb(rows)


def _build_products_kb(tenant_id: str, items: List[Dict[str, Any]], callback_prefix: str) -> Dict[str, Any]:
    rows = []

    for it in items[:30]:
        sku = str(it.get("sku") or "").strip()
        name = str(it.get("name") or "").strip()
        price_txt = fmt_price_short(it.get("price", 0))
        rows.append([(f"{name} — Bs {price_txt}", f"{callback_prefix}|sku|{sku}")])

    rows.append([("⬅️ Cancelar", f"admpromo|{tenant_id}|home")])
    return kb(rows)


def _build_combo_quick_add_kb(tenant_id: str) -> Dict[str, Any]:
    return kb([
        [("✅ Listo, seguir", f"admpromo|{tenant_id}|create_discount_preview")],
        [("⬅️ Cancelar", f"admpromo|{tenant_id}|home")],
    ])


def handle_admin_promotions_message(
    tenant: Dict[str, Any],
    tenant_id: str,
    bot_token: str,
    chat_id: int,
    msg: Dict[str, Any],
    orders_sh,
    tmp: Dict[str, Any],
) -> Optional[Dict[str, Any]]:
    text = (msg.get("text") or "").strip()
    if not text:
        return None

    step = str(tmp.get("admin_promo_create_step") or "").strip().lower()
    if not step:
        return None

    promo_type = str(tmp.get("admin_promo_create_type") or "").strip().lower()

    # -------------------------------------------------
    # STEP: name
    # -------------------------------------------------
    if step == "name":
        name = str(text or "").strip()
        if not name:
            _safe_send_text(
                bot_token,
                chat_id,
                "⚠️ El nombre no puede estar vacío.",
            )
            return {"ok": True}

        tmp["admin_promo_create_name"] = name

        # -------- discount flow --------
        if promo_type == "discount":
            menu_idx = load_menu_admin_index(orders_sh, force=False)
            cats = group_menu_admin_by_category(menu_idx)

            tmp["admin_promo_create_step"] = "discount_select_category"
            tmp["admin_promo_discount_categories"] = sorted(cats.keys(), key=lambda x: normalize(x))

            _safe_send_text(
                bot_token,
                chat_id,
                (
                    "💸 DESCUENTO DE PRODUCTO\n\n"
                    f"Nombre promo: {name}\n\n"
                    "Ahora elige la categoría del producto:"
                ),
                reply_markup=_build_categories_kb(
                    tenant_id=tenant_id,
                    cats=cats,
                    callback_prefix=f"admpromo|{tenant_id}|create_discount_select",
                ),
            )
            return {"ok": True}

        # -------- combo flow --------
        if promo_type == "combo":
            tmp["admin_promo_create_step"] = "combo_items"
            tmp["admin_promo_create_combo_items"] = []

            _safe_send_text(
                bot_token,
                chat_id,
                (
                    "🎁 CREAR COMBO\n\n"
                    f"Nombre promo: {name}\n\n"
                    "Ahora escribe los productos del combo en este formato:\n"
                    "`sku1 x2, sku2 x1`\n\n"
                    "Ejemplo:\n"
                    "`burg01 x1, papa01 x1, coca01 x1`"
                ),
                reply_markup=_build_combo_quick_add_kb(tenant_id),
                parse_mode="Markdown",
            )
            return {"ok": True}

        return {"ok": True}

    # -------------------------------------------------
    # STEP: combo_items
    # -------------------------------------------------
    if step == "combo_items":
        menu_idx = load_menu_admin_index(orders_sh, force=False)
        combo_items = _parse_combo_items_text(text, menu_idx)

        if not combo_items:
            _safe_send_text(
                bot_token,
                chat_id,
                (
                    "⚠️ No pude leer los items del combo.\n\n"
                    "Usa este formato:\n"
                    "`sku1 x2, sku2 x1`"
                ),
                parse_mode="Markdown",
            )
            return {"ok": True}

        tmp["admin_promo_create_combo_items"] = combo_items

        original_price = 0.0
        lines = []
        for it in combo_items:
            qty = int(it.get("qty") or 1)
            name = str(it.get("name") or it.get("sku") or "").strip()
            unit_price = float(it.get("unit_price") or 0.0)
            original_price += qty * unit_price
            lines.append(f"• {qty} x {name}")

        tmp["admin_promo_create_original_price"] = round(original_price, 2)
        tmp["admin_promo_create_step"] = "promo_price"

        _safe_send_text(
            bot_token,
            chat_id,
            (
                "🎁 COMBO CONFIGURADO\n\n"
                f"Items:\n{chr(10).join(lines)}\n\n"
                f"Precio normal total: Bs {fmt_price_short(original_price)}\n\n"
                "Ahora escribe el precio promocional del combo."
            ),
        )
        return {"ok": True}

    # -------------------------------------------------
    # STEP: promo_price
    # -------------------------------------------------
    if step == "promo_price":
        promo_price = _parse_price_text(text)
        if promo_price is None:
            _safe_send_text(
                bot_token,
                chat_id,
                "⚠️ No pude leer el precio promocional. Ejemplo válido: 25",
            )
            return {"ok": True}

        original_price = float(tmp.get("admin_promo_create_original_price") or 0.0)
        if original_price <= 0:
            _safe_send_text(
                bot_token,
                chat_id,
                "⚠️ El precio original no es válido.",
            )
            return {"ok": True}

        if promo_price > original_price:
            _safe_send_text(
                bot_token,
                chat_id,
                f"⚠️ El precio promocional no puede ser mayor al precio normal (Bs {fmt_price_short(original_price)}).",
            )
            return {"ok": True}

        tmp["admin_promo_create_promo_price"] = round(promo_price, 2)
        tmp["admin_promo_create_step"] = "description"

        _safe_send_text(
            bot_token,
            chat_id,
            (
                "✍️ DESCRIPCIÓN DE PROMOCIÓN\n\n"
                "Escribe una descripción corta visible.\n"
                "También puedes escribir `auto` para que el sistema la genere."
            ),
        )
        return {"ok": True}

    # -------------------------------------------------
    # STEP: description
    # -------------------------------------------------
    if step == "description":
        description = str(text or "").strip()

        if normalize(description) == "auto":
            description = ""

        tmp["admin_promo_create_description"] = description

        summary = build_admin_promo_create_summary(tmp)

        _safe_send_text(
            bot_token,
            chat_id,
            summary,
            reply_markup=admin_promotions_confirm_kb(tenant_id),
        )
        return {"ok": True}

    # -------------------------------------------------
    # STEP: discount_select_product_manual
    # (por si luego queremos entrada manual de sku)
    # -------------------------------------------------
    if step == "discount_select_product_manual":
        sku = str(text or "").strip()

        try:
            product = get_menu_product_or_404(orders_sh, sku)
        except Exception:
            _safe_send_text(
                bot_token,
                chat_id,
                "⚠️ No encontré ese SKU. Intenta otra vez.",
            )
            return {"ok": True}

        tmp["admin_promo_create_product_sku"] = sku
        tmp["admin_promo_create_original_price"] = float(product.get("price") or 0.0)
        tmp["admin_promo_create_step"] = "promo_price"

        _safe_send_text(
            bot_token,
            chat_id,
            (
                "💸 PRODUCTO SELECCIONADO\n\n"
                f"Producto: {product.get('name', '')}\n"
                f"Precio actual: Bs {fmt_price_short(product.get('price', 0))}\n\n"
                "Ahora escribe el nuevo precio promocional."
            ),
        )
        return {"ok": True}

    return None
