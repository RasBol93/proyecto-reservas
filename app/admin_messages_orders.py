# app/admin_messages_orders.py

from typing import Any, Dict, Optional

from app.telegram_api import telegram_send_text
from app.telegram_keyboard import kb
from app.webhook_helpers import (
    assert_admin_authorized,
    admin_fixed_kb,
)
from app.utils import log_event
from app.alerts import alert_system_error
from app.admin_manual_order import (
    _admin_order_reset,
    _send_admin_order_home,
    _admin_order_time_choice_kb,
)
from app.admin_order_runtime import (
    finalize_admin_manual_order,
)


def handle_admin_orders_message(
    tenant: Dict[str, Any],
    tenant_id: str,
    bot_token: str,
    chat_id: int,
    msg: Dict[str, Any],
    orders_sh,
    tmp: Dict[str, Any],
    is_owner: bool,
) -> Optional[Dict[str, Any]]:

    text = (msg.get("text") or "").strip()

    # =========================================================
    # 📷 COMPROBANTE FOTO
    # =========================================================
    if bool(tmp.get("admin_order_waiting_proof")):
        assert_admin_authorized(tenant, chat_id, tenant_id)

        if msg.get("photo"):
            tmp["admin_order_waiting_proof"] = False
            tmp["admin_order_proof_received"] = True

            last_order_id = str(tmp.get("admin_order_last_id") or "").strip()

            telegram_send_text(
                bot_token,
                chat_id,
                (
                    "✅ Comprobante recibido correctamente.\n"
                    f"Pedido: {last_order_id or '(sin referencia)'}"
                ),
                reply_markup=kb([
                    [("✅ Fotografía OK", f"admord|{tenant_id}|proof_ok")],
                    [("🧭 Panel admin", "admin_panel")],
                ]),
            )
            return {"ok": True}

        telegram_send_text(
            bot_token,
            chat_id,
            "📷 Estoy esperando la foto del comprobante.",
            reply_markup=admin_fixed_kb(),
        )
        return {"ok": True}

    # =========================================================
    # 🛒 FLUJO PEDIDO MANUAL (POR TEXTO)
    # =========================================================
    admin_order_step = str(tmp.get("admin_order_step") or "").strip()

    if not admin_order_step:
        return None

    assert_admin_authorized(tenant, chat_id, tenant_id)

    if is_owner:
        _admin_order_reset(tmp)
        tmp.pop("admin_order_step", None)
        telegram_send_text(
            bot_token,
            chat_id,
            "🚫 Como propietario no puedes crear pedidos.",
            reply_markup=admin_fixed_kb(),
        )
        return {"ok": True}

    # ---------------------------------------------------------
    # 👤 NOMBRE
    # ---------------------------------------------------------
    if admin_order_step == "awaiting_name":
        customer_name = text.strip()

        if not customer_name:
            telegram_send_text(
                bot_token,
                chat_id,
                "El nombre no puede estar vacío. Escribe el nombre del cliente:",
                reply_markup=admin_fixed_kb(),
            )
            return {"ok": True}

        tmp["admin_order_name"] = customer_name
        tmp["admin_order_step"] = "awaiting_contact"

        telegram_send_text(
            bot_token,
            chat_id,
            "Escribe el contacto del cliente (teléfono o referencia):",
            reply_markup=admin_fixed_kb(),
        )
        return {"ok": True}

    # ---------------------------------------------------------
    # 📱 CONTACTO
    # ---------------------------------------------------------
    if admin_order_step == "awaiting_contact":
        customer_contact = text.strip()

        if not customer_contact:
            telegram_send_text(
                bot_token,
                chat_id,
                "El contacto no puede estar vacío. Escribe el contacto del cliente:",
                reply_markup=admin_fixed_kb(),
            )
            return {"ok": True}

        tmp["admin_order_contact"] = customer_contact
        tmp["admin_order_step"] = "awaiting_time_choice"

        telegram_send_text(
            bot_token,
            chat_id,
            "Elige cuándo se preparará el pedido:",
            reply_markup=_admin_order_time_choice_kb(tenant_id),
        )
        return {"ok": True}

    # ---------------------------------------------------------
    # ⏰ HORA MANUAL
    # ---------------------------------------------------------
    if admin_order_step == "awaiting_time_manual":
        requested_time = text.strip()

        if not requested_time:
            telegram_send_text(
                bot_token,
                chat_id,
                "Escribe una hora válida.\nEjemplos: 19:30, 20h",
                reply_markup=admin_fixed_kb(),
            )
            return {"ok": True}

        tmp["admin_order_requested_time"] = requested_time
        tmp["admin_order_step"] = "finalize_manual_order"

        return finalize_admin_manual_order(
            tenant_id=tenant_id,
            bot_token=bot_token,
            chat_id=chat_id,
            orders_sh=orders_sh,
            tmp=tmp,
        )

    # ---------------------------------------------------------
    # 🧾 FINALIZACIÓN
    # ---------------------------------------------------------
    if admin_order_step == "finalize_manual_order":
        return finalize_admin_manual_order(
            tenant_id=tenant_id,
            bot_token=bot_token,
            chat_id=chat_id,
            orders_sh=orders_sh,
            tmp=tmp,
        )

    return {"ok": True}
