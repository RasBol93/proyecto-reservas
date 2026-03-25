# app/admin_flow.py

import json
import re
from datetime import datetime, timedelta, time, timezone
from typing import Any, Dict, List, Tuple, Optional

from fastapi import HTTPException

from app.menu import (
    load_menu_admin_index,
    group_menu_admin_by_category,
    get_menu_product_or_404,
    set_menu_product_active,
    set_menu_product_price,
    invalidate_menu_cache,
)
from app.orders import (
    get_order_by_id,
    update_order_status,
    append_order_row,
    gen_order_id,
    build_items_snapshot,
)
from app.telegram_api import telegram_send_text, telegram_get_file_path, telegram_download_file_bytes
from app.telegram_keyboard import kb
from app.utils import normalize, log_event
from app.stats import build_periods, resolve_period, build_stats_report_text
from app.image_storage import upload_product_photo_for_tenant
from app.webhook_helpers import (
    get_sess,
    get_client_bot_token,
    assert_admin_authorized,
    set_menu_photo_url,
    admin_fixed_kb,
    admin_periods_inline_kb,
    fmt_price_short,
    extract_first_number,
    get_business_status_safe,
    fmt_snapshot_lines,
)
from app.admin_hours import (
    DAY_ORDER,
    send_admin_hours_menu,
    send_admin_days_menu,
    send_admin_norm_open_menu,
    send_admin_norm_close_menu,
    send_admin_norm_last_menu,
    send_admin_early_close_menu,
    send_admin_early_last_menu,
    send_admin_late_open_menu,
    compact_to_hhmm,
    admin_restore_habitual,
    admin_set_weekly_open_days,
    admin_set_weekly_normal_hours,
    admin_set_today_closed,
    admin_set_today_open_force,
    admin_set_today_open_override,
    admin_set_today_close_override,
    admin_set_today_last_order_override,
)
from app.admin_menu import (
    send_admin_menu_home,
    send_admin_menu_category,
    send_admin_menu_product_detail,
    send_admin_menu_price_editor,
    apply_price_delta,
)
from app.consumer_db import (
    consumer_periods_inline_kb,
    consumer_filters_inline_kb,
    build_consumers_report_pages,
    resolve_consumer_period,
)
from app.alerts import (
    alert_order_status_failed,
    alert_order_failed,
    alert_menu_error,
    alert_photo_upload_failed,
    alert_tenant_error,
    alert_system_error,
)
from app.pickup import get_pickup_config


PICKUP_HOLD_STATUS = "HOLD"
PICKUP_HOLD_RELEASED_STATUS = "HOLD_EXPIRED"

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


def _safe_str(v: Any) -> str:
    return str(v or "").strip()


def _safe_client_chat_id_from_order(order: Dict[str, Any]) -> str:
    chat_id = str(order.get("customer_telegram_chat_id") or "").strip()
    if chat_id and chat_id.isdigit():
        return chat_id

    fallback = str(order.get("customer_contact") or "").strip()
    if fallback and fallback.isdigit():
        return fallback

    return ""


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


def _extract_slot_hhmm(requested_time: str) -> Optional[str]:
    s = _safe_str(requested_time)
    if not s:
        return None

    m = re.search(r"(\d{1,2}:\d{2})", s)
    if m:
        hhmm = m.group(1)
        return hhmm if _parse_hhmm(hhmm) else None

    return None


def _get_orders_ws(orders_sh):
    for title in ["ORDERS", "Orders"]:
        try:
            return orders_sh.worksheet(title)
        except Exception:
            pass
    try:
        return orders_sh.get_worksheet(0)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Orders worksheet not found: {e}")


def _get_header(ws) -> List[str]:
    return [str(x or "").strip() for x in ws.row_values(1)]


def _find_col_idx(header: List[str], col_name: str) -> Optional[int]:
    for i, h in enumerate(header):
        if str(h or "").strip() == str(col_name or "").strip():
            return i
    return None


def _iter_rows_as_dicts(ws) -> List[Dict[str, Any]]:
    header = _get_header(ws)
    if not header:
        return []
    values = ws.get_all_values()
    if len(values) <= 1:
        return []

    out: List[Dict[str, Any]] = []
    for r in values[1:]:
        if not any(str(x).strip() for x in r):
            continue
        d: Dict[str, Any] = {}
        for i, h in enumerate(header):
            d[h] = r[i] if i < len(r) else ""
        out.append(d)
    return out


def _find_row_index_by_order_id(ws, order_id: str) -> Optional[int]:
    header = _get_header(ws)
    if not header:
        return None

    oid_col = _find_col_idx(header, "order_id")
    if oid_col is None:
        return None

    values = ws.get_all_values()
    for i in range(1, len(values)):
        row = values[i]
        cell = row[oid_col] if oid_col < len(row) else ""
        if str(cell).strip() == (order_id or "").strip():
            return i + 1
    return None


def _update_requested_time(orders_sh, order_id: str, new_requested_time: str) -> bool:
    try:
        ws = _get_orders_ws(orders_sh)
        header = _get_header(ws)
        ridx = _find_row_index_by_order_id(ws, order_id)
        if ridx is None:
            return False

        col = _find_col_idx(header, "requested_time")
        if col is None:
            raise RuntimeError("Missing requested_time column")

        ws.update_cell(ridx, col + 1, str(new_requested_time).strip())
        return True
    except Exception as e:
        log_event("update_requested_time_failed", order_id=order_id, error=str(e))
        return False


def _parse_hold_notes(notes: str) -> Dict[str, Any]:
    s = _safe_str(notes)
    if not s:
        return {}
    try:
        obj = json.loads(s)
        return obj if isinstance(obj, dict) else {}
    except Exception:
        return {}


def _is_active_hold(row: Dict[str, Any], now_utc: datetime) -> bool:
    status = _safe_str(row.get("status")).upper()
    if status != PICKUP_HOLD_STATUS:
        return False

    meta = _parse_hold_notes(row.get("notes"))
    expires_at_raw = _safe_str(meta.get("hold_expires_at"))
    dt = _parse_iso_datetime(expires_at_raw)
    if not dt:
        return False

    return dt > now_utc


def _hold_belongs_to_chat(row: Dict[str, Any], client_chat_id: str) -> bool:
    meta = _parse_hold_notes(row.get("notes"))
    return _safe_str(meta.get("hold_for_chat_id")) == _safe_str(client_chat_id)


def _count_slot_occupancy(
    rows: List[Dict[str, Any]],
    target_hhmm: str,
    client_chat_id: str,
    exclude_order_id: str = "",
) -> int:
    now_utc = datetime.now(timezone.utc)
    target_hhmm = _safe_str(target_hhmm)
    exclude_order_id = _safe_str(exclude_order_id)
    client_chat_id = _safe_str(client_chat_id)

    count = 0

    for r in rows:
        row_order_id = _safe_str(r.get("order_id"))
        if exclude_order_id and row_order_id == exclude_order_id:
            continue

        hhmm = _extract_slot_hhmm(r.get("requested_time"))
        if hhmm != target_hhmm:
            continue

        status = _safe_str(r.get("status")).upper()
        if status in ESTADOS_NO_CONTAR:
            continue

        if status in ESTADOS_DEFINITIVOS_OCUPAN:
            count += 1
            continue

        if _is_active_hold(r, now_utc):
            # Si es el hold del mismo cliente, no lo contamos como conflicto final.
            if _hold_belongs_to_chat(r, client_chat_id):
                continue
            count += 1
            continue

    return count


