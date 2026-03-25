# app/admin_flow.py

from typing import Any, Dict

from app.admin_callbacks import handle_admin_callback_impl
from app.admin_messages import handle_admin_message_impl


def handle_admin_callback(
    tenant: Dict[str, Any],
    tenant_id: str,
    bot_token: str,
    chat_id: int,
    data: str,
    orders_sh,
    tenant_tz: str,
) -> Dict[str, Any]:
    return handle_admin_callback_impl(
        tenant=tenant,
        tenant_id=tenant_id,
        bot_token=bot_token,
        chat_id=chat_id,
        data=data,
        orders_sh=orders_sh,
        tenant_tz=tenant_tz,
    )


def handle_admin_message(
    tenant: Dict[str, Any],
    tenant_id: str,
    bot_token: str,
    chat_id: int,
    msg: Dict[str, Any],
    orders_sh,
    tenant_tz: str,
) -> Dict[str, Any]:
    return handle_admin_message_impl(
        tenant=tenant,
        tenant_id=tenant_id,
        bot_token=bot_token,
        chat_id=chat_id,
        msg=msg,
        orders_sh=orders_sh,
        tenant_tz=tenant_tz,
    )
