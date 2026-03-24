# app/pickup.py

from __future__ import annotations

import re
from datetime import datetime, timedelta, time
from typing import Any, Dict, List, Optional, Tuple
from zoneinfo import ZoneInfo

from fastapi import HTTPException

from app.telegram_keyboard import kb
from app.utils import normalize, to_bool, log_event
from app.webhook_helpers import get_business_status_safe


ADMIN_SETTINGS_SHEET_NAME = "AdminSettings"
ORDERS_SHEET_CANDIDATES = ["Orders", "ORDERS"]

DEFAULT_TIEMPO_MINIMO_PREPARACION_MINUTOS = 20
DEFAULT_INTERVALO_HORARIOS_RECOJO_MINUTOS = 15
DEFAULT_MAXIMO_PEDIDOS_POR_HORARIO = 3
HORIZONTE_MINUTOS_DEFAULT = 120

ESTADOS_NO_CONTAR = {
    "CANCELLED",
    "CANCELED",
    "REJECTED",
    "DECLINED",
}


# =========================================================
# Helpers base
# =========================================================

def _safe_str(v: Any) -> str:
    return str(v or "").strip()


def _to_int(v: Any, default: int) -> int:
    try:
        n = int(str(v or "").strip())
        return n
    except Exception:
        return default


def _now_local(tz_name: str) -> datetime:
    try:
        return datetime.now(ZoneInfo(tz_name or "America/La_Paz"))
    except Exception:
        return datetime.now()


def _parse_hhmm(v: str) -> Optional[time]:
    s = _safe_str(v)
    if not s:
        return None

    m = re.fullmatch(r"(\d{1,2}):(\d{2})", s)
    if not m:
        return None

    hh = int(m.group(1))
    mm = int(m.group(2))
    if hh < 0 or hh > 23 or mm < 0 or mm > 59:
        return None

    return time(hour=hh, minute=mm)


def _format_hhmm(dt: datetime) -> str:
    return dt.strftime("%H:%M")


def _time_to_datetime(base_dt: datetime, t: time) -> datetime:
    return base_dt.replace(hour=t.hour, minute=t.minute, second=0, microsecond=0)


def _round_up_datetime(dt: datetime, interval_minutes: int) -> datetime:
    if interval_minutes <= 0:
        return dt.replace(second=0, microsecond=0)

    base = dt.replace(second=0, microsecond=0)
    minutes_total = base.hour * 60 + base.minute
    rounded_total = ((minutes_total + interval_minutes - 1) // interval_minutes) * interval_minutes

    day_shift = rounded_total // (24 * 60)
    rounded_total = rounded_total % (24 * 60)

    hh = rounded_total // 60
    mm = rounded_total % 60

    out = base.replace(hour=hh, minute=mm)
    if day_shift:
        out = out + timedelta(days=day_shift)
    return out


def _open_days_to_spanish(days: List[str]) -> str:
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
        "lun": "Lunes",
        "mar": "Martes",
        "mie": "Miércoles",
        "mié": "Miércoles",
        "jue": "Jueves",
        "vie": "Viernes",
        "sab": "Sábado",
        "sáb": "Sábado",
        "dom": "Domingo",
    }

    desired_order = ["Lunes", "Martes", "Miércoles", "Jueves", "Viernes", "Sábado", "Domingo"]
    found: List[str] = []

    for d in days or []:
        x = alias_map.get(_safe_str(d), alias_map.get(_safe_str(d).upper()))
        if x and x not in found:
            found.append(x)

    ordered = [d for d in desired_order if d in found]
    return ", ".join(ordered) if ordered else "No configurado"


# =========================================================
# Lectura de AdminSettings
# =========================================================

def _get_admin_settings_ws(orders_sh):
    try:
        return orders_sh.worksheet(ADMIN_SETTINGS_SHEET_NAME)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Missing worksheet '{ADMIN_SETTINGS_SHEET_NAME}': {e}")


