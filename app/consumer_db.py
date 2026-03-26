import json
import re
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple
from zoneinfo import ZoneInfo

from app.menu import load_menu_index
from app.sheets import detect_header_row
from app.telegram_keyboard import kb
from app.utils import log_event, normalize


MAX_TELEGRAM_TEXT = 3500
ORDERS_WORKSHEET_CANDIDATES = ["ORDERS", "Orders", "orders"]


@dataclass
class ConsumerPeriod:
    key: str
    label: str
    start_local: datetime
    end_local: datetime


def _month_name_es(month: int) -> str:
    names = ["Enero", "Febrero", "Marzo", "Abril", "Mayo", "Junio", "Julio", "Agosto", "Septiembre", "Octubre", "Noviembre", "Diciembre"]
    try:
        return names[month - 1]
    except Exception:
        return str(month)


def _quarter_number(month: int) -> int:
    if month in (1, 2, 3):
        return 1
    if month in (4, 5, 6):
        return 2
    if month in (7, 8, 9):
        return 3
    return 4


def _shift_year_month(year: int, month: int, delta_months: int) -> Tuple[int, int]:
    total = (year * 12 + (month - 1)) + delta_months
    new_year = total // 12
    new_month = (total % 12) + 1
    return new_year, new_month


def _start_of_day(dt: datetime) -> datetime:
    return datetime(dt.year, dt.month, dt.day, 0, 0, 0, tzinfo=dt.tzinfo)


def _start_of_month(dt: datetime) -> datetime:
    return datetime(dt.year, dt.month, 1, 0, 0, 0, tzinfo=dt.tzinfo)


def _start_of_year(dt: datetime) -> datetime:
    return datetime(dt.year, 1, 1, 0, 0, 0, tzinfo=dt.tzinfo)


def _start_of_week(dt: datetime) -> datetime:
    day_start = _start_of_day(dt)
    return day_start - timedelta(days=day_start.weekday())


def _start_of_quarter(dt: datetime) -> datetime:
    q = _quarter_number(dt.month)
    if q == 1:
        m = 1
    elif q == 2:
        m = 4
    elif q == 3:
        m = 7
    else:
        m = 10
    return datetime(dt.year, m, 1, 0, 0, 0, tzinfo=dt.tzinfo)


def consumer_period_options(tenant_tz: str) -> List[Tuple[str, str]]:
    tz = ZoneInfo(tenant_tz)
    now_local = datetime.now(tz)

    year = now_local.year
    month = now_local.month

    y1, m1 = _shift_year_month(year, month, -1)
    y2, m2 = _shift_year_month(year, month, -2)
    y3, m3 = _shift_year_month(year, month, -3)

    return [
        ("Esta semana", "this_week"),
        ("Semana pasada", "last_week"),
        ("Mes en curso", "month_to_date"),
        ("Mes anterior", "last_month"),
        (f"{_month_name_es(m1)} {y1}", "month_1_ago"),
        (f"{_month_name_es(m2)} {y2}", "month_2_ago"),
        (f"{_month_name_es(m3)} {y3}", "month_3_ago"),
        ("Trimestre en curso", "quarter_to_date"),
        ("Último trimestre", "last_quarter"),
        ("Año en curso", "year_to_date"),
    ]


def consumer_periods_inline_kb(tenant_id: str, tenant_tz: str = "America/La_Paz") -> Dict[str, Any]:
    rows = []
    for label, key in consumer_period_options(tenant_tz):
        rows.append([(f"👥 {label}", f"admcons|{tenant_id}|period|{key}")])
    rows.append([("⬅️ Volver al panel", f"admcons|{tenant_id}|panel")])
    return kb(rows)


def consumer_filters_inline_kb(tenant_id: str, period_key: str) -> Dict[str, Any]:
    return kb([
        [("📋 Todos", f"admcons|{tenant_id}|report|{period_key}|all")],
        [("🔁 Más de 1 vez", f"admcons|{tenant_id}|report|{period_key}|gt1")],
        [("🔁 Más de 2 veces", f"admcons|{tenant_id}|report|{period_key}|gt2")],
        [("🔁 Más de 3 veces", f"admcons|{tenant_id}|report|{period_key}|gt3")],
        [("⬅️ Períodos", f"admcons|{tenant_id}|menu")],
    ])


