from typing import Any, Dict

from fastapi import APIRouter

from app.tenants import get_tenant_or_404, resolve_bot_by_secret
from app.sheets import get_gspread_client, open_spreadsheet_by_key
from app.telegram_api import telegram_answer_callback, telegram_send_text
from app.utils import normalize, log_event
from app.webhook_helpers import safe_int
from app.client_flow import handle_client_callback, handle_client_message
from app.admin_flow import handle_admin_callback, handle_admin_message

router = APIRouter()


@router.post("/telegram/webhook/{tenant_id}/{secret}")
async def telegram_webhook(tenant_id: str, secret: str, update: Dict[str, Any]):
    try:
        tenant_id = (tenant_id or "").strip()
        if not tenant_id:
            log_event("webhook_missing_tenant_id")
            return {"ok": True}

        log_event(
            "webhook_received",
            tenant_id=tenant_id,
            has_callback=bool(update.get("callback_query")),
            has_message=bool(update.get("message")),
            has_edited_message=bool(update.get("edited_message")),
        )

        gc = get_gspread_client()
        tenant = get_tenant_or_404(tenant_id, gc=gc)

        mode, bot_token = resolve_bot_by_secret(tenant, secret)
        if not bot_token:
            log_event("webhook_invalid_secret", tenant_id=tenant_id)
            return {"ok": True}

        log_event("webhook_bot_resolved", tenant_id=tenant_id, mode=mode)

        orders_sheet_id = (tenant.get("orders_sheet_id") or "").strip()
        if not orders_sheet_id:
            log_event("webhook_missing_orders_sheet_id", tenant_id=tenant_id, mode=mode)
            return {"ok": True}

        orders_sh = open_spreadsheet_by_key(gc, orders_sheet_id)
        tenant_tz = (tenant.get("timezone") or "America/La_Paz").strip()

        cb = update.get("callback_query")
        if cb:
            data = (cb.get("data") or "").strip()
            cb_id = cb.get("id")

            msg_obj = cb.get("message") or {}
            chat_obj = msg_obj.get("chat") or {}
            chat_id = safe_int(chat_obj.get("id"))

            if chat_id is None:
                log_event(
                    "callback_missing_chat_id",
                    tenant_id=tenant_id,
                    mode=mode,
                    data=data,
                )
                return {"ok": True}

            log_event(
                "callback_received",
                tenant_id=tenant_id,
                mode=mode,
                chat_id=chat_id,
                data=data,
            )

            if cb_id:
                telegram_answer_callback(bot_token, cb_id, "OK")

            if mode == "client":
                log_event(
                    "callback_dispatch_client",
                    tenant_id=tenant_id,
                    chat_id=chat_id,
                    data=data,
                )
                return handle_client_callback(
                    tenant=tenant,
                    tenant_id=tenant_id,
                    bot_token=bot_token,
                    chat_id=chat_id,
                    data=data,
                    orders_sh=orders_sh,
                    tenant_tz=tenant_tz,
                )

            if mode == "admin":
                log_event(
                    "callback_dispatch_admin",
                    tenant_id=tenant_id,
                    chat_id=chat_id,
                    data=data,
                )
                return handle_admin_callback(
                    tenant=tenant,
                    tenant_id=tenant_id,
                    bot_token=bot_token,
                    chat_id=chat_id,
                    data=data,
                    orders_sh=orders_sh,
                    tenant_tz=tenant_tz,
                )

            log_event("callback_unknown_mode", tenant_id=tenant_id, mode=mode, chat_id=chat_id)
            return {"ok": True}

        msg = update.get("message") or update.get("edited_message")
        if msg:
            chat_id = safe_int((msg.get("chat") or {}).get("id"))
            if chat_id is None:
                log_event("message_missing_chat_id", tenant_id=tenant_id, mode=mode)
                return {"ok": True}

            text = (msg.get("text") or "").strip()

            log_event(
                "message_received",
                tenant_id=tenant_id,
                mode=mode,
                chat_id=chat_id,
                has_text=bool(text),
                has_photo=bool(msg.get("photo")),
                has_document=bool(msg.get("document")),
            )

            if normalize(text) in ("/id", "id"):
                telegram_send_text(bot_token, chat_id, f"chat_id = {chat_id}")
                log_event("message_id_command", tenant_id=tenant_id, mode=mode, chat_id=chat_id)
                return {"ok": True}

            if mode == "client":
                log_event("message_dispatch_client", tenant_id=tenant_id, chat_id=chat_id)
                return handle_client_message(
                    tenant=tenant,
                    tenant_id=tenant_id,
                    bot_token=bot_token,
                    chat_id=chat_id,
                    msg=msg,
                    orders_sh=orders_sh,
                    tenant_tz=tenant_tz,
                )

            if mode == "admin":
                log_event("message_dispatch_admin", tenant_id=tenant_id, chat_id=chat_id)
                return handle_admin_message(
                    tenant=tenant,
                    tenant_id=tenant_id,
                    bot_token=bot_token,
                    chat_id=chat_id,
                    msg=msg,
                    orders_sh=orders_sh,
                    tenant_tz=tenant_tz,
                )

            log_event("message_unknown_mode", tenant_id=tenant_id, mode=mode, chat_id=chat_id)
            return {"ok": True}

        log_event("webhook_ignored_update", tenant_id=tenant_id, mode=mode)
        return {"ok": True}

    except Exception as e:
        log_event(
            "webhook_unhandled_error",
            tenant_id=(tenant_id or "").strip(),
            error_type=type(e).__name__,
            error=str(e),
        )
        return {"ok": True}
