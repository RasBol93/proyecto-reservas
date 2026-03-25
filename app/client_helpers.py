# app/client_helpers.py

from typing import Any, Dict, List

from app.telegram_api import telegram_send_text
from app.utils import log_event
from app.webhook_helpers import get_business_status_safe, send_business_blocked_text
from app.alerts import alert_system_error


def _format_open_days(days: List[str]) -> str:
    if not days:
        return "No configurado"

    alias_map = {
        "MON": "Lunes",
        "TUE": "Martes",
        "WED": "Miércoles",
        "THU": "Jueves",
        "FRI": "Viernes",
        "SAT": "Sábado",
        "SUN": "Domingo",
        "LUN": "Lunes",
        "MAR": "Martes",
        "MIE": "Miércoles",
        "MIÉ": "Miércoles",
        "JUE": "Jueves",
        "VIE": "Viernes",
        "SAB": "Sábado",
        "SÁB": "Sábado",
        "DOM": "Domingo",
    }

    normalized_days = []
    seen = set()

    for d in days:
        d_norm = str(d or "").strip().upper()
        if not d_norm:
            continue
        if d_norm in alias_map:
            nice = alias_map[d_norm]
            if nice not in seen:
                seen.add(nice)
                normalized_days.append(nice)

    if normalized_days:
        desired_order = ["Lunes", "Martes", "Miércoles", "Jueves", "Viernes", "Sábado", "Domingo"]
        ordered_names = [name for name in desired_order if name in normalized_days]
        return ", ".join(ordered_names)

    return "No configurado"


def _format_cart_detail_lines(cart: List[Dict[str, Any]], menu_idx: Dict[str, Dict[str, Any]]) -> str:
    lines: List[str] = []

    for it in cart:
        sku = str(it.get("sku") or "").strip()
        if not sku or sku not in menu_idx:
            continue

        try:
            qty = int(it.get("qty") or 0)
        except Exception:
            qty = 0

        if qty <= 0:
            continue

        name = str(menu_idx[sku].get("name") or sku).strip()
        unit_price = float(menu_idx[sku].get("price") or 0)
        line_total = unit_price * qty

        lines.append(f"• {qty} x {name} — Bs {line_total:.2f}")

    return "\n".join(lines) if lines else "Tu carrito está vacío."


def client_orders_allowed_or_notify(bot_token: str, chat_id: int, orders_sh, tenant_tz: str) -> bool:
    try:
        bs = get_business_status_safe(orders_sh=orders_sh, tenant_tz=tenant_tz)
        if bool(bs.get("accepts_orders_now")):
            return True
        telegram_send_text(bot_token, chat_id, send_business_blocked_text(bs))
        return False
    except Exception as e:
        log_event(
            "client_orders_allowed_check_error",
            chat_id=chat_id,
            error_type=type(e).__name__,
            error=str(e),
        )
        alert_system_error(error=str(e), module="client_orders_allowed_or_notify")
        telegram_send_text(bot_token, chat_id, "⚠️ Ocurrió un error verificando el horario del negocio.")
        return False