def resolve_consumer_period(period_key: str, tenant_tz: str) -> ConsumerPeriod:
    tz = ZoneInfo(tenant_tz)
    now_local = datetime.now(tz)

    if period_key == "this_week":
        start_local = _start_of_week(now_local)
        return ConsumerPeriod("this_week", "Esta semana", start_local, now_local)

    if period_key == "last_week":
        current_week_start = _start_of_week(now_local)
        last_week_start = current_week_start - timedelta(days=7)
        return ConsumerPeriod("last_week", "Semana pasada", last_week_start, current_week_start)

    if period_key == "month_to_date":
        start_local = _start_of_month(now_local)
        return ConsumerPeriod("month_to_date", "Mes en curso", start_local, now_local)

    if period_key == "last_month":
        y, m = _shift_year_month(now_local.year, now_local.month, -1)
        start_local = datetime(y, m, 1, 0, 0, 0, tzinfo=tz)
        end_local = datetime(now_local.year, now_local.month, 1, 0, 0, 0, tzinfo=tz)
        return ConsumerPeriod("last_month", "Mes anterior", start_local, end_local)

    if period_key == "month_1_ago":
        y, m = _shift_year_month(now_local.year, now_local.month, -1)
        start_local = datetime(y, m, 1, 0, 0, 0, tzinfo=tz)
        y_next, m_next = _shift_year_month(y, m, 1)
        end_local = datetime(y_next, m_next, 1, 0, 0, 0, tzinfo=tz)
        return ConsumerPeriod("month_1_ago", f"{_month_name_es(m)} {y}", start_local, end_local)

    if period_key == "month_2_ago":
        y, m = _shift_year_month(now_local.year, now_local.month, -2)
        start_local = datetime(y, m, 1, 0, 0, 0, tzinfo=tz)
        y_next, m_next = _shift_year_month(y, m, 1)
        end_local = datetime(y_next, m_next, 1, 0, 0, 0, tzinfo=tz)
        return ConsumerPeriod("month_2_ago", f"{_month_name_es(m)} {y}", start_local, end_local)

    if period_key == "month_3_ago":
        y, m = _shift_year_month(now_local.year, now_local.month, -3)
        start_local = datetime(y, m, 1, 0, 0, 0, tzinfo=tz)
        y_next, m_next = _shift_year_month(y, m, 1)
        end_local = datetime(y_next, m_next, 1, 0, 0, 0, tzinfo=tz)
        return ConsumerPeriod("month_3_ago", f"{_month_name_es(m)} {y}", start_local, end_local)

    if period_key == "quarter_to_date":
        start_local = _start_of_quarter(now_local)
        return ConsumerPeriod("quarter_to_date", "Trimestre en curso", start_local, now_local)

    if period_key == "last_quarter":
        current_q_start = _start_of_quarter(now_local)
        prev_q_end = current_q_start
        prev_q_start_year, prev_q_start_month = _shift_year_month(current_q_start.year, current_q_start.month, -3)
        prev_q_start = datetime(prev_q_start_year, prev_q_start_month, 1, 0, 0, 0, tzinfo=tz)
        prev_q_num = _quarter_number(prev_q_start_month)
        return ConsumerPeriod("last_quarter", f"T{prev_q_num} {prev_q_start_year}", prev_q_start, prev_q_end)

    if period_key == "year_to_date":
        start_local = _start_of_year(now_local)
        return ConsumerPeriod("year_to_date", "Año en curso", start_local, now_local)

    raise ValueError(f"Unknown consumer period: {period_key}")


def consumer_min_orders_from_filter(filter_key: str) -> int:
    if filter_key == "all":
        return 1
    if filter_key == "gt1":
        return 2
    if filter_key == "gt2":
        return 3
    if filter_key == "gt3":
        return 4
    raise ValueError(f"Unknown filter key: {filter_key}")


def consumer_filter_label(filter_key: str) -> str:
    if filter_key == "all":
        return "Todos los consumidores"
    if filter_key == "gt1":
        return "Clientes con más de 1 pedido"
    if filter_key == "gt2":
        return "Clientes con más de 2 pedidos"
    if filter_key == "gt3":
        return "Clientes con más de 3 pedidos"
    return "Consumidores"


