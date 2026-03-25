# app/pickup.py

from __future__ import annotations

import json
import re
import secrets
from datetime import datetime, timedelta, time, timezone
from typing import Any, Dict, List, Optional, Tuple
from zoneinfo import ZoneInfo

from fastapi import HTTPException

from app.telegram_keyboard import kb
from app.utils import normalize, to_bool
from app.webhook_helpers import get_business_status_safe


ADMIN_SETTINGS_SHEET_NAME = "AdminSettings"
ORDERS_SHEET_CANDIDATES = ["Orders", "ORDERS"]

DEFAULT_TIEMPO_MINIMO_PREPARACION_MINUTOS = 20
DEFAULT_INTERVALO_HORARIOS_RECOJO_MINUTOS = 15
DEFAULT_MAXIMO_PEDIDOS_POR_HORARIO = 3
DEFAULT_BLOQUEO_MINUTOS = 5
HORIZONTE_MINUTOS_DEFAULT = 120

ESTADOS_DEFINITIVOS_OCUPAN = {
    "PAID",
    "CONFIRMED",
    "CONFIRMADO",
    "PREPARING",
    "EN_PREPARACION",
    "READY",
    "LISTO",
}

ESTADOS_NO_CONTAR = {
    "CANCELLED",
    "CANCELED",
    "REJECTED",
    "DECLINED",
    "HOLD_EXPIRED",
    "EXPIRED",
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


def _now_utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _parse_iso_datetime(v: str) -> Optional[datetime]:
    s = _safe_str(v)
    if not s:
        return None
    try:
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            return dt.replace(tzinfo=timezone.utc)
        return dt
    except Exception:
        return None


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
        "bloqueo_minutos": DEFAULT_BLOQUEO_MINUTOS,
    }


# =========================================================
# Orders helpers
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


def _get_orders_header(orders_sh) -> List[str]:
    ws = _get_orders_ws(orders_sh)
    return [str(x or "").strip() for x in ws.row_values(1)]


def _find_col_idx(header: List[str], col_name: str) -> Optional[int]:
    for i, h in enumerate(header):
        if str(h or "").strip() == str(col_name or "").strip():
            return i
    return None


def _build_row_by_header(header: List[str], data: Dict[str, Any]) -> List[str]:
    row = [""] * len(header)
    for k, v in (data or {}).items():
        idx = _find_col_idx(header, k)
        if idx is None:
            continue
        if isinstance(v, (dict, list)):
            row[idx] = json.dumps(v, ensure_ascii=False)
        elif v is None:
            row[idx] = ""
        else:
            row[idx] = str(v)
    return row


def _append_order_like_row(orders_sh, data: Dict[str, Any]) -> None:
    ws = _get_orders_ws(orders_sh)
    header = _get_orders_header(orders_sh)
    if not header:
        raise HTTPException(status_code=500, detail="ORDERS header row missing")
    row = _build_row_by_header(header, data)
    ws.append_row(row, value_input_option="RAW")


def _extract_slot_hhmm(requested_time: str) -> Optional[str]:
    s = _safe_str(requested_time)
    if not s:
        return None

    m = re.search(r"(\d{1,2}:\d{2})", s)
    if m:
        hhmm = m.group(1)
        return hhmm if _parse_hhmm(hhmm) else None

    return None


def _parse_hold_notes(notes: str) -> Dict[str, Any]:
    s = _safe_str(notes)
    if not s:
        return {}
    try:
        obj = json.loads(s)
        return obj if isinstance(obj, dict) else {}
    except Exception:
        return {}


def _is_active_hold(row: Dict[str, str], now_utc: datetime) -> bool:
    status = _safe_str(row.get("status")).upper()
    if status != "HOLD":
        return False

    meta = _parse_hold_notes(row.get("notes"))
    expires_at_raw = _safe_str(meta.get("hold_expires_at"))
    dt = _parse_iso_datetime(expires_at_raw)
    if not dt:
        return False

    return dt > now_utc