def _get_today_business_window(orders_sh, tenant_tz: str) -> Dict[str, Any]:
    bs = get_business_status_safe(orders_sh=orders_sh, tenant_tz=tenant_tz)

    open_time = _safe_str(bs.get("open_time"))
    close_time = _safe_str(bs.get("close_time"))
    last_order_time = _safe_str(bs.get("last_order_time"))

    if not open_time or not close_time or not last_order_time:
        raise HTTPException(status_code=500, detail="Horario del negocio incompleto")

    now_local = datetime.now().astimezone()  # fallback robusto
    try:
        from zoneinfo import ZoneInfo
        now_local = datetime.now(ZoneInfo(tenant_tz or "America/La_Paz"))
    except Exception:
        pass

    open_t = _parse_hhmm(open_time)
    close_t = _parse_hhmm(close_time)
    last_t = _parse_hhmm(last_order_time)

    if not open_t or not close_t or not last_t:
        raise HTTPException(status_code=500, detail="Formato de horario inválido en configuración")

    return {
        "now_local": now_local,
        "open_dt": _time_to_datetime(now_local, open_t),
        "close_dt": _time_to_datetime(now_local, close_t),
        "last_order_dt": _time_to_datetime(now_local, last_t),
        "open_time": open_time,
        "close_time": close_time,
        "last_order_time": last_order_time,
    }


def _find_next_available_slot_for_confirmation(
    orders_sh,
    tenant_tz: str,
    desired_hhmm: str,
    client_chat_id: str,
    exclude_order_id: str = "",
) -> Optional[str]:
    cfg = get_pickup_config(orders_sh)
    ctx = _get_today_business_window(orders_sh, tenant_tz)
    now_local = ctx["now_local"]
    last_order_dt = ctx["last_order_dt"]
    open_dt = ctx["open_dt"]

    rows = _iter_rows_as_dicts(_get_orders_ws(orders_sh))

    desired_t = _parse_hhmm(desired_hhmm)
    if not desired_t:
        return None

    desired_dt = _time_to_datetime(now_local, desired_t)
    earliest_confirm_dt = _round_up_datetime(
        now_local + timedelta(minutes=cfg["tiempo_minimo_preparacion_minutos"]),
        cfg["intervalo_horarios_recojo_minutos"],
    )

    if earliest_confirm_dt < open_dt:
        earliest_confirm_dt = open_dt

    current = desired_dt if desired_dt > earliest_confirm_dt else earliest_confirm_dt
    if current > last_order_dt:
        return None

    while current <= last_order_dt:
        hhmm = _format_hhmm(current)
        ocupados = _count_slot_occupancy(
            rows=rows,
            target_hhmm=hhmm,
            client_chat_id=client_chat_id,
            exclude_order_id=exclude_order_id,
        )
        if ocupados < cfg["maximo_pedidos_por_horario"]:
            return hhmm

        current = current + timedelta(minutes=cfg["intervalo_horarios_recojo_minutos"])

    return None


def _release_client_holds_for_slot(
    orders_sh,
    client_chat_id: str,
    slot_hhmm: str,
) -> int:
    try:
        ws = _get_orders_ws(orders_sh)
        header = _get_header(ws)
        status_col = _find_col_idx(header, "status")
        if status_col is None:
            return 0

        rows = _iter_rows_as_dicts(ws)
        changed = 0

        for row in rows:
            status = _safe_str(row.get("status")).upper()
            if status != PICKUP_HOLD_STATUS:
                continue

            hhmm = _extract_slot_hhmm(row.get("requested_time"))
            if hhmm != _safe_str(slot_hhmm):
                continue

            if not _hold_belongs_to_chat(row, client_chat_id):
                continue

            if not _is_active_hold(row, datetime.now(timezone.utc)):
                continue

            ridx = _find_row_index_by_order_id(ws, _safe_str(row.get("order_id")))
            if ridx is None:
                continue

            ws.update_cell(ridx, status_col + 1, PICKUP_HOLD_RELEASED_STATUS)
            changed += 1

        return changed
    except Exception as e:
        log_event("release_client_holds_failed", client_chat_id=client_chat_id, slot_hhmm=slot_hhmm, error=str(e))
        return 0


# =========================================================
# CONSUMER DB HELPERS
# =========================================================

def _send_consumers_menu(bot_token: str, chat_id: int, tenant_id: str) -> bool:
    return telegram_send_text(
        bot_token,
        chat_id,
        "👥 BASE DE CONSUMIDORES\n\nElige un período:",
        reply_markup=consumer_periods_inline_kb(tenant_id),
    )


def _send_consumers_filters(bot_token: str, chat_id: int, tenant_id: str, period_key: str, tenant_tz: str) -> bool:
    period = resolve_consumer_period(period_key, tenant_tz)
    return telegram_send_text(
        bot_token,
        chat_id,
        (
            "👥 BASE DE CONSUMIDORES\n\n"
            f"Período elegido: {period.label}\n\n"
            "Ahora elige qué lista quieres ver:"
        ),
        reply_markup=consumer_filters_inline_kb(tenant_id, period_key),
    )


def _send_consumers_report(
    bot_token: str,
    chat_id: int,
    tenant_id: str,
    orders_sh,
    tenant_tz: str,
    period_key: str,
    filter_key: str,
) -> bool:
    pages = build_consumers_report_pages(
        orders_sh=orders_sh,
        tenant_tz=tenant_tz,
        period_key=period_key,
        filter_key=filter_key,
    )

    if not pages:
        pages = ["No encontré resultados."]

    for idx, page in enumerate(pages):
        if idx == len(pages) - 1:
            telegram_send_text(
                bot_token,
                chat_id,
                page,
                reply_markup=consumer_filters_inline_kb(tenant_id, period_key),
            )
        else:
            telegram_send_text(bot_token, chat_id, page)

    return True


# =========================================================
# ADMIN MANUAL ORDER HELPERS
# =========================================================

def _admin_order_reset(tmp: Dict[str, Any]) -> None:
    tmp.pop("admin_order_cart", None)
    tmp.pop("admin_order_step", None)
    tmp.pop("admin_order_name", None)
    tmp.pop("admin_order_contact", None)
    tmp.pop("admin_order_requested_time", None)
    tmp.pop("admin_order_categories", None)
    tmp.pop("admin_order_current_category", None)


def _admin_order_get_active_categories(orders_sh) -> Tuple[Dict[str, Any], Dict[str, List[Dict[str, Any]]], List[str]]:
    menu_idx = load_menu_admin_index(orders_sh, force=False)
    cats_raw = group_menu_admin_by_category(menu_idx)

    cats_active: Dict[str, List[Dict[str, Any]]] = {}
    for cat, items in cats_raw.items():
        only_active = [it for it in items if bool(it.get("active", False))]
        if only_active:
            cats_active[cat] = only_active

    cat_names = sorted(cats_active.keys(), key=lambda x: normalize(x))
    return menu_idx, cats_active, cat_names


def _admin_order_home_kb(tenant_id: str, cat_names: List[str], cats: Dict[str, List[Dict[str, Any]]]) -> Dict[str, Any]:
    rows: List[List[Tuple[str, str]]] = []

    for idx, cat in enumerate(cat_names):
        total_n = len(cats.get(cat, []))
        rows.append([(f"📂 {cat} ({total_n})", f"admord|{tenant_id}|cat|{idx}")])

    rows.append([("🛒 Ver carrito", f"admord|{tenant_id}|cart")])
    rows.append([("❌ Cancelar", f"admord|{tenant_id}|panel")])
    return kb(rows)


