# app/admin_callbacks_hours.py

from typing import Any, Dict, Optional

from app.webhook_helpers import assert_admin_authorized
from app.admin_hours import (
    handle_admin_hours_callback,
    send_admin_hours_menu,
)


def handle_admin_hours_routed_callback(
    tenant: Dict[str, Any],
    tenant_id: str,
    bot_token: str,
    chat_id: int,
    data: str,
    orders_sh,
    tenant_tz: str,
) -> Optional[Dict[str, Any]]:
    if data == "admin_hours":
        assert_admin_authorized(tenant, chat_id, tenant_id)
        return {"ok": send_admin_hours_menu(bot_token, chat_id, tenant_id, orders_sh, tenant_tz)}

    if not data.startswith("admhrs|"):
        return None

    assert_admin_authorized(tenant, chat_id, tenant_id)

    handled = handle_admin_hours_callback(
        bot_token=bot_token,
        chat_id=chat_id,
        tenant_id=tenant_id,
        data=data,
        orders_sh=orders_sh,
        tenant_tz=tenant_tz,
    )
    if handled.get("ok"):
        return handled

    return {"ok": True}