def _load_admin_settings_map(orders_sh) -> Dict[str, str]:
    ws = _get_admin_settings_ws(orders_sh)
    values = ws.get_all_values()
    if not values:
        return {}

    header = values[0]
    idx_map: Dict[str, int] = {}
    for i, h in enumerate(header):
        key = normalize(h)
        if key and key not in idx_map:
            idx_map[key] = i

    key_idx = idx_map.get("key")
    value_idx = idx_map.get("value")
    active_idx = idx_map.get("active")

    if key_idx is None or value_idx is None:
        raise HTTPException(status_code=500, detail="AdminSettings requires columns 'key' and 'value'")

    out: Dict[str, str] = {}

    for row in values[1:]:
        key_raw = row[key_idx] if key_idx < len(row) else ""
        val_raw = row[value_idx] if value_idx < len(row) else ""
        active_raw = row[active_idx] if active_idx is not None and active_idx < len(row) else "TRUE"

        key_norm = normalize(key_raw).replace(" ", "_")
        if not key_norm:
            continue
        if not to_bool(active_raw):
            continue

        out[key_norm] = _safe_str(val_raw)

    return out


def get_pickup_config(orders_sh) -> Dict[str, int]:
    m = _load_admin_settings_map(orders_sh)

    tiempo_minimo_preparacion_minutos = _to_int(
        m.get("tiempo_minimo_preparacion_minutos"),
        DEFAULT_TIEMPO_MINIMO_PREPARACION_MINUTOS,
    )
    intervalo_horarios_recojo_minutos = _to_int(
        m.get("intervalo_horarios_recojo_minutos"),
        DEFAULT_INTERVALO_HORARIOS_RECOJO_MINUTOS,
    )
    maximo_pedidos_por_horario = _to_int(
        m.get("maximo_pedidos_por_horario"),
        DEFAULT_MAXIMO_PEDIDOS_POR_HORARIO,
    )

    if tiempo_minimo_preparacion_minutos <= 0:
        tiempo_minimo_preparacion_minutos = DEFAULT_TIEMPO_MINIMO_PREPARACION_MINUTOS
    if intervalo_horarios_recojo_minutos <= 0:
        intervalo_horarios_recojo_minutos = DEFAULT_INTERVALO_HORARIOS_RECOJO_MINUTOS
    if maximo_pedidos_por_horario <= 0:
        maximo_pedidos_por_horario = DEFAULT_MAXIMO_PEDIDOS_POR_HORARIO

    return {
        "tiempo_minimo_preparacion_minutos": tiempo_minimo_preparacion_minutos,
        "intervalo_horarios_recojo_minutos": intervalo_horarios_recojo_minutos,
        "maximo_pedidos_por_horario": maximo_pedidos_por_horario,
    }


# =========================================================
# Lectura de Orders y conteo por horario
# =========================================================

def _get_orders_ws(orders_sh):
    for title in ORDERS_SHEET_CANDIDATES:
        try:
            return orders_sh.worksheet(title)
        except Exception:
            pass
    try:
        return orders_sh.get_worksheet(0)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Orders worksheet not found: {e}")


def _load_orders_records(orders_sh) -> List[Dict[str, str]]:
    ws = _get_orders_ws(orders_sh)
    values = ws.get_all_values()
    if not values:
        return []

    header = values[0]
    idx_map: Dict[str, int] = {}
    for i, h in enumerate(header):
        key = normalize(h)
        if key and key not in idx_map:
            idx_map[key] = i

    out: List[Dict[str, str]] = []

    for row in values[1:]:
        if not any(_safe_str(x) for x in row):
            continue

        rec: Dict[str, str] = {}
        for k, i in idx_map.items():
            rec[k] = row[i] if i < len(row) else ""
        out.append(rec)

    return out


def _extract_slot_hhmm(requested_time: str) -> Optional[str]:
    s = _safe_str(requested_time)
    if not s:
        return None

    # Si viene "Lo antes posible (18:30)" o parecido
    m = re.search(r"(\d{1,2}:\d{2})", s)
    if m:
        hhmm = m.group(1)
        return hhmm if _parse_hhmm(hhmm) else None

    return None


def count_orders_for_slot(orders_sh, slot_hhmm: str) -> int:
    slot_hhmm = _safe_str(slot_hhmm)
    if not _parse_hhmm(slot_hhmm):
        return 0

    count = 0
    rows = _load_orders_records(orders_sh)

    for r in rows:
        status = _safe_str(r.get("status")).upper()
        if status in ESTADOS_NO_CONTAR:
            continue

        requested_time = _extract_slot_hhmm(r.get("requested_time"))
        if requested_time == slot_hhmm:
            count += 1

    return count


# =========================================================
# Parsing flexible de hora manual
# =========================================================

