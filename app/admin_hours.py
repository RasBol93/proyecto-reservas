from typing import Any, Dict, Optional, List, Tuple, Set

from fastapi import HTTPException

from app.telegram_api import telegram_send_text
from app.telegram_keyboard import kb
from app.utils import normalize, now_iso_utc
from app.webhook_helpers import get_business_status_safe

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

ADMIN_SETTINGS_SHEET_NAME = "AdminSettings"


def get_admin_settings_ws(orders_sh):
    try:
        return orders_sh.worksheet(ADMIN_SETTINGS_SHEET_NAME)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Missing worksheet '{ADMIN_SETTINGS_SHEET_NAME}': {e}")


def admin_headers_map(ws) -> Dict[str, int]:
    values = ws.get_all_values()
    if not values:
        raise HTTPException(status_code=500, detail="AdminSettings is empty")

    header = values[0]
    out: Dict[str, int] = {}
    for i, h in enumerate(header):
        k = normalize(h)
        if k and k not in out:
            out[k] = i + 1
    return out


def admin_find_row_by_key(ws, key: str) -> Optional[int]:
    values = ws.get_all_values()
    if not values:
        return None

    key_norm = normalize(key).replace(" ", "_")
    for ridx in range(2, len(values) + 1):
        row = values[ridx - 1]
        cell = row[0] if len(row) >= 1 else ""
        if normalize(cell).replace(" ", "_") == key_norm:
            return ridx
    return None


def admin_upsert_setting(
    orders_sh,
    key: str,
    value: str,
    scope: str,
    updated_by: str,
    notes: str = "",
    active: str = "TRUE",
) -> None:
    ws = get_admin_settings_ws(orders_sh)
    headers = admin_headers_map(ws)
    ridx = admin_find_row_by_key(ws, key)

    now_ts = now_iso_utc()
    key_val = str(key or "").strip()
    value_val = str(value or "").strip()
    scope_val = str(scope or "").strip()
    updated_by_val = str(updated_by or "").strip()
    notes_val = str(notes or "").strip()
    active_val = str(active or "TRUE").strip()

    if ridx is None:
        header_len = max(headers.values()) if headers else 7
        row = [""] * header_len

        def put(col_name: str, val: str) -> None:
            c = headers.get(normalize(col_name))
            if c:
                row[c - 1] = val

        put("key", key_val)
        put("value", value_val)
        put("active", active_val)
        put("scope", scope_val)
        put("updated_at", now_ts)
        put("updated_by", updated_by_val)
        put("notes", notes_val)

        ws.append_row(row, value_input_option="RAW")
        return

    def update_if_exists(col_name: str, val: str) -> None:
        c = headers.get(normalize(col_name))
        if c:
            ws.update_cell(ridx, c, val)

    update_if_exists("key", key_val)
    update_if_exists("value", value_val)
    update_if_exists("active", active_val)
    update_if_exists("scope", scope_val)
    update_if_exists("updated_at", now_ts)
    update_if_exists("updated_by", updated_by_val)
    update_if_exists("notes", notes_val)


def admin_set_weekly_open_days(orders_sh, days: List[str], updated_by: str) -> None:
    safe_days = [d for d in DAY_ORDER if d in set(days or [])]
    csv = ",".join(safe_days)
    admin_upsert_setting(
        orders_sh=orders_sh,
        key="weekly_open_days",
        value=csv,
        scope="global",
        updated_by=updated_by,
        notes="dias normales de apertura",
    )


def admin_set_weekly_normal_hours(
    orders_sh,
    open_time: str,
    close_time: str,
    last_order_time: str,
    updated_by: str,
) -> None:
    admin_upsert_setting(
        orders_sh=orders_sh,
        key="weekly_open_time",
        value=open_time,
        scope="global",
        updated_by=updated_by,
        notes="hora de apertura normal",
    )
    admin_upsert_setting(
        orders_sh=orders_sh,
        key="weekly_close_time",
        value=close_time,
        scope="global",
        updated_by=updated_by,
        notes="hora de cierre normal",
    )
    admin_upsert_setting(
        orders_sh=orders_sh,
        key="weekly_last_order_time",
        value=last_order_time,
        scope="global",
        updated_by=updated_by,
        notes="ultima hora normal de pedido",
    )


