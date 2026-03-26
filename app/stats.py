# app/stats.py — versión UX mejorada tipo app + resumen ejecutivo + insights + mini gráfico

import json
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, date
from typing import Any, Dict, List, Optional, Tuple

try:
    from zoneinfo import ZoneInfo
except Exception:
    ZoneInfo = None  # type: ignore

from fastapi import HTTPException

from app.utils import normalize, to_bool, log_event
from app.menu import load_menu_index


EVENTS_SHEET_NAME = "Events"
EVENTS_HEADERS = ["ts_utc", "tenant_id", "chat_id", "event_type", "meta_json"]


# -------------------------
# Time helpers
# -------------------------

def _tz(tenant_tz: str):
    if ZoneInfo is None:
        return None
    try:
        return ZoneInfo(tenant_tz or "America/La_Paz")
    except Exception:
        return ZoneInfo("America/La_Paz")


def _parse_iso_dt(s: str) -> Optional[datetime]:
    s = (s or "").strip()
    if not s:
        return None
    try:
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        return datetime.fromisoformat(s)
    except Exception:
        return None


def _now_utc() -> datetime:
    return datetime.utcnow().replace(tzinfo=None)


def _utc_iso() -> str:
    return datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")


def _local_day_range_utc(tenant_tz: str, day_local: date) -> Tuple[datetime, datetime]:
    tz = _tz(tenant_tz)
    if tz is None:
        start = datetime(day_local.year, day_local.month, day_local.day, 0, 0, 0)
        end = start + timedelta(days=1)
        return start, end

    start_local = datetime(day_local.year, day_local.month, day_local.day, 0, 0, 0, tzinfo=tz)
    end_local = start_local + timedelta(days=1)

    start_utc = start_local.astimezone(ZoneInfo("UTC")).replace(tzinfo=None)
    end_utc = end_local.astimezone(ZoneInfo("UTC")).replace(tzinfo=None)
    return start_utc, end_utc


def _local_month_range_utc(tenant_tz: str, year: int, month: int) -> Tuple[datetime, datetime]:
    tz = _tz(tenant_tz)
    if tz is None:
        start = datetime(year, month, 1, 0, 0, 0)
        if month == 12:
            end = datetime(year + 1, 1, 1, 0, 0, 0)
        else:
            end = datetime(year, month + 1, 1, 0, 0, 0)
        return start, end

    start_local = datetime(year, month, 1, 0, 0, 0, tzinfo=tz)
    if month == 12:
        end_local = datetime(year + 1, 1, 1, 0, 0, 0, tzinfo=tz)
    else:
        end_local = datetime(year, month + 1, 1, 0, 0, 0, tzinfo=tz)

    start_utc = start_local.astimezone(ZoneInfo("UTC")).replace(tzinfo=None)
    end_utc = end_local.astimezone(ZoneInfo("UTC")).replace(tzinfo=None)
    return start_utc, end_utc


def _fmt_date(d: datetime) -> str:
    return d.strftime("%d-%m-%Y")


def _fmt_date_local(d: datetime) -> str:
    return d.strftime("%d/%m/%Y")


def _weekday_es(dt: datetime) -> str:
    return ["Lun", "Mar", "Mié", "Jue", "Vie", "Sáb", "Dom"][dt.weekday()]


def _hour_bucket(dt: datetime) -> str:
    return dt.strftime("%H:00")


def _safe_float(v: Any) -> float:
    try:
        return float(str(v).replace(",", "."))
    except Exception:
        return 0.0


def _to_local(dt: datetime, tenant_tz: str) -> datetime:
    tz = _tz(tenant_tz)
    if tz is None:
        return dt
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=ZoneInfo("UTC"))
    return dt.astimezone(tz)


def _normalize_contact(value: Any) -> str:
    s = str(value or "").strip()
    if not s:
        return ""
    return re.sub(r"\D+", "", s)


def _period_range_text_local(period: "Period", tenant_tz: str) -> str:
    start_local = _to_local(period.start_utc.replace(tzinfo=ZoneInfo("UTC")), tenant_tz)
    end_local = _to_local((period.end_utc - timedelta(seconds=1)).replace(tzinfo=ZoneInfo("UTC")), tenant_tz)
    return f"{_fmt_date_local(start_local)} – {_fmt_date_local(end_local)}"


# -------------------------
# Worksheet helpers
# -------------------------

