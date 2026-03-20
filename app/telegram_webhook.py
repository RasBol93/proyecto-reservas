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
    tenant_id = (tenant_id or "").strip()
    if not tenant_id:
        return {"ok": True}

    gc = get_gspread_client()
    tenant = get_tenant_or_404(tenant_id, gc=gc)

    mode, bot_token = resolve_bot_by_secret(tenant, secret)
    if not bot_token:
        return {"ok": True}

    orders_sheet_id = (tenant.get("orders_sheet_id") or "").strip()
    if not orders_sheet_id:
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
            log_event("callback_missing_chat_id", tenant_id=tenant_id, data=data)
            return {"ok": True}

        if cb_id:
            telegram_answer_callback(bot_token, cb_id, "OK")

        if mode == "client":
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
            return handle_admin_callback(
                tenant=tenant,
                tenant_id=tenant_id,
                bot_token=bot_token,
                chat_id=chat_id,
                data=data,
                orders_sh=orders_sh,
                tenant_tz=tenant_tz,
            )

        return {"ok": True}

    msg = update.get("message") or update.get("edited_message")
    if msg:
        chat_id = safe_int((msg.get("chat") or {}).get("id"))
        if chat_id is None:
            return {"ok": True}

        text = (msg.get("text") or "").strip()

        if normalize(text) in ("/id", "id"):
            telegram_send_text(bot_token, chat_id, f"chat_id = {chat_id}")
            return {"ok": True}

        if mode == "client":
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
            return handle_admin_message(
                tenant=tenant,
                tenant_id=tenant_id,
                bot_token=bot_token,
                chat_id=chat_id,
                msg=msg,
                orders_sh=orders_sh,
                tenant_tz=tenant_tz,
            )

        return {"ok": True}

    return {"ok": True}