def parse_manual_time_text(text: str) -> Optional[str]:
    s = _safe_str(text).lower()
    if not s:
        return None

    s = s.replace(".", ":")
    s = s.replace("hs", "")
    s = s.replace(" h", "")
    s = s.replace("am", " am")
    s = s.replace("pm", " pm")
    s = re.sub(r"\s+", " ", s).strip()

    # 20:15 / 8:15
    m = re.fullmatch(r"(\d{1,2}):(\d{2})(?:\s*(am|pm))?", s)
    if m:
        hh = int(m.group(1))
        mm = int(m.group(2))
        suffix = m.group(3)

        if suffix == "pm" and hh < 12:
            hh += 12
        if suffix == "am" and hh == 12:
            hh = 0

        if 0 <= hh <= 23 and 0 <= mm <= 59:
            return f"{hh:02d}:{mm:02d}"
        return None

    # 2015 / 815
    m = re.fullmatch(r"(\d{3,4})", s)
    if m:
        raw = m.group(1)
        if len(raw) == 3:
            hh = int(raw[0])
            mm = int(raw[1:])
        else:
            hh = int(raw[:2])
            mm = int(raw[2:])
        if 0 <= hh <= 23 and 0 <= mm <= 59:
            return f"{hh:02d}:{mm:02d}"
        return None

    # 8 pm / 20
    m = re.fullmatch(r"(\d{1,2})(?:\s*(am|pm))?", s)
    if m:
        hh = int(m.group(1))
        suffix = m.group(2)

        if suffix == "pm" and hh < 12:
            hh += 12
        if suffix == "am" and hh == 12:
            hh = 0

        if 0 <= hh <= 23:
            return f"{hh:02d}:00"
        return None

    return None


# =========================================================
# Validaciones de horario
# =========================================================

def _get_today_business_window(orders_sh, tenant_tz: str) -> Dict[str, Any]:
    bs = get_business_status_safe(orders_sh=orders_sh, tenant_tz=tenant_tz)

    open_time = _safe_str(bs.get("open_time"))
    close_time = _safe_str(bs.get("close_time"))
    last_order_time = _safe_str(bs.get("last_order_time"))
    weekly_open_days = bs.get("weekly_open_days") or []

    if not open_time or not close_time or not last_order_time:
        raise HTTPException(status_code=500, detail="Horario del negocio incompleto")

    now_local = _now_local(tenant_tz)
    open_t = _parse_hhmm(open_time)
    close_t = _parse_hhmm(close_time)
    last_t = _parse_hhmm(last_order_time)

    if not open_t or not close_t or not last_t:
        raise HTTPException(status_code=500, detail="Formato de horario inválido en configuración")

    return {
        "business_status": bs,
        "now_local": now_local,
        "open_dt": _time_to_datetime(now_local, open_t),
        "close_dt": _time_to_datetime(now_local, close_t),
        "last_order_dt": _time_to_datetime(now_local, last_t),
        "weekly_open_days": weekly_open_days,
        "open_time": open_time,
        "close_time": close_time,
        "last_order_time": last_order_time,
    }


def validate_pickup_hhmm(orders_sh, tenant_tz: str, hhmm: str) -> Dict[str, Any]:
    cfg = get_pickup_config(orders_sh)
    ctx = _get_today_business_window(orders_sh, tenant_tz)
    now_local = ctx["now_local"]

    slot_t = _parse_hhmm(hhmm)
    if not slot_t:
        return {"ok": False, "message": "Hora inválida. Escribe una hora como 20:15 o 8:15 pm."}

    slot_dt = _time_to_datetime(now_local, slot_t)
    earliest_dt = _round_up_datetime(
        now_local + timedelta(minutes=cfg["tiempo_minimo_preparacion_minutos"]),
        cfg["intervalo_horarios_recojo_minutos"],
    )

    if slot_dt < earliest_dt:
        return {
            "ok": False,
            "message": f"Esa hora ya no alcanza. La más pronta disponible sería {earliest_dt.strftime('%H:%M')}.",
        }

    if slot_dt < ctx["open_dt"]:
        return {
            "ok": False,
            "message": f"Esa hora está antes de la apertura. Hoy abrimos a las {ctx['open_time']}.",
        }

    if slot_dt > ctx["last_order_dt"]:
        return {
            "ok": False,
            "message": f"Esa hora supera la última hora de pedido de hoy ({ctx['last_order_time']}).",
        }

    ocupados = count_orders_for_slot(orders_sh, hhmm)
    if ocupados >= cfg["maximo_pedidos_por_horario"]:
        return {
            "ok": False,
            "message": f"Ese horario ya está completo. Elige otro por favor.",
        }

    return {
        "ok": True,
        "hhmm": hhmm,
        "ocupados": ocupados,
    }


