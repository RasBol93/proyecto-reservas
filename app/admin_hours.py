# app/admin_hours.py — UX nueva para días/horarios habituales y acciones del día + intervalo pickup configurable

from typing import Any, Dict, List, Tuple

from app.telegram_api import telegram_send_text
from app.telegram_keyboard import kb
from app.webhook_helpers import get_business_status_safe, get_sess
from app.admin_settings import (
    load_admin_settings,
    get_admin_setting_value,
    set_admin_setting_days,
    set_admin_setting_time,
    set_admin_setting_value,
    set_today_mode,
)
from app.utils import normalize


DAY_ORDER = ["lun", "mar", "mie", "jue", "vie", "sab", "dom"]
DAY_LABELS = {
    "lun": "Lun",
    "mar": "Mar",
    "mie": "Mié",
    "jue": "Jue",
    "vie": "Vie",
    "sab": "Sáb",
    "dom": "Dom",
}

DEFAULT_PICKUP_INTERVAL_MINUTES = 15


def compact_to_hhmm(v: str) -> str:
    s = str(v or "").strip()
    if len(s) == 4 and s.isdigit():
        return f"{s[:2]}:{s[2:]}"
    return s


def _safe_int(v: Any, default: int) -> int:
    try:
        n = int(str(v or "").strip())
        if n <= 0:
            return default
        return n
    except Exception:
        return default


def _hour_rows(prefix: str) -> List[List[Tuple[str, str]]]:
    rows = []
    current = []
    for h in range(0, 24):
        hh = f"{h:02d}"
        current.append((hh, f"{prefix}|{hh}"))
        if len(current) == 6:
            rows.append(current)
            current = []
    if current:
        rows.append(current)
    return rows


def _minute_rows(prefix: str) -> List[List[Tuple[str, str]]]:
    mins = ["00", "15", "30", "45"]
    return [[(m, f"{prefix}|{m}") for m in mins]]


def _format_slots_for_admin(settings_map: Dict[str, Dict[str, Any]]) -> str:
    mode = get_admin_setting_value(settings_map, "weekly_slot_mode", "1").strip()
    s1o = get_admin_setting_value(settings_map, "weekly_slot1_open", "")
    s1c = get_admin_setting_value(settings_map, "weekly_slot1_close", "")
    s2o = get_admin_setting_value(settings_map, "weekly_slot2_open", "")
    s2c = get_admin_setting_value(settings_map, "weekly_slot2_close", "")

    if mode == "2":
        return f"1) {s1o}-{s1c}\n2) {s2o}-{s2c}"
    return f"1) {s1o}-{s1c}"


def _days_summary(settings_map: Dict[str, Dict[str, Any]]) -> str:
    weekly_days = get_admin_setting_value(settings_map, "weekly_open_days", "")
    raw_days = [x.strip() for x in weekly_days.split(",") if x.strip()]
    labels = [DAY_LABELS.get(d, d) for d in raw_days]
    return ", ".join(labels) if labels else "No definido"


def _pickup_interval_value(settings_map: Dict[str, Dict[str, Any]]) -> int:
    return _safe_int(
        get_admin_setting_value(settings_map, "pickup_interval_minutes", str(DEFAULT_PICKUP_INTERVAL_MINUTES)),
        DEFAULT_PICKUP_INTERVAL_MINUTES,
    )


def _pickup_interval_label(settings_map: Dict[str, Dict[str, Any]]) -> str:
    return f"{_pickup_interval_value(settings_map)} min"


def _hours_root_kb() -> Dict[str, Any]:
    return kb([
        [("📅 Días habituales", "admhrs|days")],
        [("🕒 Horarios habituales", "admhrs|hours")],
        [("⏱ Intervalo recojo", "admhrs|pickup_interval")],
        [("🟢 Abrir ahora", "admhrs|open_now")],
        [("🔴 Cerrar ahora", "admhrs|close_now")],
        [("⛔ No abrir hoy", "admhrs|closed_today")],
        [("♻️ Volver a lo habitual", "admhrs|restore")],
        [("⬅️ Volver", "admin_panel")],
        [("🧭 Panel admin", "admin_panel")],
    ])