def _detect_header_row(values: List[List[str]], required_headers: List[str], max_scan: int = 30) -> int:
    req = [normalize(h) for h in required_headers]
    scan = values[:max_scan]
    for idx, row in enumerate(scan):
        row_norm = [normalize(x) for x in row]
        if all(h in row_norm for h in req):
            return idx
    return 0


def ensure_events_ws(orders_sh):
    try:
        ws = orders_sh.worksheet(EVENTS_SHEET_NAME)
        try:
            values = ws.get_all_values()
        except Exception:
            values = []
        if not values or not values[0]:
            ws.update("A1:E1", [EVENTS_HEADERS])
        return ws
    except Exception:
        try:
            ws = orders_sh.add_worksheet(title=EVENTS_SHEET_NAME, rows=2000, cols=10)
            ws.update("A1:E1", [EVENTS_HEADERS])
            return ws
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"Cannot create Events worksheet: {e}")


def log_event_to_sheet(orders_sh, tenant_id: str, chat_id: str, event_type: str, meta: Optional[Dict[str, Any]] = None) -> None:
    try:
        ws = ensure_events_ws(orders_sh)
        meta_json = json.dumps(meta or {}, ensure_ascii=False)
        row = [_utc_iso(), str(tenant_id), str(chat_id), str(event_type), meta_json]
        ws.append_row(row, value_input_option="USER_ENTERED")
    except Exception as e:
        log_event("events_append_failed", tenant_id=tenant_id, chat_id=str(chat_id), event_type=event_type, error=str(e))


def count_conversations_started(orders_sh, tenant_id: str, start_utc: datetime, end_utc: datetime) -> int:
    try:
        ws = orders_sh.worksheet(EVENTS_SHEET_NAME)
    except Exception:
        return 0

    try:
        values = ws.get_all_values()
    except Exception:
        return 0
    if not values:
        return 0

    hdr_idx = _detect_header_row(values, required_headers=EVENTS_HEADERS, max_scan=10)
    headers_raw = values[hdr_idx]
    headers_norm = [normalize(h) for h in headers_raw]

    def cidx(name: str) -> Optional[int]:
        k = normalize(name)
        return headers_norm.index(k) if k in headers_norm else None

    i_ts = cidx("ts_utc")
    i_tid = cidx("tenant_id")
    i_type = cidx("event_type")

    if i_ts is None or i_tid is None or i_type is None:
        return 0

    n = 0
    for row in values[hdr_idx + 1:]:
        tid = row[i_tid] if i_tid < len(row) else ""
        et = row[i_type] if i_type < len(row) else ""
        if normalize(tid) != normalize(tenant_id):
            continue
        if normalize(et) != normalize("client_start"):
            continue
        ts = _parse_iso_dt(row[i_ts] if i_ts < len(row) else "")
        if not ts:
            continue
        ts_utc = ts.replace(tzinfo=None)
        if start_utc <= ts_utc < end_utc:
            n += 1
    return n


# -------------------------
# Stats builder
# -------------------------

@dataclass
class Period:
    label: str
    start_utc: datetime
    end_utc: datetime


def build_periods(tenant_tz: str, now_utc: Optional[datetime] = None) -> List[Tuple[str, str]]:
    if now_utc is None:
        now_utc = _now_utc()

    tz = _tz(tenant_tz)
    if tz is None:
        now_local = now_utc
    else:
        now_local = now_utc.replace(tzinfo=ZoneInfo("UTC")).astimezone(tz)

    out: List[Tuple[str, str]] = []
    out.append(("Hoy", "today"))
    out.append(("Mes en curso", "mtd"))

    y = now_local.year
    m = now_local.month
    for _ in range(6):
        m -= 1
        if m <= 0:
            m = 12
            y -= 1
        out.append((f"{_month_name_es(m)} {y}", f"m:{y:04d}-{m:02d}"))

    return out


def _month_name_es(month: int) -> str:
    names = ["Enero","Febrero","Marzo","Abril","Mayo","Junio","Julio","Agosto","Septiembre","Octubre","Noviembre","Diciembre"]
    try:
        return names[month - 1]
    except Exception:
        return str(month)