# =========================================================
# Generación de opciones propuestas
# =========================================================

def generate_pickup_slots(
    orders_sh,
    tenant_tz: str,
    horizonte_minutos: int = HORIZONTE_MINUTOS_DEFAULT,
) -> Dict[str, Any]:
    cfg = get_pickup_config(orders_sh)
    ctx = _get_today_business_window(orders_sh, tenant_tz)
    bs = ctx["business_status"]

    now_local = ctx["now_local"]
    last_order_dt = ctx["last_order_dt"]
    open_dt = ctx["open_dt"]

    if not bool(bs.get("accepts_orders_now")):
        return {
            "ok": False,
            "message": str(bs.get("public_message") or "En este momento no estamos aceptando pedidos."),
            "slots": [],
            "config": cfg,
        }

    earliest_dt = _round_up_datetime(
        now_local + timedelta(minutes=cfg["tiempo_minimo_preparacion_minutos"]),
        cfg["intervalo_horarios_recojo_minutos"],
    )

    if earliest_dt < open_dt:
        earliest_dt = open_dt

    if earliest_dt > last_order_dt:
        return {
            "ok": False,
            "message": (
                "Lo sentimos, por hoy ya no alcanzamos a aceptar más pedidos. "
                "Por favor vuelve mañana."
            ),
            "slots": [],
            "config": cfg,
        }

    end_dt = earliest_dt + timedelta(minutes=max(horizonte_minutos, 120))
    if end_dt > last_order_dt:
        end_dt = last_order_dt

    slots: List[Dict[str, Any]] = []

    current = earliest_dt
    seen = set()

    while current <= end_dt:
        hhmm = _format_hhmm(current)

        if hhmm not in seen:
            seen.add(hhmm)
            ocupados = count_orders_for_slot(orders_sh, hhmm)
            lleno = ocupados >= cfg["maximo_pedidos_por_horario"]

            if not lleno:
                if not slots:
                    label = f"Lo antes posible ({hhmm})"
                    slot_id = "pickup|asap"
                else:
                    label = hhmm
                    slot_id = f"pickup|slot|{hhmm.replace(':', '')}"

                slots.append(
                    {
                        "id": slot_id,
                        "label": label,
                        "hhmm": hhmm,
                        "ocupados": ocupados,
                    }
                )

        current = current + timedelta(minutes=cfg["intervalo_horarios_recojo_minutos"])

    if not slots:
        return {
            "ok": False,
            "message": (
                "Lo sentimos, por hoy ya no quedan horarios disponibles dentro del tiempo permitido. "
                "Por favor vuelve mañana."
            ),
            "slots": [],
            "config": cfg,
        }

    return {
        "ok": True,
        "message": "Elige una hora de recojo:",
        "slots": slots,
        "config": cfg,
        "weekly_open_days_text": _open_days_to_spanish(ctx["weekly_open_days"]),
        "open_time": ctx["open_time"],
        "close_time": ctx["close_time"],
        "last_order_time": ctx["last_order_time"],
        "earliest_hhmm": slots[0]["hhmm"],
    }


def build_pickup_slots_kb(tenant_id: str, slots: List[Dict[str, Any]]) -> Dict[str, Any]:
    rows: List[List[Tuple[str, str]]] = []

    current_row: List[Tuple[str, str]] = []
    for s in slots:
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


# =========================================================
# Mensajes listos para usar en el flujo
# =========================================================

def build_pickup_offer_text(data: Dict[str, Any]) -> str:
    if not data.get("ok"):
        return _safe_str(data.get("message")) or "No hay horarios disponibles."

    parts = [
        "🕒 Hora de recojo",
        _safe_str(data.get("message")) or "Elige una hora de recojo:",
    ]

    weekly_text = _safe_str(data.get("weekly_open_days_text"))
    if weekly_text:
        parts.append(f"Días regulares: {weekly_text}")

    open_time = _safe_str(data.get("open_time"))
    close_time = _safe_str(data.get("close_time"))
    last_order_time = _safe_str(data.get("last_order_time"))

    if open_time and close_time:
        parts.append(f"Horario regular: {open_time} - {close_time}")
    if last_order_time:
        parts.append(f"Última hora de pedido: {last_order_time}")

    parts.append("También puedes tocar “Más tarde” y escribir otra hora manualmente.")

    return "\n\n".join(parts)
