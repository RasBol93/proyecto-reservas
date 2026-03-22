# app/admin_flow.py (HARDENED)

from typing import Any, Dict, List, Tuple
from fastapi import HTTPException

from app.menu import (
    load_menu_admin_index,
    group_menu_admin_by_category,
    get_menu_product_or_404,
    set_menu_product_active,
    set_menu_product_price,
    invalidate_menu_cache,
)
from app.orders import (
    get_order_by_id,
    update_order_status,
    append_order_row,
    gen_order_id,
    build_items_snapshot,
)
from app.telegram_api import telegram_send_text, telegram_get_file_path, telegram_download_file_bytes
from app.telegram_keyboard import kb
from app.utils import normalize, log_event
from app.stats import build_periods, resolve_period, build_stats_report_text
from app.image_storage import upload_product_photo_for_tenant
from app.webhook_helpers import (
    get_sess,
    get_client_bot_token,
    assert_admin_authorized,
    set_menu_photo_url,
    admin_fixed_kb,
    admin_periods_inline_kb,
    fmt_price_short,
    extract_first_number,
    get_business_status_safe,
    fmt_snapshot_lines,
)
from app.admin_hours import *
from app.admin_menu import *
from app.consumer_db import *


# =========================================================
# CALLBACK
# =========================================================

def handle_admin_callback(
    tenant: Dict[str, Any],
    tenant_id: str,
    bot_token: str,
    chat_id: int,
    data: str,
    orders_sh,
    tenant_tz: str,
) -> Dict[str, Any]:

    try:
        log_event("admin_callback", tenant_id=tenant_id, chat_id=chat_id, data=data)

        # =========================
        # PAGO CONFIRMADO
        # =========================
        if data.startswith("paid|"):
            parts = data.split("|")
            if len(parts) != 3:
                return {"ok": True}

            cb_tenant_id = parts[1].strip()
            order_id = parts[2].strip()

            if cb_tenant_id != tenant_id:
                raise HTTPException(status_code=400, detail="Tenant mismatch")

            assert_admin_authorized(tenant, chat_id, tenant_id)

            res = update_order_status(orders_sh, order_id, "PAID")

            if not res.get("ok"):
                log_event("admin_mark_paid_error", tenant_id=tenant_id, order_id=order_id)
                telegram_send_text(bot_token, chat_id, "⚠️ Error actualizando estado")
                return {"ok": True}

            log_event("admin_mark_paid", tenant_id=tenant_id, order_id=order_id)

            telegram_send_text(bot_token, chat_id, f"✅ Pedido {order_id} marcado como PAID")

            order = get_order_by_id(orders_sh, order_id)
            if order:
                try:
                    client_token = get_client_bot_token(tenant)
                    client_chat = (order.get("customer_contact") or "").strip()

                    if client_token and client_chat:
                        telegram_send_text(client_token, int(client_chat), f"✅ Pago confirmado. Pedido {order_id}")
                except Exception as e:
                    log_event("notify_client_failed", tenant_id=tenant_id, error=str(e))

            return {"ok": True}

        # =========================
        # PEDIDO MANUAL
        # =========================
        if data.startswith("admord|"):

            assert_admin_authorized(tenant, chat_id, tenant_id)

            parts = data.split("|")
            action = parts[2].strip()

            sess = get_sess(tenant_id, chat_id)
            tmp = sess.setdefault("tmp", {})

            if action == "confirm":

                cart = tmp.get("admin_order_cart") or []

                if not cart:
                    telegram_send_text(bot_token, chat_id, "⚠️ Carrito vacío")
                    return {"ok": True}

                tmp["admin_order_step"] = "awaiting_name"
                return {"ok": True}

            return {"ok": True}

        return {"ok": True}

    except Exception as e:
        log_event(
            "admin_callback_error",
            tenant_id=tenant_id,
            chat_id=chat_id,
            error_type=type(e).__name__,
            error=str(e),
        )
        telegram_send_text(bot_token, chat_id, "⚠️ Error en admin")
        return {"ok": True}


# =========================================================
# MESSAGES
# =========================================================

def handle_admin_message(
    tenant: Dict[str, Any],
    tenant_id: str,
    bot_token: str,
    chat_id: int,
    msg: Dict[str, Any],
    orders_sh,
    tenant_tz: str,
) -> Dict[str, Any]:

    try:
        text = (msg.get("text") or "").strip()
        txt_norm = normalize(text)

        log_event("admin_message", tenant_id=tenant_id, chat_id=chat_id, text=text[:30])

        sess = get_sess(tenant_id, chat_id)
        tmp = sess.setdefault("tmp", {})

        # =========================
        # PEDIDO MANUAL (CREACIÓN FINAL)
        # =========================
        if tmp.get("admin_order_step") == "awaiting_time":

            order_id = gen_order_id()

            result = append_order_row(
                orders_sh=orders_sh,
                tenant_id=tenant_id,
                order_id=order_id,
                customer_name=tmp.get("admin_order_name"),
                customer_contact=tmp.get("admin_order_contact"),
                items=tmp.get("admin_order_cart"),
                items_snapshot=[],
                delivery_type="pickup",
                requested_time=text,
                status="PAID",
                source="admin_manual",
                total_amount=0,
            )

            if not result.get("ok"):
                log_event("admin_manual_order_failed", tenant_id=tenant_id)
                telegram_send_text(bot_token, chat_id, "⚠️ Error guardando pedido")
                return {"ok": True}

            log_event("admin_manual_order_created", tenant_id=tenant_id, order_id=order_id)

            telegram_send_text(bot_token, chat_id, f"✅ Pedido creado {order_id}")

            return {"ok": True}

        # =========================
        # FOTO PRODUCTO
        # =========================
        if msg.get("photo"):

            try:
                file_id = msg["photo"][-1]["file_id"]

                path = telegram_get_file_path(bot_token, file_id)
                file_bytes = telegram_download_file_bytes(bot_token, path)

                photo_url = upload_product_photo_for_tenant(
                    tenant=tenant,
                    tenant_id=tenant_id,
                    sku=tmp.get("admin_menu_price_sku"),
                    file_bytes=file_bytes,
                    mime_type="image/jpeg",
                )

                log_event("admin_photo_uploaded", tenant_id=tenant_id)

                set_menu_photo_url(orders_sh, tmp.get("admin_menu_price_sku"), photo_url)

                telegram_send_text(bot_token, chat_id, "✅ Foto guardada")

            except Exception as e:
                log_event("admin_photo_error", tenant_id=tenant_id, error=str(e))
                telegram_send_text(bot_token, chat_id, "⚠️ Error subiendo foto")

            return {"ok": True}

        # =========================
        # COMANDOS BASE
        # =========================
        if txt_norm in ("start", "/start"):
            telegram_send_text(bot_token, chat_id, "Admin activo", reply_markup=admin_fixed_kb())
            return {"ok": True}

        telegram_send_text(bot_token, chat_id, "OK admin", reply_markup=admin_fixed_kb())

        return {"ok": True}

    except Exception as e:
        log_event(
            "admin_message_error",
            tenant_id=tenant_id,
            chat_id=chat_id,
            error_type=type(e).__name__,
            error=str(e),
        )
        telegram_send_text(bot_token, chat_id, "⚠️ Error admin")
        return {"ok": True}