def _send_admin_order_home(
    bot_token: str,
    chat_id: int,
    tenant_id: str,
    orders_sh,
    sess: Dict[str, Any],
) -> bool:
    _, cats, cat_names = _admin_order_get_active_categories(orders_sh)
    tmp = sess.setdefault("tmp", {})
    tmp["admin_order_categories"] = cat_names
    tmp.pop("admin_order_current_category", None)

    msg = (
        "➕ CREAR PEDIDO MANUAL\n\n"
        "Elige una categoría para agregar productos al carrito:"
    )

    return telegram_send_text(
        bot_token,
        chat_id,
        msg,
        reply_markup=_admin_order_home_kb(tenant_id, cat_names, cats),
    )


def _admin_order_category_kb(tenant_id: str, category: str, items: List[Dict[str, Any]]) -> Dict[str, Any]:
    rows: List[List[Tuple[str, str]]] = []

    for it in items[:25]:
        sku = str(it.get("sku") or "").strip()
        price_txt = fmt_price_short(it.get("price", 0))
        rows.append([(f"{it.get('name','')} — Bs {price_txt}", f"admord|{tenant_id}|prd|{sku}")])

    rows.append([("🛒 Ver carrito", f"admord|{tenant_id}|cart")])
    rows.append([("⬅️ Categorías", f"admord|{tenant_id}|home")])
    rows.append([("❌ Cancelar", f"admord|{tenant_id}|panel")])

    return kb(rows)


def _send_admin_order_category(
    bot_token: str,
    chat_id: int,
    tenant_id: str,
    orders_sh,
    sess: Dict[str, Any],
    category: str,
) -> bool:
    _, cats, _ = _admin_order_get_active_categories(orders_sh)
    items = cats.get(category, [])

    tmp = sess.setdefault("tmp", {})
    tmp["admin_order_current_category"] = category

    msg = (
        f"📂 CATEGORÍA: {category}\n\n"
        "Elige un producto:"
    )

    return telegram_send_text(
        bot_token,
        chat_id,
        msg,
        reply_markup=_admin_order_category_kb(tenant_id, category, items),
    )


def _admin_order_qty_kb(tenant_id: str, sku: str) -> Dict[str, Any]:
    return kb([
        [("1", f"admord|{tenant_id}|qty|{sku}|1"), ("2", f"admord|{tenant_id}|qty|{sku}|2"),
         ("3", f"admord|{tenant_id}|qty|{sku}|3"), ("4", f"admord|{tenant_id}|qty|{sku}|4")],
        [("🛒 Ver carrito", f"admord|{tenant_id}|cart")],
        [("⬅️ Volver", f"admord|{tenant_id}|catback")],
        [("❌ Cancelar", f"admord|{tenant_id}|panel")],
    ])


def _send_admin_order_product_qty(
    bot_token: str,
    chat_id: int,
    tenant_id: str,
    orders_sh,
    sku: str,
) -> bool:
    item = get_menu_product_or_404(orders_sh, sku)
    msg = (
        "➕ AGREGAR AL PEDIDO\n\n"
        f"Producto: {item.get('name','')}\n"
        f"Precio: Bs {fmt_price_short(item.get('price', 0))}\n\n"
        "Selecciona cantidad:"
    )
    return telegram_send_text(
        bot_token,
        chat_id,
        msg,
        reply_markup=_admin_order_qty_kb(tenant_id, sku),
    )


def _admin_order_add_to_cart(tmp: Dict[str, Any], sku: str, qty: int) -> None:
    cart = tmp.get("admin_order_cart") or []
    found = False
    for it in cart:
        if str(it.get("sku") or "").strip() == sku:
            it["qty"] = int(it.get("qty") or 0) + qty
            found = True
            break
    if not found:
        cart.append({"sku": sku, "qty": qty})
    tmp["admin_order_cart"] = cart


def _admin_order_cart_kb(tenant_id: str, has_items: bool) -> Dict[str, Any]:
    rows: List[List[Tuple[str, str]]] = []

    if has_items:
        rows.append([("✅ Confirmar pedido", f"admord|{tenant_id}|confirm")])
        rows.append([("🧹 Vaciar carrito", f"admord|{tenant_id}|clear")])

    rows.append([("⬅️ Seguir agregando", f"admord|{tenant_id}|home")])
    rows.append([("❌ Cancelar", f"admord|{tenant_id}|panel")])
    return kb(rows)


def _send_admin_order_cart(
    bot_token: str,
    chat_id: int,
    tenant_id: str,
    orders_sh,
    sess: Dict[str, Any],
) -> bool:
    tmp = sess.setdefault("tmp", {})
    cart = tmp.get("admin_order_cart") or []

    menu_idx = load_menu_admin_index(orders_sh, force=False)
    items_snapshot = build_items_snapshot(cart, menu_idx)
    lines_txt, total, total_qty = fmt_snapshot_lines(items_snapshot)

    msg = (
        "🛒 PEDIDO MANUAL\n\n"
        f"Cantidad total: {total_qty}\n"
        f"Total: Bs {total:.2f}\n\n"
        f"Detalle:\n{lines_txt}"
    )

    return telegram_send_text(
        bot_token,
        chat_id,
        msg,
        reply_markup=_admin_order_cart_kb(tenant_id, total_qty > 0),
    )


# =========================================================
# ADMIN CALLBACKS
# =========================================================

