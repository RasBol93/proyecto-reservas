from typing import Any, Dict, Optional

from app.admin_callbacks_sales_goals import (
    SALES_GOAL_KEYS,
    clear_admin_sales_goals_state,
    send_admin_sales_goals_home,
)
from app.admin_settings import set_admin_setting_value
from app.telegram_api import telegram_send_text
from app.webhook_helpers import assert_admin_authorized


def _safe_send_text(
    bot_token: str,
    chat_id: int,
    text: str,
    reply_markup=None,
    parse_mode: Optional[str] = None,
) -> bool:
    try:
        return telegram_send_text(
            bot_token,
            chat_id,
            text,
            reply_markup=reply_markup,
            parse_mode=parse_mode,
        )
    except Exception:
        return False


def _parse_sales_goal_amount(raw_text: str) -> float:
    clean_text = str(raw_text or "").strip().replace(",", ".")
    if not clean_text:
        raise ValueError("Envía un monto válido en Bs.")

    try:
        amount = float(clean_text)
    except Exception:
        raise ValueError("Envía un monto numérico válido. Ejemplos: 500, 1250.50")

    if amount < 0:
        raise ValueError("El objetivo no puede ser negativo.")

    return amount


def _format_sales_goal_amount(amount: float) -> str:
    if abs(amount - round(amount)) < 0.000001:
        return str(int(round(amount)))
    return f"{amount:.2f}".rstrip("0").rstrip(".")


def handle_admin_sales_goals_message(
    tenant: Dict[str, Any],
    tenant_id: str,
    bot_token: str,
    chat_id: int,
    msg: Dict[str, Any],
    orders_sh,
    tmp: Dict[str, Any],
) -> Optional[Dict[str, Any]]:
    mode = str(tmp.get("admin_sales_goals_mode") or "").strip()
    if not mode:
        return None

    assert_admin_authorized(tenant, chat_id, tenant_id)

    if mode != "awaiting_amount":
        return None

    goal_key = str(tmp.get("admin_sales_goals_key") or "").strip()
    if goal_key not in SALES_GOAL_KEYS:
        clear_admin_sales_goals_state(tmp)
        return {"ok": send_admin_sales_goals_home(bot_token, chat_id, tenant_id, orders_sh)}

    raw_text = msg.get("text")
    if raw_text is None:
        _safe_send_text(
            bot_token,
            chat_id,
            "Estoy esperando un monto en Bs. Ejemplos: 500, 1250.50",
        )
        return {"ok": True}

    try:
        amount = _parse_sales_goal_amount(raw_text)
    except ValueError as e:
        _safe_send_text(
            bot_token,
            chat_id,
            str(e),
        )
        return {"ok": True}

    stored_value = _format_sales_goal_amount(amount)
    set_admin_setting_value(
        orders_sh=orders_sh,
        key=goal_key,
        value=stored_value,
        updated_by=f"admin_bot:{chat_id}",
    )

    clear_admin_sales_goals_state(tmp)

    _safe_send_text(
        bot_token,
        chat_id,
        f"✅ {SALES_GOAL_KEYS[goal_key]} guardado en Bs {stored_value}.",
    )
    return {"ok": send_admin_sales_goals_home(bot_token, chat_id, tenant_id, orders_sh)}