def _pickup_interval_menu_kb() -> Dict[str, Any]:
    return kb([
        [("10 min", "admhrs|pickupset|10"), ("15 min", "admhrs|pickupset|15")],
        [("20 min", "admhrs|pickupset|20"), ("30 min", "admhrs|pickupset|30")],
        [("45 min", "admhrs|pickupset|45"), ("60 min", "admhrs|pickupset|60")],
        [("✍️ Escribir otro valor", "admhrs|pickupcustom")],
        [("⬅️ Volver", "admhrs|menu")],
        [("🧭 Panel admin", "admin_panel")],
    ])


def send_admin_hours_menu(bot_token: str, chat_id: int, tenant_id: str, orders_sh, tenant_tz: str) -> bool:
    settings = load_admin_settings(orders_sh)
    bs = get_business_status_safe(orders_sh=orders_sh, tenant_tz=tenant_tz)

    current_mode = str(bs.get("today_mode") or "habitual")
    slots_txt = _format_slots_for_admin(settings)
    days_txt = _days_summary(settings)
    pickup_interval_txt = _pickup_interval_label(settings)

    mode_label = {
        "habitual": "Habitual",
        "open_now": "Abrir ahora",
        "closed_now": "Cerrar ahora",
        "closed_today": "No abrir hoy",
    }.get(current_mode, current_mode)

    msg = (
        "⚙️ CONFIG DÍAS Y HORARIOS\n\n"
        f"Días habituales: {days_txt}\n"
        f"Horarios habituales:\n{slots_txt}\n\n"
        f"Intervalo de recojo: {pickup_interval_txt}\n\n"
        f"Estado de hoy: {mode_label}"
    )

    return telegram_send_text(
        bot_token,
        chat_id,
        msg,
        reply_markup=_hours_root_kb(),
    )


def send_admin_days_menu(bot_token: str, chat_id: int, tenant_id: str, sess: Dict[str, Any], bs: Dict[str, Any]) -> bool:
    current = set(sess.setdefault("tmp", {}).get("admin_days_selected") or bs.get("weekly_open_days") or [])

    rows: List[List[Tuple[str, str]]] = []
    current_row: List[Tuple[str, str]] = []

    for day in DAY_ORDER:
        mark = "✅" if day in current else "⬜"
        current_row.append((f"{mark} {DAY_LABELS[day]}", f"admhrs|dayt|{day}"))
        if len(current_row) == 4:
            rows.append(current_row)
            current_row = []

    if current_row:
        rows.append(current_row)

    rows.append([("💾 Guardar días", "admhrs|dayssave")])
    rows.append([("⬅️ Volver", "admhrs|menu")])
    rows.append([("🧭 Panel admin", "admin_panel")])

    return telegram_send_text(
        bot_token,
        chat_id,
        "📅 DÍAS HABITUALES\n\nSelecciona los días en los que abre normalmente:",
        reply_markup=kb(rows),
    )


def send_admin_hours_mode_menu(bot_token: str, chat_id: int, tenant_id: str) -> bool:
    return telegram_send_text(
        bot_token,
        chat_id,
        "🕒 HORARIOS HABITUALES\n\n¿Quieres configurar 1 horario o 2 horarios?",
        reply_markup=kb([
            [("1 horario", "admhrs|hmode|1")],
            [("2 horarios", "admhrs|hmode|2")],
            [("⬅️ Volver", "admhrs|menu")],
            [("🧭 Panel admin", "admin_panel")],
        ]),
    )


def send_pickup_interval_menu(bot_token: str, chat_id: int, orders_sh) -> bool:
    settings = load_admin_settings(orders_sh)
    current_value = _pickup_interval_value(settings)

    return telegram_send_text(
        bot_token,
        chat_id,
        (
            "⏱ INTERVALO DE RECOJO\n\n"
            "Este valor se usa para dos cosas:\n"
            "1) “Lo antes posible” = ahora + X minutos\n"
            "2) Las demás opciones = bloques siguientes de X en X\n\n"
            f"Valor actual: {current_value} min\n\n"
            "Elige un valor o escribe uno manualmente:"
        ),
        reply_markup=_pickup_interval_menu_kb(),
    )


