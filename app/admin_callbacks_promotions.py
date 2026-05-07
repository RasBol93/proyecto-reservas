# app/admin_callbacks_promotions.py — callbacks promociones admin (descuentos y combos por botones)

from typing import Any, Dict, Optional

from app.telegram_api import telegram_send_text
from app.webhook_helpers import get_sess, assert_admin_authorized
from app.promotions import (
    invalidate_promotions_cache,
    get_promotion_by_id,
    set_promotion_active,
    delete_promotion,
    create_discount_promotion,
    create_combo_promotion,
)
from app.menu import (
    load_menu_admin_index,
)
from app.admin_promotions import (
    _clear_promotions_cache,
    send_admin_promotions_home,
    send_admin_promotions_list,
    send_admin_promotion_detail,
    send_admin_promotions_create_home,
    send_admin_promotions_ask_name,
    send_admin_promotions_discount_categories,
    send_admin_promotions_discount_products,
    send_admin_promotions_combo_categories,
    send_admin_promotions_combo_products,
    send_admin_promotions_combo_builder,
    build_admin_promo_create_summary,
    admin_promotions_confirm_kb,
)


def _safe_send_text(
    bot_token: str,
    chat_id: int,
    text: str,
    reply_markup=None,
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


def _clear_admin_promo_create_state(tmp: Dict[str, Any]) -> None:
    tmp.pop("admin_promo_create_step", None)
    tmp.pop("admin_promo_create_type", None)
    tmp.pop("admin_promo_create_name", None)
    tmp.pop("admin_promo_create_product_sku", None)
    tmp.pop("admin_promo_create_combo_items", None)
    tmp.pop("admin_promo_create_original_price", None)
    tmp.pop("admin_promo_create_promo_price", None)
    tmp.pop("admin_promo_create_description", None)
    tmp.pop("admin_promo_discount_category", None)
    tmp.pop("admin_promo_combo_category", None)


def _combo_original_total(combo_items) -> float:
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


def _find_combo_item_index(combo_items, sku: str) -> int:
    for idx, it in enumerate(combo_items or []):
        if str(it.get("sku") or "").strip() == sku:
            return idx
    return -1


def _go_back_to_list(
    bot_token: str,
    chat_id: int,
    tenant_id: str,
    orders_sh,
    sess: Dict[str, Any],
) -> Dict[str, Any]:
    tmp = sess.setdefault("tmp", {})
    list_mode = str(tmp.get("admin_promo_current_filter") or "").strip().lower()
    if list_mode not in ("active", "inactive"):
        return {"ok": send_admin_promotions_home(bot_token, chat_id, tenant_id, orders_sh, sess)}
    return {"ok": send_admin_promotions_list(bot_token, chat_id, tenant_id, orders_sh, sess, list_mode)}


def handle_admin_promotions_callback(
    tenant: Dict[str, Any],
    tenant_id: str,
    bot_token: str,
    chat_id: int,
    data: str,
    orders_sh,
    get_effective_admin_role,
) -> Optional[Dict[str, Any]]:

    if not data.startswith("admpromo|"):
        return None

    assert_admin_authorized(tenant, chat_id, tenant_id)

    parts = data.split("|")
    if len(parts) < 3:
        return {"ok": True}

    action = str(parts[2] or "").strip()

    sess = get_sess(tenant_id, chat_id)
    tmp = sess.setdefault("tmp", {})

    # -----------------------------
    # no-op
    # -----------------------------
    if action == "noop":
        return {"ok": True}

    # -----------------------------
    # HOME
    # -----------------------------
    if action == "home":
        _clear_promotions_cache(sess)
        _clear_admin_promo_create_state(tmp)
        return {"ok": send_admin_promotions_home(
            bot_token,
            chat_id,
            tenant_id,
            orders_sh,
            sess,
        )}

    # -----------------------------
    # REFRESH
    # -----------------------------
    if action == "refresh":
        invalidate_promotions_cache(orders_sh)
        _clear_promotions_cache(sess)
        _safe_send_text(bot_token, chat_id, "✅ Promociones refrescadas.")
        return {"ok": send_admin_promotions_home(
            bot_token,
            chat_id,
            tenant_id,
            orders_sh,
            sess,
        )}

    # -----------------------------
    # CREAR
    # -----------------------------
    if action == "create":
        _clear_promotions_cache(sess)
        _clear_admin_promo_create_state(tmp)
        return {"ok": send_admin_promotions_create_home(
            bot_token,
            chat_id,
            tenant_id,
            sess,
        )}

    # -----------------------------
    # LISTADOS
    # -----------------------------
    if action == "list" and len(parts) >= 4:
        list_mode = str(parts[3] or "").strip().lower()
        if list_mode not in ("active", "inactive"):
            list_mode = "active"

        _clear_promotions_cache(sess)
        return {"ok": send_admin_promotions_list(
            bot_token,
            chat_id,
            tenant_id,
            orders_sh,
            sess,
            list_mode,
        )}

    if action == "backlist":
        return _go_back_to_list(bot_token, chat_id, tenant_id, orders_sh, sess)

    # -----------------------------
    # DETALLE / TOGGLE / DELETE
    # -----------------------------
    if action == "detail" and len(parts) >= 4:
        promo_id = str(parts[3] or "").strip()
        if not promo_id:
            return {"ok": True}
        return {"ok": send_admin_promotion_detail(
            bot_token,
            chat_id,
            tenant_id,
            orders_sh,
            sess,
            promo_id,
        )}

    if action == "toggle" and len(parts) >= 4:
        promo_id = str(parts[3] or "").strip()
        promo_before = get_promotion_by_id(orders_sh, promo_id)
        if not promo_before:
            _safe_send_text(bot_token, chat_id, "⚠️ No encontré esa promoción.")
            return _go_back_to_list(bot_token, chat_id, tenant_id, orders_sh, sess)

        updated = set_promotion_active(
            orders_sh=orders_sh,
            promo_id=promo_id,
            is_active=not bool(promo_before.get("active", False)),
        )

        invalidate_promotions_cache(orders_sh)
        _clear_promotions_cache(sess)

        _safe_send_text(
            bot_token,
            chat_id,
            f"✅ Promoción actualizada.\nEstado: {'Activa' if updated.get('active') else 'Inactiva'}",
        )
        return {"ok": send_admin_promotion_detail(
            bot_token,
            chat_id,
            tenant_id,
            orders_sh,
            sess,
            promo_id,
        )}

    if action == "delete" and len(parts) >= 4:
        promo_id = str(parts[3] or "").strip()
        promo = get_promotion_by_id(orders_sh, promo_id)
        promo_name = str((promo or {}).get("name") or promo_id).strip()

        delete_promotion(orders_sh, promo_id)
        invalidate_promotions_cache(orders_sh)
        _clear_promotions_cache(sess)
        tmp.pop("admin_promo_current_id", None)

        _safe_send_text(
            bot_token,
            chat_id,
            f"🗑 Promoción eliminada.\n{promo_name}",
        )
        return {"ok": send_admin_promotions_home(
            bot_token,
            chat_id,
            tenant_id,
            orders_sh,
            sess,
        )}

    # -----------------------------
    # SELECCIÓN DE TIPO
    # -----------------------------
    if action == "create_type" and len(parts) >= 4:
        promo_type = str(parts[3] or "").strip().lower()

        if promo_type not in ("discount", "combo"):
            return {"ok": send_admin_promotions_create_home(
                bot_token,
                chat_id,
                tenant_id,
                sess,
            )}

        return {"ok": send_admin_promotions_ask_name(
            bot_token,
            chat_id,
            tenant_id,
            promo_type,
            sess,
        )}

    # -----------------------------
    # DESCUENTO — categorías / productos
    # -----------------------------
    if action == "discount_back_categories":
        return {"ok": send_admin_promotions_discount_categories(
            bot_token,
            chat_id,
            tenant_id,
            orders_sh,
            sess,
        )}

    if action == "discount_pick" and len(parts) >= 5:
        pick_mode = str(parts[3] or "").strip().lower()

        # Elegir categoría
        if pick_mode == "cat":
            try:
                idx = int(parts[4].strip())
            except Exception:
                idx = -1

            _menu_idx, _cats, cat_names = (
                load_menu_admin_index(orders_sh, force=False),
                None,
                None,
            )
            # usamos la pantalla para recalcular de forma consistente
            _menu_idx2 = load_menu_admin_index(orders_sh, force=False)
            from app.menu import group_menu_admin_by_category
            cats = group_menu_admin_by_category(_menu_idx2, orders_sh=orders_sh)
            cat_names = list(cats.keys())

            if idx < 0 or idx >= len(cat_names):
                return {"ok": send_admin_promotions_discount_categories(
                    bot_token,
                    chat_id,
                    tenant_id,
                    orders_sh,
                    sess,
                )}

            category = str(cat_names[idx]).strip()
            return {"ok": send_admin_promotions_discount_products(
                bot_token,
                chat_id,
                tenant_id,
                orders_sh,
                sess,
                category,
            )}

        # Elegir producto
        if pick_mode == "sku":
            sku = str(parts[4] or "").strip()
            if not sku:
                return {"ok": True}

            menu_idx = load_menu_admin_index(orders_sh, force=False)
            product = menu_idx.get(sku)
            if not product:
                _safe_send_text(bot_token, chat_id, "⚠️ No encontré ese producto.")
                category = str(tmp.get("admin_promo_discount_category") or "").strip()
                if category:
                    return {"ok": send_admin_promotions_discount_products(
                        bot_token,
                        chat_id,
                        tenant_id,
                        orders_sh,
                        sess,
                        category,
                    )}
                return {"ok": send_admin_promotions_discount_categories(
                    bot_token,
                    chat_id,
                    tenant_id,
                    orders_sh,
                    sess,
                )}

            tmp["admin_promo_create_product_sku"] = sku
            tmp["admin_promo_create_original_price"] = float(product.get("price") or 0.0)
            tmp["admin_promo_create_step"] = "promo_price"

            _safe_send_text(
                bot_token,
                chat_id,
                (
                    "💸 PRODUCTO SELECCIONADO\n\n"
                    f"Producto: {str(product.get('name') or '').strip()}\n"
                    f"SKU: {sku}\n"
                    f"Precio actual: Bs {int(float(product.get('price') or 0)) if float(product.get('price') or 0).is_integer() else float(product.get('price') or 0)}\n\n"
                    "Ahora escribe el nuevo precio promocional."
                ),
            )
            return {"ok": True}

    # -----------------------------
    # COMBO — categorías / productos / builder
    # -----------------------------
    if action == "combo_back_categories":
        return {"ok": send_admin_promotions_combo_categories(
            bot_token,
            chat_id,
            tenant_id,
            orders_sh,
            sess,
        )}

    if action == "combo_pick" and len(parts) >= 5:
        pick_mode = str(parts[3] or "").strip().lower()

        # Elegir categoría
        if pick_mode == "cat":
            try:
                idx = int(parts[4].strip())
            except Exception:
                idx = -1

            _menu_idx2 = load_menu_admin_index(orders_sh, force=False)
            from app.menu import group_menu_admin_by_category
            cats = group_menu_admin_by_category(_menu_idx2, orders_sh=orders_sh)
            cat_names = list(cats.keys())

            if idx < 0 or idx >= len(cat_names):
                return {"ok": send_admin_promotions_combo_categories(
                    bot_token,
                    chat_id,
                    tenant_id,
                    orders_sh,
                    sess,
                )}

            category = str(cat_names[idx]).strip()
            return {"ok": send_admin_promotions_combo_products(
                bot_token,
                chat_id,
                tenant_id,
                orders_sh,
                sess,
                category,
            )}

        # Elegir producto del combo
        if pick_mode == "sku":
            sku = str(parts[4] or "").strip()
            if not sku:
                return {"ok": True}

            menu_idx = load_menu_admin_index(orders_sh, force=False)
            product = menu_idx.get(sku)
            if not product:
                _safe_send_text(bot_token, chat_id, "⚠️ No encontré ese producto.")
                category = str(tmp.get("admin_promo_combo_category") or "").strip()
                if category:
                    return {"ok": send_admin_promotions_combo_products(
                        bot_token,
                        chat_id,
                        tenant_id,
                        orders_sh,
                        sess,
                        category,
                    )}
                return {"ok": send_admin_promotions_combo_categories(
                    bot_token,
                    chat_id,
                    tenant_id,
                    orders_sh,
                    sess,
                )}

            combo_items = tmp.get("admin_promo_create_combo_items") or []
            idx_existing = _find_combo_item_index(combo_items, sku)

            if idx_existing >= 0:
                combo_items[idx_existing]["qty"] = int(combo_items[idx_existing].get("qty") or 1) + 1
            else:
                combo_items.append({
                    "sku": sku,
                    "qty": 1,
                    "name": str(product.get("name") or sku).strip(),
                    "unit_price": float(product.get("price") or 0.0),
                })

            tmp["admin_promo_create_combo_items"] = combo_items
            tmp["admin_promo_create_original_price"] = _combo_original_total(combo_items)

            return {"ok": send_admin_promotions_combo_builder(
                bot_token,
                chat_id,
                tenant_id,
                sess,
            )}

    if action == "combo_qty" and len(parts) >= 5:
        try:
            idx = int(parts[3].strip())
        except Exception:
            idx = -1

        qty_action = str(parts[4] or "").strip().lower()
        combo_items = tmp.get("admin_promo_create_combo_items") or []

        if idx < 0 or idx >= len(combo_items):
            return {"ok": send_admin_promotions_combo_builder(
                bot_token,
                chat_id,
                tenant_id,
                sess,
            )}

        current_qty = int(combo_items[idx].get("qty") or 1)

        if qty_action == "inc":
            current_qty += 1
        elif qty_action == "dec":
            current_qty = max(1, current_qty - 1)

        combo_items[idx]["qty"] = current_qty
        tmp["admin_promo_create_combo_items"] = combo_items
        tmp["admin_promo_create_original_price"] = _combo_original_total(combo_items)

        return {"ok": send_admin_promotions_combo_builder(
            bot_token,
            chat_id,
            tenant_id,
            sess,
        )}

    if action == "combo_remove" and len(parts) >= 4:
        try:
            idx = int(parts[3].strip())
        except Exception:
            idx = -1

        combo_items = tmp.get("admin_promo_create_combo_items") or []
        if 0 <= idx < len(combo_items):
            combo_items.pop(idx)

        tmp["admin_promo_create_combo_items"] = combo_items
        tmp["admin_promo_create_original_price"] = _combo_original_total(combo_items)

        if not combo_items:
            return {"ok": send_admin_promotions_combo_categories(
                bot_token,
                chat_id,
                tenant_id,
                orders_sh,
                sess,
            )}

        return {"ok": send_admin_promotions_combo_builder(
            bot_token,
            chat_id,
            tenant_id,
            sess,
        )}

    if action == "combo_continue_price":
        combo_items = tmp.get("admin_promo_create_combo_items") or []
        if not combo_items:
            _safe_send_text(bot_token, chat_id, "⚠️ Debes agregar al menos un producto al combo.")
            return {"ok": send_admin_promotions_combo_categories(
                bot_token,
                chat_id,
                tenant_id,
                orders_sh,
                sess,
            )}

        tmp["admin_promo_create_original_price"] = _combo_original_total(combo_items)
        tmp["admin_promo_create_step"] = "promo_price"

        _safe_send_text(
            bot_token,
            chat_id,
            (
                "🎁 PRECIO DEL COMBO\n\n"
                f"Precio normal total: Bs {tmp.get('admin_promo_create_original_price', 0)}\n\n"
                "Ahora escribe el precio promocional del combo."
            ),
        )
        return {"ok": True}

    # -----------------------------
    # CONFIRMAR CREACIÓN
    # -----------------------------
    if action == "create_confirm":
        promo_type = str(tmp.get("admin_promo_create_type") or "").strip().lower()

        if promo_type == "discount":
            created = create_discount_promotion(
                orders_sh=orders_sh,
                name=str(tmp.get("admin_promo_create_name") or "").strip(),
                product_sku=str(tmp.get("admin_promo_create_product_sku") or "").strip(),
                original_price=float(tmp.get("admin_promo_create_original_price") or 0),
                promo_price=float(tmp.get("admin_promo_create_promo_price") or 0),
                description=str(tmp.get("admin_promo_create_description") or "").strip(),
                active=True,
            )
        elif promo_type == "combo":
            created = create_combo_promotion(
                orders_sh=orders_sh,
                name=str(tmp.get("admin_promo_create_name") or "").strip(),
                combo_items=tmp.get("admin_promo_create_combo_items") or [],
                original_price=float(tmp.get("admin_promo_create_original_price") or 0),
                promo_price=float(tmp.get("admin_promo_create_promo_price") or 0),
                description=str(tmp.get("admin_promo_create_description") or "").strip(),
                active=True,
            )
        else:
            _safe_send_text(bot_token, chat_id, "⚠️ No hay una promoción lista para confirmar.")
            return {"ok": send_admin_promotions_home(
                bot_token,
                chat_id,
                tenant_id,
                orders_sh,
                sess,
            )}

        invalidate_promotions_cache(orders_sh)
        _clear_promotions_cache(sess)
        _clear_admin_promo_create_state(tmp)

        _safe_send_text(
            bot_token,
            chat_id,
            f"✅ Promoción creada correctamente.\n{str(created.get('name') or '').strip()}",
        )
        return {"ok": send_admin_promotion_detail(
            bot_token,
            chat_id,
            tenant_id,
            orders_sh,
            sess,
            str(created.get("promo_id") or "").strip(),
        )}

    # -----------------------------
    # preview resumen
    # -----------------------------
    if action == "create_discount_preview":
        summary = build_admin_promo_create_summary(tmp)
        _safe_send_text(
            bot_token,
            chat_id,
            summary,
            reply_markup=admin_promotions_confirm_kb(tenant_id),
        )
        return {"ok": True}

    return {"ok": True}