def _parse_iso_dt_any(value: Any) -> Optional[datetime]:
    s = str(value or "").strip()
    if not s:
        return None

    candidates = [
        s,
        s.replace("Z", "+00:00"),
        s.replace(" ", "T"),
        s.replace(" ", "T").replace("Z", "+00:00"),
    ]

    for c in candidates:
        try:
            dt = datetime.fromisoformat(c)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=ZoneInfo("UTC"))
            return dt
        except Exception:
            continue

    return None


def _to_local(dt: datetime, tenant_tz: str) -> datetime:
    tz = ZoneInfo(tenant_tz)
    return dt.astimezone(tz)


def _normalize_header_name(h: Any) -> str:
    s = str(h or "").strip()
    return normalize(s).replace(" ", "_")


def _load_orders_records(orders_sh) -> List[Dict[str, Any]]:
    ws = None

    for name in ORDERS_WORKSHEET_CANDIDATES:
        try:
            ws = orders_sh.worksheet(name)
            break
        except Exception:
            continue

    if ws is None:
        try:
            ws = orders_sh.get_worksheet(0)
        except Exception as e:
            log_event("consumer_db_orders_ws_missing", error=str(e))
            return []

    try:
        values = ws.get_all_values()
    except Exception as e:
        log_event("consumer_db_get_all_values_failed", error=str(e))
        return []

    if not values:
        log_event("consumer_db_empty_sheet")
        return []

    try:
        header_row_1based = detect_header_row(
            values,
            required_headers=["created_at", "customer_name", "customer_contact", "status"],
            max_scan=min(10, len(values)),
        )
    except Exception as e:
        log_event("consumer_db_detect_header_failed", error=str(e))
        return []

    header = values[header_row_1based - 1]
    header_norm = [_normalize_header_name(h) for h in header]

    records: List[Dict[str, Any]] = []
    for ridx in range(header_row_1based + 1, len(values) + 1):
        row = values[ridx - 1]

        if not any(str(cell or "").strip() for cell in row):
            continue

        rec: Dict[str, Any] = {}
        for col_idx, key in enumerate(header_norm):
            if not key:
                continue
            rec[key] = row[col_idx] if col_idx < len(row) else ""

        records.append(rec)

    log_event(
        "consumer_db_records_loaded",
        worksheet_title=getattr(ws, "title", ""),
        header_row=header_row_1based,
        total_rows=len(values),
        loaded_records=len(records),
    )
    return records


def _parse_items_snapshot(order: Dict[str, Any]) -> List[Dict[str, Any]]:
    raw = order.get("items_snapshot")
    if isinstance(raw, list):
        return raw

    s = str(raw or "").strip()
    if not s:
        return []

    try:
        v = json.loads(s)
        return v if isinstance(v, list) else []
    except Exception:
        return []


def _parse_items(order: Dict[str, Any]) -> List[Dict[str, Any]]:
    raw = order.get("items")
    if isinstance(raw, list):
        return raw

    s = str(raw or "").strip()
    if not s:
        return []

    try:
        v = json.loads(s)
        return v if isinstance(v, list) else []
    except Exception:
        return []


def _resolve_product_name_from_sku(sku: str, menu_idx: Dict[str, Any]) -> str:
    sku = str(sku or "").strip()
    if not sku:
        return ""
    item = menu_idx.get(sku) or {}
    name = str(item.get("name") or "").strip()
    return name or sku


def _extract_product_counts(order: Dict[str, Any], menu_idx: Dict[str, Any]) -> Counter:
    out: Counter = Counter()

    items_snapshot = _parse_items_snapshot(order)
    if items_snapshot:
        for it in items_snapshot:
            name = str(it.get("name") or "").strip()
            sku = str(it.get("sku") or "").strip()

            if not name and sku:
                name = _resolve_product_name_from_sku(sku, menu_idx)

            if not name:
                continue

            try:
                qty = int(it.get("qty") or 1)
            except Exception:
                qty = 1
            qty = max(1, qty)
            out[name] += qty
        return out

    items = _parse_items(order)
    for it in items:
        sku = str(it.get("sku") or "").strip()
        name = str(it.get("name") or "").strip()

        if not name and sku:
            name = _resolve_product_name_from_sku(sku, menu_idx)

        if not name:
            continue

        try:
            qty = int(it.get("qty") or 1)
        except Exception:
            qty = 1
        qty = max(1, qty)
        out[name] += qty

    return out