def admin_set_today_closed(orders_sh, enabled: bool, updated_by: str) -> None:
    admin_upsert_setting(
        orders_sh=orders_sh,
        key="today_closed",
        value="TRUE" if enabled else "FALSE",
        scope="today",
        updated_by=updated_by,
        notes="negocio cerrado hoy",
    )


def admin_set_today_open_force(orders_sh, enabled: bool, updated_by: str) -> None:
    admin_upsert_setting(
        orders_sh=orders_sh,
        key="today_open_force",
        value="TRUE" if enabled else "FALSE",
        scope="today",
        updated_by=updated_by,
        notes="abrir excepcionalmente hoy",
    )


def admin_set_today_open_override(orders_sh, open_time: str, updated_by: str) -> None:
    admin_upsert_setting(
        orders_sh=orders_sh,
        key="today_open_time_override",
        value=open_time,
        scope="today",
        updated_by=updated_by,
        notes="apertura especial hoy",
    )


def admin_set_today_close_override(orders_sh, close_time: str, updated_by: str) -> None:
    admin_upsert_setting(
        orders_sh=orders_sh,
        key="today_close_time_override",
        value=close_time,
        scope="today",
        updated_by=updated_by,
        notes="cierre especial hoy",
    )


def admin_set_today_last_order_override(orders_sh, last_order_time: str, updated_by: str) -> None:
    admin_upsert_setting(
        orders_sh=orders_sh,
        key="today_last_order_time_override",
        value=last_order_time,
        scope="today",
        updated_by=updated_by,
        notes="ultima hora especial hoy",
    )


def admin_restore_habitual(orders_sh, updated_by: str) -> None:
    admin_set_today_closed(orders_sh, enabled=False, updated_by=updated_by)
    admin_set_today_open_force(orders_sh, enabled=False, updated_by=updated_by)
    admin_set_today_open_override(orders_sh, open_time="", updated_by=updated_by)
    admin_set_today_close_override(orders_sh, close_time="", updated_by=updated_by)
    admin_set_today_last_order_override(orders_sh, last_order_time="", updated_by=updated_by)


def hhmm_to_compact(hhmm: str) -> str:
    return str(hhmm or "").replace(":", "")


def compact_to_hhmm(v: str) -> str:
    v = str(v or "").strip()
    if len(v) != 4 or not v.isdigit():
        raise HTTPException(status_code=400, detail=f"Invalid compact time: {v}")
    return f"{v[:2]}:{v[2:]}"


def build_halfhour_slots() -> List[str]:
    slots: List[str] = []
    hour = 6
    minute = 0
    while True:
        slots.append(f"{hour:02d}:{minute:02d}")
        if hour == 23 and minute == 30:
            break
        minute += 30
        if minute >= 60:
            minute = 0
            hour += 1
    return slots


TIME_SLOTS = build_halfhour_slots()


def admin_hours_menu_kb(tenant_id: str) -> Dict[str, Any]:
    return kb([
        [("📅 Días normales", f"admhrs|{tenant_id}|days"), ("🕒 Horario normal", f"admhrs|{tenant_id}|norm")],
        [("🌙 Cerrar más temprano hoy", f"admhrs|{tenant_id}|early"), ("🌅 Abrir más tarde hoy", f"admhrs|{tenant_id}|late")],
        [("🔴 No abrir hoy", f"admhrs|{tenant_id}|closed"), ("✨ Abrir excepcionalmente hoy", f"admhrs|{tenant_id}|openforce")],
        [("🔄 Volver a lo habitual", f"admhrs|{tenant_id}|habitual")],
    ])


