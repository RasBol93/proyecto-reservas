# app/pickup.py — generación de horarios de recojo configurable desde AdminSettings

from datetime import datetime, timedelta, time
from typing import Any, Dict, List, Optional
from zoneinfo import ZoneInfo

from fastapi import HTTPException

from app.telegram_keyboard import kb
from app.webhook_helpers import get_business_status_safe
from app.admin_settings import load_admin_settings, get_admin_setting_value


DEFAULT_PICKUP_INTERVAL_MINUTES = 15
HORIZONTE_MINUTOS_DEFAULT = 120


def _safe_str(v: Any) -> str:
    return str(v or "").strip()


def _safe_int(v: Any, default: int) -> int:
    try:
        n = int(str(v or "").strip())
        if n <= 0:
            return default
        return n
    except Exception:
        return default


def _parse_hhmm(v: str) -> Optional[time]:
    s = _safe_str(v)
    try:
        hh, mm = s.split(":")
        return time(int(hh), int(mm))
    except Exception:
        return None


def _format_hhmm(dt: datetime) -> str:
    return dt.strftime("%H:%M")


def _now_local(tz_name: str) -> datetime:
    try:
        return datetime.now(ZoneInfo(tz_name or "America/La_Paz"))
    except Exception:
        return datetime.now()