def _normalize_contact(value: Any) -> str:
    s = str(value or "").strip()
    if not s:
        return ""
    digits = re.sub(r"\D+", "", s)
    return digits


def _clean_display_name(value: Any) -> str:
    s = " ".join(str(value or "").strip().split())
    return s if s else "Sin nombre"


def _normalized_name_key(value: Any) -> str:
    return normalize(" ".join(str(value or "").strip().split()))


def _build_consumer_key(contact_norm: str, name_norm: str, row_idx: int) -> str:
    if contact_norm:
        return f"contact:{contact_norm}"
    if name_norm:
        return f"nocontact_name:{name_norm}"
    return f"nocontact_row:{row_idx}"


def _pick_hybrid_display_name(
    name_counter: Counter,
    latest_name: str,
    latest_name_norm: str,
    latest_display_by_norm: Dict[str, str],
) -> str:
    if not name_counter:
        return latest_name or "Sin nombre"

    max_count = max(name_counter.values())
    tied_norms = [norm_name for norm_name, cnt in name_counter.items() if cnt == max_count]

    if latest_name_norm and latest_name_norm in tied_norms and latest_name:
        return latest_name

    chosen_norm = sorted(tied_norms)[0]
    return latest_display_by_norm.get(chosen_norm) or latest_name or "Sin nombre"


def _fmt_local_dt(dt: Optional[datetime]) -> str:
    if not dt:
        return "-"
    return dt.strftime("%Y-%m-%d %H:%M")


def _fmt_local_date(dt: datetime) -> str:
    return dt.strftime("%d/%m/%Y")


def _period_range_text(period: ConsumerPeriod) -> str:
    if period.end_local <= period.start_local:
        return f"{_fmt_local_date(period.start_local)} – {_fmt_local_date(period.start_local)}"
    end_inclusive = period.end_local - timedelta(seconds=1)
    return f"{_fmt_local_date(period.start_local)} – {_fmt_local_date(end_inclusive)}"


def _fmt_money(v: float) -> str:
    return f"{v:.2f}"


def _top_products_text(product_counter: Counter, top_n: int = 8) -> str:
    if not product_counter:
        return "-"

    parts = []
    for name, qty in product_counter.most_common(top_n):
        parts.append(f"{name} x{qty}")
    return ", ".join(parts)


def _match_period(created_local: Optional[datetime], period: ConsumerPeriod) -> bool:
    if not created_local:
        return False
    return period.start_local <= created_local < period.end_local


def _is_paid_order(order: Dict[str, Any]) -> bool:
    status = str(order.get("status") or "").strip().upper()
    return status == "PAID"


