# app/stats.py

import json
from dataclasses import dataclass
from datetime import datetime, timedelta, date
from typing import Any, Dict, List, Optional, Tuple

try:
    from zoneinfo import ZoneInfo
except Exception:
    ZoneInfo = None  # type: ignore

from fastapi import HTTPException

from app.utils import normalize, to_bool, log_event
from app.menu import load_menu_index, group_menu_by_category


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
        # soporta ...Z o +00:00
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        return datetime.fromisoformat(s)
    except Exception:
        return None


def _now_utc() -> datetime:
    return datetime.utcnow().replace(tzinfo=None)


def _utc_iso() -> str:
    # ISO UTC naive, consistente con tu now_iso_utc() pero sin import para no tocar utils
    return datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")


def _local_day_range_utc(tenant_tz: str, day_local: date) -> Tuple[datetime, datetime]:
    """
    Devuelve (start_utc_naive, end_utc_naive) para el día local.
    """
    tz = _tz(tenant_tz)
    if tz is None:
        # fallback naive: asume UTC
        start = datetime(day_local.year, day_local.month, day_local.day, 0, 0, 0)
        end = start + timedelta(days=1)
        return start, end

    start_local = datetime(day_local.year, day_local.month, day_local.day, 0, 0, 0, tzinfo=tz)
    end_local = start_local + timedelta(days=1)

    start_utc = start_local.astimezone(ZoneInfo("UTC")).replace(tzinfo=None)
    end_utc = end_local.astimezone(ZoneInfo("UTC")).replace(tzinfo=None)
    return start_utc, end_utc


def _local_month_range_utc(tenant_tz: str, year: int, month: int) -> Tuple[datetime, datetime]:
    """
    (start_utc_naive, end_utc_naive) para un mes local [start, next_month_start)
    """
    tz = _tz(tenant_tz)
    if tz is None:
        start = datetime(year, month, 1, 0, 0, 0)
        # next month
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


def _row_dict(headers_norm: List[str], row: List[Any]) -> Dict[str, Any]:
    out: Dict[str, Any] = {}
    for j, h in enumerate(headers_norm):
        out[h] = row[j] if j < len(row) else ""
    return out


def ensure_events_ws(orders_sh):
    """
    Crea (si no existe) una pestaña Events con headers.
    """
    try:
        ws = orders_sh.worksheet(EVENTS_SHEET_NAME)
        # si existe pero está vacía, asegurar headers
        try:
            values = ws.get_all_values()
        except Exception:
            values = []
        if not values or not values[0]:
            ws.update("A1:E1", [EVENTS_HEADERS])
        return ws
    except Exception:
        # crear
        try:
            ws = orders_sh.add_worksheet(title=EVENTS_SHEET_NAME, rows=2000, cols=10)
            ws.update("A1:E1", [EVENTS_HEADERS])
            return ws
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"Cannot create Events worksheet: {e}")


def log_event_to_sheet(orders_sh, tenant_id: str, chat_id: str, event_type: str, meta: Optional[Dict[str, Any]] = None) -> None:
    """
    Persiste eventos para métricas (conversaciones iniciadas).
    """
    try:
        ws = ensure_events_ws(orders_sh)
        meta_json = json.dumps(meta or {}, ensure_ascii=False)
        row = [_utc_iso(), str(tenant_id), str(chat_id), str(event_type), meta_json]
        ws.append_row(row, value_input_option="USER_ENTERED")
    except Exception as e:
        # No romper el flujo del bot si falla la métrica
        log_event("events_append_failed", tenant_id=tenant_id, chat_id=str(chat_id), event_type=event_type, error=str(e))


