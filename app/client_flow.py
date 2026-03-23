# app/client_flow.py

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
from app.alerts import (
    alert_order_failed,
    alert_payment_proof_failed,
    alert_payment_failed,
    alert_menu_error,
    alert_tenant_error,
    alert_system_error,
)

# 🔥 NUEVO
from app.content import (
    build_start_text,
    build_location_text,
    build_faq_text,
    build_survey_text,
    load_content_map,
    has_location,
    has_faq,
    has_survey,
)


# ================================
# NUEVO HOME DINÁMICO
# ================================
def build_dynamic_home_kb(content_map):
    rows = [
        [("📋 Ver menú", "menu")],
        [("🛒 Ver carrito", "cart")],
    ]

    if has_location(content_map):
        rows.append([("📍 Ubicación", "location")])

    rows.append([("⏰ Horarios", "hours")])

    if has_faq(content_map):
        rows.append([("❓ FAQ", "faq")])

    if has_survey(content_map):
        rows.append([("📝 Encuesta", "survey")])

    return kb(rows)


def client_orders_allowed_or_notify(bot_token: str, chat_id: int, orders_sh, tenant_tz: str) -> bool:
    try:
        bs = get_business_status_safe(orders_sh=orders_sh, tenant_tz=tenant_tz)
        if bool(bs.get("accepts_orders_now")):
            return True
        telegram_send_text(bot_token, chat_id, send_business_blocked_text(bs))
        return False
    except Exception as e:
        alert_system_error(error=str(e), module="client_orders_allowed_or_notify")
        telegram_send_text(bot_token, chat_id, "⚠️ Error verificando horario.")
        return False


# ================================
# CALLBACKS
# ================================
def handle_client_callback(
    tenant,
    tenant_id,
    bot_token,
    chat_id,
    data,
    orders_sh,
    tenant_tz,
):
    try:
        sess = get_sess(tenant_id, chat_id)

        content_map = load_content_map(orders_sh)

        # =========================
        # HOME
        # =========================
        if data == "home":
            text = build_start_text(orders_sh)
            telegram_send_text(bot_token, chat_id, text, build_dynamic_home_kb(content_map))
            return {"ok": True}

        # =========================
        # LOCATION
        # =========================
        if data == "location":
            telegram_send_text(bot_token, chat_id, build_location_text(orders_sh))
            return {"ok": True}

        # =========================
        # FAQ
        # =========================
        if data == "faq":
            telegram_send_text(bot_token, chat_id, build_faq_text(orders_sh))
            return {"ok": True}

        # =========================
        # SURVEY
        # =========================
        if data == "survey":
            telegram_send_text(bot_token, chat_id, build_survey_text(orders_sh))
            return {"ok": True}

        # =========================
        # HORARIOS
        # =========================
        if data == "hours":
            bs = get_business_status_safe(orders_sh=orders_sh, tenant_tz=tenant_tz)

            open_now = "🟢 Abierto" if bs.get("accepts_orders_now") else "🔴 Cerrado"

            txt = f"{open_now}\n\nHorario:\n{bs.get('open_time')} - {bs.get('close_time')}"
            telegram_send_text(bot_token, chat_id, txt)
            return {"ok": True}

        # =========================
        # RESTO DEL FLUJO (SIN CAMBIOS)
        # =========================

        # 👇 TODO LO DEMÁS SE MANTIENE EXACTAMENTE IGUAL