def _round_up_datetime(dt: datetime, interval: int) -> datetime:
    interval = max(1, int(interval))
    minutes = dt.hour * 60 + dt.minute
    rounded = ((minutes + interval - 1) // interval) * interval
    hh = min(23, rounded // 60)
    mm = rounded % 60
    return dt.replace(hour=hh, minute=mm, second=0, microsecond=0)


def get_pickup_config(orders_sh) -> Dict[str, int]:
    try:
        settings = load_admin_settings(orders_sh)
        interval = _safe_int(
            get_admin_setting_value(settings, "pickup_interval_minutes", str(DEFAULT_PICKUP_INTERVAL_MINUTES)),
            DEFAULT_PICKUP_INTERVAL_MINUTES,
        )
        return {
            "pickup_interval_minutes": interval,
            "pickup_lead_time_minutes": interval,
        }
    except Exception:
        return {
            "pickup_interval_minutes": DEFAULT_PICKUP_INTERVAL_MINUTES,
            "pickup_lead_time_minutes": DEFAULT_PICKUP_INTERVAL_MINUTES,
        }


def _build_dt(now: datetime, hhmm: str) -> datetime:
    hh, mm = hhmm.split(":")
    return now.replace(hour=int(hh), minute=int(mm), second=0, microsecond=0)


def _get_today_business_window(orders_sh, tenant_tz: str):
    bs = get_business_status_safe(orders_sh=orders_sh, tenant_tz=tenant_tz)

    now = _now_local(tenant_tz)

    open_time = _safe_str(bs.get("open_time"))
    close_time = _safe_str(bs.get("close_time"))
    last_order_time = _safe_str(bs.get("last_order_time"))
    today_slots = bs.get("today_slots") or []

    if not isinstance(today_slots, list):
        today_slots = []

    slot_windows = []
    for slot in today_slots:
        if not isinstance(slot, (list, tuple)) or len(slot) != 2:
            continue
        slot_open = _safe_str(slot[0])
        slot_close = _safe_str(slot[1])
        if not slot_open or not slot_close:
            continue
        slot_windows.append({
            "open_time": slot_open,
            "close_time": slot_close,
            "open_dt": _build_dt(now, slot_open),
            "close_dt": _build_dt(now, slot_close),
        })

    if not open_time or not close_time or not last_order_time:
        if not bs.get("accepts_orders_now"):
            return {
                "now": now,
                "open_dt": None,
                "close_dt": None,
                "last_dt": None,
                "open_time": "",
                "close_time": "",
                "last_order_time": "",
                "accepts_orders_now": False,
                "public_message": bs.get("public_message"),
                "today_slots": slot_windows,
            }
        raise HTTPException(status_code=500, detail="Horario incompleto")

    open_dt = _build_dt(now, open_time)
    close_dt = _build_dt(now, close_time)
    last_dt = _build_dt(now, last_order_time)

    return {
        "now": now,
        "open_dt": open_dt,
        "close_dt": close_dt,
        "last_dt": last_dt,
        "open_time": open_time,
        "close_time": close_time,
        "last_order_time": last_order_time,
        "accepts_orders_now": bool(bs.get("accepts_orders_now")),
        "public_message": bs.get("public_message"),
        "today_slots": slot_windows,
    }


def generate_pickup_slots(orders_sh, tenant_tz: str) -> Dict[str, Any]:
    cfg = get_pickup_config(orders_sh)
    ctx = _get_today_business_window(orders_sh, tenant_tz)
    interval = max(1, int(cfg["pickup_interval_minutes"]))

    base_response = {
        "open_time": ctx["open_time"],
        "close_time": ctx["close_time"],
        "last_order_time": ctx["last_order_time"],
        "pickup_interval_minutes": interval,
    }

    if not ctx["accepts_orders_now"]:
        return {
            "ok": False,
            "message": ctx["public_message"] or "No estamos abiertos.",
            "slots": [],
            **base_response,
        }

    now = ctx["now"]
    lead = max(1, int(cfg["pickup_lead_time_minutes"]))

    earliest_asap = now + timedelta(minutes=lead)

    first_interval_slot = _round_up_datetime(earliest_asap, interval)
    if ctx["open_dt"] and first_interval_slot < ctx["open_dt"]:
        first_interval_slot = ctx["open_dt"]

    asap_hhmm = _format_hhmm(earliest_asap)

    if ctx["last_dt"] and earliest_asap > ctx["last_dt"]:
        return {
            "ok": False,
            "message": "Ya no estamos aceptando pedidos hoy.",
            "slots": [],
            **base_response,
        }

    if ctx["last_dt"] and first_interval_slot > ctx["last_dt"]:
        slots = [{
            "id": "pickup|asap",
            "label": f"Lo antes posible ({asap_hhmm})",
            "hhmm": asap_hhmm,
        }]
        return {
            "ok": True,
            "message": "Elige una hora de recojo:",
            "slots": slots,
            **base_response,
        }

    slots = [{
        "id": "pickup|asap",
        "label": f"Lo antes posible ({asap_hhmm})",
        "hhmm": asap_hhmm,
    }]

    current = first_interval_slot
    end = first_interval_slot + timedelta(minutes=HORIZONTE_MINUTOS_DEFAULT)

    if ctx["last_dt"] and end > ctx["last_dt"]:
        end = ctx["last_dt"]

    while current <= end:
        hhmm = _format_hhmm(current)
        slots.append({
            "id": f"pickup|slot|{hhmm.replace(':', '')}",
            "label": hhmm,
            "hhmm": hhmm,
        })
        current += timedelta(minutes=interval)

    return {
        "ok": True,
        "message": "Elige una hora de recojo:",
        "slots": slots,
        **base_response,
    }


def build_pickup_slots_kb(tenant_id: str, slots: List[Dict[str, Any]]) -> Dict[str, Any]:
    rows = []

    if slots:
        rows.append([(slots[0]["label"], slots[0]["id"])])

    current_row = []
    for s in slots[1:]:
        current_row.append((s["label"], s["id"]))
        if len(current_row) == 2:
            rows.append(current_row)
            current_row = []

    if current_row:
        rows.append(current_row)

    rows.append([("Más tarde", "pickup|custom")])
    rows.append([("⬅️ Volver al carrito", "cart")])
    rows.append([("🏠 Inicio", "home")])

    return kb(rows)


def build_pickup_offer_text(data: Dict[str, Any]) -> str:
    if not data.get("ok"):
        return data.get("message", "No hay horarios.")

    interval = int(data.get("pickup_interval_minutes") or DEFAULT_PICKUP_INTERVAL_MINUTES)

    return (
        "🕒 Hora de recojo\n\n"
        f"{data['message']}\n\n"
        f"Intervalo configurado: {interval} min\n"
        f"Horario actual: {data['open_time']} - {data['close_time']}\n"
        f"Última hora de pedido actual: {data['last_order_time']}"
    )