def resolve_period(tenant_tz: str, period_key: str, now_utc: Optional[datetime] = None) -> Period:
    if now_utc is None:
        now_utc = _now_utc()

    tz = _tz(tenant_tz)
    if tz is None:
        now_local = now_utc
    else:
        now_local = now_utc.replace(tzinfo=ZoneInfo("UTC")).astimezone(tz)

    if period_key == "today":
        d = now_local.date()
        s, e = _local_day_range_utc(tenant_tz, d)
        return Period(label=f"{d.strftime('%d-%m-%Y')}", start_utc=s, end_utc=e)

    if period_key == "mtd":
        y = now_local.year
        m = now_local.month
        s, e = _local_month_range_utc(tenant_tz, y, m)
        return Period(label=f"{_month_name_es(m)} {y}", start_utc=s, end_utc=e)

    if period_key.startswith("m:"):
        ym = period_key.split(":", 1)[1].strip()
        try:
            y_s, m_s = ym.split("-", 1)
            y = int(y_s)
            m = int(m_s)
        except Exception:
            raise HTTPException(status_code=400, detail="Invalid period key")
        s, e = _local_month_range_utc(tenant_tz, y, m)
        return Period(label=f"{_month_name_es(m)} {y}", start_utc=s, end_utc=e)

    raise HTTPException(status_code=400, detail="Invalid period key")


def _parse_items(items_field: Any) -> List[Dict[str, Any]]:
    if isinstance(items_field, list):
        return items_field
    if isinstance(items_field, str) and items_field.strip():
        try:
            v = json.loads(items_field)
            return v if isinstance(v, list) else []
        except Exception:
            return []
    return []


def _bar(value: float, max_value: float, width: int = 10) -> str:
    if max_value <= 0:
        return ""
    filled = int(round((value / max_value) * width))
    filled = max(0, min(width, filled))
    return "█" * filled + "░" * (width - filled)