def _build_slot_counter(rows: List[Dict[str, str]]) -> Dict[str, int]:
    now_utc = datetime.now(timezone.utc)
    counter: Dict[str, int] = {}

    for r in rows:
        status = _safe_str(r.get("status")).upper()

        hhmm = _extract_slot_hhmm(r.get("requested_time"))
        if not hhmm:
            continue

        if status in ESTADOS_NO_CONTAR:
            continue

        if status in ESTADOS_DEFINITIVOS_OCUPAN:
            counter[hhmm] = counter.get(hhmm, 0) + 1
            continue

        if _is_active_hold(r, now_utc):
            counter[hhmm] = counter.get(hhmm, 0) + 1
            continue

    return counter


def count_orders_for_slot(orders_sh, slot_hhmm: str) -> int:
    slot_hhmm = _safe_str(slot_hhmm)
    if not _parse_hhmm(slot_hhmm):
        return 0

    rows = _load_orders_records(orders_sh)
    counter = _build_slot_counter(rows)
    return counter.get(slot_hhmm, 0)


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
# Ventana de negocio
# =========================================================

def _get_today_business_window(orders_sh, tenant_tz: str) -> Dict[str, Any]:
    bs = get_business_status_safe(orders_sh=orders_sh, tenant_tz=tenant_tz)

    open_time = _safe_str(bs.get("open_time"))
    close_time = _safe_str(bs.get("close_time"))
    last_order_time = _safe_str(bs.get("last_order_time"))

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
        "open_time": open_time,
        "close_time": close_time,
        "last_order_time": last_order_time,
    }


# =========================================================
# Validación / generación de horarios visibles
# =========================================================

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

    rows = _load_orders_records(orders_sh)
    counter = _build_slot_counter(rows)
    ocupados = counter.get(hhmm, 0)

    if ocupados >= cfg["maximo_pedidos_por_horario"]:
        return {
            "ok": False,
            "message": f"Ese horario ya no está disponible. Elige otro por favor.",
        }

    return {
        "ok": True,
        "hhmm": hhmm,
        "ocupados": ocupados,
    }


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

    rows = _load_orders_records(orders_sh)
    counter = _build_slot_counter(rows)

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
            ocupados = counter.get(hhmm, 0)
            lleno = ocupados >= cfg["maximo_pedidos_por_horario"]

            if not lleno:
                if not slots:
                    label = f"Ahora {hhmm}"
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
                "Lo sentimos, por ahora ya no quedan horarios disponibles. "
                "Por favor intenta nuevamente más tarde."
            ),
            "slots": [],
            "config": cfg,
        }

    return {
        "ok": True,
        "message": "Elige una hora de recojo:",
        "slots": slots,
        "config": cfg,
        "open_time": ctx["open_time"],
        "close_time": ctx["close_time"],
        "last_order_time": ctx["last_order_time"],
        "earliest_hhmm": slots[0]["hhmm"],
    }


def build_pickup_slots_kb(tenant_id: str, slots: List[Dict[str, Any]]) -> Dict[str, Any]:
    rows: List[List[Tuple[str, str]]] = []

    if slots:
        first = slots[0]
        rows.append([(first["label"], first["id"])])

    current_row: List[Tuple[str, str]] = []
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
        return _safe_str(data.get("message")) or "No hay horarios disponibles."

    open_time = _safe_str(data.get("open_time"))
    close_time = _safe_str(data.get("close_time"))
    last_order_time = _safe_str(data.get("last_order_time"))
    bloqueo_minutos = int((data.get("config") or {}).get("bloqueo_minutos") or DEFAULT_BLOQUEO_MINUTOS)

    parts = [
        "🕒 Hora de recojo",
        _safe_str(data.get("message")) or "Elige una hora de recojo:",
    ]

    if open_time and close_time:
        parts.append(f"Horario: {open_time} - {close_time}")
    if last_order_time:
        parts.append(f"Última hora de pedido: {last_order_time}")

    parts.append(
        f"ℹ️ Cuando elijas una hora, quedará bloqueada para ti por {bloqueo_minutos} minutos mientras realizas el pago."
    )
    parts.append(
        "El horario se confirma definitivamente cuando tu pago sea validado."
    )
    parts.append(
        "Si no pagas dentro del tiempo, el horario se libera automáticamente."
    )

    return "\n\n".join(parts)