def send_hour_picker(bot_token: str, chat_id: int, title: str, prefix: str) -> bool:
    rows = _hour_rows(prefix)
    rows.append([("⬅️ Volver", "admhrs|hours")])
    rows.append([("🧭 Panel admin", "admin_panel")])
    return telegram_send_text(
        bot_token,
        chat_id,
        title,
        reply_markup=kb(rows),
    )


def send_minute_picker(bot_token: str, chat_id: int, title: str, prefix: str) -> bool:
    rows = _minute_rows(prefix)
    rows.append([("⬅️ Volver", "admhrs|hours")])
    rows.append([("🧭 Panel admin", "admin_panel")])
    return telegram_send_text(
        bot_token,
        chat_id,
        title,
        reply_markup=kb(rows),
    )


def _slot_summary(tmp: Dict[str, Any]) -> str:
    mode = str(tmp.get("admin_hours_mode") or "1")
    s1o = str(tmp.get("slot1_open") or "--:--")
    s1c = str(tmp.get("slot1_close") or "--:--")
    if mode == "2":
        s2o = str(tmp.get("slot2_open") or "--:--")
        s2c = str(tmp.get("slot2_close") or "--:--")
        return f"1) {s1o}-{s1c}\n2) {s2o}-{s2c}"
    return f"1) {s1o}-{s1c}"


def _save_weekly_hours(orders_sh, tmp: Dict[str, Any], updated_by: str) -> None:
    mode = str(tmp.get("admin_hours_mode") or "1")
    slot1_open = str(tmp.get("slot1_open") or "").strip()
    slot1_close = str(tmp.get("slot1_close") or "").strip()
    slot2_open = str(tmp.get("slot2_open") or "").strip()
    slot2_close = str(tmp.get("slot2_close") or "").strip()

    set_admin_setting_value(orders_sh, "weekly_slot_mode", mode, updated_by=updated_by)
    set_admin_setting_time(orders_sh, "weekly_slot1_open", slot1_open, updated_by=updated_by)
    set_admin_setting_time(orders_sh, "weekly_slot1_close", slot1_close, updated_by=updated_by)

    if mode == "2":
        set_admin_setting_time(orders_sh, "weekly_slot2_open", slot2_open, updated_by=updated_by)
        set_admin_setting_time(orders_sh, "weekly_slot2_close", slot2_close, updated_by=updated_by)
    else:
        set_admin_setting_value(orders_sh, "weekly_slot2_open", "", updated_by=updated_by)
        set_admin_setting_value(orders_sh, "weekly_slot2_close", "", updated_by=updated_by)


def _combine_hm(hour: str, minute: str) -> str:
    return f"{int(hour):02d}:{int(minute):02d}"