def count_conversations_started(orders_sh, tenant_id: str, start_utc: datetime, end_utc: datetime) -> int:
    """
    Cuenta Events(event_type=client_start) en rango UTC.
    """
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

    # columnas
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
        # ts viene UTC; lo tratamos como aware? aquí lo mantenemos naive UTC
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
    """
    Devuelve lista de (button_label, period_key)
    period_key: "today", "mtd", "m:YYYY-MM"
    """
    if now_utc is None:
        now_utc = _now_utc()

    tz = _tz(tenant_tz)
    if tz is None:
        # fallback UTC
        now_local = now_utc
    else:
        now_local = now_utc.replace(tzinfo=ZoneInfo("UTC")).astimezone(tz)

    today_local = now_local.date()

    out: List[Tuple[str, str]] = []
    out.append(("Hoy", "today"))
    out.append(("Mes en curso", "mtd"))

    # últimos 6 meses (incluyendo el mes actual? no, ya está mtd; mostramos 6 anteriores o 6 últimos contando el actual?)
    # Pedido: "últimos seis meses" + mes en curso => 7 meses + hoy => 8 botones. Perfecto.
    y = now_local.year
    m = now_local.month
    for _ in range(6):
        # ir al mes anterior
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
        return Period(label=f"{d.strftime('%d %b %Y')}", start_utc=s, end_utc=e)

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


