# app/telegram_webhook.py — limpio SIN mensaje vacío
# hardened incremental: misma estructura, mismos contratos, más robustez

from typing import Any, Dict

from fastapi import APIRouter

from app.tenants import get_tenant_or_404, resolve_bot_by_secret
from app.sheets import get_gspread_client, open_spreadsheet_by_key, set_sheets_observation_context
from app.telegram_api import telegram_answer_callback, telegram_send_text
from app.utils import normalize, log_event
from app.webhook_helpers import safe_int, rate_limit_allow
from app.client_flow import handle_client_callback, handle_client_message
from app.admin_flow import handle_admin_callback, handle_admin_message
from app.alerts import alert_webhook_error, alert_tenant_error, alert_sheet_error

router = APIRouter()

# 🔥 CACHE SIMPLE DE SPREADSHEETS
_SHEET_CACHE = {}


def _get_orders_sheet_cached(gc, sheet_id):
    clean_sheet_id = str(sheet_id or "").strip()
    if not clean_sheet_id:
        raise RuntimeError("Missing sheet_id")

    if clean_sheet_id in _SHEET_CACHE:
        return _SHEET_CACHE[clean_sheet_id]

    sh = open_spreadsheet_by_key(gc, clean_sheet_id)
    _SHEET_CACHE[clean_sheet_id] = sh
    return sh


def invalidate_webhook_sheet_cache(sheet_id: str | None = None) -> None:
    if sheet_id is None:
        _SHEET_CACHE.clear()
        return

    clean_sheet_id = str(sheet_id or "").strip()
    if not clean_sheet_id:
        return

    _SHEET_CACHE.pop(clean_sheet_id, None)


@router.post("/telegram/webhook/{tenant_id}/{secret}")
async def telegram_webhook(tenant_id: str, secret: str, update: Dict[str, Any]):
    mode = ""
    bot_token = ""
    tenant = {}

    try:
        tenant_id = (tenant_id or "").strip()
        secret = (secret or "").strip()

        if not tenant_id:
            return {"ok": True}

        if not isinstance(update, dict):
            log_event("webhook_invalid_update_type", tenant_id=tenant_id, update_type=type(update).__name__)
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
        set_sheets_observation_context(flow_name="telegram_webhook", tenant_id=str(tenant.get("tenant_id") or tenant_id).strip())

        # -------------------------
        # Bot resolve
        # -------------------------
        try:
            mode, bot_token = resolve_bot_by_secret(tenant, secret)
        except Exception as e:
            alert_tenant_error(tenant_id=tenant_id, error=str(e))
            return {"ok": True}

        if not bot_token:
            log_event("webhook_missing_bot_token", tenant_id=tenant_id, mode=mode)
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
            set_sheets_observation_context(flow_name=f"telegram_webhook:{mode or 'unknown'}:callback")
            if not isinstance(cb, dict):
                log_event("webhook_invalid_callback_payload", tenant_id=tenant_id, mode=mode)
                return {"ok": True}

            data = (cb.get("data") or "").strip()
            cb_id = cb.get("id")

            chat_id = safe_int(((cb.get("message") or {}).get("chat") or {}).get("id"))
            if chat_id is None:
                log_event("webhook_callback_missing_chat_id", tenant_id=tenant_id, mode=mode, data=data)
                return {"ok": True}

            # rate limit defensivo para callback spam
            if not rate_limit_allow(tenant_id, chat_id, f"callback:{mode}", limit=20, window_seconds=10):
                log_event(
                    "webhook_callback_rate_limited",
                    tenant_id=tenant_id,
                    mode=mode,
                    chat_id=chat_id,
                    data=data,
                )
                return {"ok": True}

            if cb_id:
                try:
                    telegram_answer_callback(bot_token, cb_id, "OK")
                except Exception as e:
                    log_event(
                        "webhook_callback_answer_failed",
                        tenant_id=tenant_id,
                        mode=mode,
                        chat_id=chat_id,
                        error_type=type(e).__name__,
                        error=str(e),
                    )

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

            log_event("webhook_unknown_mode_callback", tenant_id=tenant_id, mode=mode, chat_id=chat_id)
            return {"ok": True}

        # -------------------------
        # MESSAGE
        # -------------------------
        msg = update.get("message") or update.get("edited_message")
        if msg:
            set_sheets_observation_context(flow_name=f"telegram_webhook:{mode or 'unknown'}:message")
            if not isinstance(msg, dict):
                log_event("webhook_invalid_message_payload", tenant_id=tenant_id, mode=mode)
                return {"ok": True}

            chat_id = safe_int((msg.get("chat") or {}).get("id"))
            if chat_id is None:
                log_event("webhook_message_missing_chat_id", tenant_id=tenant_id, mode=mode)
                return {"ok": True}

            # rate limit defensivo para spam de mensajes
            if not rate_limit_allow(tenant_id, chat_id, f"message:{mode}", limit=12, window_seconds=10):
                log_event(
                    "webhook_message_rate_limited",
                    tenant_id=tenant_id,
                    mode=mode,
                    chat_id=chat_id,
                )
                return {"ok": True}

            text = (msg.get("text") or "").strip()

            if normalize(text) in ("/id", "id"):
                try:
                    telegram_send_text(bot_token, chat_id, f"chat_id = {chat_id}")
                except Exception as e:
                    log_event(
                        "webhook_send_id_failed",
                        tenant_id=tenant_id,
                        mode=mode,
                        chat_id=chat_id,
                        error_type=type(e).__name__,
                        error=str(e),
                    )
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

            log_event("webhook_unknown_mode_message", tenant_id=tenant_id, mode=mode, chat_id=chat_id)
            return {"ok": True}

        log_event("webhook_ignored_update", tenant_id=tenant_id, mode=mode, keys=list(update.keys())[:10])
        return {"ok": True}

    except Exception as e:
        log_event(
            "webhook_unhandled_error",
            tenant_id=tenant_id,
            mode=mode,
            error_type=type(e).__name__,
            error=str(e),
        )
        alert_webhook_error(tenant_id=tenant_id, mode=mode, error=str(e))
        return {"ok": True}
