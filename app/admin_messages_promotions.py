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
    send_admin_promotions_discount_categories,
    send_admin_promotions_combo_categories,
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

    Se conserva por compatibilidad/soporte, aunque la UX principal
    ahora usa botones para construir combos.
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
    """
    Se conserva por compatibilidad. La UX principal la arma admin_promotions.py,
    pero no eliminamos este helper para no romper futuras referencias.
    """
    cat_names = sorted(cats.keys(), key=lambda x: normalize(x))
    rows = []

    for idx, cat in enumerate(cat_names[:30]):
        rows.append([(f"📂 {cat}", f"{callback_prefix}|cat|{idx}")])

    rows.append([("⬅️ Cancelar", f"admpromo|{tenant_id}|home")])
    return kb(rows)


def _build_products_kb(tenant_id: str, items: List[Dict[str, Any]], callback_prefix: str) -> Dict[str, Any]:
    """
    Se conserva por compatibilidad. La UX principal la arma admin_promotions.py,
    pero no eliminamos este helper para no romper futuras referencias.
    """
    rows = []

    for it in items[:30]:
        sku = str(it.get("sku") or "").strip()
        name = str(it.get("name") or "").strip()
        price_txt = fmt_price_short(it.get("price", 0))
        rows.append([(f"{name} — Bs {price_txt}", f"{callback_prefix}|sku|{sku}")])

    rows.append([("⬅️ Cancelar", f"admpromo|{tenant_id}|home")])
    return kb(rows)


def _build_combo_quick_add_kb(tenant_id: str) -> Dict[str, Any]:
    """
    Se conserva por compatibilidad con el flujo anterior.
    """
    return kb([
        [("✅ Listo, seguir", f"admpromo|{tenant_id}|create_discount_preview")],
        [("⬅️ Cancelar", f"admpromo|{tenant_id}|home")],
    ])


def _compute_original_price_from_combo_items(combo_items: List[Dict[str, Any]]) -> float:
    total = 0.0

    for it in combo_items or []:
        try:
            qty = int(it.get("qty") or 1)
        except Exception:
            qty = 1
        qty = max(1, qty)

        try:
            unit_price = float(it.get("unit_price") or 0.0)
        except Exception:
            unit_price = 0.0

        total += qty * unit_price

    return round(total, 2)


def _send_invalid_state(bot_token: str, chat_id: int) -> Dict[str, Any]:
    _safe_send_text(
        bot_token,
        chat_id,
        "⚠️ El flujo de creación quedó en un estado inválido. Vuelve a entrar a Promociones.",
    )
    return {"ok": True}


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
            tmp["admin_promo_create_step"] = "discount_select_category"
            return {
                "ok": send_admin_promotions_discount_categories(
                    bot_token=bot_token,
                    chat_id=chat_id,
                    tenant_id=tenant_id,
                    orders_sh=orders_sh,
                    sess={"tmp": tmp},
                )
            }

        # -------- combo flow --------
        if promo_type == "combo":
            tmp["admin_promo_create_combo_items"] = []
            tmp["admin_promo_create_original_price"] = 0.0
            tmp["admin_promo_create_step"] = "combo_select_category"
            return {
                "ok": send_admin_promotions_combo_categories(
                    bot_token=bot_token,
                    chat_id=chat_id,
                    tenant_id=tenant_id,
                    orders_sh=orders_sh,
                    sess={"tmp": tmp},
                )
            }

        _safe_send_text(
            bot_token,
            chat_id,
            "⚠️ Tipo de promoción no válido.",
        )
        return {"ok": True}

    # -------------------------------------------------
    # STEP: combo_items
    # Compatibilidad con flujo viejo por texto.
    # La UX preferida ahora es por botones.
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
                    "Ahora este flujo usa botones.\n"
                    "Vuelve a entrar a la creación del combo y elige categorías y productos desde el menú."
                ),
            )
            return {"ok": True}

        tmp["admin_promo_create_combo_items"] = combo_items
        tmp["admin_promo_create_original_price"] = _compute_original_price_from_combo_items(combo_items)
        tmp["admin_promo_create_step"] = "promo_price"

        lines = []
        for it in combo_items:
            qty = int(it.get("qty") or 1)
            name = str(it.get("name") or it.get("sku") or "").strip()
            lines.append(f"• {qty} x {name}")

        _safe_send_text(
            bot_token,
            chat_id,
            (
                "🎁 COMBO CONFIGURADO\n\n"
                f"Items:\n{chr(10).join(lines)}\n\n"
                f"Precio normal total: Bs {fmt_price_short(tmp['admin_promo_create_original_price'])}\n\n"
                "Ahora escribe el precio promocional del combo."
            ),
        )
        return {"ok": True}

    # -------------------------------------------------
    # STEP: promo_price
    # Aplica tanto a descuento como a combo.
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
    # Compatibilidad con flujo viejo manual.
    # La UX nueva usa botones.
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

    # -------------------------------------------------
    # STEPS controlados por callbacks
    # Si el usuario escribe texto cuando debía tocar botones,
    # respondemos sin romper el flujo.
    # -------------------------------------------------
    if step in (
        "discount_select_category",
        "discount_select_product",
        "combo_select_category",
        "combo_select_product",
        "combo_builder",
    ):
        _safe_send_text(
            bot_token,
            chat_id,
            "Usa los botones de la pantalla para continuar con la promoción.",
        )
        return {"ok": True}

    # -------------------------------------------------
    # Fallback
    # -------------------------------------------------
    return None