def handle_admin_hours_callback(
    bot_token: str,
    chat_id: int,
    tenant_id: str,
    data: str,
    orders_sh,
    tenant_tz: str,
) -> Dict[str, Any]:
    sess = get_sess(tenant_id, chat_id)
    tmp = sess.setdefault("tmp", {})
    updated_by = f"admin_bot:{chat_id}"

    if data == "admhrs|menu":
        return {"ok": send_admin_hours_menu(bot_token, chat_id, tenant_id, orders_sh, tenant_tz)}

    if data == "admhrs|days":
        bs = get_business_status_safe(orders_sh=orders_sh, tenant_tz=tenant_tz)
        tmp["admin_days_selected"] = list(bs.get("weekly_open_days") or [])
        return {"ok": send_admin_days_menu(bot_token, chat_id, tenant_id, sess, bs)}

    if data.startswith("admhrs|dayt|"):
        code = data.split("|", 2)[2].strip()
        current = set(tmp.get("admin_days_selected") or [])
        if code in current:
            current.remove(code)
        else:
            current.add(code)
        tmp["admin_days_selected"] = [d for d in DAY_ORDER if d in current]
        bs = get_business_status_safe(orders_sh=orders_sh, tenant_tz=tenant_tz)
        return {"ok": send_admin_days_menu(bot_token, chat_id, tenant_id, sess, bs)}

    if data == "admhrs|dayssave":
        selected = [d for d in DAY_ORDER if d in set(tmp.get("admin_days_selected") or [])]
        set_admin_setting_days(orders_sh, "weekly_open_days", selected, updated_by=updated_by)
        telegram_send_text(
            bot_token,
            chat_id,
            "✅ Días habituales guardados.",
        )
        return {"ok": send_admin_hours_menu(bot_token, chat_id, tenant_id, orders_sh, tenant_tz)}

    if data == "admhrs|hours":
        tmp.pop("admin_hours_mode", None)
        tmp.pop("slot1_open", None)
        tmp.pop("slot1_close", None)
        tmp.pop("slot2_open", None)
        tmp.pop("slot2_close", None)
        return {"ok": send_admin_hours_mode_menu(bot_token, chat_id, tenant_id)}

    if data == "admhrs|pickup_interval":
        return {"ok": send_pickup_interval_menu(bot_token, chat_id, orders_sh)}

    if data.startswith("admhrs|pickupset|"):
        value_raw = data.split("|", 2)[2].strip()
        interval = _safe_int(value_raw, DEFAULT_PICKUP_INTERVAL_MINUTES)
        set_admin_setting_value(
            orders_sh,
            "pickup_interval_minutes",
            str(interval),
            updated_by=updated_by,
        )
        telegram_send_text(
            bot_token,
            chat_id,
            f"✅ Intervalo de recojo actualizado a {interval} min.",
        )
        return {"ok": send_admin_hours_menu(bot_token, chat_id, tenant_id, orders_sh, tenant_tz)}

    if data == "admhrs|pickupcustom":
        tmp["admin_hours_mode_input"] = "pickup_interval_custom"
        telegram_send_text(
            bot_token,
            chat_id,
            "Escribe el nuevo intervalo en minutos.\nEjemplos: 12, 15, 20, 30",
        )
        return {"ok": True}

    if data.startswith("admhrs|hmode|"):
        mode = data.split("|", 2)[2].strip()
        mode = "2" if mode == "2" else "1"
        tmp["admin_hours_mode"] = mode
        return {
            "ok": send_hour_picker(
                bot_token,
                chat_id,
                "Horario 1 — Elige la HORA de apertura:",
                "admhrs|s1oh",
            )
        }

    if data.startswith("admhrs|s1oh|"):
        hour = data.split("|", 2)[2].strip()
        tmp["slot1_open_hour"] = hour
        return {
            "ok": send_minute_picker(
                bot_token,
                chat_id,
                f"Horario 1 — Hora de apertura: {hour}:__\nElige los MINUTOS:",
                "admhrs|s1om",
            )
        }

    if data.startswith("admhrs|s1om|"):
        minute = data.split("|", 2)[2].strip()
        hour = str(tmp.get("slot1_open_hour") or "00")
        tmp["slot1_open"] = _combine_hm(hour, minute)
        return {
            "ok": send_hour_picker(
                bot_token,
                chat_id,
                "Horario 1 — Elige la HORA de cierre:",
                "admhrs|s1ch",
            )
        }

    if data.startswith("admhrs|s1ch|"):
        hour = data.split("|", 2)[2].strip()
        tmp["slot1_close_hour"] = hour
        return {
            "ok": send_minute_picker(
                bot_token,
                chat_id,
                f"Horario 1 — Hora de cierre: {hour}:__\nElige los MINUTOS:",
                "admhrs|s1cm",
            )
        }

    if data.startswith("admhrs|s1cm|"):
        minute = data.split("|", 2)[2].strip()
        hour = str(tmp.get("slot1_close_hour") or "00")
        tmp["slot1_close"] = _combine_hm(hour, minute)

        if str(tmp.get("admin_hours_mode") or "1") == "2":
            return {
                "ok": send_hour_picker(
                    bot_token,
                    chat_id,
                    "Horario 2 — Elige la HORA de apertura:",
                    "admhrs|s2oh",
                )
            }

        _save_weekly_hours(orders_sh, tmp, updated_by)
        telegram_send_text(
            bot_token,
            chat_id,
            f"✅ Horarios habituales guardados.\n\n{_slot_summary(tmp)}",
        )
        return {"ok": send_admin_hours_menu(bot_token, chat_id, tenant_id, orders_sh, tenant_tz)}

    if data.startswith("admhrs|s2oh|"):
        hour = data.split("|", 2)[2].strip()
        tmp["slot2_open_hour"] = hour
        return {
            "ok": send_minute_picker(
                bot_token,
                chat_id,
                f"Horario 2 — Hora de apertura: {hour}:__\nElige los MINUTOS:",
                "admhrs|s2om",
            )
        }

    if data.startswith("admhrs|s2om|"):
        minute = data.split("|", 2)[2].strip()
        hour = str(tmp.get("slot2_open_hour") or "00")
        tmp["slot2_open"] = _combine_hm(hour, minute)
        return {
            "ok": send_hour_picker(
                bot_token,
                chat_id,
                "Horario 2 — Elige la HORA de cierre:",
                "admhrs|s2ch",
            )
        }

    if data.startswith("admhrs|s2ch|"):
        hour = data.split("|", 2)[2].strip()
        tmp["slot2_close_hour"] = hour
        return {
            "ok": send_minute_picker(
                bot_token,
                chat_id,
                f"Horario 2 — Hora de cierre: {hour}:__\nElige los MINUTOS:",
                "admhrs|s2cm",
            )
        }

    if data.startswith("admhrs|s2cm|"):
        minute = data.split("|", 2)[2].strip()
        hour = str(tmp.get("slot2_close_hour") or "00")
        tmp["slot2_close"] = _combine_hm(hour, minute)

        _save_weekly_hours(orders_sh, tmp, updated_by)
        telegram_send_text(
            bot_token,
            chat_id,
            f"✅ Horarios habituales guardados.\n\n{_slot_summary(tmp)}",
        )
        return {"ok": send_admin_hours_menu(bot_token, chat_id, tenant_id, orders_sh, tenant_tz)}

    if data == "admhrs|open_now":
        set_today_mode(orders_sh, "open_now", tenant_tz=tenant_tz, updated_by=updated_by)
        telegram_send_text(
            bot_token,
            chat_id,
            "✅ El negocio quedó abierto desde ahora para el día de hoy.",
        )
        return {"ok": send_admin_hours_menu(bot_token, chat_id, tenant_id, orders_sh, tenant_tz)}

    if data == "admhrs|close_now":
        set_today_mode(orders_sh, "closed_now", tenant_tz=tenant_tz, updated_by=updated_by)
        telegram_send_text(
            bot_token,
            chat_id,
            "✅ El negocio quedó cerrado desde ahora para el día de hoy.",
        )
        return {"ok": send_admin_hours_menu(bot_token, chat_id, tenant_id, orders_sh, tenant_tz)}

    if data == "admhrs|closed_today":
        set_today_mode(orders_sh, "closed_today", tenant_tz=tenant_tz, updated_by=updated_by)
        telegram_send_text(
            bot_token,
            chat_id,
            "✅ Hoy quedó marcado como no abrir.",
        )
        return {"ok": send_admin_hours_menu(bot_token, chat_id, tenant_id, orders_sh, tenant_tz)}

    if data == "admhrs|restore":
        set_today_mode(orders_sh, "habitual", tenant_tz=tenant_tz, updated_by=updated_by)
        telegram_send_text(
            bot_token,
            chat_id,
            "✅ Se volvió a lo habitual para hoy.",
        )
        return {"ok": send_admin_hours_menu(bot_token, chat_id, tenant_id, orders_sh, tenant_tz)}

    return {"ok": False}