def admin_hours_status_text(bs: Dict[str, Any]) -> str:
    status_txt = "ABIERTO" if bs.get("accepts_orders_now") else "CERRADO / NO DISPONIBLE"
    today_code = str(bs.get("today_weekday_code") or "").strip()
    weekly_days = bs.get("weekly_open_days") or []
    days_txt = ", ".join(weekly_days) or "-"
    today_in_weekly = today_code in set(weekly_days)

    msg = (
        "⚙️ CONFIG DÍAS Y HORARIOS\n\n"
        f"Estado actual: {status_txt}\n"
        f"Día de hoy: {today_code or '-'}\n"
        f"Hoy está dentro de días normales: {'Sí' if today_in_weekly else 'No'}\n"
        f"Abre hoy: {'Sí' if bs.get('is_open_today') else 'No'}\n"
        f"Acepta pedidos ahora: {'Sí' if bs.get('accepts_orders_now') else 'No'}\n"
        f"Hora apertura: {bs.get('open_time') or '-'}\n"
        f"Hora cierre: {bs.get('close_time') or '-'}\n"
        f"Última hora de pedido: {bs.get('last_order_time') or '-'}\n"
        f"Días normales: {days_txt}\n"
        f"No abrir hoy: {'Sí' if bs.get('today_closed') else 'No'}\n"
        f"Abrir excepcionalmente hoy: {'Sí' if bs.get('today_open_force') else 'No'}\n"
        f"Override apertura hoy: {'Sí' if bs.get('has_open_override') else 'No'}\n"
        f"Override cierre hoy: {'Sí' if bs.get('has_close_override') else 'No'}\n"
        f"Override última hora hoy: {'Sí' if bs.get('has_last_order_override') else 'No'}\n"
    )

    public_message = str(bs.get("public_message") or "").strip()
    if public_message:
        msg += f"\nMensaje público actual:\n{public_message}\n"

    msg += "\nElige una opción:"
    return msg


def send_admin_hours_menu(bot_token: str, chat_id: int, tenant_id: str, orders_sh, tenant_tz: str) -> bool:
    bs = get_business_status_safe(orders_sh=orders_sh, tenant_tz=tenant_tz)
    return telegram_send_text(
        bot_token,
        chat_id,
        admin_hours_status_text(bs),
        reply_markup=admin_hours_menu_kb(tenant_id),
    )


def admin_days_state(sess: Dict[str, Any], bs: Dict[str, Any]) -> Set[str]:
    tmp = sess.setdefault("tmp", {})
    raw = tmp.get("admin_days_selected")
    if isinstance(raw, list):
        return set(raw)
    return set(bs.get("weekly_open_days") or [])


def admin_days_kb(tenant_id: str, selected_days: Set[str]) -> Dict[str, Any]:
    row1 = []
    row2 = []
    for code in DAY_ORDER[:4]:
        prefix = "✅" if code in selected_days else "⬜"
        row1.append((f"{prefix} {DAY_LABELS[code]}", f"admhrs|{tenant_id}|dayt|{code}"))
    for code in DAY_ORDER[4:]:
        prefix = "✅" if code in selected_days else "⬜"
        row2.append((f"{prefix} {DAY_LABELS[code]}", f"admhrs|{tenant_id}|dayt|{code}"))

    return kb([
        row1,
        row2,
        [("💾 Guardar días", f"admhrs|{tenant_id}|dayssave")],
        [("⬅️ Volver", f"admhrs|{tenant_id}|menu")],
    ])


def send_admin_days_menu(bot_token: str, chat_id: int, tenant_id: str, sess: Dict[str, Any], bs: Dict[str, Any]) -> bool:
    selected_days = admin_days_state(sess, bs)
    sess.setdefault("tmp", {})["admin_days_selected"] = list(selected_days)
    selected_txt = ", ".join([DAY_LABELS[d] for d in DAY_ORDER if d in selected_days]) or "Ninguno"

    msg = (
        "📅 DÍAS NORMALES DE APERTURA\n\n"
        f"Seleccionados: {selected_txt}\n\n"
        "Toca los días para marcar o desmarcar.\n"
        "Luego presiona “Guardar días”."
    )
    return telegram_send_text(
        bot_token,
        chat_id,
        msg,
        reply_markup=admin_days_kb(tenant_id, selected_days),
    )