def build_stats_report_text(orders_sh, tenant_id: str, tenant_tz: str, period: Period) -> str:
    """
    Reporte grande:
    - ventas/top/categorías SOLO PAID
    - embudo: conversaciones (Events) + pedidos creados (todas órdenes) + pagados
    """
    # 1) leer órdenes
    try:
        ws = orders_sh.worksheet("Orders")
        values = ws.get_all_values()
    except Exception:
        # fallback: buscar por headers mínimos en cualquier pestaña
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
        return f"📊 ESTADÍSTICAS — {period.label}\n\n(No hay datos.)"

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
        return f"📊 ESTADÍSTICAS — {period.label}\n\n(Orders no tiene columnas required.)"

    # 2) menu idx para nombres y categorías
    try:
        menu_idx = load_menu_index(orders_sh)
    except Exception:
        menu_idx = {}

    # 3) embudo
    conversations = count_conversations_started(orders_sh, tenant_id, period.start_utc, period.end_utc)

    orders_created = 0
    orders_paid = 0

    paid_total_sales = 0.0
    paid_units_total = 0
    paid_customers: set = set()

    # por sku
    sku_units: Dict[str, int] = {}
    sku_sales: Dict[str, float] = {}

    # por categoría
    cat_sales: Dict[str, float] = {}
    cat_orders: Dict[str, int] = {}  # conteo de pedidos donde aparece esa categoría (aprox)

    for row in values[hdr_idx + 1:]:
        created_s = row[i_created] if i_created < len(row) else ""
        dt = _parse_iso_dt(created_s)
        if not dt:
            continue
        dt_utc = dt.replace(tzinfo=None)
        if not (period.start_utc <= dt_utc < period.end_utc):
            continue

        orders_created += 1

        status = row[i_status] if i_status < len(row) else ""
        is_paid = normalize(status) == normalize("PAID")
        if not is_paid:
            continue

        orders_paid += 1

        # total
        total_s = row[i_total] if (i_total is not None and i_total < len(row)) else "0"
        try:
            paid_total_sales += float(str(total_s).replace(",", "."))
        except Exception:
            pass

        # customer unique
        if i_contact is not None:
            c = row[i_contact] if i_contact < len(row) else ""
            if c:
                paid_customers.add(str(c).strip())

        # items
        items_field = row[i_items] if (i_items is not None and i_items < len(row)) else ""
        items = _parse_items(items_field)

        cats_in_order: set = set()

        for it in items:
            sku = str(it.get("sku", "") or "").strip()
            try:
                qty = int(it.get("qty", 1) or 1)
            except Exception:
                qty = 1
            qty = max(1, qty)

            if not sku:
                continue

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

        for cat in cats_in_order:
            cat_orders[cat] = cat_orders.get(cat, 0) + 1

    unpaid = max(0, orders_created - orders_paid)

    conv_to_order = (orders_created / conversations * 100.0) if conversations > 0 else 0.0
    order_to_paid = (orders_paid / orders_created * 100.0) if orders_created > 0 else 0.0

    ticket_avg = (paid_total_sales / orders_paid) if orders_paid > 0 else 0.0
    units_avg = (paid_units_total / orders_paid) if orders_paid > 0 else 0.0

    # Top productos por unidades / ventas
    def sku_name(sku: str) -> str:
        if sku in menu_idx:
            return str(menu_idx[sku].get("name") or sku)
        return sku

    def sku_cat(sku: str) -> str:
        if sku in menu_idx:
            return str(menu_idx[sku].get("category") or "Otros")
        return "Otros"

    # construir ventas por categoría (sumando sku_sales)
    for sku, sales in sku_sales.items():
        cat = sku_cat(sku)
        cat_sales[cat] = cat_sales.get(cat, 0.0) + float(sales)

    top_units = sorted(sku_units.items(), key=lambda x: x[1], reverse=True)[:5]
    top_sales = sorted(sku_sales.items(), key=lambda x: x[1], reverse=True)[:5]
    top_cat = sorted(cat_sales.items(), key=lambda x: x[1], reverse=True)

    # Insights simples
    star_product = top_sales[0][0] if top_sales else ""
    star_product_name = sku_name(star_product) if star_product else ""

    # armar texto
    lines: List[str] = []
    lines.append(f"📊 ESTADÍSTICAS — {period.label} (solo pedidos PAID)")
    lines.append(f"Período (UTC): {period.start_utc.strftime('%Y-%m-%d %H:%M')} → {period.end_utc.strftime('%Y-%m-%d %H:%M')}")
    lines.append("")
    lines.append("1) Resultado general")
    lines.append(f"- ✅ Pedidos pagados: {orders_paid}")
    lines.append(f"- 💰 Ventas totales: {paid_total_sales:.2f} BOB")
    lines.append(f"- 🧾 Ticket promedio: {ticket_avg:.2f} BOB")
    lines.append(f"- 📦 Unidades promedio por pedido: {units_avg:.1f}")
    lines.append(f"- 🧍 Clientes únicos (aprox): {len(paid_customers)}")
    lines.append("")
    lines.append("2) Conversión (embudo)")
    lines.append(f"- 💬 Conversaciones iniciadas (/start): {conversations}")
    lines.append(f"- 🛒 Pedidos creados (todos): {orders_created}")
    lines.append(f"- ✅ Pedidos pagados: {orders_paid}")
    lines.append(f"- 📈 Conversación → pedido: {conv_to_order:.1f}%")
    lines.append(f"- 📈 Pedido → pago: {order_to_paid:.1f}%")
    lines.append(f"- 🧊 Pedidos sin pagar: {unpaid}")
    lines.append("")
    lines.append("3) Top productos")
    lines.append("Por unidades:")
    if top_units:
        for i, (sku, u) in enumerate(top_units, 1):
            lines.append(f"{i}. {sku_name(sku)} — {u} u")
    else:
        lines.append("- (sin datos)")

    lines.append("")
    lines.append("Por ventas:")
    if top_sales:
        for i, (sku, s) in enumerate(top_sales, 1):
            share = (s / paid_total_sales * 100.0) if paid_total_sales > 0 else 0.0
            lines.append(f"{i}. {sku_name(sku)} — {s:.2f} BOB ({share:.1f}%)")
    else:
        lines.append("- (sin datos)")

    lines.append("")
    lines.append("4) Rendimiento por categoría")
    if top_cat:
        for cat, s in top_cat:
            share = (s / paid_total_sales * 100.0) if paid_total_sales > 0 else 0.0
            n_orders = cat_orders.get(cat, 0)
            lines.append(f"- {cat} — {s:.2f} BOB ({share:.1f}%) — {n_orders} pedidos")
    else:
        lines.append("- (sin datos)")

    lines.append("")
    lines.append("5) Insights")
    if star_product_name:
        lines.append(f"- ⭐ Producto estrella (mayor facturación): {star_product_name}")
    if unpaid > 0:
        lines.append(f"- 💡 Oportunidad: {unpaid} pedidos quedaron sin pagar → revisar fricción de pago / recordatorios")
    if orders_paid > 0 and ticket_avg > 0:
        lines.append("- 📌 Recomendación: destacar combos / upsells para subir ticket promedio")

    return "\n".join(lines)
