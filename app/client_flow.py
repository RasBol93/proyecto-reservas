# app/client_flow.py (VERSIÓN HARDENED)

import time
from typing import Any, Dict, List

from fastapi import HTTPException

from app.menu import load_menu_index, group_menu_by_category
from app.orders import (
    append_order_row,
    update_order_payment_proof,
    find_latest_pending_order_for_contact,
    get_order_by_id,
    gen_order_id,
    build_items_snapshot,
)
from app.telegram_api import telegram_send_text, telegram_send_photo
from app.telegram_keyboard import kb
from app.utils import normalize, log_event
from app.stats import log_event_to_sheet
from app.webhook_helpers import (
    REMINDER_COOLDOWN_SECONDS,
    CONTACT_AFTER_SECONDS,
    clear_sess,
    get_sess,
    get_payment_qr_file_id,
    get_payment_qr_url,
    fmt_cart_lines,
    fmt_snapshot_lines,
    build_order_recap_text,
    get_business_status_safe,
    send_business_blocked_text,
    contact_link_for_admin,
    client_home_kb,
    cart_kb,
    i_paid_kb,
    paid_actions_kb,
    contact_admin_kb,
)
from app.payment_flow import notify_admin_payment_reported


def client_orders_allowed_or_notify(bot_token, chat_id, orders_sh, tenant_tz):
    bs = get_business_status_safe(orders_sh=orders_sh, tenant_tz=tenant_tz)
    if bool(bs.get("accepts_orders_now")):
        return True
    telegram_send_text(bot_token, chat_id, send_business_blocked_text(bs))
    return False


def handle_client_callback(tenant, tenant_id, bot_token, chat_id, data, orders_sh, tenant_tz):
    try:
        sess = get_sess(tenant_id, chat_id)
        tmp = sess.get("tmp") or {}
        sess["tmp"] = tmp

        log_event("client_callback", tenant_id=tenant_id, chat_id=chat_id, data=data)

        # (NO cambio lógica existente, solo agrego logs en puntos críticos)

        if data == "menu":
            log_event("menu_open", tenant_id=tenant_id, chat_id=chat_id)

        if data.startswith("qty|"):
            log_event("cart_add", tenant_id=tenant_id, chat_id=chat_id, data=data)

        if data == "cart_confirm":
            log_event("cart_confirm", tenant_id=tenant_id, chat_id=chat_id)

        if data.startswith("i_paid|"):
            log_event("payment_reported", tenant_id=tenant_id, chat_id=chat_id, data=data)

        if data.startswith("remind|"):
            log_event("payment_reminder", tenant_id=tenant_id, chat_id=chat_id, data=data)

        if data.startswith("contact|"):
            log_event("contact_admin", tenant_id=tenant_id, chat_id=chat_id)

        # 👇 TODO el código original sin tocar
        # (pega aquí TODO tu código actual desde if data == "home" hacia abajo)
        # 👇 EXACTAMENTE COMO ESTÁ

    except Exception as e:
        log_event(
            "client_callback_error",
            tenant_id=tenant_id,
            chat_id=chat_id,
            error_type=type(e).__name__,
            error=str(e),
        )
        telegram_send_text(bot_token, chat_id, "⚠️ Ocurrió un error. Intenta nuevamente.")
        return {"ok": True}


def handle_client_message(tenant, tenant_id, bot_token, chat_id, msg, orders_sh, tenant_tz):
    try:
        text = (msg.get("text") or "").strip()
        sess = get_sess(tenant_id, chat_id)

        log_event("client_message", tenant_id=tenant_id, chat_id=chat_id, text=text[:30])

        # 👇 DETECCIÓN DE COMPROBANTE
        proof_file_id = None
        proof_type = None

        if msg.get("photo"):
            proof_file_id = msg["photo"][-1].get("file_id")
            proof_type = "photo"
        elif msg.get("document"):
            proof_file_id = (msg.get("document") or {}).get("file_id")
            proof_type = "document"

        if proof_file_id:
            log_event("proof_received", tenant_id=tenant_id, chat_id=chat_id)

            result = update_order_payment_proof(
                orders_sh=orders_sh,
                order_id=find_latest_pending_order_for_contact(
                    orders_sh=orders_sh,
                    customer_contact=str(chat_id),
                ),
                proof_file_id=proof_file_id,
                proof_type=proof_type,
            )

            if not result.get("ok"):
                log_event("proof_update_failed", tenant_id=tenant_id, chat_id=chat_id)
                telegram_send_text(bot_token, chat_id, "⚠️ Error guardando comprobante.")
                return {"ok": True}

        # 👇 CREACIÓN DE PEDIDO
        if sess.get("stage") == "awaiting_name":
            log_event("creating_order", tenant_id=tenant_id, chat_id=chat_id)

            result = append_order_row(...)

            if not result.get("ok"):
                log_event("order_create_failed", tenant_id=tenant_id, chat_id=chat_id)
                telegram_send_text(bot_token, chat_id, "⚠️ Error creando pedido.")
                return {"ok": True}

        # 👇 QR
        qr_file_id = get_payment_qr_file_id(tenant)
        qr_url = get_payment_qr_url(tenant)

        if not qr_file_id and not qr_url:
            log_event("missing_qr", tenant_id=tenant_id)

        # 👇 TODO resto código original SIN CAMBIOS

    except Exception as e:
        log_event(
            "client_message_error",
            tenant_id=tenant_id,
            chat_id=chat_id,
            error_type=type(e).__name__,
            error=str(e),
        )
        telegram_send_text(bot_token, chat_id, "⚠️ Ocurrió un error. Intenta nuevamente.")
        return {"ok": True}
