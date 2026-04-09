# app/admin_callbacks_promotions.py — callbacks admin para promociones

from typing import Any, Dict, Optional

from fastapi import HTTPException

from app.telegram_api import telegram_send_text
from app.telegram_keyboard import kb
from app.webhook_helpers import (
    get_sess,
    assert_admin_authorized,
)
from app.admin_nav import admin_panel_kb
from app.admin_promotions import (
    _clear_promotions_cache,
    send_admin_promotions_home,
    send_admin_promotions_list,
    send_admin_promotion_detail,
    send_admin_promotions_create_home,
    send_admin_promotions_ask_name,
    build_admin_promo_create_summary,
    admin_promotions_confirm_kb,
)
from app.promotions import (
    invalidate_promotions_cache,
    get_promotion_by_id,
    set_promotion_active,
    delete_promotion,
    create_discount_promotion,
    create_combo_promotion,
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


def _clear_admin_promo_create_state(tmp: Dict[str, Any]) -> None:
    tmp.pop("admin_promo_create_step", None)
    tmp.pop("admin_promo_create_type", None)
    tmp.pop("admin_promo_create_name", None)
    tmp.pop("admin_promo_create_product_sku", None)
    tmp.pop("admin_promo_create_combo_items", None)
    tmp.pop("admin_promo_create_original_price", None)
    tmp.pop("admin_promo_create_promo_price", None)
    tmp.pop("admin_promo_create_description", None)


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


def _send_combo_builder_placeholder(
    bot_token: str,
    chat_id: int,
    tenant_id: str,
    tmp: Dict[str, Any],
) -> Dict[str, Any]:
    combo_items = tmp.get("admin_promo_create_combo_items") or []

    if combo_items:
        lines = []
        for it in combo_items:
            qty = int(it.get("qty") or 1)
            name = str(it.get("name") or it.get("sku") or "").strip()
            lines.append(f"• {qty} x {name}")
        combo_txt = "\n".join(lines)
    else:
        combo_txt = "• Aún no agregaste productos"

    msg = (
        "🎁 CREAR COMBO\n\n"
        "Construcción inicial del combo.\n\n"
        f"Items actuales:\n{combo_txt}\n\n"
        "Por ahora, el siguiente paso recomendado es escribir los productos en el mensaje "
        "siguiendo un formato como:\n"
        "`sku1 x2, sku2 x1`\n\n"
        "Luego definiremos el precio total del combo."
    )

    _safe_send_text(
        bot_token,
        chat_id,
        msg,
        reply_markup=kb([
            [("⬅️ Cancelar", f"admpromo|{tenant_id}|home")],
        ]),
        parse_mode="Markdown",
    )
    return {"ok": True}


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

    cb_tenant_id = parts[1].strip()
    if cb_tenant_id != tenant_id:
        raise HTTPException(status_code=400, detail="Tenant mismatch in admin promotions callback")

    action = parts[2].strip()
    sess = get_sess(tenant_id, chat_id)
    tmp = sess.setdefault("tmp", {})

    if action == "panel":
        _clear_admin_promo_create_state(tmp)
        _clear_promotions_cache(sess)

        user_role = get_effective_admin_role(tenant, chat_id)
        _safe_send_text(
            bot_token,
            chat_id,
            "🧭 *PANEL ADMIN*\n\nElige una opción:",
            reply_markup=admin_panel_kb(user_role=user_role),
            parse_mode="Markdown",
        )
        return {"ok": True}

    if action == "menu":
        _clear_admin_promo_create_state(tmp)
        _clear_promotions_cache(sess)
        _safe_send_text(
            bot_token,
            chat_id,
            "🍔 *MENÚ*\n\nVuelve al módulo menú desde el panel.",
            reply_markup=kb([
                [("🧭 Panel admin", "admin_panel")],
            ]),
            parse_mode="Markdown",
        )
        return {"ok": True}

    if action == "home":
        _clear_admin_promo_create_state(tmp)
        _clear_promotions_cache(sess)
        return {"ok": send_admin_promotions_home(bot_token, chat_id, tenant_id, orders_sh, sess)}

    if action == "refresh":
        invalidate_promotions_cache(orders_sh)
        _clear_promotions_cache(sess)
        _safe_send_text(
            bot_token,
            chat_id,
            "✅ Promociones refrescadas.",
        )
        return {"ok": send_admin_promotions_home(bot_token, chat_id, tenant_id, orders_sh, sess)}

    if action == "list" and len(parts) >= 4:
        list_mode = str(parts[3] or "").strip().lower()
        if list_mode not in ("active", "inactive"):
            return {"ok": send_admin_promotions_home(bot_token, chat_id, tenant_id, orders_sh, sess)}

        _clear_admin_promo_create_state(tmp)
        _clear_promotions_cache(sess)
        return {"ok": send_admin_promotions_list(bot_token, chat_id, tenant_id, orders_sh, sess, list_mode)}

    if action == "backlist":
        return _go_back_to_list(bot_token, chat_id, tenant_id, orders_sh, sess)

    if action == "detail" and len(parts) >= 4:
        promo_id = str(parts[3] or "").strip()
        if not promo_id:
            return _go_back_to_list(bot_token, chat_id, tenant_id, orders_sh, sess)

        _clear_admin_promo_create_state(tmp)
        return {"ok": send_admin_promotion_detail(bot_token, chat_id, tenant_id, orders_sh, sess, promo_id)}

    if action == "toggle" and len(parts) >= 4:
        promo_id = str(parts[3] or "").strip()
        if not promo_id:
            return {"ok": send_admin_promotions_home(bot_token, chat_id, tenant_id, orders_sh, sess)}

        promo_before = get_promotion_by_id(orders_sh, promo_id)
        if not promo_before:
            _safe_send_text(
                bot_token,
                chat_id,
                "⚠️ No encontré esa promoción.",
            )
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
        return {"ok": send_admin_promotion_detail(bot_token, chat_id, tenant_id, orders_sh, sess, promo_id)}

    if action == "delete" and len(parts) >= 4:
        promo_id = str(parts[3] or "").strip()
        if not promo_id:
            return {"ok": send_admin_promotions_home(bot_token, chat_id, tenant_id, orders_sh, sess)}

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
        return {"ok": send_admin_promotions_home(bot_token, chat_id, tenant_id, orders_sh, sess)}

    if action == "create":
        _clear_promotions_cache(sess)
        return {"ok": send_admin_promotions_create_home(bot_token, chat_id, tenant_id, sess)}

    if action == "create_type" and len(parts) >= 4:
        promo_type = str(parts[3] or "").strip().lower()
        if promo_type not in ("discount", "combo"):
            return {"ok": send_admin_promotions_create_home(bot_token, chat_id, tenant_id, sess)}

        return {"ok": send_admin_promotions_ask_name(bot_token, chat_id, tenant_id, promo_type, sess)}

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
            _safe_send_text(
                bot_token,
                chat_id,
                "⚠️ No hay una promoción lista para confirmar.",
            )
            return {"ok": send_admin_promotions_home(bot_token, chat_id, tenant_id, orders_sh, sess)}

        invalidate_promotions_cache(orders_sh)
        _clear_promotions_cache(sess)
        _clear_admin_promo_create_state(tmp)

        _safe_send_text(
            bot_token,
            chat_id,
            f"✅ Promoción creada correctamente.\n{str(created.get('name') or '').strip()}",
        )
        return {"ok": send_admin_promotion_detail(bot_token, chat_id, tenant_id, orders_sh, sess, str(created.get("promo_id") or "").strip())}

    if action == "create_combo_builder":
        tmp["admin_promo_create_step"] = "combo_items"
        if not isinstance(tmp.get("admin_promo_create_combo_items"), list):
            tmp["admin_promo_create_combo_items"] = []
        return _send_combo_builder_placeholder(bot_token, chat_id, tenant_id, tmp)

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