def build_stats_report_text(orders_sh, tenant_id: str, tenant_tz: str, period: Period) -> str:
    try:
        ws = orders_sh.worksheet("Orders")
        values = ws.get_all_values()
    except Exception:
        try:
            for w in orders_sh.worksheets():
                vals = w.get_all_values()
                if not vals:
                    continue
                hdr_idx = _detect_header_row(vals, required_headers=["order_id", "created_at", "status"], max_scan=30)
                hdr_norm = [normalize(x) for x in (vals[hdr_idx] or [])]
                if "order_id" in hdr_norm and "status" in hdr_norm:
                    ws = w
                    values = vals
                    break
            else:
                raise Exception("Orders not found")
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"Cannot read Orders: {e}")

    if not values:
        return f"📊 ESTADÍSTICAS\n\n📅 {_period_range_text_local(period, tenant_tz)}\n\nSin datos."

    hdr_idx = _detect_header_row(values, required_headers=["order_id", "created_at", "status"], max_scan=30)
    headers_raw = values[hdr_idx]
    headers_norm = [normalize(h) for h in headers_raw]

    def cidx(name: str) -> Optional[int]:
        k = normalize(name)
        return headers_norm.index(k) if k in headers_norm else None

    i_created = cidx("created_at")
    i_status = cidx("status")
    i_total = cidx("total_amount")
    i_items = cidx("items")
    i_contact = cidx("customer_contact")

    if i_created is None or i_status is None:
        return f"📊 ESTADÍSTICAS\n\n📅 {_period_range_text_local(period, tenant_tz)}\n\nLa hoja Orders no tiene las columnas requeridas."

    try:
        menu_idx = load_menu_index(orders_sh)
    except Exception:
        menu_idx = {}

    conversations = count_conversations_started(orders_sh, tenant_id, period.start_utc, period.end_utc)

    orders_created = 0
    orders_paid = 0

    paid_total_sales = 0.0
    paid_units_total = 0
    paid_customers: set = set()

    sku_units: Dict[str, int] = {}
    sku_sales: Dict[str, float] = {}

    cat_sales: Dict[str, float] = {}
    cat_orders: Dict[str, int] = {}

    weekday_stats: Dict[str, Dict[str, float]] = {}
    hour_stats_sales: Dict[str, float] = {}
    hour_stats_orders: Dict[str, int] = {}

    weekday_order = ["Lun", "Mar", "Mié", "Jue", "Vie", "Sáb", "Dom"]

    for row in values[hdr_idx + 1:]:
        created_s = row[i_created] if i_created < len(row) else ""
        dt = _parse_iso_dt(created_s)
        if not dt:
            continue

        dt_utc = dt.replace(tzinfo=None)
        if not (period.start_utc <= dt_utc < period.end_utc):
            continue

        dt_local = _to_local(dt, tenant_tz)

        orders_created += 1

        status = row[i_status] if i_status < len(row) else ""
        is_paid = normalize(status) == normalize("PAID")
        if not is_paid:
            continue

        orders_paid += 1

        total_s = row[i_total] if (i_total is not None and i_total < len(row)) else "0"
        order_sales = _safe_float(total_s)
        paid_total_sales += order_sales

        if i_contact is not None:
            c = row[i_contact] if i_contact < len(row) else ""
            c_norm = _normalize_contact(c)
            if c_norm:
                paid_customers.add(c_norm)

        items_field = row[i_items] if (i_items is not None and i_items < len(row)) else ""
        items = _parse_items(items_field)

        cats_in_order: set = set()
        order_units = 0

        for it in items:
            sku = str(it.get("sku", "") or "").strip()
            try:
                qty = int(it.get("qty", 1) or 1)
            except Exception:
                qty = 1
            qty = max(1, qty)

            if not sku:
                continue

            order_units += qty
            paid_units_total += qty
            sku_units[sku] = sku_units.get(sku, 0) + qty

            price = 0.0
            cat = "Otros"
            if sku in menu_idx:
                try:
                    price = float(menu_idx[sku].get("price") or 0)
                except Exception:
                    price = 0.0
                cat = str(menu_idx[sku].get("category") or "Otros")

            sku_sales[sku] = sku_sales.get(sku, 0.0) + (price * qty)
            cats_in_order.add(cat)

        weekday = _weekday_es(dt_local)
        if weekday not in weekday_stats:
            weekday_stats[weekday] = {"orders": 0, "units": 0, "sales": 0.0}
        weekday_stats[weekday]["orders"] += 1
        weekday_stats[weekday]["units"] += order_units
        weekday_stats[weekday]["sales"] += order_sales

        hour_key = _hour_bucket(dt_local)
        hour_stats_sales[hour_key] = hour_stats_sales.get(hour_key, 0.0) + order_sales
        hour_stats_orders[hour_key] = hour_stats_orders.get(hour_key, 0) + 1

        for cat in cats_in_order:
            cat_orders[cat] = cat_orders.get(cat, 0) + 1

    unpaid = max(0, orders_created - orders_paid)

    conv_to_order = (orders_created / conversations * 100.0) if conversations > 0 else 0.0
    order_to_paid = (orders_paid / orders_created * 100.0) if orders_created > 0 else 0.0

    ticket_avg = (paid_total_sales / orders_paid) if orders_paid > 0 else 0.0
    units_avg = (paid_units_total / orders_paid) if orders_paid > 0 else 0.0

    def sku_name(sku: str) -> str:
        if sku in menu_idx:
            return str(menu_idx[sku].get("name") or sku)
        return sku

    def sku_cat(sku: str) -> str:
        if sku in menu_idx:
            return str(menu_idx[sku].get("category") or "Otros")
        return "Otros"

    for sku, sales in sku_sales.items():
        cat = sku_cat(sku)
        cat_sales[cat] = cat_sales.get(cat, 0.0) + float(sales)

    top_units = sorted(sku_units.items(), key=lambda x: x[1], reverse=True)[:5]
    top_sales = sorted(sku_sales.items(), key=lambda x: x[1], reverse=True)[:5]
    top_cat = sorted(cat_sales.items(), key=lambda x: x[1], reverse=True)
    top_hours = sorted(hour_stats_sales.items(), key=lambda x: x[1], reverse=True)[:3]

    star_product = top_sales[0][0] if top_sales else ""
    star_product_name = sku_name(star_product) if star_product else ""

    best_day_sales = None
    best_day_value = -1.0
    for d in weekday_order:
        v = weekday_stats.get(d, {}).get("sales", 0.0)
        if v > best_day_value:
            best_day_value = v
            best_day_sales = d

    best_hour = top_hours[0][0] if top_hours else ""
    max_day_sales = max([weekday_stats.get(d, {}).get("sales", 0.0) for d in weekday_order], default=0.0)

    lines: List[str] = []

    lines.append("📊 ESTADÍSTICAS")
    lines.append("")
    lines.append(f"📅 {_period_range_text_local(period, tenant_tz)}")
    lines.append("")

    lines.append("🧠 RESUMEN EJECUTIVO")
    if orders_paid == 0:
        lines.append("No hubo pedidos pagados en este período.")
    else:
        summary_parts = [
            f"{orders_paid} pedidos pagados",
            f"Bs {paid_total_sales:.2f} en ventas",
            f"ticket promedio de Bs {ticket_avg:.2f}",
        ]
        if best_day_sales:
            summary_parts.append(f"mejor día: {best_day_sales}")
        if best_hour:
            summary_parts.append(f"mejor hora: {best_hour}")
        lines.append("• " + " | ".join(summary_parts))
    lines.append("")

    lines.append("🔹 RESUMEN")
    lines.append(f"Pedidos pagados: {orders_paid}")
    lines.append(f"Ventas: Bs {paid_total_sales:.2f}")
    lines.append(f"Ticket promedio: Bs {ticket_avg:.2f}")
    lines.append(f"Unidades promedio: {units_avg:.1f}")
    lines.append(f"Clientes únicos aprox.: {len(paid_customers)}")
    lines.append("")

    lines.append("🔹 EMBUDO")
    lines.append(f"Conversaciones iniciadas: {conversations}")
    lines.append(f"Pedidos creados: {orders_created}")
    lines.append(f"Pedidos pagados: {orders_paid}")
    lines.append(f"Conversación → pedido: {conv_to_order:.1f}%")
    lines.append(f"Pedido → pagado: {order_to_paid:.1f}%")
    lines.append(f"Pedidos no pagados: {unpaid}")
    lines.append("")

    lines.append("🔹 VENTAS POR DÍA")
    has_weekday_data = False
    for d in weekday_order:
        if d in weekday_stats:
            has_weekday_data = True
            v = weekday_stats[d]
            lines.append(f"{d}: {int(v['orders'])} pedidos | {int(v['units'])} unidades | Bs {float(v['sales']):.2f}")
    if not has_weekday_data:
        lines.append("Sin datos")
    lines.append("")

    lines.append("📈 GRÁFICO RÁPIDO DE VENTAS POR DÍA")
    if has_weekday_data and max_day_sales > 0:
        for d in weekday_order:
            sales = weekday_stats.get(d, {}).get("sales", 0.0)
            if sales > 0:
                lines.append(f"{d:<3} {_bar(float(sales), max_day_sales, 10)} Bs {float(sales):.0f}")
    else:
        lines.append("Sin datos")
    lines.append("")

    lines.append("🔹 MEJORES HORAS")
    if top_hours:
        for h, s in top_hours:
            n_orders = hour_stats_orders.get(h, 0)
            lines.append(f"{h} → Bs {s:.2f} | {n_orders} pedidos")
    else:
        lines.append("Sin datos")
    lines.append("")

    lines.append("🔹 TOP PRODUCTOS POR VENTAS")
    if top_sales:
        for i, (sku, s) in enumerate(top_sales, 1):
            share = (s / paid_total_sales * 100.0) if paid_total_sales > 0 else 0.0
            lines.append(f"{i}. {sku_name(sku)} — Bs {s:.2f} ({share:.1f}%)")
    else:
        lines.append("Sin datos")
    lines.append("")

    lines.append("🔹 TOP PRODUCTOS POR UNIDADES")
    if top_units:
        for i, (sku, u) in enumerate(top_units, 1):
            lines.append(f"{i}. {sku_name(sku)} — {u}")
    else:
        lines.append("Sin datos")
    lines.append("")

    lines.append("🔹 CATEGORÍAS")
    if top_cat:
        for cat, s in top_cat:
            share = (s / paid_total_sales * 100.0) if paid_total_sales > 0 else 0.0
            n_orders = cat_orders.get(cat, 0)
            lines.append(f"{cat} — Bs {s:.2f} ({share:.1f}%) | {n_orders} pedidos")
    else:
        lines.append("Sin datos")
    lines.append("")

    lines.append("💡 INSIGHTS")
    if orders_paid == 0:
        lines.append("• Aún no hay suficiente información en este período.")
    else:
        if star_product_name:
            lines.append(f"• Producto estrella por ventas: {star_product_name}.")
        if unpaid > 0:
            lines.append(f"• Hay {unpaid} pedidos no pagados: conviene revisar fricción de pago y recordatorios.")
        if best_day_sales:
            lines.append(f"• El día con mayor venta fue {best_day_sales}.")
        if best_hour:
            lines.append(f"• La hora más fuerte fue {best_hour}.")
        if units_avg < 2:
            lines.append("• Hay espacio para crecer en unidades por pedido con combos y adicionales.")
        else:
            lines.append("• El promedio de unidades por pedido ya muestra una compra relativamente completa.")

    return "\n".join(lines)