def time_grid_kb(prefix: str, tenant_id: str, back_action: str) -> Dict[str, Any]:
    rows: List[List[Tuple[str, str]]] = []
    current: List[Tuple[str, str]] = []
    for hhmm in TIME_SLOTS:
        current.append((hhmm, f"admhrs|{tenant_id}|{prefix}|{hhmm_to_compact(hhmm)}"))
        if len(current) == 4:
            rows.append(current)
            current = []
    if current:
        rows.append(current)
    rows.append([("⬅️ Volver", f"admhrs|{tenant_id}|{back_action}")])
    return kb(rows)


def send_admin_norm_open_menu(bot_token: str, chat_id: int, tenant_id: str) -> bool:
    msg = (
        "🕒 HORARIO NORMAL\n\n"
        "Paso 1 de 3:\n"
        "Elige la hora normal de apertura."
    )
    return telegram_send_text(
        bot_token,
        chat_id,
        msg,
        reply_markup=time_grid_kb(prefix="normopen", tenant_id=tenant_id, back_action="menu"),
    )


def send_admin_norm_close_menu(bot_token: str, chat_id: int, tenant_id: str, open_time: str) -> bool:
    msg = (
        "🕒 HORARIO NORMAL\n\n"
        f"Apertura elegida: {open_time}\n\n"
        "Paso 2 de 3:\n"
        "Elige la hora normal de cierre."
    )
    return telegram_send_text(
        bot_token,
        chat_id,
        msg,
        reply_markup=time_grid_kb(prefix="normclose", tenant_id=tenant_id, back_action="norm"),
    )


def send_admin_norm_last_menu(bot_token: str, chat_id: int, tenant_id: str, open_time: str, close_time: str) -> bool:
    msg = (
        "🕒 HORARIO NORMAL\n\n"
        f"Apertura: {open_time}\n"
        f"Cierre: {close_time}\n\n"
        "Paso 3 de 3:\n"
        "Elige la última hora normal de pedido."
    )
    return telegram_send_text(
        bot_token,
        chat_id,
        msg,
        reply_markup=time_grid_kb(prefix="normlast", tenant_id=tenant_id, back_action="norm"),
    )


def send_admin_early_close_menu(bot_token: str, chat_id: int, tenant_id: str) -> bool:
    msg = (
        "🌙 CERRAR MÁS TEMPRANO HOY\n\n"
        "Paso 1 de 2:\n"
        "Elige la nueva hora de cierre de hoy."
    )
    return telegram_send_text(
        bot_token,
        chat_id,
        msg,
        reply_markup=time_grid_kb(prefix="earlyclose", tenant_id=tenant_id, back_action="menu"),
    )


def send_admin_early_last_menu(bot_token: str, chat_id: int, tenant_id: str, close_time: str) -> bool:
    msg = (
        "🌙 CERRAR MÁS TEMPRANO HOY\n\n"
        f"Cierre de hoy elegido: {close_time}\n\n"
        "Paso 2 de 2:\n"
        "Elige la nueva última hora de pedido de hoy."
    )
    return telegram_send_text(
        bot_token,
        chat_id,
        msg,
        reply_markup=time_grid_kb(prefix="earlylast", tenant_id=tenant_id, back_action="early"),
    )


def send_admin_late_open_menu(bot_token: str, chat_id: int, tenant_id: str) -> bool:
    msg = (
        "🌅 ABRIR MÁS TARDE HOY\n\n"
        "Elige la nueva hora de apertura de hoy."
    )
    return telegram_send_text(
        bot_token,
        chat_id,
        msg,
        reply_markup=time_grid_kb(prefix="lateopen", tenant_id=tenant_id, back_action="menu"),
    )