def aggregate_consumers(
    orders_sh,
    tenant_tz: str,
    period_key: str,
    min_orders: int,
) -> Tuple[ConsumerPeriod, List[Dict[str, Any]], int]:
    period = resolve_consumer_period(period_key, tenant_tz)
    rows = _load_orders_records(orders_sh)

    try:
        menu_idx = load_menu_index(orders_sh, force=False)
    except Exception as e:
        log_event("consumer_db_menu_load_failed", error=str(e))
        menu_idx = {}

    consumers: Dict[str, Dict[str, Any]] = {}
    paid_orders_in_period = 0

    for row_idx, row in enumerate(rows, start=1):
        if not _is_paid_order(row):
            continue

        created_dt = _parse_iso_dt_any(row.get("created_at"))
        if not created_dt:
            continue

        created_local = _to_local(created_dt, tenant_tz)
        if not _match_period(created_local, period):
            continue

        paid_orders_in_period += 1

        contact_raw = str(row.get("customer_contact") or "").strip()
        contact_norm = _normalize_contact(contact_raw)
        display_contact = contact_norm if contact_norm else "Sin contacto"

        raw_name = str(row.get("customer_name") or "").strip()
        display_name = _clean_display_name(raw_name)
        name_norm = _normalized_name_key(display_name)

        consumer_key = _build_consumer_key(contact_norm, name_norm, row_idx)

        try:
            total_amount = float(row.get("total_amount") or 0)
        except Exception:
            total_amount = 0.0

        product_counts = _extract_product_counts(row, menu_idx)

        if consumer_key not in consumers:
            consumers[consumer_key] = {
                "contact": display_contact,
                "contact_norm": contact_norm,
                "name_counter": Counter(),
                "latest_name": display_name,
                "latest_name_norm": name_norm,
                "latest_name_dt": created_local,
                "latest_display_by_norm": {},
                "orders_count": 0,
                "total_spent": 0.0,
                "last_purchase_dt": created_local,
                "last_order_counter": Counter(),
                "product_counter": Counter(),
            }

        c = consumers[consumer_key]
        c["name_counter"][name_norm] += 1
        c["orders_count"] += 1
        c["total_spent"] += total_amount
        c["product_counter"].update(product_counts)

        if name_norm:
            c["latest_display_by_norm"][name_norm] = display_name

        if created_local >= c["last_purchase_dt"]:
            c["last_purchase_dt"] = created_local
            c["last_order_counter"] = Counter(product_counts)

        if created_local >= c["latest_name_dt"]:
            c["latest_name_dt"] = created_local
            c["latest_name"] = display_name
            c["latest_name_norm"] = name_norm

    output: List[Dict[str, Any]] = []
    for _, c in consumers.items():
        if c["orders_count"] < min_orders:
            continue

        resolved_name = _pick_hybrid_display_name(
            c["name_counter"],
            c["latest_name"],
            c["latest_name_norm"],
            c["latest_display_by_norm"],
        )

        output.append({
            "name": resolved_name,
            "contact": c["contact"],
            "orders_count": int(c["orders_count"]),
            "total_spent": round(float(c["total_spent"]), 2),
            "last_purchase_dt": c["last_purchase_dt"],
            "last_order_text": _top_products_text(c["last_order_counter"]),
            "products_text": _top_products_text(c["product_counter"]),
        })

    output.sort(
        key=lambda x: (
            -int(x["orders_count"]),
            -float(x["total_spent"]),
            str(x["name"]).lower(),
        )
    )

    log_event(
        "consumer_db_aggregate_done",
        period_key=period_key,
        min_orders=min_orders,
        paid_orders_in_period=paid_orders_in_period,
        consumers_found=len(output),
    )

    return period, output, paid_orders_in_period


def build_consumers_report_pages(
    orders_sh,
    tenant_tz: str,
    period_key: str,
    filter_key: str,
) -> List[str]:
    min_orders = consumer_min_orders_from_filter(filter_key)
    filter_label = consumer_filter_label(filter_key)

    period, consumers, paid_orders_in_period = aggregate_consumers(
        orders_sh=orders_sh,
        tenant_tz=tenant_tz,
        period_key=period_key,
        min_orders=min_orders,
    )

    header = (
        "👥 BASE DE CONSUMIDORES\n\n"
        f"Período: {period.label}\n"
        f"Rango: {_period_range_text(period)}\n"
        f"Filtro: {filter_label}\n"
        f"Pedidos PAID en período: {paid_orders_in_period}\n"
        f"Consumidores encontrados: {len(consumers)}\n\n"
    )

    if not consumers:
        return [header + "No encontré consumidores para ese criterio."]

    entries: List[str] = []
    for idx, c in enumerate(consumers, start=1):
        entries.append(
            f"{idx}. {c['name']}\n"
            f"Contacto: {c['contact']}\n"
            f"Pedidos: {c['orders_count']}\n"
            f"Total gastado: Bs {_fmt_money(c['total_spent'])}\n"
            f"Última compra: {_fmt_local_dt(c['last_purchase_dt'])}\n"
            f"Último pedido: {c['last_order_text']}\n"
            f"Pedidos en total: {c['products_text']}\n"
        )

    pages: List[str] = []
    current = header

    for entry in entries:
        if len(current) + len(entry) + 2 > MAX_TELEGRAM_TEXT:
            pages.append(current.rstrip())
            current = header + entry
        else:
            current += entry + "\n"

    if current.strip():
        pages.append(current.rstrip())

    if len(pages) > 1:
        final_pages = []
        total_pages = len(pages)
        for i, page in enumerate(pages, start=1):
            final_pages.append(f"{page}\n\nPágina {i}/{total_pages}")
        return final_pages

    return pages