# =========================================================
# Bloqueo temporal (5 min)
# =========================================================

def _build_hold_notes(
    hold_expires_at: str,
    hold_for_chat_id: str,
    hold_for_tenant_id: str,
) -> str:
    return json.dumps(
        {
            "hold_expires_at": hold_expires_at,
            "hold_for_chat_id": hold_for_chat_id,
            "hold_for_tenant_id": hold_for_tenant_id,
        },
        ensure_ascii=False,
    )


def acquire_pickup_hold(
    orders_sh,
    tenant_id: str,
    tenant_tz: str,
    client_chat_id: str,
    hhmm: str,
    hold_minutes: int = DEFAULT_BLOQUEO_MINUTOS,
) -> Dict[str, Any]:
    hhmm = _safe_str(hhmm)
    client_chat_id = _safe_str(client_chat_id)

    validation = validate_pickup_hhmm(orders_sh=orders_sh, tenant_tz=tenant_tz, hhmm=hhmm)
    if not validation.get("ok"):
        return validation

    hold_expires_at = datetime.now(timezone.utc) + timedelta(minutes=max(1, hold_minutes))
    hold_expires_at_iso = hold_expires_at.isoformat()

    hold_id = f"hold_{secrets.token_hex(4)}"

    data = {
        "order_id": hold_id,
        "created_at": _now_utc_iso(),
        "tenant_id": tenant_id,
        "customer_name": "",
        "customer_contact": client_chat_id,
        "customer_telegram_chat_id": client_chat_id,
        "items": "",
        "items_snapshot": "",
        "currency": "BOB",
        "pricing_version": "hold_v1",
        "notes": _build_hold_notes(
            hold_expires_at=hold_expires_at_iso,
            hold_for_chat_id=client_chat_id,
            hold_for_tenant_id=tenant_id,
        ),
        "delivery_type": "pickup",
        "requested_time": hhmm,
        "status": "HOLD",
        "source": "telegram_hold",
        "total_amount": 0,
        "payment_proof_file_id": "",
        "payment_confirmed_at": "",
        "payment_proof_type": "",
        "payment_proof_caption": "",
    }

    _append_order_like_row(orders_sh, data)

    return {
        "ok": True,
        "hold_id": hold_id,
        "hhmm": hhmm,
        "hold_expires_at": hold_expires_at_iso,
        "hold_minutes": hold_minutes,
        "message": (
            f"Hora {hhmm} bloqueada para ti por {hold_minutes} minutos. "
            "Completa el pago para confirmarla."
        ),
    }


# =========================================================
# Confirmación final del horario al pagar
# =========================================================

def find_next_available_slot(
    orders_sh,
    tenant_tz: str,
    start_hhmm: str,
    horizonte_minutos: int = HORIZONTE_MINUTOS_DEFAULT,
) -> Dict[str, Any]:
    data = generate_pickup_slots(
        orders_sh=orders_sh,
        tenant_tz=tenant_tz,
        horizonte_minutos=horizonte_minutos,
    )

    if not data.get("ok"):
        return data

    slots = data.get("slots") or []
    if not slots:
        return {"ok": False, "message": "No hay horarios disponibles."}

    start_t = _parse_hhmm(start_hhmm)
    if not start_t:
        return {"ok": False, "message": "Hora inicial inválida."}

    start_minutes = start_t.hour * 60 + start_t.minute

    best = None
    best_minutes = None

    for s in slots:
        hhmm = _safe_str(s.get("hhmm"))
        t = _parse_hhmm(hhmm)
        if not t:
            continue
        mins = t.hour * 60 + t.minute
        if mins >= start_minutes:
            if best is None or mins < best_minutes:
                best = s
                best_minutes = mins

    if best:
        return {"ok": True, "slot": best}

    return {"ok": False, "message": "No hay una siguiente hora disponible."}
