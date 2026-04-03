# app/telegram_webhook.py — limpio SIN mensaje vacío

from typing import Any, Dict

from fastapi import APIRouter

from app.tenants import get_tenant_or_404, resolve_bot_by_secret
from app.sheets import get_gspread_client, open_spreadsheet_by_key
from app.telegram_api import telegram_answer_callback, telegram_send_text
from app.utils import normalize, log_event
from app.webhook_helpers import safe_int
from app.client_flow import handle_client_callback, handle_client_message
from app.admin_flow import handle_admin_callback, handle_admin_message
from app.alerts import alert_webhook_error, alert_tenant_error, alert_sheet_error

router = APIRouter()

# 🔥 CACHE SIMPLE DE SPREADSHEETS
_SHEET_CACHE = {}


def _get_orders_sheet_cached(gc, sheet_id):
    if sheet_id in _SHEET_CACHE:
        return _SHEET_CACHE[sheet_id]

    sh = open_spreadsheet_by_key(gc, sheet_id)
    _SHEET_CACHE[sheet_id] = sh
    return sh


@router.post("/telegram/webhook/{tenant_id}/{secret}")
async def telegram_webhook(tenant_id: str, secret: str, update: Dict[str, Any]):
    mode = ""

    try:
        tenant_id = (tenant_id or "").strip()
        if not tenant_id:
            return {"ok": True}

        gc = get_gspread_client()

        # -------------------------
        # Tenant
        # -------------------------
        try:
            tenant = get_tenant_or_404(tenant_id, gc=gc)
        except Exception as e:
            alert_tenant_error(tenant_id=tenant_id, error=str(e))
            return {"ok": True}

        # -------------------------
        # Bot resolve
        # -------------------------
        try:
            mode, bot_token = resolve_bot_by_secret(tenant, secret)
        except Exception as e:
            alert_tenant_error(tenant_id=tenant_id, error=str(e))
            return {"ok": True}

        if not bot_token:
            return {"ok": True}

        # 🔴 detectar owner
        owner_secret = (tenant.get("webhook_secret_owner") or "").strip()
        tenant["_is_owner_bot"] = bool(owner_secret and secret == owner_secret)

        # -------------------------
        # Sheet (cache)
        # -------------------------
        orders_sheet_id = (tenant.get("orders_sheet_id") or "").strip()
        if not orders_sheet_id:
            alert_tenant_error(tenant_id=tenant_id, error="orders_sheet_id missing")
            return {"ok": True}

        try:
            orders_sh = _get_orders_sheet_cached(gc, orders_sheet_id)
        except Exception as e:
            alert_sheet_error(tenant_id=tenant_id, error=str(e))
            return {"ok": True}

        tenant_tz = (tenant.get("timezone") or "America/La_Paz").strip()

        # -------------------------
        # CALLBACK
        # -------------------------
        cb = update.get("callback_query")
        if cb:
            data = (cb.get("data") or "").strip()
            cb_id = cb.get("id")

            chat_id = safe_int(((cb.get("message") or {}).get("chat") or {}).get("id"))
            if chat_id is None:
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

        # -------------------------
        # MESSAGE
        # -------------------------
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

    except Exception as e:
        log_event("webhook_unhandled_error", tenant_id=tenant_id, error=str(e))
        alert_webhook_error(tenant_id=tenant_id, mode=mode, error=str(e))
        return {"ok": True}
