# app/admin_callbacks_promotions.py — callbacks promociones admin

from typing import Any, Dict, Optional

from app.telegram_api import telegram_send_text
from app.telegram_keyboard import kb
from app.webhook_helpers import get_sess, assert_admin_authorized
from app.promotions import (
    invalidate_promotions_cache,
    get_active_promotions,
)
from app.admin_promotions import (
    send_admin_promotions_home,
    send_admin_promotions_create_home,
)


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

    action = parts[2]

    sess = get_sess(tenant_id, chat_id)
    tmp = sess.setdefault("tmp", {})

    # -----------------------------
    # HOME
    # -----------------------------
    if action == "home":
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
        return {"ok": send_admin_promotions_create_home(
            bot_token,
            chat_id,
            tenant_id,
            sess,
        )}

    # -----------------------------
    # SELECCIÓN DE TIPO
    # -----------------------------
    if action == "create_type" and len(parts) >= 4:
        promo_type = parts[3]

        tmp["admin_promo_create_type"] = promo_type
        tmp["admin_promo_create_step"] = "name"

        telegram_send_text(
            bot_token,
            chat_id,
            "✍️ Escribe el nombre de la promoción",
        )
        return {"ok": True}

    # -----------------------------
    # CONFIRMAR CREACIÓN
    # -----------------------------
    if action == "create_confirm":
        # Aquí solo refrescamos cache por ahora
        invalidate_promotions_cache(orders_sh)

        telegram_send_text(
            bot_token,
            chat_id,
            "✅ Promoción creada",
        )

        return {"ok": send_admin_promotions_home(
            bot_token,
            chat_id,
            tenant_id,
            orders_sh,
            sess,
        )}

    # -----------------------------
    # LISTAR ACTIVAS
    # -----------------------------
    if action == "list_active":
        promos = get_active_promotions(orders_sh)

        if not promos:
            telegram_send_text(
                bot_token,
                chat_id,
                "No hay promociones activas",
            )
            return {"ok": True}

        txt = "🎁 Promociones activas:\n\n"
        for p in promos:
            txt += f"- {p.get('name')} Bs {int(p.get('promo_price', 0))}\n"

        telegram_send_text(
            bot_token,
            chat_id,
            txt,
        )
        return {"ok": True}

    return {"ok": True}
