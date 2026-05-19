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


def _local_year_range_utc(tenant_tz: str, year: int) -> Tuple[datetime, datetime]:
    tz = _tz(tenant_tz)
    if tz is None:
        start = datetime(year, 1, 1, 0, 0, 0)
        end = datetime(year + 1, 1, 1, 0, 0, 0)
        return start, end

    start_local = datetime(year, 1, 1, 0, 0, 0, tzinfo=tz)
    end_local = datetime(year + 1, 1, 1, 0, 0, 0, tzinfo=tz)

    start_utc = start_local.astimezone(ZoneInfo("UTC")).replace(tzinfo=None)
    end_utc = end_local.astimezone(ZoneInfo("UTC")).replace(tzinfo=None)
    return start_utc, end_utc


def _local_datetime_to_utc_naive(dt_local: datetime) -> datetime:
    if dt_local.tzinfo is None:
        return dt_local
    return dt_local.astimezone(ZoneInfo("UTC")).replace(tzinfo=None)


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


def _quarter_start_month(month: int) -> int:
    q = _quarter_number(month)
    if q == 1:
        return 1
    if q == 2:
        return 4
    if q == 3:
        return 7
    return 10


def _quarter_label(year: int, quarter: int) -> str:
    return f"T{quarter} {year}"


def _shift_year_month(year: int, month: int, delta_months: int) -> Tuple[int, int]:
    total = (year * 12 + (month - 1)) + delta_months
    new_year = total // 12
    new_month = (total % 12) + 1
    return new_year, new_month


def _local_week_start(now_local: datetime) -> datetime:
    return datetime(now_local.year, now_local.month, now_local.day, 0, 0, 0, tzinfo=now_local.tzinfo) - timedelta(days=now_local.weekday())


