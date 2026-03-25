# app/pickup.py

from datetime import datetime, timedelta, time
from typing import Any, Dict, List, Optional, Tuple
from zoneinfo import ZoneInfo

from fastapi import HTTPException

from app.telegram_keyboard import kb
from app.webhook_helpers import get_business_status_safe


DEFAULT_TIEMPO_MINIMO_PREPARACION_MINUTOS = 20
DEFAULT_INTERVALO_HORARIOS_RECOJO_MINUTOS = 15
HORIZONTE_MINUTOS_DEFAULT = 120


# =========================================================
# Helpers
# =========================================================

def _safe_str(v: Any) -> str:
    return str(v or "").strip()


def _parse_hhmm(v: str) -> Optional[time]:
    s = _safe_str(v)
    try:
        hh, mm = s.split(":")
        return time(int(hh), int(mm))
    except:
        return None


def _format_hhmm(dt: datetime) -> str:
    return dt.strftime("%H:%M")


def _now_local(tz_name: str) -> datetime:
    try:
        return datetime.now(ZoneInfo(tz_name or "America/La_Paz"))
    except:
        return datetime.now()


def _round_up_datetime(dt: datetime, interval: int) -> datetime:
    minutes = dt.hour * 60 + dt.minute
    rounded = ((minutes + interval - 1) // interval) * interval
    hh = rounded // 60
    mm = rounded % 60
    return dt.replace(hour=hh, minute=mm, second=0, microsecond=0)


# =========================================================
# Config simple
# =========================================================

def get_pickup_config(orders_sh) -> Dict[str, int]:
    return {
        "tiempo_minimo_preparacion_minutos": DEFAULT_TIEMPO_MINIMO_PREPARACION_MINUTOS,
        "intervalo_horarios_recojo_minutos": DEFAULT_INTERVALO_HORARIOS_RECOJO_MINUTOS,
    }


# =========================================================
# Ventana de negocio
# =========================================================

def _get_today_business_window(orders_sh, tenant_tz: str):
    bs = get_business_status_safe(orders_sh=orders_sh, tenant_tz=tenant_tz)

    open_time = _safe_str(bs.get("open_time"))
    close_time = _safe_str(bs.get("close_time"))
    last_order_time = _safe_str(bs.get("last_order_time"))

    if not open_time or not close_time or not last_order_time:
        raise HTTPException(status_code=500, detail="Horario incompleto")

    now = _now_local(tenant_tz)

    open_dt = now.replace(hour=int(open_time[:2]), minute=int(open_time[3:]), second=0)
    close_dt = now.replace(hour=int(close_time[:2]), minute=int(close_time[3:]), second=0)
    last_dt = now.replace(hour=int(last_order_time[:2]), minute=int(last_order_time[3:]), second=0)

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
    }


# =========================================================
# Generación SIMPLE de horarios
# =========================================================

def generate_pickup_slots(orders_sh, tenant_tz: str) -> Dict[str, Any]:
    cfg = get_pickup_config(orders_sh)
    ctx = _get_today_business_window(orders_sh, tenant_tz)

    if not ctx["accepts_orders_now"]:
        return {
            "ok": False,
            "message": ctx["public_message"] or "No estamos abiertos.",
            "slots": []
        }

    now = ctx["now"]

    earliest = now + timedelta(minutes=cfg["tiempo_minimo_preparacion_minutos"])
    earliest = _round_up_datetime(earliest, cfg["intervalo_horarios_recojo_minutos"])

    if earliest < ctx["open_dt"]:
        earliest = ctx["open_dt"]

    if earliest > ctx["last_dt"]:
        return {
            "ok": False,
            "message": "Ya no estamos aceptando pedidos hoy.",
            "slots": []
        }

    slots = []
    current = earliest
    end = earliest + timedelta(minutes=HORIZONTE_MINUTOS_DEFAULT)

    if end > ctx["last_dt"]:
        end = ctx["last_dt"]

    while current <= end:
        hhmm = _format_hhmm(current)

        if not slots:
            label = f"Ahora {hhmm}"
            slot_id = "pickup|asap"
        else:
            label = hhmm
            slot_id = f"pickup|slot|{hhmm.replace(':','')}"

        slots.append({
            "id": slot_id,
            "label": label,
            "hhmm": hhmm
        })

        current += timedelta(minutes=cfg["intervalo_horarios_recojo_minutos"])

    return {
        "ok": True,
        "message": "Elige una hora de recojo:",
        "slots": slots,
        "open_time": ctx["open_time"],
        "close_time": ctx["close_time"],
        "last_order_time": ctx["last_order_time"],
    }


# =========================================================
# UI
# =========================================================

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

    return (
        "🕒 Hora de recojo\n\n"
        f"{data['message']}\n\n"
        f"Horario: {data['open_time']} - {data['close_time']}\n"
        f"Última hora de pedido: {data['last_order_time']}"
    )