def handle_admin_callback(
    tenant: Dict[str, Any],
    tenant_id: str,
    bot_token: str,
    chat_id: int,
    data: str,
    orders_sh,
    tenant_tz: str,
) -> Dict[str, Any]:
    try:
        if data.startswith("paid|"):
            parts = data.split("|")
            if len(parts) != 3:
                return {"ok": True}

            cb_tenant_id = parts[1].strip()
            order_id = parts[2].strip()

            if cb_tenant_id != tenant_id:
                raise HTTPException(status_code=400, detail="Tenant mismatch in callback")

            assert_admin_authorized(tenant, chat_id, tenant_id)

            order_before = get_order_by_id(orders_sh, order_id)
            if not order_before:
                telegram_send_text(bot_token, chat_id, f"⚠️ Pedido {order_id} no encontrado en Sheets.", reply_markup=admin_fixed_kb())
                return {"ok": True}

            client_chat = _safe_client_chat_id_from_order(order_before)
            desired_hhmm = _extract_slot_hhmm(order_before.get("requested_time"))
            final_hhmm = desired_hhmm

            if desired_hhmm:
                next_slot = _find_next_available_slot_for_confirmation(
                    orders_sh=orders_sh,
                    tenant_tz=tenant_tz,
                    desired_hhmm=desired_hhmm,
                    client_chat_id=client_chat,
                    exclude_order_id=order_id,
                )

                if next_slot:
                    final_hhmm = next_slot
                else:
                    telegram_send_text(
                        bot_token,
                        chat_id,
                        "⚠️ No encontré una franja disponible para confirmar este pago. Revísalo manualmente.",
                        reply_markup=admin_fixed_kb(),
                    )
                    return {"ok": True}

                if final_hhmm != desired_hhmm:
                    ok_update = _update_requested_time(orders_sh, order_id, final_hhmm)
                    if not ok_update:
                        telegram_send_text(
                            bot_token,
                            chat_id,
                            "⚠️ No pude actualizar la hora final del pedido.",
                            reply_markup=admin_fixed_kb(),
                        )
                        return {"ok": True}

                _release_client_holds_for_slot(
                    orders_sh=orders_sh,
                    client_chat_id=client_chat,
                    slot_hhmm=desired_hhmm,
                )

            res = update_order_status(orders_sh, order_id, "PAID")
            if not res.get("ok"):
                alert_order_status_failed(
                    tenant_id=tenant_id,
                    order_id=order_id,
                    new_status="PAID",
                    error=res.get("error") or "update_order_status failed",
                )
                telegram_send_text(bot_token, chat_id, "⚠️ Error actualizando el estado.", reply_markup=admin_fixed_kb())
                return {"ok": True}

            if not res.get("found"):
                telegram_send_text(bot_token, chat_id, f"⚠️ Pedido {order_id} no encontrado en Sheets.", reply_markup=admin_fixed_kb())
                return {"ok": True}

            if final_hhmm and desired_hhmm and final_hhmm != desired_hhmm:
                telegram_send_text(
                    bot_token,
                    chat_id,
                    f"✅ Pedido {order_id} marcado como PAID.\nHora ajustada de {desired_hhmm} a {final_hhmm}.",
                    reply_markup=admin_fixed_kb(),
                )
            elif final_hhmm:
                telegram_send_text(
                    bot_token,
                    chat_id,
                    f"✅ Pedido {order_id} marcado como PAID.\nHora confirmada: {final_hhmm}.",
                    reply_markup=admin_fixed_kb(),
                )
            else:
                telegram_send_text(
                    bot_token,
                    chat_id,
                    f"✅ Pedido {order_id} marcado como PAID",
                    reply_markup=admin_fixed_kb(),
                )

            order_after = get_order_by_id(orders_sh, order_id)
            if order_after:
                client_token = get_client_bot_token(tenant)
                client_chat = _safe_client_chat_id_from_order(order_after)

                if client_token and client_chat:
                    try:
                        final_slot_for_msg = _safe_str(final_hhmm or _extract_slot_hhmm(order_after.get("requested_time")))
                        if desired_hhmm and final_slot_for_msg and final_slot_for_msg != desired_hhmm:
                            msg_client = (
                                f"✅ Pago validado. Tu pedido {order_id} fue confirmado.\n\n"
                                f"Tu horario elegido ({desired_hhmm}) ya no estaba disponible, "
                                f"así que te asignamos la siguiente hora disponible: *{final_slot_for_msg}*."
                            )
                        elif final_slot_for_msg:
                            msg_client = (
                                f"✅ Pago validado. Tu pedido {order_id} fue confirmado.\n\n"
                                f"Hora de recojo confirmada: *{final_slot_for_msg}*."
                            )
                        else:
                            msg_client = f"✅ Pago validado. Tu pedido {order_id} fue confirmado. ¡Gracias!"

                        telegram_send_text(
                            client_token,
                            int(client_chat),
                            msg_client,
                            parse_mode="Markdown",
                        )
                    except Exception as e:
                        log_event(
                            "notify_client_paid_failed",
                            tenant_id=tenant_id,
                            order_id=order_id,
                            client_chat=client_chat,
                            error=str(e),
                        )
                else:
                    log_event(
                        "notify_client_paid_skipped",
                        tenant_id=tenant_id,
                        order_id=order_id,
                        reason="missing_client_token_or_chat_id",
                    )

            return {"ok": True}

        if data.startswith("admin_stats_period|"):
            parts = data.split("|")
            if len(parts) != 3:
                return {"ok": True}
            _, cb_tenant_id, period_key = parts
            cb_tenant_id = cb_tenant_id.strip()
            period_key = period_key.strip()

            if cb_tenant_id != tenant_id:
                raise HTTPException(status_code=400, detail="Tenant mismatch in stats callback")

            assert_admin_authorized(tenant, chat_id, tenant_id)

            period = resolve_period(tenant_tz, period_key)
            txt = build_stats_report_text(orders_sh, tenant_id=tenant_id, tenant_tz=tenant_tz, period=period)

            telegram_send_text(bot_token, chat_id, txt, reply_markup=admin_fixed_kb())
            return {"ok": True}

        if data.startswith("admcons|"):
            assert_admin_authorized(tenant, chat_id, tenant_id)

            parts = data.split("|")
            if len(parts) < 3:
                return {"ok": True}

            cb_tenant_id = parts[1].strip()
            if cb_tenant_id != tenant_id:
                raise HTTPException(status_code=400, detail="Tenant mismatch in consumer db callback")

            action = parts[2].strip()

            if action == "panel":
                telegram_send_text(bot_token, chat_id, "Panel admin:", reply_markup=admin_fixed_kb())
                return {"ok": True}

            if action == "menu":
                return {"ok": _send_consumers_menu(bot_token, chat_id, tenant_id)}

            if action == "period" and len(parts) == 4:
                period_key = parts[3].strip()
                return {"ok": _send_consumers_filters(bot_token, chat_id, tenant_id, period_key, tenant_tz)}

            if action == "report" and len(parts) == 5:
                period_key = parts[3].strip()
                filter_key = parts[4].strip()
                return {"ok": _send_consumers_report(
                    bot_token=bot_token,
                    chat_id=chat_id,
                    tenant_id=tenant_id,
                    orders_sh=orders_sh,
                    tenant_tz=tenant_tz,
                    period_key=period_key,
                    filter_key=filter_key,
                )}

            return {"ok": True}

        # =========================================
        # ADMIN MANUAL ORDER FLOW
        # =========================================
        if data.startswith("admord|"):
            assert_admin_authorized(tenant, chat_id, tenant_id)

            parts = data.split("|")
            if len(parts) < 3:
                return {"ok": True}

            cb_tenant_id = parts[1].strip()
            if cb_tenant_id != tenant_id:
                raise HTTPException(status_code=400, detail="Tenant mismatch in admin order callback")

            action = parts[2].strip()
            sess = get_sess(tenant_id, chat_id)
            tmp = sess.setdefault("tmp", {})

            if action == "start":
                _admin_order_reset(tmp)
                tmp["admin_order_cart"] = []
                return {"ok": _send_admin_order_home(bot_token, chat_id, tenant_id, orders_sh, sess)}

            if action == "panel":
                _admin_order_reset(tmp)
                telegram_send_text(bot_token, chat_id, "Panel admin:", reply_markup=admin_fixed_kb())
                return {"ok": True}

            if action == "home":
                return {"ok": _send_admin_order_home(bot_token, chat_id, tenant_id, orders_sh, sess)}

            if action == "cat" and len(parts) == 4:
                try:
                    idx = int(parts[3].strip())
                except Exception:
                    idx = -1

                _, cats, cat_names = _admin_order_get_active_categories(orders_sh)
                if idx < 0 or idx >= len(cat_names):
                    return {"ok": _send_admin_order_home(bot_token, chat_id, tenant_id, orders_sh, sess)}

                category = cat_names[idx]
                return {"ok": _send_admin_order_category(bot_token, chat_id, tenant_id, orders_sh, sess, category)}

            if action == "catback":
                current_category = str(tmp.get("admin_order_current_category") or "").strip()
                if not current_category:
                    return {"ok": _send_admin_order_home(bot_token, chat_id, tenant_id, orders_sh, sess)}
                return {"ok": _send_admin_order_category(bot_token, chat_id, tenant_id, orders_sh, sess, current_category)}

            if action == "prd" and len(parts) == 4:
                sku = parts[3].strip()
                return {"ok": _send_admin_order_product_qty(bot_token, chat_id, tenant_id, orders_sh, sku)}

            if action == "qty" and len(parts) == 5:
                sku = parts[3].strip()
                try:
                    qty = int(parts[4].strip())
                except Exception:
                    qty = 1
                qty = max(1, qty)

                item = get_menu_product_or_404(orders_sh, sku)
                _admin_order_add_to_cart(tmp, sku, qty)

                telegram_send_text(
                    bot_token,
                    chat_id,
                    f"✅ Agregado al pedido: {qty} x {item.get('name','')}",
                )
                return {"ok": _send_admin_order_cart(bot_token, chat_id, tenant_id, orders_sh, sess)}

            if action == "cart":
                return {"ok": _send_admin_order_cart(bot_token, chat_id, tenant_id, orders_sh, sess)}

            if action == "clear":
                tmp["admin_order_cart"] = []
                telegram_send_text(bot_token, chat_id, "🧹 Carrito manual vaciado.")
                return {"ok": _send_admin_order_home(bot_token, chat_id, tenant_id, orders_sh, sess)}

            if action == "confirm":
                cart = tmp.get("admin_order_cart") or []
                if not cart:
                    telegram_send_text(bot_token, chat_id, "⚠️ El carrito está vacío.")
                    return {"ok": _send_admin_order_home(bot_token, chat_id, tenant_id, orders_sh, sess)}

                tmp["admin_order_step"] = "awaiting_name"
                telegram_send_text(bot_token, chat_id, "Escribe el nombre del cliente:")
                return {"ok": True}

            return {"ok": True}

        if data.startswith("admhrs|"):
            assert_admin_authorized(tenant, chat_id, tenant_id)

            parts = data.split("|")
            if len(parts) < 3:
                return {"ok": True}

            cb_tenant_id = parts[1].strip()
            if cb_tenant_id != tenant_id:
                raise HTTPException(status_code=400, detail="Tenant mismatch in admin hours callback")

            action = parts[2].strip()
            sess = get_sess(tenant_id, chat_id)
            tmp = sess.setdefault("tmp", {})
            updated_by = f"admin_bot:{chat_id}"

            if action == "menu":
                tmp.pop("admin_days_selected", None)
                tmp.pop("admin_norm_open", None)
                tmp.pop("admin_norm_close", None)
                tmp.pop("admin_early_close", None)
                return {"ok": send_admin_hours_menu(bot_token, chat_id, tenant_id, orders_sh, tenant_tz)}

            if action == "habitual":
                tmp.pop("admin_days_selected", None)
                tmp.pop("admin_norm_open", None)
                tmp.pop("admin_norm_close", None)
                tmp.pop("admin_early_close", None)
                admin_restore_habitual(orders_sh=orders_sh, updated_by=updated_by)
                telegram_send_text(bot_token, chat_id, "✅ Se restauró la configuración habitual de hoy.")
                return {"ok": send_admin_hours_menu(bot_token, chat_id, tenant_id, orders_sh, tenant_tz)}

            if action == "days":
                bs = get_business_status_safe(orders_sh=orders_sh, tenant_tz=tenant_tz)
                tmp["admin_days_selected"] = list(bs.get("weekly_open_days") or [])
                return {"ok": send_admin_days_menu(bot_token, chat_id, tenant_id, sess, bs)}

            if action == "dayt" and len(parts) == 4:
                code = parts[3].strip()
                if code not in DAY_ORDER:
                    return {"ok": True}
                current = set(tmp.get("admin_days_selected") or [])
                if code in current:
                    current.remove(code)
                else:
                    current.add(code)
                tmp["admin_days_selected"] = list(current)
                bs = get_business_status_safe(orders_sh=orders_sh, tenant_tz=tenant_tz)
                return {"ok": send_admin_days_menu(bot_token, chat_id, tenant_id, sess, bs)}

            if action == "dayssave":
                selected = [d for d in DAY_ORDER if d in set(tmp.get("admin_days_selected") or [])]
                admin_set_weekly_open_days(
                    orders_sh=orders_sh,
                    days=selected,
                    updated_by=updated_by,
                )
                tmp.pop("admin_days_selected", None)

                bs_after = get_business_status_safe(orders_sh=orders_sh, tenant_tz=tenant_tz)
                today_code = str(bs_after.get("today_weekday_code") or "").strip()
                today_in = today_code in set(bs_after.get("weekly_open_days") or [])
                force_open = bool(bs_after.get("today_open_force"))
                today_closed = bool(bs_after.get("today_closed"))

                msg = "✅ Días normales actualizados."
                if today_code and (not today_in) and (not force_open) and (not today_closed):
                    msg += f"\n⚠️ Ojo: hoy ({today_code}) quedó fuera de los días normales."

                telegram_send_text(bot_token, chat_id, msg)
                return {"ok": send_admin_hours_menu(bot_token, chat_id, tenant_id, orders_sh, tenant_tz)}

            if action == "norm":
                tmp.pop("admin_norm_open", None)
                tmp.pop("admin_norm_close", None)
                return {"ok": send_admin_norm_open_menu(bot_token, chat_id, tenant_id)}

            if action == "normopen" and len(parts) == 4:
                open_time = compact_to_hhmm(parts[3].strip())
                tmp["admin_norm_open"] = open_time
                return {"ok": send_admin_norm_close_menu(bot_token, chat_id, tenant_id, open_time)}

            if action == "normclose" and len(parts) == 4:
                close_time = compact_to_hhmm(parts[3].strip())
                open_time = str(tmp.get("admin_norm_open") or "").strip()
                if not open_time:
                    return {"ok": send_admin_norm_open_menu(bot_token, chat_id, tenant_id)}
                tmp["admin_norm_close"] = close_time
                return {"ok": send_admin_norm_last_menu(bot_token, chat_id, tenant_id, open_time, close_time)}

            if action == "normlast" and len(parts) == 4:
                last_time = compact_to_hhmm(parts[3].strip())
                open_time = str(tmp.get("admin_norm_open") or "").strip()
                close_time = str(tmp.get("admin_norm_close") or "").strip()
                if not open_time or not close_time:
                    return {"ok": send_admin_norm_open_menu(bot_token, chat_id, tenant_id)}

                admin_set_weekly_normal_hours(
                    orders_sh=orders_sh,
                    open_time=open_time,
                    close_time=close_time,
                    last_order_time=last_time,
                    updated_by=updated_by,
                )

                tmp.pop("admin_norm_open", None)
                tmp.pop("admin_norm_close", None)

                telegram_send_text(
                    bot_token,
                    chat_id,
                    f"✅ Horario normal actualizado.\nApertura: {open_time}\nCierre: {close_time}\nÚltima hora de pedido: {last_time}",
                )
                return {"ok": send_admin_hours_menu(bot_token, chat_id, tenant_id, orders_sh, tenant_tz)}

            if action == "early":
                tmp.pop("admin_early_close", None)
                return {"ok": send_admin_early_close_menu(bot_token, chat_id, tenant_id)}

            if action == "earlyclose" and len(parts) == 4:
                close_time = compact_to_hhmm(parts[3].strip())
                tmp["admin_early_close"] = close_time
                return {"ok": send_admin_early_last_menu(bot_token, chat_id, tenant_id, close_time)}

            if action == "earlylast" and len(parts) == 4:
                last_time = compact_to_hhmm(parts[3].strip())
                close_time = str(tmp.get("admin_early_close") or "").strip()
                if not close_time:
                    return {"ok": send_admin_early_close_menu(bot_token, chat_id, tenant_id)}

                admin_set_today_closed(orders_sh=orders_sh, enabled=False, updated_by=updated_by)
                admin_set_today_open_force(orders_sh=orders_sh, enabled=False, updated_by=updated_by)
                admin_set_today_close_override(orders_sh=orders_sh, close_time=close_time, updated_by=updated_by)
                admin_set_today_last_order_override(orders_sh=orders_sh, last_order_time=last_time, updated_by=updated_by)

                tmp.pop("admin_early_close", None)
                telegram_send_text(
                    bot_token,
                    chat_id,
                    f"✅ Cierre temprano configurado para hoy.\nCierre: {close_time}\nÚltima hora de pedido: {last_time}",
                )
                return {"ok": send_admin_hours_menu(bot_token, chat_id, tenant_id, orders_sh, tenant_tz)}

            if action == "late":
                return {"ok": send_admin_late_open_menu(bot_token, chat_id, tenant_id)}

            if action == "lateopen" and len(parts) == 4:
                open_time = compact_to_hhmm(parts[3].strip())
                admin_set_today_closed(orders_sh=orders_sh, enabled=False, updated_by=updated_by)
                admin_set_today_open_force(orders_sh=orders_sh, enabled=False, updated_by=updated_by)
                admin_set_today_open_override(orders_sh=orders_sh, open_time=open_time, updated_by=updated_by)
                telegram_send_text(
                    bot_token,
                    chat_id,
                    f"✅ Apertura tardía configurada para hoy.\nNueva apertura: {open_time}",
                )
                return {"ok": send_admin_hours_menu(bot_token, chat_id, tenant_id, orders_sh, tenant_tz)}

            if action == "closed":
                admin_set_today_closed(orders_sh=orders_sh, enabled=True, updated_by=updated_by)
                admin_set_today_open_force(orders_sh=orders_sh, enabled=False, updated_by=updated_by)
                admin_set_today_open_override(orders_sh=orders_sh, open_time="", updated_by=updated_by)
                admin_set_today_close_override(orders_sh=orders_sh, close_time="", updated_by=updated_by)
                admin_set_today_last_order_override(orders_sh=orders_sh, last_order_time="", updated_by=updated_by)
                telegram_send_text(bot_token, chat_id, "✅ Hoy quedó marcado como NO abrir.")
                return {"ok": send_admin_hours_menu(bot_token, chat_id, tenant_id, orders_sh, tenant_tz)}

            if action == "openforce":
                admin_set_today_open_force(orders_sh=orders_sh, enabled=True, updated_by=updated_by)
                admin_set_today_closed(orders_sh=orders_sh, enabled=False, updated_by=updated_by)
                admin_set_today_open_override(orders_sh=orders_sh, open_time="", updated_by=updated_by)
                admin_set_today_close_override(orders_sh=orders_sh, close_time="", updated_by=updated_by)
                admin_set_today_last_order_override(orders_sh=orders_sh, last_order_time="", updated_by=updated_by)
                telegram_send_text(bot_token, chat_id, "✅ Hoy quedó marcado como abrir excepcionalmente.")
                return {"ok": send_admin_hours_menu(bot_token, chat_id, tenant_id, orders_sh, tenant_tz)}

            return {"ok": True}

        if data.startswith("admmenu|"):
            assert_admin_authorized(tenant, chat_id, tenant_id)

            parts = data.split("|")
            if len(parts) < 3:
                return {"ok": True}

            cb_tenant_id = parts[1].strip()
            if cb_tenant_id != tenant_id:
                raise HTTPException(status_code=400, detail="Tenant mismatch in admin menu callback")

            action = parts[2].strip()
            sess = get_sess(tenant_id, chat_id)
            tmp = sess.setdefault("tmp", {})

            if action == "panel":
                tmp.pop("admin_menu_categories", None)
                tmp.pop("admin_menu_current_category", None)
                tmp.pop("admin_menu_last_sku", None)
                tmp.pop("admin_menu_price_sku", None)
                tmp.pop("admin_menu_price_work", None)
                tmp.pop("admin_menu_input_mode", None)
                telegram_send_text(bot_token, chat_id, "Panel admin:", reply_markup=admin_fixed_kb())
                return {"ok": True}

            if action == "home":
                return {"ok": send_admin_menu_home(bot_token, chat_id, tenant_id, orders_sh, sess)}

            if action == "refresh":
                invalidate_menu_cache(orders_sh)
                telegram_send_text(bot_token, chat_id, "✅ Menú refrescado.")
                return {"ok": send_admin_menu_home(bot_token, chat_id, tenant_id, orders_sh, sess)}

            if action == "catrefresh":
                invalidate_menu_cache(orders_sh)
                current_category = str(tmp.get("admin_menu_current_category") or "").strip()
                if not current_category:
                    return {"ok": send_admin_menu_home(bot_token, chat_id, tenant_id, orders_sh, sess)}
                telegram_send_text(bot_token, chat_id, "✅ Categoría refrescada.")
                return {"ok": send_admin_menu_category(bot_token, chat_id, tenant_id, orders_sh, sess, current_category)}

            if action == "cat" and len(parts) == 4:
                try:
                    idx = int(parts[3].strip())
                except Exception:
                    idx = -1

                menu_idx = load_menu_admin_index(orders_sh, force=False)
                cats = group_menu_admin_by_category(menu_idx)
                cat_names = sorted(cats.keys(), key=lambda x: normalize(x))

                if idx < 0 or idx >= len(cat_names):
                    return {"ok": send_admin_menu_home(bot_token, chat_id, tenant_id, orders_sh, sess)}

                category = cat_names[idx]
                return {"ok": send_admin_menu_category(bot_token, chat_id, tenant_id, orders_sh, sess, category)}

            if action == "catback":
                current_category = str(tmp.get("admin_menu_current_category") or "").strip()
                if not current_category:
                    return {"ok": send_admin_menu_home(bot_token, chat_id, tenant_id, orders_sh, sess)}
                return {"ok": send_admin_menu_category(bot_token, chat_id, tenant_id, orders_sh, sess, current_category)}

            if action == "prd" and len(parts) == 4:
                sku = parts[3].strip()
                return {"ok": send_admin_menu_product_detail(bot_token, chat_id, tenant_id, orders_sh, sess, sku)}

            if action == "toggle" and len(parts) == 4:
                sku = parts[3].strip()
                item_before = get_menu_product_or_404(orders_sh, sku)
                new_active = not bool(item_before.get("active", False))
                set_menu_product_active(orders_sh, sku, new_active)
                item_after = get_menu_product_or_404(orders_sh, sku)

                telegram_send_text(
                    bot_token,
                    chat_id,
                    f"✅ Estado actualizado.\nProducto: {item_after.get('name','')}\nActivo: {'Sí' if item_after.get('active') else 'No'}",
                )
                return {"ok": send_admin_menu_product_detail(bot_token, chat_id, tenant_id, orders_sh, sess, sku)}

            if action == "price" and len(parts) == 4:
                sku = parts[3].strip()
                return {"ok": send_admin_menu_price_editor(bot_token, chat_id, tenant_id, orders_sh, sess, sku)}

            if action == "padj" and len(parts) == 5:
                sku = parts[3].strip()
                token = parts[4].strip().lower()

                item = get_menu_product_or_404(orders_sh, sku)
                current_sku = str(tmp.get("admin_menu_price_sku") or "").strip()
                if current_sku != sku:
                    tmp["admin_menu_price_sku"] = sku
                    tmp["admin_menu_price_work"] = float(item.get("price", 0.0))

                work_price = float(tmp.get("admin_menu_price_work") or 0.0)
                work_price = apply_price_delta(work_price, token)
                tmp["admin_menu_price_work"] = work_price

                return {"ok": send_admin_menu_price_editor(bot_token, chat_id, tenant_id, orders_sh, sess, sku)}

            if action == "psave" and len(parts) == 4:
                sku = parts[3].strip()
                current_sku = str(tmp.get("admin_menu_price_sku") or "").strip()

                if current_sku != sku:
                    item = get_menu_product_or_404(orders_sh, sku)
                    tmp["admin_menu_price_sku"] = sku
                    tmp["admin_menu_price_work"] = float(item.get("price", 0.0))

                new_price = float(tmp.get("admin_menu_price_work") or 0.0)
                result = set_menu_product_price(orders_sh, sku, new_price)

                tmp.pop("admin_menu_price_sku", None)
                tmp.pop("admin_menu_price_work", None)
                tmp.pop("admin_menu_input_mode", None)

                telegram_send_text(
                    bot_token,
                    chat_id,
                    f"✅ Precio actualizado.\nSKU: {sku}\nNuevo precio: Bs {fmt_price_short(result.get('price', 0))}",
                )
                return {"ok": send_admin_menu_product_detail(bot_token, chat_id, tenant_id, orders_sh, sess, sku)}

            if action == "pback" and len(parts) == 4:
                sku = parts[3].strip()
                tmp.pop("admin_menu_price_sku", None)
                tmp.pop("admin_menu_price_work", None)
                tmp.pop("admin_menu_input_mode", None)
                return {"ok": send_admin_menu_product_detail(bot_token, chat_id, tenant_id, orders_sh, sess, sku)}

            if action == "pricewrite" and len(parts) == 4:
                sku = parts[3].strip()
                item = get_menu_product_or_404(orders_sh, sku)
                tmp["admin_menu_input_mode"] = "price_final"
                tmp["admin_menu_price_sku"] = sku
                tmp["admin_menu_price_work"] = float(item.get("price", 0.0))

                telegram_send_text(
                    bot_token,
                    chat_id,
                    (
                        "✍️ ESCRIBIR PRECIO FINAL\n\n"
                        f"Producto: {item.get('name','')}\n"
                        f"Precio actual: Bs {fmt_price_short(item.get('price', 0))}\n\n"
                        "Escribe el nuevo precio final.\n"
                        "Ejemplos válidos:\n"
                        "- 25\n"
                        "- 25 bs\n"
                        "- 25 bolivianos"
                    ),
                )
                return {"ok": True}

            if action == "discount" and len(parts) == 4:
                sku = parts[3].strip()
                item = get_menu_product_or_404(orders_sh, sku)
                tmp["admin_menu_input_mode"] = "discount_pct"
                tmp["admin_menu_price_sku"] = sku
                tmp["admin_menu_price_work"] = float(item.get("price", 0.0))

                telegram_send_text(
                    bot_token,
                    chat_id,
                    (
                        "🏷️ APLICAR DESCUENTO %\n\n"
                        f"Producto: {item.get('name','')}\n"
                        f"Precio actual: Bs {fmt_price_short(item.get('price', 0))}\n\n"
                        "Escribe el porcentaje de descuento.\n"
                        "Ejemplos válidos:\n"
                        "- 10\n"
                        "- 15%\n"
                        "- 20 por ciento"
                    ),
                )
                return {"ok": True}

            if action == "photo" and len(parts) == 4:
                sku = parts[3].strip()
                tmp["admin_menu_input_mode"] = "awaiting_photo"
                tmp["admin_menu_price_sku"] = sku

                item = get_menu_product_or_404(orders_sh, sku)

                telegram_send_text(
                    bot_token,
                    chat_id,
                    f"📷 Envía ahora la foto para:\n{item.get('name','')}"
                )
                return {"ok": True}

            return {"ok": True}

        return {"ok": True}

    except Exception as e:
        log_event(
            "admin_callback_error",
            tenant_id=tenant_id,
            chat_id=chat_id,
            data=data,
            error_type=type(e).__name__,
            error=str(e),
        )
        alert_system_error(error=str(e), module="admin_callback")
        telegram_send_text(bot_token, chat_id, "⚠️ Ocurrió un error en el panel admin.", reply_markup=admin_fixed_kb())
        return {"ok": True}