def _local_day_start(now_local: datetime) -> datetime:
    return datetime(now_local.year, now_local.month, now_local.day, 0, 0, 0, tzinfo=now_local.tzinfo)


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

    year = now_local.year
    month = now_local.month

    y1, m1 = _shift_year_month(year, month, -1)
    y2, m2 = _shift_year_month(year, month, -2)
    y3, m3 = _shift_year_month(year, month, -3)

    return [
        ("Hoy", "today"),
        ("Ayer", "yesterday"),
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


def resolve_period(tenant_tz: str, period_key: str, now_utc: Optional[datetime] = None) -> Period:
    if now_utc is None:
        now_utc = _now_utc()

    tz = _tz(tenant_tz)
    if tz is None:
        now_local = now_utc
    else:
        now_local = now_utc.replace(tzinfo=ZoneInfo("UTC")).astimezone(tz)

    if period_key == "today":
        start_local = _local_day_start(now_local)
        return Period(
            label="Hoy",
            start_utc=_local_datetime_to_utc_naive(start_local),
            end_utc=now_utc,
        )

    if period_key == "yesterday":
        current_day_start = _local_day_start(now_local)
        yesterday_start = current_day_start - timedelta(days=1)
        return Period(
            label="Ayer",
            start_utc=_local_datetime_to_utc_naive(yesterday_start),
            end_utc=_local_datetime_to_utc_naive(current_day_start),
        )

    if period_key == "this_week":
        start_local = _local_week_start(now_local)
        end_utc = now_utc
        return Period(
            label="Esta semana",
            start_utc=_local_datetime_to_utc_naive(start_local),
            end_utc=end_utc,
        )

    if period_key == "last_week":
        current_week_start = _local_week_start(now_local)
        last_week_start = current_week_start - timedelta(days=7)
        last_week_end = current_week_start
        return Period(
            label="Semana pasada",
            start_utc=_local_datetime_to_utc_naive(last_week_start),
            end_utc=_local_datetime_to_utc_naive(last_week_end),
        )

    if period_key == "month_to_date":
        start_utc, _ = _local_month_range_utc(tenant_tz, now_local.year, now_local.month)
        return Period(
            label="Mes en curso",
            start_utc=start_utc,
            end_utc=now_utc,
        )

    if period_key == "last_month":
        y, m = _shift_year_month(now_local.year, now_local.month, -1)
        s, e = _local_month_range_utc(tenant_tz, y, m)
        return Period(
            label="Mes anterior",
            start_utc=s,
            end_utc=e,
        )

    if period_key == "month_1_ago":
        y, m = _shift_year_month(now_local.year, now_local.month, -1)
        s, e = _local_month_range_utc(tenant_tz, y, m)
        return Period(
            label=f"{_month_name_es(m)} {y}",
            start_utc=s,
            end_utc=e,
        )

    if period_key == "month_2_ago":
        y, m = _shift_year_month(now_local.year, now_local.month, -2)
        s, e = _local_month_range_utc(tenant_tz, y, m)
        return Period(
            label=f"{_month_name_es(m)} {y}",
            start_utc=s,
            end_utc=e,
        )

    if period_key == "month_3_ago":
        y, m = _shift_year_month(now_local.year, now_local.month, -3)
        s, e = _local_month_range_utc(tenant_tz, y, m)
        return Period(
            label=f"{_month_name_es(m)} {y}",
            start_utc=s,
            end_utc=e,
        )

    if period_key == "quarter_to_date":
        q_start_month = _quarter_start_month(now_local.month)
        tzinfo = now_local.tzinfo
        start_local = datetime(now_local.year, q_start_month, 1, 0, 0, 0, tzinfo=tzinfo)
        return Period(
            label="Trimestre en curso",
            start_utc=_local_datetime_to_utc_naive(start_local),
            end_utc=now_utc,
        )

    if period_key == "last_quarter":
        current_q_start_month = _quarter_start_month(now_local.month)
        current_q_start_local = datetime(now_local.year, current_q_start_month, 1, 0, 0, 0, tzinfo=now_local.tzinfo)
        prev_q_end_local = current_q_start_local

        prev_q_start_year = current_q_start_local.year
        prev_q_start_month = current_q_start_month - 3
        if prev_q_start_month <= 0:
            prev_q_start_month += 12
            prev_q_start_year -= 1

        prev_q_start_local = datetime(prev_q_start_year, prev_q_start_month, 1, 0, 0, 0, tzinfo=now_local.tzinfo)
        prev_q_num = _quarter_number(prev_q_start_month)

        return Period(
            label=_quarter_label(prev_q_start_year, prev_q_num),
            start_utc=_local_datetime_to_utc_naive(prev_q_start_local),
            end_utc=_local_datetime_to_utc_naive(prev_q_end_local),
        )

    if period_key == "year_to_date":
        s, _ = _local_year_range_utc(tenant_tz, now_local.year)
        return Period(
            label="Año en curso",
            start_utc=s,
            end_utc=now_utc,
        )

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


def _item_qty(it: Dict[str, Any]) -> int:
    try:
        qty = int(it.get("qty", 1) or 1)
    except Exception:
        qty = 1
    return max(1, qty)


def _item_display_name(it: Dict[str, Any], menu_idx: Dict[str, Any]) -> str:
    sku = str(it.get("sku", "") or "").strip()
    if sku and sku in menu_idx:
        return str(menu_idx[sku].get("name") or sku).strip() or sku

    item_name = str(it.get("name", "") or "").strip()
    if item_name:
        return item_name

    return sku


def _build_order_combination(items: List[Dict[str, Any]], menu_idx: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    aggregated: Dict[str, Dict[str, Any]] = {}

    for it in items or []:
        item_name = _item_display_name(it, menu_idx)
        if not item_name:
            continue

        qty = _item_qty(it)
        item_key = normalize(item_name)
        current = aggregated.get(item_key)
        if current is None:
            aggregated[item_key] = {
                "name": item_name,
                "qty": qty,
            }
        else:
            current["qty"] = int(current.get("qty") or 0) + qty

    if not aggregated:
        return None
    if len(aggregated) <= 1:
        return None

    parts: List[str] = []
    for item_key in sorted(aggregated.keys()):
        row = aggregated[item_key]
        item_name = str(row.get("name") or "").strip()
        qty = int(row.get("qty") or 0)
        if qty > 1:
            parts.append(f"{item_name} x{qty}")
        else:
            parts.append(item_name)

    if not parts:
        return None

    return {
        "key": "||".join(parts),
        "products": list(parts),
        "label": " + ".join(parts),
    }


def _bar(value: float, max_value: float, width: int = 10) -> str:
    if max_value <= 0:
        return ""
    filled = int(round((value / max_value) * width))
    filled = max(0, min(width, filled))
    return "█" * filled + "░" * (width - filled)


def _legacy_build_stats_summary_data(orders_sh, tenant_id: str, tenant_tz: str, period: Period) -> Dict[str, Any]:
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

    def _empty_summary(insights: List[str]) -> Dict[str, Any]:
        return {
            "period": {
                "label": period.label,
                "range_text": _period_range_text_local(period, tenant_tz),
            },
            "kpis": {
                "sales_total": 0.0,
                "orders_created": 0,
                "orders_paid": 0,
                "orders_unpaid": 0,
                "avg_ticket": 0.0,
                "avg_units_per_order": 0.0,
                "conversations_started": 0,
                "conv_conversation_to_order": 0.0,
                "conv_order_to_paid": 0.0,
                "unique_customers": 0,
            },
            "sales_by_day": [],
            "sales_by_hour": [],
            "top_products": [],
            "categories": [],
            "insights": insights,
        }

    if not values:
        return _empty_summary(["Aún no hay datos disponibles para este período."])

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
        return _empty_summary(["La hoja Orders no tiene las columnas requeridas."])

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
    item_count_distribution: Dict[int, int] = {}
    combination_counts: Dict[str, Dict[str, Any]] = {}
    weekday_stats: Dict[str, Dict[str, float]] = {}
    hour_stats_sales: Dict[str, float] = {}
    hour_stats_orders: Dict[str, int] = {}
    weekday_order = ["Lun", "Mar", "MiÃ©", "Jue", "Vie", "SÃ¡b", "Dom"]

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
            qty = _item_qty(it)

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

        if order_units <= 0 and order_sales > 0:
            order_units = 1

        item_count_distribution[order_units] = item_count_distribution.get(order_units, 0) + 1
        combination = _build_order_combination(items, menu_idx)
        if combination is not None:
            combo_key = str(combination.get("key") or "").strip()
            if combo_key:
                current_combo = combination_counts.get(combo_key)
                if current_combo is None:
                    combination_counts[combo_key] = {
                        "products": list(combination.get("products") or []),
                        "label": str(combination.get("label") or "").strip(),
                        "orders_count": 1,
                        "sales": float(order_sales),
                    }
                else:
                    current_combo["orders_count"] = int(current_combo.get("orders_count") or 0) + 1
                    current_combo["sales"] = float(current_combo.get("sales") or 0.0) + float(order_sales)

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
    top_hours = sorted(hour_stats_sales.items(), key=lambda x: x[1], reverse=True)

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

    insights: List[str] = []
    if orders_paid == 0:
        insights.append("Aún no hay suficiente información en este período.")
    else:
        if star_product_name:
            insights.append(f"Producto estrella por ventas: {star_product_name}.")
        if unpaid > 0:
            insights.append(f"Hay {unpaid} pedidos no pagados: conviene revisar fricción de pago y recordatorios.")
        if best_day_sales:
            insights.append(f"El día con mayor venta fue {best_day_sales}.")
        if best_hour:
            insights.append(f"La hora más fuerte fue {best_hour}.")
        if units_avg < 2:
            insights.append("Hay espacio para crecer en unidades por pedido con combos y adicionales.")
        else:
            insights.append("El promedio de unidades por pedido ya muestra una compra relativamente completa.")

    sales_by_day = []
    for d in weekday_order:
        if d not in weekday_stats:
            continue
        v = weekday_stats[d]
        sales_by_day.append({
            "label": d,
            "orders": int(v["orders"]),
            "units": int(v["units"]),
            "sales": round(float(v["sales"]), 2),
        })

    sales_by_hour = []
    for hour_key, sales in top_hours:
        sales_by_hour.append({
            "label": hour_key,
            "orders": int(hour_stats_orders.get(hour_key, 0)),
            "sales": round(float(sales), 2),
        })

    top_products = []
    top_sales_map = {sku: float(sales) for sku, sales in top_sales}
    top_skus: List[str] = []
    for sku, _ in top_sales:
        if sku not in top_skus:
            top_skus.append(sku)
    for sku, _ in top_units:
        if sku not in top_skus:
            top_skus.append(sku)
    for sku in top_skus[:5]:
        units_value = int(sku_units.get(sku, 0) or 0)
        if float(top_sales_map.get(sku, 0.0)) > 0 and units_value <= 0:
            units_value = 1
        top_products.append({
            "sku": sku,
            "name": sku_name(sku),
            "sales": round(float(top_sales_map.get(sku, 0.0)), 2),
            "units": units_value,
            "category": sku_cat(sku),
        })

    categories = []
    total_category_sales = sum(float(sales) for _, sales in top_cat)
    for cat, sales in top_cat:
        categories.append({
            "name": cat,
            "sales": round(float(sales), 2),
            "orders": int(cat_orders.get(cat, 0)),
            "percent": round((float(sales) / total_category_sales) * 100.0, 2) if total_category_sales > 0 else 0.0,
        })

    order_item_count_distribution_out = []
    for item_count in sorted(item_count_distribution.keys()):
        orders_count = int(item_count_distribution[item_count] or 0)
        order_item_count_distribution_out.append({
            "item_count": int(item_count),
            "orders_count": orders_count,
            "percent": round((orders_count / orders_paid) * 100.0, 2) if orders_paid > 0 else 0.0,
        })

    top_order_combinations = []
    sorted_combinations = sorted(
        combination_counts.values(),
        key=lambda x: (
            -int(x.get("orders_count") or 0),
            -float(x.get("sales") or 0.0),
            str(x.get("label") or ""),
        ),
    )[:5]
    for combo in sorted_combinations:
        orders_count = int(combo.get("orders_count") or 0)
        top_order_combinations.append({
            "products": list(combo.get("products") or []),
            "label": str(combo.get("label") or "").strip(),
            "orders_count": orders_count,
            "sales": round(float(combo.get("sales") or 0.0), 2),
            "percent": round((orders_count / orders_paid) * 100.0, 2) if orders_paid > 0 else 0.0,
        })

    return {
        "period": {
            "label": period.label,
            "range_text": _period_range_text_local(period, tenant_tz),
        },
        "kpis": {
            "sales_total": round(float(paid_total_sales), 2),
            "orders_created": int(orders_created),
            "orders_paid": int(orders_paid),
            "orders_unpaid": int(unpaid),
            "avg_ticket": round(float(ticket_avg), 2),
            "avg_units_per_order": round(float(units_avg), 2),
            "conversations_started": int(conversations),
            "conv_conversation_to_order": round(float(conv_to_order), 2),
            "conv_order_to_paid": round(float(order_to_paid), 2),
            "unique_customers": len(paid_customers),
        },
        "sales_by_day": sales_by_day,
        "sales_by_hour": sales_by_hour,
        "top_products": top_products,
        "categories": categories,
        "order_item_count_distribution": order_item_count_distribution_out,
        "top_order_combinations": top_order_combinations,
        "insights": insights,
    }


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
        return (
            "📊 *ESTADÍSTICAS*\n\n"
            f"📅 *Período:* {period.label}\n"
            f"🗓 *Rango:* {_period_range_text_local(period, tenant_tz)}\n\n"
            "No hay datos disponibles para este período."
        )

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
        return (
            "📊 *ESTADÍSTICAS*\n\n"
            f"📅 *Período:* {period.label}\n"
            f"🗓 *Rango:* {_period_range_text_local(period, tenant_tz)}\n\n"
            "La hoja Orders no tiene las columnas requeridas."
        )

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

    lines.append("📊 *ESTADÍSTICAS*")
    lines.append("")
    lines.append(f"🏷 *Período:* {period.label}")
    lines.append(f"🗓 *Rango:* {_period_range_text_local(period, tenant_tz)}")
    lines.append("")

    lines.append("🧠 *RESUMEN EJECUTIVO*")
    if orders_paid == 0:
        lines.append("• No hubo pedidos pagados en este período.")
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

    lines.append("🔹 *KPIs CLAVE*")
    lines.append(f"💳 Pedidos pagados: {orders_paid}")
    lines.append(f"💰 Ventas totales: Bs {paid_total_sales:.2f}")
    lines.append(f"🧾 Ticket promedio: Bs {ticket_avg:.2f}")
    lines.append(f"📦 Unidades promedio: {units_avg:.1f}")
    lines.append(f"👥 Clientes únicos aprox.: {len(paid_customers)}")
    lines.append("")

    lines.append("🔹 *EMBUDO COMERCIAL*")
    lines.append(f"💬 Conversaciones iniciadas: {conversations}")
    lines.append(f"🛒 Pedidos creados: {orders_created}")
    lines.append(f"✅ Pedidos pagados: {orders_paid}")
    lines.append(f"📈 Conversación → pedido: {conv_to_order:.1f}%")
    lines.append(f"📈 Pedido → pagado: {order_to_paid:.1f}%")
    lines.append(f"⏳ Pedidos no pagados: {unpaid}")
    lines.append("")

    lines.append("🔹 *VENTAS POR DÍA*")
    has_weekday_data = False
    for d in weekday_order:
        if d in weekday_stats:
            has_weekday_data = True
            v = weekday_stats[d]
            lines.append(f"{d}: {int(v['orders'])} pedidos | {int(v['units'])} unidades | Bs {float(v['sales']):.2f}")
    if not has_weekday_data:
        lines.append("Sin datos")
    lines.append("")

    lines.append("📈 *MINI GRÁFICO — VENTAS POR DÍA*")
    if has_weekday_data and max_day_sales > 0:
        for d in weekday_order:
            sales = weekday_stats.get(d, {}).get("sales", 0.0)
            if sales > 0:
                lines.append(f"{d:<3} {_bar(float(sales), max_day_sales, 10)} Bs {float(sales):.0f}")
    else:
        lines.append("Sin datos")
    lines.append("")

    lines.append("🔹 *MEJORES HORAS*")
    if top_hours:
        for h, s in top_hours:
            n_orders = hour_stats_orders.get(h, 0)
            lines.append(f"⏰ {h} → Bs {s:.2f} | {n_orders} pedidos")
    else:
        lines.append("Sin datos")
    lines.append("")

    lines.append("🔹 *TOP PRODUCTOS POR VENTAS*")
    if top_sales:
        for i, (sku, s) in enumerate(top_sales, 1):
            share = (s / paid_total_sales * 100.0) if paid_total_sales > 0 else 0.0
            lines.append(f"{i}. {sku_name(sku)} — Bs {s:.2f} ({share:.1f}%)")
    else:
        lines.append("Sin datos")
    lines.append("")

    lines.append("🔹 *TOP PRODUCTOS POR UNIDADES*")
    if top_units:
        for i, (sku, u) in enumerate(top_units, 1):
            lines.append(f"{i}. {sku_name(sku)} — {u}")
    else:
        lines.append("Sin datos")
    lines.append("")

    lines.append("🔹 *CATEGORÍAS*")
    if top_cat:
        for cat, s in top_cat:
            share = (s / paid_total_sales * 100.0) if paid_total_sales > 0 else 0.0
            n_orders = cat_orders.get(cat, 0)
            lines.append(f"{cat} — Bs {s:.2f} ({share:.1f}%) | {n_orders} pedidos")
    else:
        lines.append("Sin datos")
    lines.append("")

    lines.append("💡 *INSIGHTS*")
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


def _empty_stats_summary_v2(period: Period, tenant_tz: str, insights: List[str]) -> Dict[str, Any]:
    return {
        "period": {
            "label": period.label,
            "range_text": _period_range_text_local(period, tenant_tz),
        },
        "kpis": {
            "sales_total": 0.0,
            "orders_created": 0,
            "orders_paid": 0,
            "orders_unpaid": 0,
            "avg_ticket": 0.0,
            "avg_units_per_order": 0.0,
            "conversations_started": 0,
            "conv_conversation_to_order": 0.0,
            "conv_order_to_paid": 0.0,
            "unique_customers": 0,
        },
        "sales_by_day": [],
        "sales_by_hour": [],
        "top_products": [],
        "categories": [],
        "insights": insights,
    }


def load_stats_source_data(orders_sh) -> Dict[str, Any]:
    try:
        ws = orders_sh.worksheet("Orders")
        values = ws.get_all_values()
    except Exception:
        try:
            values = []
            for w in orders_sh.worksheets():
                vals = w.get_all_values()
                if not vals:
                    continue
                hdr_idx = _detect_header_row(vals, required_headers=["order_id", "created_at", "status"], max_scan=30)
                hdr_norm = [normalize(x) for x in (vals[hdr_idx] or [])]
                if "order_id" in hdr_norm and "status" in hdr_norm:
                    values = vals
                    break
            if not values:
                raise Exception("Orders not found")
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"Cannot read Orders: {e}")

    hdr_idx = 0
    headers_norm: List[str] = []
    if values:
        hdr_idx = _detect_header_row(values, required_headers=["order_id", "created_at", "status"], max_scan=30)
        headers_norm = [normalize(h) for h in values[hdr_idx]]

    orders_records: List[Dict[str, Any]] = []
    if values and headers_norm:
        record_keys = [str(h or "").strip().replace(" ", "_") for h in headers_norm]
        for row in values[hdr_idx + 1:]:
            if not any(str(cell or "").strip() for cell in row):
                continue

            rec: Dict[str, Any] = {}
            for col_idx, key in enumerate(record_keys):
                if not key:
                    continue
                rec[key] = row[col_idx] if col_idx < len(row) else ""
            orders_records.append(rec)

    try:
        menu_idx = load_menu_index(orders_sh)
    except Exception:
        menu_idx = {}

    try:
        events_ws = orders_sh.worksheet(EVENTS_SHEET_NAME)
        events_values = events_ws.get_all_values()
    except Exception:
        events_values = []

    events_hdr_idx = 0
    events_headers_norm: List[str] = []
    if events_values:
        events_hdr_idx = _detect_header_row(events_values, required_headers=EVENTS_HEADERS, max_scan=10)
        events_headers_norm = [normalize(h) for h in events_values[events_hdr_idx]]

    return {
        "orders_values": values,
        "orders_hdr_idx": hdr_idx,
        "orders_headers_norm": headers_norm,
        "orders_records": orders_records,
        "menu_idx": menu_idx,
        "events_values": events_values,
        "events_hdr_idx": events_hdr_idx,
        "events_headers_norm": events_headers_norm,
    }


def _count_conversations_started_from_source(source_data: Dict[str, Any], tenant_id: str, start_utc: datetime, end_utc: datetime) -> int:
    values = list(source_data.get("events_values") or [])
    if not values:
        return 0

    hdr_idx = int(source_data.get("events_hdr_idx") or 0)
    headers_norm = list(source_data.get("events_headers_norm") or [])

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


def _compute_stats_summary_from_source(source_data: Dict[str, Any], tenant_id: str, tenant_tz: str, period: Period) -> Dict[str, Any]:
    values = list(source_data.get("orders_values") or [])
    if not values:
        return _empty_stats_summary_v2(period, tenant_tz, ["Aún no hay datos disponibles para este período."])

    hdr_idx = int(source_data.get("orders_hdr_idx") or 0)
    headers_norm = list(source_data.get("orders_headers_norm") or [])
    menu_idx = dict(source_data.get("menu_idx") or {})

    def cidx(name: str) -> Optional[int]:
        k = normalize(name)
        return headers_norm.index(k) if k in headers_norm else None

    i_created = cidx("created_at")
    i_status = cidx("status")
    i_total = cidx("total_amount")
    i_items = cidx("items")
    i_contact = cidx("customer_contact")

    if i_created is None or i_status is None:
        return _empty_stats_summary_v2(period, tenant_tz, ["La hoja Orders no tiene las columnas requeridas."])

    conversations = _count_conversations_started_from_source(source_data, tenant_id, period.start_utc, period.end_utc)

    orders_created = 0
    orders_paid = 0
    paid_total_sales = 0.0
    paid_units_total = 0
    paid_customers: set = set()
    sku_units: Dict[str, int] = {}
    sku_sales: Dict[str, float] = {}
    cat_sales: Dict[str, float] = {}
    cat_orders: Dict[str, int] = {}
    item_count_distribution: Dict[int, int] = {}
    combination_counts: Dict[str, Dict[str, Any]] = {}
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
            qty = _item_qty(it)
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

        if order_units <= 0 and order_sales > 0:
            order_units = 1

        item_count_distribution[order_units] = item_count_distribution.get(order_units, 0) + 1
        combination = _build_order_combination(items, menu_idx)
        if combination is not None:
            combo_key = str(combination.get("key") or "").strip()
            if combo_key:
                current_combo = combination_counts.get(combo_key)
                if current_combo is None:
                    combination_counts[combo_key] = {
                        "products": list(combination.get("products") or []),
                        "label": str(combination.get("label") or "").strip(),
                        "orders_count": 1,
                        "sales": float(order_sales),
                    }
                else:
                    current_combo["orders_count"] = int(current_combo.get("orders_count") or 0) + 1
                    current_combo["sales"] = float(current_combo.get("sales") or 0.0) + float(order_sales)

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
    top_hours = sorted(hour_stats_sales.items(), key=lambda x: x[1], reverse=True)

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

    insights: List[str] = []
    if orders_paid == 0:
        insights.append("Aún no hay suficiente información en este período.")
    else:
        if star_product_name:
            insights.append(f"Producto estrella por ventas: {star_product_name}.")
        if unpaid > 0:
            insights.append(f"Hay {unpaid} pedidos no pagados: conviene revisar fricción de pago y recordatorios.")
        if best_day_sales:
            insights.append(f"El día con mayor venta fue {best_day_sales}.")
        if best_hour:
            insights.append(f"La hora más fuerte fue {best_hour}.")
        if units_avg < 2:
            insights.append("Hay espacio para crecer en unidades por pedido con combos y adicionales.")
        else:
            insights.append("El promedio de unidades por pedido ya muestra una compra relativamente completa.")

    sales_by_day = []
    for d in weekday_order:
        if d not in weekday_stats:
            continue
        v = weekday_stats[d]
        sales_by_day.append({
            "label": d,
            "orders": int(v["orders"]),
            "units": int(v["units"]),
            "sales": round(float(v["sales"]), 2),
        })

    sales_by_hour = []
    for hour_key, sales in top_hours:
        sales_by_hour.append({
            "label": hour_key,
            "orders": int(hour_stats_orders.get(hour_key, 0)),
            "sales": round(float(sales), 2),
        })

    top_products = []
    top_sales_map = {sku: float(sales) for sku, sales in top_sales}
    top_skus: List[str] = []
    for sku, _ in top_sales:
        if sku not in top_skus:
            top_skus.append(sku)
    for sku, _ in top_units:
        if sku not in top_skus:
            top_skus.append(sku)
    for sku in top_skus[:5]:
        units_value = int(sku_units.get(sku, 0) or 0)
        if float(top_sales_map.get(sku, 0.0)) > 0 and units_value <= 0:
            units_value = 1
        top_products.append({
            "sku": sku,
            "name": sku_name(sku),
            "sales": round(float(top_sales_map.get(sku, 0.0)), 2),
            "units": units_value,
            "category": sku_cat(sku),
        })

    categories = []
    total_category_sales = sum(float(sales) for _, sales in top_cat)
    for cat, sales in top_cat:
        categories.append({
            "name": cat,
            "sales": round(float(sales), 2),
            "orders": int(cat_orders.get(cat, 0)),
            "percent": round((float(sales) / total_category_sales) * 100.0, 2) if total_category_sales > 0 else 0.0,
        })

    order_item_count_distribution_out = []
    for item_count in sorted(item_count_distribution.keys()):
        orders_count = int(item_count_distribution[item_count] or 0)
        order_item_count_distribution_out.append({
            "item_count": int(item_count),
            "orders_count": orders_count,
            "percent": round((orders_count / orders_paid) * 100.0, 2) if orders_paid > 0 else 0.0,
        })

    top_order_combinations = []
    sorted_combinations = sorted(
        combination_counts.values(),
        key=lambda x: (
            -int(x.get("orders_count") or 0),
            -float(x.get("sales") or 0.0),
            str(x.get("label") or ""),
        ),
    )[:5]
    for combo in sorted_combinations:
        orders_count = int(combo.get("orders_count") or 0)
        top_order_combinations.append({
            "products": list(combo.get("products") or []),
            "label": str(combo.get("label") or "").strip(),
            "orders_count": orders_count,
            "sales": round(float(combo.get("sales") or 0.0), 2),
            "percent": round((orders_count / orders_paid) * 100.0, 2) if orders_paid > 0 else 0.0,
        })

    return {
        "period": {
            "label": period.label,
            "range_text": _period_range_text_local(period, tenant_tz),
        },
        "kpis": {
            "sales_total": round(float(paid_total_sales), 2),
            "orders_created": int(orders_created),
            "orders_paid": int(orders_paid),
            "orders_unpaid": int(unpaid),
            "avg_ticket": round(float(ticket_avg), 2),
            "avg_units_per_order": round(float(units_avg), 2),
            "conversations_started": int(conversations),
            "conv_conversation_to_order": round(float(conv_to_order), 2),
            "conv_order_to_paid": round(float(order_to_paid), 2),
            "unique_customers": len(paid_customers),
        },
        "sales_by_day": sales_by_day,
        "sales_by_hour": sales_by_hour,
        "top_products": top_products,
        "categories": categories,
        "order_item_count_distribution": order_item_count_distribution_out,
        "top_order_combinations": top_order_combinations,
        "insights": insights,
    }


def build_stats_summary_data(
    orders_sh,
    tenant_id: str,
    tenant_tz: str,
    period: Period,
    *,
    source_data: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    source = source_data or load_stats_source_data(orders_sh)
    return _compute_stats_summary_from_source(source, tenant_id, tenant_tz, period)


def _period_to_local_bounds(period: Period, tenant_tz: str) -> Tuple[datetime, datetime]:
    start_local = _to_local(period.start_utc.replace(tzinfo=ZoneInfo("UTC")), tenant_tz)
    end_local = _to_local(period.end_utc.replace(tzinfo=ZoneInfo("UTC")), tenant_tz)
    return start_local, end_local


def _direction_and_sentiment(current_value: float, reference_value: float) -> Tuple[str, str]:
    if reference_value <= 0:
        return ("flat", "neutral")
    if current_value > reference_value:
        return ("up", "positive")
    if current_value < reference_value:
        return ("down", "negative")
    return ("flat", "neutral")


def _normalize_metric_value(metric_key: str, value: float) -> Any:
    if metric_key in {"sales_total", "avg_ticket"}:
        return round(float(value), 2)
    raw = float(value)
    rounded_2 = round(raw, 2)
    if abs(rounded_2 - round(rounded_2)) < 1e-9:
        return int(round(rounded_2))
    return rounded_2


def _build_comparison(metric_key: str, key: str, label: str, current_value: float, reference_value: float) -> Dict[str, Any]:
    current_norm = _normalize_metric_value(metric_key, current_value)
    reference_norm = _normalize_metric_value(metric_key, reference_value)
    current_num = float(current_norm)
    reference_num = float(reference_norm)
    delta_absolute = current_num - reference_num

    if reference_num > 0:
        delta_percent = round((delta_absolute / reference_num) * 100.0, 2)
        direction, sentiment = _direction_and_sentiment(current_num, reference_num)
    else:
        delta_percent = None
        direction = "flat"
        sentiment = "neutral"

    return {
        "key": key,
        "label": label,
        "current_value": current_norm,
        "reference_value": reference_norm,
        "delta_absolute": _normalize_metric_value(metric_key, delta_absolute),
        "delta_percent": delta_percent,
        "direction": direction,
        "sentiment": sentiment,
    }


def _kpi_value_from_summary(summary: Dict[str, Any], metric_key: str) -> float:
    try:
        return float(((summary.get("kpis") or {}).get(metric_key) or 0.0))
    except Exception:
        return 0.0


def _days_in_range(start_local: datetime, end_local: datetime) -> float:
    seconds = max(0.0, (end_local - start_local).total_seconds())
    return seconds / 86400.0


def _start_of_month_local(dt_local: datetime) -> datetime:
    return datetime(dt_local.year, dt_local.month, 1, 0, 0, 0, tzinfo=dt_local.tzinfo)


def _start_of_year_local(dt_local: datetime) -> datetime:
    return datetime(dt_local.year, 1, 1, 0, 0, 0, tzinfo=dt_local.tzinfo)


def _last_month_start_local(dt_local: datetime) -> datetime:
    year = dt_local.year
    month = dt_local.month - 1
    if month <= 0:
        month = 12
        year -= 1
    return datetime(year, month, 1, 0, 0, 0, tzinfo=dt_local.tzinfo)


def _build_period_from_local(label: str, start_local: datetime, end_local: datetime) -> Period:
    return Period(
        label=label,
        start_utc=_local_datetime_to_utc_naive(start_local),
        end_utc=_local_datetime_to_utc_naive(end_local),
    )


def _next_month_start_local(dt_local: datetime) -> datetime:
    year = dt_local.year
    month = dt_local.month + 1
    if month > 12:
        month = 1
        year += 1
    return datetime(year, month, 1, 0, 0, 0, tzinfo=dt_local.tzinfo)


def _build_previous_year_month_same_progress_periods(start_local: datetime, end_local: datetime) -> List[Period]:
    elapsed = end_local - start_local
    periods: List[Period] = []
    for month in range(1, start_local.month):
        month_start = datetime(start_local.year, month, 1, 0, 0, 0, tzinfo=start_local.tzinfo)
        month_end = _next_month_start_local(month_start)
        same_progress_end = month_start + elapsed
        if same_progress_end > month_end:
            same_progress_end = month_end
        if same_progress_end <= month_start:
            continue
        periods.append(_build_period_from_local(f"{month_start.year}-{month_start.month:02d}", month_start, same_progress_end))
    return periods


def _average_reference_from_summaries(metric_key: str, summaries: List[Dict[str, Any]]) -> float:
    if not summaries:
        return 0.0

    if metric_key == "avg_ticket":
        total_sales = 0.0
        total_orders_paid = 0.0
        for summary in summaries:
            total_sales += _kpi_value_from_summary(summary, "sales_total")
            total_orders_paid += _kpi_value_from_summary(summary, "orders_paid")
        if total_orders_paid <= 0:
            return 0.0
        return total_sales / total_orders_paid

    total = 0.0
    count = 0
    for summary in summaries:
        total += _kpi_value_from_summary(summary, metric_key)
        count += 1
    if count <= 0:
        return 0.0
    return total / count


def build_kpi_comparisons(
    orders_sh,
    tenant_id: str,
    tenant_tz: str,
    period_key: str,
    period: Period,
    current_summary: Dict[str, Any],
    *,
    source_data: Optional[Dict[str, Any]] = None,
) -> Dict[str, List[Dict[str, Any]]]:
    source = source_data or load_stats_source_data(orders_sh)
    start_local, end_local = _period_to_local_bounds(period, tenant_tz)
    elapsed = end_local - start_local
    elapsed_days = _days_in_range(start_local, end_local)

    metric_keys = ["sales_total", "orders_paid", "avg_ticket", "unique_customers"]
    out: Dict[str, List[Dict[str, Any]]] = {k: [] for k in metric_keys}

    comparison_specs: List[Tuple[str, str, Period, bool]] = []

    if period_key == "today":
        comparison_specs.append((
            "previous_day_same_time",
            "vs ayer, misma hora",
            _build_period_from_local("Ayer", start_local - timedelta(days=1), end_local - timedelta(days=1)),
            False,
        ))
        comparison_specs.append((
            "same_weekday_last_week_same_time",
            "vs mismo día semana pasada, misma hora",
            _build_period_from_local("Mismo día semana pasada", start_local - timedelta(days=7), end_local - timedelta(days=7)),
            False,
        ))
        prev_month_start = _last_month_start_local(end_local)
        prev_month_end = _start_of_month_local(end_local)
        comparison_specs.append((
            "previous_month_daily_average_scaled",
            "vs promedio diario mes anterior, hasta esta misma hora",
            _build_period_from_local("Mes anterior", prev_month_start, prev_month_end),
            True,
        ))

    elif period_key == "this_week":
        comparison_specs.append((
            "previous_week_same_progress",
            "vs semana anterior, hasta este mismo día",
            _build_period_from_local("Semana pasada", start_local - timedelta(days=7), end_local - timedelta(days=7)),
            False,
        ))
        prev_month_start = _last_month_start_local(end_local)
        prev_month_end = _start_of_month_local(end_local)
        comparison_specs.append((
            "previous_month_weekly_average_scaled",
            "vs promedio semanal mes anterior, hasta este mismo día",
            _build_period_from_local("Mes anterior", prev_month_start, prev_month_end),
            True,
        ))

    elif period_key == "month_to_date":
        prev_month_start_local = _last_month_start_local(start_local)
        current_month_start_local = _start_of_month_local(start_local)
        prev_month_full_end_local = current_month_start_local
        prev_month_same_progress_end_local = prev_month_start_local + elapsed
        if prev_month_same_progress_end_local > prev_month_full_end_local:
            prev_month_same_progress_end_local = prev_month_full_end_local

        comparison_specs.append((
            "previous_month_same_progress",
            "vs mismo avance mes anterior",
            _build_period_from_local("Mismo avance mes anterior", prev_month_start_local, prev_month_same_progress_end_local),
            False,
        ))

    for comp_key, comp_label, ref_period, scaled in comparison_specs:
        ref_summary = _compute_stats_summary_from_source(source, tenant_id, tenant_tz, ref_period)
        ref_start_local, ref_end_local = _period_to_local_bounds(ref_period, tenant_tz)
        ref_days = max(_days_in_range(ref_start_local, ref_end_local), 0.0)

        for metric_key in metric_keys:
            current_value = _kpi_value_from_summary(current_summary, metric_key)
            if metric_key == "avg_ticket":
                reference_value = _kpi_value_from_summary(ref_summary, metric_key)
            elif scaled and ref_days > 0:
                base_value = _kpi_value_from_summary(ref_summary, metric_key)
                reference_value = (base_value / ref_days) * elapsed_days
            else:
                reference_value = _kpi_value_from_summary(ref_summary, metric_key)

            out[metric_key].append(
                _build_comparison(metric_key, comp_key, comp_label, current_value, reference_value)
            )

    if period_key == "month_to_date":
        previous_month_periods = _build_previous_year_month_same_progress_periods(start_local, end_local)
        previous_month_summaries = [
            _compute_stats_summary_from_source(source, tenant_id, tenant_tz, ref_period)
            for ref_period in previous_month_periods
        ]
        for metric_key in metric_keys:
            current_value = _kpi_value_from_summary(current_summary, metric_key)
            reference_value = _average_reference_from_summaries(metric_key, previous_month_summaries)
            out[metric_key].append(
                _build_comparison(
                    metric_key,
                    "year_to_date_monthly_average_same_progress",
                    "vs promedio mensual del año hasta esta fecha",
                    current_value,
                    reference_value,
                )
            )

    return out
