from typing import Any, Dict, Optional

from fastapi import HTTPException

from app.admin_nav import admin_panel_kb
from app.admin_settings import get_admin_setting_value, load_admin_settings, set_admin_setting_value
from app.telegram_api import telegram_send_text
from app.telegram_keyboard import kb
from app.webhook_helpers import assert_admin_authorized, get_sess


SALES_GOAL_KEYS = {
    "daily_sales_goal_amount": "Objetivo diario",
    "weekly_sales_goal_amount": "Objetivo semanal",
    "monthly_sales_goal_amount": "Objetivo mensual",
}


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


def clear_admin_sales_goals_state(tmp: Dict[str, Any]) -> None:
    tmp.pop("admin_sales_goals_mode", None)
    tmp.pop("admin_sales_goals_key", None)


def _format_goal_amount(value: str) -> str:
    clean_value = str(value or "").strip()
    if not clean_value:
        return "No configurado"

    try:
        amount = float(clean_value)
    except Exception:
        return clean_value

    if amount <= 0:
        return "No configurado"
    if abs(amount - round(amount)) < 0.000001:
        return f"Bs {int(round(amount))}"
    return f"Bs {amount:.2f}".rstrip("0").rstrip(".")


def _build_sales_goals_text(settings_map: Dict[str, Dict[str, Any]]) -> str:
    daily_value = _format_goal_amount(get_admin_setting_value(settings_map, "daily_sales_goal_amount", ""))
    weekly_value = _format_goal_amount(get_admin_setting_value(settings_map, "weekly_sales_goal_amount", ""))
    monthly_value = _format_goal_amount(get_admin_setting_value(settings_map, "monthly_sales_goal_amount", ""))

    return (
        "🎯 *OBJETIVOS DE VENTAS*\n\n"
        "Configura metas monetarias para el dashboard del restaurante.\n\n"
        f"Objetivo diario: {daily_value}\n"
        f"Objetivo semanal: {weekly_value}\n"
        f"Objetivo mensual: {monthly_value}\n\n"
        "Elige una acción:"
    )


def admin_sales_goals_home_kb(tenant_id: str):
    return kb([
        [("Cambiar objetivo diario", f"admsgoal|{tenant_id}|edit|daily_sales_goal_amount")],
        [("Cambiar objetivo semanal", f"admsgoal|{tenant_id}|edit|weekly_sales_goal_amount")],
        [("Cambiar objetivo mensual", f"admsgoal|{tenant_id}|edit|monthly_sales_goal_amount")],
        [("Limpiar objetivo diario", f"admsgoal|{tenant_id}|clear|daily_sales_goal_amount")],
        [("Limpiar objetivo semanal", f"admsgoal|{tenant_id}|clear|weekly_sales_goal_amount")],
        [("Limpiar objetivo mensual", f"admsgoal|{tenant_id}|clear|monthly_sales_goal_amount")],
        [("⬅️ Volver", f"admsgoal|{tenant_id}|panel")],
    ])


def send_admin_sales_goals_home(bot_token: str, chat_id: int, tenant_id: str, orders_sh) -> bool:
    settings_map = load_admin_settings(orders_sh, force=False)
    return _safe_send_text(
        bot_token,
        chat_id,
        _build_sales_goals_text(settings_map),
        reply_markup=admin_sales_goals_home_kb(tenant_id),
        parse_mode="Markdown",
    )


def _goal_prompt(goal_key: str) -> str:
    label = SALES_GOAL_KEYS.get(goal_key, goal_key)
    return (
        f"🎯 *{label}*\n\n"
        "Envía el monto en Bs.\n"
        "Puedes usar entero o decimal.\n"
        "Ejemplos: `500`, `1250.50`, `1250,50`"
    )


def handle_admin_sales_goals_callback(
    tenant: Dict[str, Any],
    tenant_id: str,
    bot_token: str,
    chat_id: int,
    data: str,
    orders_sh,
    get_effective_admin_role,
) -> Optional[Dict[str, Any]]:
    if data != "admin_sales_goals" and not data.startswith("admsgoal|"):
        return None

    assert_admin_authorized(tenant, chat_id, tenant_id)

    sess = get_sess(tenant_id, chat_id)
    tmp = sess.setdefault("tmp", {})

    if data == "admin_sales_goals":
        clear_admin_sales_goals_state(tmp)
        return {"ok": send_admin_sales_goals_home(bot_token, chat_id, tenant_id, orders_sh)}

    parts = data.split("|")
    if len(parts) < 3:
        return {"ok": True}

    cb_tenant_id = parts[1].strip()
    if cb_tenant_id != tenant_id:
        raise HTTPException(status_code=400, detail="Tenant mismatch in admin sales goals callback")

    action = parts[2].strip()

    if action == "home":
        clear_admin_sales_goals_state(tmp)
        return {"ok": send_admin_sales_goals_home(bot_token, chat_id, tenant_id, orders_sh)}

    if action == "panel":
        clear_admin_sales_goals_state(tmp)
        user_role = get_effective_admin_role(tenant, chat_id)
        return {
            "ok": _safe_send_text(
                bot_token,
                chat_id,
                "🧭 PANEL ADMIN\n\nElige una opción:",
                reply_markup=admin_panel_kb(user_role=user_role, tenant=tenant),
            )
        }

    if action == "edit" and len(parts) == 4:
        goal_key = parts[3].strip()
        if goal_key not in SALES_GOAL_KEYS:
            return {"ok": True}

        clear_admin_sales_goals_state(tmp)
        tmp["admin_sales_goals_mode"] = "awaiting_amount"
        tmp["admin_sales_goals_key"] = goal_key
        return {
            "ok": _safe_send_text(
                bot_token,
                chat_id,
                _goal_prompt(goal_key),
                reply_markup=admin_sales_goals_home_kb(tenant_id),
                parse_mode="Markdown",
            )
        }

    if action == "clear" and len(parts) == 4:
        goal_key = parts[3].strip()
        if goal_key not in SALES_GOAL_KEYS:
            return {"ok": True}

        settings_map = load_admin_settings(orders_sh, force=False)
        current_value = str(get_admin_setting_value(settings_map, goal_key, "") or "").strip()
        if current_value:
            set_admin_setting_value(
                orders_sh=orders_sh,
                key=goal_key,
                value="",
                updated_by=f"admin_bot:{chat_id}",
            )
        clear_admin_sales_goals_state(tmp)
        _safe_send_text(
            bot_token,
            chat_id,
            f"✅ {SALES_GOAL_KEYS[goal_key]} limpiado.",
        )
        return {"ok": send_admin_sales_goals_home(bot_token, chat_id, tenant_id, orders_sh)}

    return {"ok": True}