# =========================================================
# ADMIN MESSAGES
# =========================================================

def handle_admin_message(
    tenant: Dict[str, Any],
    tenant_id: str,
    bot_token: str,
    chat_id: int,
    msg: Dict[str, Any],
    orders_sh,
    tenant_tz: str,
) -> Dict[str, Any]:
    try:
        text = (msg.get("text") or "").strip()
        txt_norm = normalize(text)
        sess = get_sess(tenant_id, chat_id)
        tmp = sess.setdefault("tmp", {})

        admin_order_step = str(tmp.get("admin_order_step") or "").strip()

        if admin_order_step:
            assert_admin_authorized(tenant, chat_id, tenant_id)

            if admin_order_step == "awaiting_name":
                customer_name = text.strip()
                if not customer_name:
                    telegram_send_text(bot_token, chat_id, "El nombre no puede estar vacío. Escribe el nombre del cliente:")
                    return {"ok": True}

                tmp["admin_order_name"] = customer_name
                tmp["admin_order_step"] = "awaiting_contact"
                telegram_send_text(bot_token, chat_id, "Escribe el contacto del cliente (teléfono o referencia):")
                return {"ok": True}

            if admin_order_step == "awaiting_contact":
                customer_contact = text.strip()
                if not customer_contact:
                    telegram_send_text(bot_token, chat_id, "El contacto no puede estar vacío. Escribe el contacto del cliente:")
                    return {"ok": True}

                tmp["admin_order_contact"] = customer_contact
                tmp["admin_order_step"] = "awaiting_time"
                telegram_send_text(
                    bot_token,
                    chat_id,
                    "Escribe la hora solicitada.\nEjemplos: ahora, 19:30, 20h",
                )
                return {"ok": True}

            if admin_order_step == "awaiting_time":
                requested_time = text.strip()
                if not requested_time:
                    requested_time = "ahora"

                cart = tmp.get("admin_order_cart") or []
                customer_name = str(tmp.get("admin_order_name") or "").strip()
                customer_contact = str(tmp.get("admin_order_contact") or "").strip()

                if not cart or not customer_name or not customer_contact:
                    _admin_order_reset(tmp)
                    telegram_send_text(
                        bot_token,
                        chat_id,
                        "⚠️ Faltaban datos del pedido manual. Empecemos de nuevo.",
                        reply_markup=admin_fixed_kb(),
                    )
                    return {"ok": True}

                menu_idx = load_menu_admin_index(orders_sh, force=False)
                items_snapshot = build_items_snapshot(cart, menu_idx)
                _, total_amount, total_qty = fmt_snapshot_lines(items_snapshot)

                order_id = gen_order_id()

                result = append_order_row(
                    orders_sh=orders_sh,
                    tenant_id=tenant_id,
                    order_id=order_id,
                    customer_name=customer_name,
                    customer_contact=customer_contact,
                    customer_telegram_chat_id="",
                    items=cart,
                    items_snapshot=items_snapshot,
                    currency="BOB",
                    pricing_version="v1",
                    notes="",
                    delivery_type="pickup",
                    requested_time=requested_time,
                    status="PAID",
                    source="admin_manual",
                    total_amount=total_amount,
                )

                if not result.get("ok"):
                    alert_order_failed(
                        tenant_id=tenant_id,
                        order_id=order_id,
                        error=result.get("error") or "append_order_row failed",
                    )
                    telegram_send_text(
                        bot_token,
                        chat_id,
                        "⚠️ Error guardando el pedido manual.",
                        reply_markup=admin_fixed_kb(),
                    )
                    return {"ok": True}

                _admin_order_reset(tmp)

                telegram_send_text(
                    bot_token,
                    chat_id,
                    (
                        "✅ PEDIDO MANUAL REGISTRADO\n\n"
                        f"ID: {order_id}\n"
                        f"Cliente: {customer_name}\n"
                        f"Contacto: {customer_contact}\n"
                        f"Hora: {requested_time}\n"
                        f"Cantidad total: {total_qty}\n"
                        f"Total: Bs {total_amount:.2f}\n\n"
                        "Se guardó como PAID y ya cuenta para estadísticas."
                    ),
                    reply_markup=admin_fixed_kb(),
                )
                return {"ok": True}

        input_mode = str(tmp.get("admin_menu_input_mode") or "").strip()
        input_sku = str(tmp.get("admin_menu_price_sku") or "").strip()

        if input_mode == "awaiting_photo" and input_sku:
            assert_admin_authorized(tenant, chat_id, tenant_id)

            if msg.get("photo"):
                admin_file_id = msg["photo"][-1]["file_id"]

                try:
                    admin_file_path = telegram_get_file_path(bot_token, admin_file_id)
                    file_bytes = telegram_download_file_bytes(bot_token, admin_file_path)

                    content_type = "image/jpeg"
                    low_path = admin_file_path.lower()
                    if low_path.endswith(".png"):
                        content_type = "image/png"
                    elif low_path.endswith(".webp"):
                        content_type = "image/webp"

                    photo_url = upload_product_photo_for_tenant(
                        tenant=tenant,
                        tenant_id=tenant_id,
                        sku=input_sku,
                        file_bytes=file_bytes,
                        mime_type=content_type,
                    )
                except Exception as e:
                    telegram_send_text(bot_token, chat_id, "No pude subir la foto al storage configurado.")
                    log_event("admin_product_photo_storage_upload_failed", tenant_id=tenant_id, sku=input_sku, error=str(e))
                    alert_photo_upload_failed(tenant_id=tenant_id, sku=input_sku, error=str(e))
                    return {"ok": True}

                found = set_menu_photo_url(orders_sh, input_sku, photo_url)

                if not found:
                    alert_menu_error(tenant_id=tenant_id, sku=input_sku, error="SKU not found in Menu for photo update")
                    telegram_send_text(bot_token, chat_id, f"No encontré el producto SKU {input_sku} en la hoja Menu.")
                    return {"ok": True}

                invalidate_menu_cache(orders_sh)

                tmp.pop("admin_menu_input_mode", None)
                tmp.pop("admin_menu_price_sku", None)

                telegram_send_text(
                    bot_token,
                    chat_id,
                    "✅ Foto guardada correctamente y vinculada al producto.",
                )

                return {"ok": send_admin_menu_product_detail(
                    bot_token, chat_id, tenant_id, orders_sh, sess, input_sku
                )}

            telegram_send_text(
                bot_token,
                chat_id,
                "📷 Estoy esperando una foto del producto. Envíala como imagen de Telegram.",
            )
            return {"ok": True}

        if input_mode and input_sku:
            assert_admin_authorized(tenant, chat_id, tenant_id)

            item = get_menu_product_or_404(orders_sh, input_sku)
            current_price = float(item.get("price", 0.0))
            n = extract_first_number(text)

            if n is None:
                if input_mode == "price_final":
                    telegram_send_text(
                        bot_token,
                        chat_id,
                        "No pude leer un número válido.\nEscribe solo el precio o algo como: 25 bs",
                    )
                elif input_mode == "discount_pct":
                    telegram_send_text(
                        bot_token,
                        chat_id,
                        "No pude leer un porcentaje válido.\nEscribe algo como: 10 o 15%",
                    )
                return {"ok": True}

            if input_mode == "price_final":
                if n < 0:
                    telegram_send_text(bot_token, chat_id, "El precio no puede ser negativo. Intenta otra vez.")
                    return {"ok": True}

                result = set_menu_product_price(orders_sh, input_sku, float(n))
                tmp.pop("admin_menu_input_mode", None)
                tmp.pop("admin_menu_price_sku", None)
                tmp.pop("admin_menu_price_work", None)

                telegram_send_text(
                    bot_token,
                    chat_id,
                    f"✅ Precio actualizado.\nSKU: {input_sku}\nNuevo precio: Bs {fmt_price_short(result.get('price', 0))}",
                )
                return {"ok": send_admin_menu_product_detail(bot_token, chat_id, tenant_id, orders_sh, sess, input_sku)}

            if input_mode == "discount_pct":
                if n < 0:
                    telegram_send_text(bot_token, chat_id, "El descuento no puede ser negativo. Intenta otra vez.")
                    return {"ok": True}
                if n > 100:
                    telegram_send_text(bot_token, chat_id, "El descuento no puede ser mayor a 100%. Intenta otra vez.")
                    return {"ok": True}

                new_price = round(current_price * (1.0 - (float(n) / 100.0)), 2)
                if new_price < 0:
                    new_price = 0.0

                result = set_menu_product_price(orders_sh, input_sku, new_price)
                tmp.pop("admin_menu_input_mode", None)
                tmp.pop("admin_menu_price_sku", None)
                tmp.pop("admin_menu_price_work", None)

                telegram_send_text(
                    bot_token,
                    chat_id,
                    (
                        f"✅ Descuento aplicado.\n"
                        f"SKU: {input_sku}\n"
                        f"Descuento: {n}%\n"
                        f"Precio anterior: Bs {fmt_price_short(current_price)}\n"
                        f"Nuevo precio: Bs {fmt_price_short(result.get('price', 0))}"
                    ),
                )
                return {"ok": send_admin_menu_product_detail(bot_token, chat_id, tenant_id, orders_sh, sess, input_sku)}

        if txt_norm in ("estadisticas", "/stats", "stats"):
            assert_admin_authorized(tenant, chat_id, tenant_id)
            periods = build_periods(tenant_tz)
            telegram_send_text(
                bot_token,
                chat_id,
                "📊 Elige el período:",
                reply_markup=admin_periods_inline_kb(tenant_id, periods),
            )
            telegram_send_text(bot_token, chat_id, "Panel admin:", reply_markup=admin_fixed_kb())
            return {"ok": True}

        if (
            txt_norm in ("crear pedido", "crear pedido manual", "pedido manual", "nuevo pedido")
            or "crear pedido" in txt_norm
        ):
            assert_admin_authorized(tenant, chat_id, tenant_id)
            _admin_order_reset(tmp)
            tmp["admin_order_cart"] = []
            return {"ok": _send_admin_order_home(bot_token, chat_id, tenant_id, orders_sh, sess)}

        if txt_norm in (
            "base de consumidores",
            "consumidores",
            "clientes",
            "base consumidores",
        ):
            assert_admin_authorized(tenant, chat_id, tenant_id)
            return {"ok": _send_consumers_menu(bot_token, chat_id, tenant_id)}

        if txt_norm in (
            "config dias y horarios",
            "dias y horarios",
            "configuracion dias y horarios",
            "configuracion de dias y horarios",
        ):
            assert_admin_authorized(tenant, chat_id, tenant_id)
            return {"ok": send_admin_hours_menu(bot_token, chat_id, tenant_id, orders_sh, tenant_tz)}

        if txt_norm in (
            "config menu y precios",
            "menu y precios",
            "configuracion menu y precios",
            "configuracion de menu y precios",
        ):
            assert_admin_authorized(tenant, chat_id, tenant_id)
            return {"ok": send_admin_menu_home(bot_token, chat_id, tenant_id, orders_sh, sess)}

        if txt_norm in ("start", "/start", "hola"):
            telegram_send_text(bot_token, chat_id, "Admin bot listo ✅", reply_markup=admin_fixed_kb())
            return {"ok": True}

        telegram_send_text(bot_token, chat_id, "OK admin ✅", reply_markup=admin_fixed_kb())
        return {"ok": True}

    except Exception as e:
        log_event(
            "admin_message_error",
            tenant_id=tenant_id,
            chat_id=chat_id,
            error_type=type(e).__name__,
            error=str(e),
        )
        alert_system_error(error=str(e), module="admin_message")
        telegram_send_text(bot_token, chat_id, "⚠️ Ocurrió un error en el panel admin.", reply_markup=admin_fixed_kb())
        return {"ok": True}
