import argparse
import json
from collections import Counter
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any, Dict, List, Optional, Sequence, Tuple
from zoneinfo import ZoneInfo

import requests

from app.sheets import detect_header_row, get_gspread_client, open_spreadsheet_by_key
from app.tenants import get_tenant_or_404
from app.utils import normalize


ORDERS_WORKSHEET_CANDIDATES = ["ORDERS", "Orders", "orders"]
MENU_WORKSHEET_CANDIDATES = ["Menu", "MENU", "menu"]
SURVEY_RESPONSES_WORKSHEET = "Survey_Responses"

DEFAULT_BASE_URL = "https://proyecto-reservas-idwl.onrender.com"
DEFAULT_TENANT_TZ = "America/La_Paz"
HTTP_TIMEOUT_SECONDS = 60
MONEY_TOLERANCE = 0.01
AVERAGE_TOLERANCE = 0.01

PRESET_DEMO_CORE = [
    {"period": "today", "date": "2026-05-13"},
    {"period": "this_week", "week_start": "2026-05-11"},
    {"period": "month_to_date", "month": "2026-05"},
]


@dataclass
class PeriodSelection:
    period: str
    tenant_tz: str
    date_value: Optional[str] = None
    week_start_value: Optional[str] = None
    month_value: Optional[str] = None


@dataclass
class AuditCase:
    group: str
    label: str
    selection: PeriodSelection


@dataclass
class AuditRunResult:
    case: AuditCase
    passed: bool
    failures: List[Dict[str, Any]]


def _safe_text(value: Any) -> str:
    try:
        return str(value or "").strip()
    except Exception:
        return ""


def _safe_float(value: Any) -> float:
    try:
        return float(str(value).replace(",", "."))
    except Exception:
        return 0.0


def _normalize_header_name(header: Any) -> str:
    return normalize(header).replace(" ", "_")


def _normalize_contact(value: Any) -> str:
    raw = _safe_text(value)
    if not raw:
        return ""
    return "".join(ch for ch in raw if ch.isdigit())


def _normalize_name_key(value: Any) -> str:
    return normalize(_safe_text(value)).replace(" ", "")


def _normalize_tenant_id(value: Any) -> str:
    return normalize(_safe_text(value)).replace(" ", "")


def _money(value: Any) -> float:
    return round(float(_safe_float(value)), 2)


def _parse_day(value: str, label: str) -> date:
    try:
        return datetime.strptime(_safe_text(value), "%Y-%m-%d").date()
    except Exception as exc:
        raise SystemExit(f"Invalid {label}: {value}. Use YYYY-MM-DD.") from exc


def _parse_month(value: str) -> Tuple[int, int]:
    try:
        parsed = datetime.strptime(_safe_text(value), "%Y-%m")
        return parsed.year, parsed.month
    except Exception as exc:
        raise SystemExit(f"Invalid month: {value}. Use YYYY-MM.") from exc


def _parse_iso_dt_any(value: Any) -> Optional[datetime]:
    raw = _safe_text(value)
    if not raw:
        return None

    candidates = [
        raw,
        raw.replace("Z", "+00:00"),
        raw.replace(" ", "T"),
        raw.replace(" ", "T").replace("Z", "+00:00"),
    ]
    for candidate in candidates:
        try:
            dt = datetime.fromisoformat(candidate)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=ZoneInfo("UTC"))
            return dt
        except Exception:
            continue
    return None


def _to_tenant_local_naive(dt: datetime, tenant_tz: str) -> datetime:
    if dt.tzinfo is None:
        return dt
    return dt.astimezone(ZoneInfo(tenant_tz)).replace(tzinfo=None)


def _shift_year_month(year: int, month: int, delta_months: int) -> Tuple[int, int]:
    total = (year * 12 + (month - 1)) + delta_months
    return total // 12, (total % 12) + 1


def _local_day_start(dt_local: datetime) -> datetime:
    return datetime(dt_local.year, dt_local.month, dt_local.day, 0, 0, 0, tzinfo=dt_local.tzinfo)


def _local_week_start(dt_local: datetime) -> datetime:
    return _local_day_start(dt_local) - timedelta(days=dt_local.weekday())


def _local_month_start(dt_local: datetime) -> datetime:
    return datetime(dt_local.year, dt_local.month, 1, 0, 0, 0, tzinfo=dt_local.tzinfo)


def _period_range_text(start_local: datetime, end_local: datetime) -> str:
    start_display = start_local
    end_display = end_local - timedelta(seconds=1)
    return f"{start_display.strftime('%d/%m/%Y')} - {end_display.strftime('%d/%m/%Y')}"


def resolve_period(selection: PeriodSelection) -> Dict[str, Any]:
    tenant_tz = _safe_text(selection.tenant_tz) or DEFAULT_TENANT_TZ
    now_local = datetime.now(ZoneInfo(tenant_tz))
    period = _safe_text(selection.period)

    if period == "today":
        if selection.week_start_value or selection.month_value:
            raise SystemExit("period=today only supports --date.")
        if selection.date_value:
            chosen_day = _parse_day(selection.date_value, "date")
            start_local = datetime(chosen_day.year, chosen_day.month, chosen_day.day, 0, 0, 0, tzinfo=now_local.tzinfo)
            if chosen_day == now_local.date():
                end_local = now_local
                label = "Hoy"
                is_current = True
            else:
                end_local = start_local + timedelta(days=1)
                label = start_local.strftime("%d/%m/%Y")
                is_current = False
        else:
            start_local = _local_day_start(now_local)
            end_local = now_local
            label = "Hoy"
            is_current = True

        return {
            "key": "today",
            "label": label,
            "granularity": "day",
            "is_current": is_current,
            "start_local": start_local,
            "end_local": end_local,
            "start_filter": start_local.replace(tzinfo=None),
            "end_filter": end_local.replace(tzinfo=None),
        }

    if period == "this_week":
        if selection.date_value or selection.month_value:
            raise SystemExit("period=this_week only supports --week-start.")
        current_week_start = _local_week_start(now_local)
        if selection.week_start_value:
            week_day = _parse_day(selection.week_start_value, "week-start")
            if week_day.weekday() != 0:
                raise SystemExit("week-start must be a Monday.")
            start_local = datetime(week_day.year, week_day.month, week_day.day, 0, 0, 0, tzinfo=now_local.tzinfo)
            if start_local == current_week_start:
                end_local = now_local
                label = "Esta semana"
                is_current = True
            else:
                end_local = start_local + timedelta(days=7)
                label = f"Semana del {start_local.strftime('%d/%m/%Y')}"
                is_current = False
        else:
            start_local = current_week_start
            end_local = now_local
            label = "Esta semana"
            is_current = True

        return {
            "key": "this_week",
            "label": label,
            "granularity": "week",
            "is_current": is_current,
            "start_local": start_local,
            "end_local": end_local,
            "start_filter": start_local.replace(tzinfo=None),
            "end_filter": end_local.replace(tzinfo=None),
        }

    if period == "month_to_date":
        if selection.date_value or selection.week_start_value:
            raise SystemExit("period=month_to_date only supports --month.")
        current_month_start = _local_month_start(now_local)
        if selection.month_value:
            year, month = _parse_month(selection.month_value)
            start_local = datetime(year, month, 1, 0, 0, 0, tzinfo=now_local.tzinfo)
            if start_local == current_month_start:
                end_local = now_local
                label = "Mes en curso"
                is_current = True
            else:
                next_year, next_month = _shift_year_month(year, month, 1)
                end_local = datetime(next_year, next_month, 1, 0, 0, 0, tzinfo=now_local.tzinfo)
                label = start_local.strftime("%Y-%m")
                is_current = False
        else:
            start_local = current_month_start
            end_local = now_local
            label = "Mes en curso"
            is_current = True

        return {
            "key": "month_to_date",
            "label": label,
            "granularity": "month",
            "is_current": is_current,
            "start_local": start_local,
            "end_local": end_local,
            "start_filter": start_local.replace(tzinfo=None),
            "end_filter": end_local.replace(tzinfo=None),
        }

    raise SystemExit(f"Unsupported period: {period}")


def _read_worksheet_records(
    spreadsheet,
    worksheet_names: Sequence[str],
    required_headers: Sequence[str],
) -> Tuple[str, List[str], List[Dict[str, Any]]]:
    worksheet = None
    for name in worksheet_names:
        try:
            worksheet = spreadsheet.worksheet(name)
            break
        except Exception:
            continue

    if worksheet is None:
        raise RuntimeError(f"Worksheet not found. Candidates: {', '.join(worksheet_names)}")

    values = worksheet.get_all_values()
    if not values:
        return getattr(worksheet, "title", worksheet_names[0]), [], []

    header_row = detect_header_row(values, required_headers=list(required_headers), max_scan=min(10, len(values)))
    headers_raw = list(values[header_row - 1])
    headers_norm = [_normalize_header_name(header) for header in headers_raw]

    records: List[Dict[str, Any]] = []
    for row in values[header_row:]:
        if not any(_safe_text(cell) for cell in row):
            continue
        record: Dict[str, Any] = {}
        for idx, key in enumerate(headers_norm):
            if not key:
                continue
            record[key] = row[idx] if idx < len(row) else ""
        records.append(record)

    return getattr(worksheet, "title", worksheet_names[0]), headers_raw, records


def _load_menu_index_independent(spreadsheet) -> Dict[str, Dict[str, Any]]:
    try:
        _title, _headers, records = _read_worksheet_records(
            spreadsheet,
            MENU_WORKSHEET_CANDIDATES,
            ["sku", "name", "price", "active", "category"],
        )
    except Exception:
        return {}

    out: Dict[str, Dict[str, Any]] = {}
    for record in records:
        sku = _safe_text(record.get("sku"))
        if not sku:
            continue
        active_raw = normalize(record.get("active"))
        if active_raw not in {"1", "true", "si", "yes", "y", "on"}:
            continue
        out[sku] = {
            "name": _safe_text(record.get("name") or sku),
            "price": _safe_float(record.get("price")),
            "category": _safe_text(record.get("category") or "Otros"),
        }
    return out


def _parse_json_list(raw: Any, warnings: List[str], warning_key: str) -> List[Dict[str, Any]]:
    if isinstance(raw, list):
        return raw
    text = _safe_text(raw)
    if not text:
        return []
    try:
        data = json.loads(text)
        return data if isinstance(data, list) else []
    except Exception:
        warnings.append(warning_key)
        return []


def _item_qty(item: Dict[str, Any]) -> int:
    try:
        qty = int(item.get("qty") or item.get("quantity") or 1)
    except Exception:
        qty = 1
    return max(1, qty)


def _resolve_customer_key(row: Dict[str, Any]) -> Tuple[str, str]:
    contact = _normalize_contact(row.get("customer_contact"))
    if contact:
        return contact, "contact"
    name_key = _normalize_name_key(row.get("customer_name"))
    if name_key:
        return name_key, "name"
    return "", "unidentified"


def _weekday_es(dt_local: datetime) -> str:
    return ["Lun", "Mar", "Mie", "Jue", "Vie", "Sab", "Dom"][dt_local.weekday()]


def _hour_bucket(dt_local: datetime) -> str:
    return dt_local.strftime("%H:00")


def _build_order_combination(items: List[Dict[str, Any]], menu_idx: Dict[str, Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    aggregated: Dict[str, int] = {}
    for item in items:
        sku = _safe_text(item.get("sku"))
        if not sku:
            continue
        name = _safe_text(item.get("name"))
        if not name:
            menu_item = menu_idx.get(sku) or {}
            name = _safe_text(menu_item.get("name") or sku)
        label = name or sku
        if not label:
            continue
        aggregated[label] = aggregated.get(label, 0) + _item_qty(item)

    if len(aggregated) <= 1:
        return None

    parts = []
    products = []
    for label, qty in sorted(aggregated.items(), key=lambda x: x[0].lower()):
        rendered = f"{label} x{qty}" if qty > 1 else label
        parts.append(rendered)
        products.append(rendered)
    joined = " + ".join(parts)
    return {"key": joined, "label": joined, "products": products}


def _resolve_order_items(
    row: Dict[str, Any],
    menu_idx: Dict[str, Dict[str, Any]],
    warnings: List[str],
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    items_snapshot = _parse_json_list(row.get("items_snapshot"), warnings, "items_snapshot_parse_failed")
    if items_snapshot:
        return items_snapshot, items_snapshot
    items = _parse_json_list(row.get("items"), warnings, "items_parse_failed")
    if not items:
        return [], []

    inferred_snapshot: List[Dict[str, Any]] = []
    for item in items:
        sku = _safe_text(item.get("sku"))
        qty = _item_qty(item)
        menu_item = menu_idx.get(sku) or {}
        unit_price = _safe_float(menu_item.get("price"))
        inferred_snapshot.append(
            {
                "sku": sku,
                "name": _safe_text(menu_item.get("name") or sku),
                "qty": qty,
                "unit_price": unit_price,
                "line_total": round(unit_price * qty, 2),
                "category": _safe_text(menu_item.get("category") or "Otros"),
            }
        )
    return items, inferred_snapshot


def _row_belongs_to_tenant(row: Dict[str, Any], tenant_id: str) -> bool:
    normalized_tenant_id = _normalize_tenant_id(tenant_id)
    if "tenant_id" not in row:
        return True
    row_tenant_id = _normalize_tenant_id(row.get("tenant_id"))
    if not row_tenant_id:
        return False
    return row_tenant_id == normalized_tenant_id


def _filter_rows_for_tenant(rows: List[Dict[str, Any]], tenant_id: str) -> List[Dict[str, Any]]:
    return [row for row in rows if _row_belongs_to_tenant(row, tenant_id)]


def _filter_orders_for_period(rows: List[Dict[str, Any]], period_info: Dict[str, Any], tenant_tz: str) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    for row in rows:
        created_dt = _parse_iso_dt_any(row.get("created_at"))
        if not created_dt:
            continue
        created_filter_dt = _to_tenant_local_naive(created_dt, tenant_tz)
        if period_info["start_filter"] <= created_filter_dt < period_info["end_filter"]:
            out.append(row)
    return out


def _resolve_survey_timestamp(row: Dict[str, Any], tenant_tz: str) -> Optional[datetime]:
    # Backend survey analytics filtra por created_at. Aqui mantenemos created_at
    # como fuente principal y solo hacemos fallback si esa columna viene vacia.
    created_dt = _parse_iso_dt_any(row.get("created_at"))
    if created_dt is not None:
        return created_dt

    submitted_dt = _parse_iso_dt_any(row.get("submitted_at"))
    if submitted_dt is not None:
        return submitted_dt

    survey_date = _safe_text(row.get("survey_date"))
    if not survey_date:
        return None
    try:
        day = datetime.strptime(survey_date, "%Y-%m-%d")
        return day.replace(tzinfo=ZoneInfo(tenant_tz))
    except Exception:
        return None


def _filter_survey_for_period(rows: List[Dict[str, Any]], period_info: Dict[str, Any], tenant_tz: str) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    for row in rows:
        survey_dt = _resolve_survey_timestamp(row, tenant_tz)
        if survey_dt is None:
            continue
        filter_dt = _to_tenant_local_naive(survey_dt, tenant_tz)
        if period_info["start_filter"] <= filter_dt < period_info["end_filter"]:
            out.append(row)
    return out


def compute_independent_orders_metrics(
    rows_in_period: List[Dict[str, Any]],
    all_orders_rows: List[Dict[str, Any]],
    menu_idx: Dict[str, Dict[str, Any]],
    tenant_tz: str,
) -> Dict[str, Any]:
    warnings: List[str] = []
    orders_created = 0
    orders_paid = 0
    sales_total = 0.0
    units_total = 0
    paid_customers: set[str] = set()
    customer_stats: Dict[str, Dict[str, Any]] = {}
    sku_units: Dict[str, int] = {}
    sku_sales: Dict[str, float] = {}
    sku_categories: Dict[str, str] = {}
    category_sales: Dict[str, float] = {}
    category_orders: Dict[str, int] = {}
    order_item_count_distribution: Dict[int, int] = {}
    combination_counts: Dict[str, Dict[str, Any]] = {}
    sales_by_day_map: Dict[str, Dict[str, Any]] = {}
    sales_by_hour_map: Dict[str, Dict[str, Any]] = {}
    independent_paid_orders: List[Dict[str, Any]] = []

    for row in rows_in_period:
        created_dt = _parse_iso_dt_any(row.get("created_at"))
        if created_dt is None:
            continue

        orders_created += 1
        status = normalize(row.get("status"))
        if status != normalize("PAID"):
            continue

        orders_paid += 1
        order_sales = _money(row.get("total_amount"))
        sales_total += order_sales
        independent_paid_orders.append(
            {
                "order_id": _safe_text(row.get("order_id")),
                "created_at": _safe_text(row.get("created_at")),
                "tenant_id": _safe_text(row.get("tenant_id")),
                "customer_name": _safe_text(row.get("customer_name")),
                "customer_contact": _safe_text(row.get("customer_contact")),
                "status": _safe_text(row.get("status")),
                "total_amount": round(order_sales, 2),
                "source": _safe_text(row.get("source")),
                "notes": _safe_text(row.get("notes")),
            }
        )

        customer_key, customer_source = _resolve_customer_key(row)
        if customer_source == "contact" and customer_key:
            paid_customers.add(customer_key)

        created_local = created_dt.astimezone(ZoneInfo(tenant_tz))
        items, items_snapshot = _resolve_order_items(row, menu_idx, warnings)
        line_source = items_snapshot or items

        order_units = 0
        categories_in_order: set[str] = set()
        products_counter: Counter[str] = Counter()
        for item in line_source:
            sku = _safe_text(item.get("sku"))
            if not sku:
                continue
            qty = _item_qty(item)
            order_units += qty
            units_total += qty
            sku_units[sku] = sku_units.get(sku, 0) + qty

            if items_snapshot:
                unit_price = _safe_float(item.get("unit_price"))
                category = _safe_text(item.get("category") or "Otros")
                item_name = _safe_text(item.get("name") or sku)
            else:
                menu_item = menu_idx.get(sku) or {}
                unit_price = _safe_float(menu_item.get("price"))
                category = _safe_text(menu_item.get("category") or "Otros")
                item_name = _safe_text(menu_item.get("name") or sku)

            sku_sales[sku] = sku_sales.get(sku, 0.0) + (unit_price * qty)
            sku_categories[sku] = category or "Otros"
            categories_in_order.add(category or "Otros")
            if item_name:
                products_counter[item_name] += qty

        if order_units <= 0 and order_sales > 0:
            order_units = 1
        order_item_count_distribution[order_units] = order_item_count_distribution.get(order_units, 0) + 1

        weekday = _weekday_es(created_local)
        weekday_bucket = sales_by_day_map.setdefault(weekday, {"label": weekday, "orders": 0, "units": 0, "sales": 0.0})
        weekday_bucket["orders"] += 1
        weekday_bucket["units"] += order_units
        weekday_bucket["sales"] = round(weekday_bucket["sales"] + order_sales, 2)

        hour_key = _hour_bucket(created_local)
        hour_bucket = sales_by_hour_map.setdefault(hour_key, {"label": hour_key, "orders": 0, "sales": 0.0})
        hour_bucket["orders"] += 1
        hour_bucket["sales"] = round(hour_bucket["sales"] + order_sales, 2)

        for category in categories_in_order:
            category_orders[category] = category_orders.get(category, 0) + 1

        combination = _build_order_combination(line_source, menu_idx)
        if combination:
            key = _safe_text(combination.get("key"))
            current = combination_counts.get(key)
            if current is None:
                combination_counts[key] = {
                    "products": list(combination.get("products") or []),
                    "label": _safe_text(combination.get("label")),
                    "orders_count": 1,
                    "sales": order_sales,
                }
            else:
                current["orders_count"] += 1
                current["sales"] = round(float(current["sales"]) + order_sales, 2)

        for category, sales in _sales_by_category_from_items(line_source, items_snapshot, menu_idx).items():
            category_sales[category] = category_sales.get(category, 0.0) + sales

        if customer_key:
            customer = customer_stats.get(customer_key)
            if customer is None:
                customer = {
                    "name": _safe_text(row.get("customer_name")),
                    "contact": _safe_text(row.get("customer_contact")),
                    "orders_count": 0,
                    "total_spent": 0.0,
                    "last_purchase_at": created_local,
                    "_products": Counter(),
                }
                customer_stats[customer_key] = customer
            customer["orders_count"] += 1
            customer["total_spent"] = round(float(customer["total_spent"]) + order_sales, 2)
            if created_local > customer["last_purchase_at"]:
                customer["last_purchase_at"] = created_local
            customer["_products"].update(products_counter)

    top_products = []
    top_skus = []
    for sku, _sales in sorted(sku_sales.items(), key=lambda x: x[1], reverse=True):
        if sku not in top_skus:
            top_skus.append(sku)
    for sku, _units in sorted(sku_units.items(), key=lambda x: x[1], reverse=True):
        if sku not in top_skus:
            top_skus.append(sku)
    for sku in top_skus[:5]:
        menu_item = menu_idx.get(sku) or {}
        top_products.append(
            {
                "sku": sku,
                "name": _safe_text(menu_item.get("name") or sku),
                "category": _safe_text(sku_categories.get(sku) or menu_item.get("category") or "Otros"),
                "units": int(sku_units.get(sku, 0) or 0),
                "sales": round(float(sku_sales.get(sku, 0.0)), 2),
            }
        )

    categories = []
    total_category_sales = sum(float(value) for value in category_sales.values())
    for category, sales in sorted(category_sales.items(), key=lambda x: x[1], reverse=True):
        categories.append(
            {
                "name": category,
                "sales": round(float(sales), 2),
                "orders": int(category_orders.get(category, 0)),
                "percent": round((float(sales) / total_category_sales) * 100.0, 2) if total_category_sales > 0 else 0.0,
            }
        )

    sales_by_day = []
    for label in ["Lun", "Mar", "Mie", "Jue", "Vie", "Sab", "Dom"]:
        if label in sales_by_day_map:
            sales_by_day.append(
                {
                    "label": label,
                    "orders": int(sales_by_day_map[label]["orders"]),
                    "units": int(sales_by_day_map[label]["units"]),
                    "sales": round(float(sales_by_day_map[label]["sales"]), 2),
                }
            )

    sales_by_hour = []
    for label, bucket in sorted(sales_by_hour_map.items(), key=lambda x: x[0]):
        sales_by_hour.append(
            {
                "label": label,
                "orders": int(bucket["orders"]),
                "sales": round(float(bucket["sales"]), 2),
            }
        )

    order_item_distribution = []
    for item_count, orders_count in sorted(order_item_count_distribution.items(), key=lambda x: x[0]):
        order_item_distribution.append(
            {
                "item_count": int(item_count),
                "orders_count": int(orders_count),
                "percent": round((orders_count / orders_paid) * 100.0, 2) if orders_paid > 0 else 0.0,
            }
        )

    top_order_combinations = []
    for combo in sorted(
        combination_counts.values(),
        key=lambda x: (-int(x.get("orders_count") or 0), -float(x.get("sales") or 0.0), _safe_text(x.get("label"))),
    )[:5]:
        orders_count = int(combo.get("orders_count") or 0)
        top_order_combinations.append(
            {
                "products": list(combo.get("products") or []),
                "label": _safe_text(combo.get("label")),
                "orders_count": orders_count,
                "sales": round(float(combo.get("sales") or 0.0), 2),
                "percent": round((orders_count / orders_paid) * 100.0, 2) if orders_paid > 0 else 0.0,
            }
        )

    customers_summary = _build_customers_summary(customer_stats)
    customer_order_type_distribution = _build_customer_order_type_distribution(all_orders_rows, rows_in_period)

    avg_ticket = round(sales_total / orders_paid, 2) if orders_paid > 0 else 0.0
    avg_units_per_order = round(units_total / orders_paid, 2) if orders_paid > 0 else 0.0

    return {
        "warnings": sorted(set(warnings)),
        "independent_paid_orders": independent_paid_orders,
        "kpis": {
            "orders_created": int(orders_created),
            "orders_paid": int(orders_paid),
            "orders_unpaid": int(max(0, orders_created - orders_paid)),
            "sales_total": round(sales_total, 2),
            "avg_ticket": avg_ticket,
            "avg_units_per_order": avg_units_per_order,
            "unique_customers": int(len(paid_customers)),
        },
        "sales_by_day": sales_by_day,
        "sales_by_hour": sales_by_hour,
        "top_products": top_products,
        "categories": categories,
        "order_item_count_distribution": order_item_distribution,
        "top_order_combinations": top_order_combinations,
        "customers_summary": customers_summary,
        "customer_order_type_distribution": customer_order_type_distribution,
    }


def _sales_by_category_from_items(
    line_source: List[Dict[str, Any]],
    items_snapshot: List[Dict[str, Any]],
    menu_idx: Dict[str, Dict[str, Any]],
) -> Dict[str, float]:
    out: Dict[str, float] = {}
    for item in line_source:
        sku = _safe_text(item.get("sku"))
        qty = _item_qty(item)
        if items_snapshot:
            category = _safe_text(item.get("category") or "Otros")
            unit_price = _safe_float(item.get("unit_price"))
        else:
            menu_item = menu_idx.get(sku) or {}
            category = _safe_text(menu_item.get("category") or "Otros")
            unit_price = _safe_float(menu_item.get("price"))
        out[category] = out.get(category, 0.0) + (unit_price * qty)
    return out


def _build_customers_summary(customer_stats: Dict[str, Dict[str, Any]]) -> Dict[str, Any]:
    customers = list(customer_stats.values())
    customers.sort(
        key=lambda row: (
            -int(row.get("orders_count") or 0),
            -float(row.get("total_spent") or 0.0),
            -float(row.get("last_purchase_at").timestamp()) if row.get("last_purchase_at") else 0.0,
            _safe_text(row.get("name")).lower(),
        )
    )

    top_customers = []
    for customer in customers[:5]:
        products_counter = customer.get("_products") or Counter()
        top_products_text = ", ".join(name for name, _count in products_counter.most_common(3))
        top_customers.append(
            {
                "name": _safe_text(customer.get("name")),
                "contact": _safe_text(customer.get("contact")),
                "orders_count": int(customer.get("orders_count") or 0),
                "total_spent": round(float(customer.get("total_spent") or 0.0), 2),
                "last_purchase_at": customer.get("last_purchase_at").isoformat() if customer.get("last_purchase_at") else "",
                "products_text": top_products_text,
            }
        )

    repeat_customers = sum(1 for customer in customers if int(customer.get("orders_count") or 0) > 1)
    return {
        "total_customers": int(len(customers)),
        "repeat_customers": int(repeat_customers),
        "top_customers": top_customers,
    }


def _build_customer_order_type_distribution(
    all_orders_rows: List[Dict[str, Any]],
    period_orders_rows: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    paid_rows: List[Tuple[datetime, Dict[str, Any]]] = []
    for row in all_orders_rows:
        if normalize(row.get("status")) != normalize("PAID"):
            continue
        created_dt = _parse_iso_dt_any(row.get("created_at"))
        if created_dt is None:
            continue
        paid_rows.append((created_dt, row))
    paid_rows.sort(key=lambda item: item[0])

    target_order_ids = {
        _safe_text(row.get("order_id"))
        for row in period_orders_rows
        if normalize(row.get("status")) == normalize("PAID")
    }
    seen_contacts: set[str] = set()
    buckets = {"new": 0, "returning": 0, "unidentified": 0}

    for _created_dt, row in paid_rows:
        order_id = _safe_text(row.get("order_id"))
        contact = _normalize_contact(row.get("customer_contact"))

        if not contact:
            bucket = "unidentified"
        elif contact in seen_contacts:
            bucket = "returning"
        else:
            bucket = "new"

        if order_id in target_order_ids:
            buckets[bucket] += 1

        if contact:
            seen_contacts.add(contact)

    total = sum(buckets.values())
    if total <= 0:
        return []

    labels = {
        "new": "Clientes nuevos",
        "returning": "Clientes recurrentes",
        "unidentified": "Sin identificar",
    }
    return [
        {
            "type": bucket,
            "label": labels[bucket],
            "orders_count": int(buckets[bucket]),
            "percent": round((buckets[bucket] / total) * 100.0, 2),
        }
        for bucket in ["new", "returning", "unidentified"]
    ]


def compute_independent_survey_metrics(rows_in_period: List[Dict[str, Any]]) -> Dict[str, Any]:
    total_answers = len(rows_in_period)
    response_ids = {
        _safe_text(row.get("response_id"))
        for row in rows_in_period
        if _safe_text(row.get("response_id"))
    }
    general_stars_values: List[int] = []
    general_hist = {1: 0, 2: 0, 3: 0, 4: 0, 5: 0}
    by_question_map: Dict[str, Dict[str, Any]] = {}

    for row in rows_in_period:
        question_id = _safe_text(row.get("question_id"))
        question_text = _safe_text(row.get("question_text"))
        answer_type = normalize(row.get("answer_type"))
        answer_value = _safe_text(row.get("answer_value") or row.get("stars"))
        question_key = question_id or question_text or f"q_{len(by_question_map) + 1}"
        try:
            question_order = int(_safe_text(row.get("question_order")) or 0)
        except Exception:
            question_order = 0

        bucket = by_question_map.setdefault(
            question_key,
            {
                "question_id": question_id,
                "question_text": question_text,
                "answer_type": answer_type,
                "count": 0,
                "order_hint": question_order,
                "stars_values": [],
                "stars_hist": {1: 0, 2: 0, 3: 0, 4: 0, 5: 0},
            },
        )
        bucket["count"] += 1
        if question_order and (int(bucket["order_hint"]) == 0 or question_order < int(bucket["order_hint"])):
            bucket["order_hint"] = question_order

        if answer_type != normalize("stars"):
            continue
        try:
            stars_value = int(answer_value)
        except Exception:
            continue
        if stars_value < 1 or stars_value > 5:
            continue

        general_stars_values.append(stars_value)
        general_hist[stars_value] += 1
        bucket["stars_values"].append(stars_value)
        bucket["stars_hist"][stars_value] += 1

    by_question = []
    for bucket in sorted(by_question_map.values(), key=lambda row: (int(row.get("order_hint") or 0), _safe_text(row.get("question_id")))):
        stars_values = list(bucket.get("stars_values") or [])
        by_question.append(
            {
                "question_id": _safe_text(bucket.get("question_id")),
                "question_text": _safe_text(bucket.get("question_text")),
                "count": int(bucket.get("count") or 0),
                "stars_avg": round(sum(stars_values) / len(stars_values), 2) if stars_values else 0.0,
                "stars_hist": dict(bucket.get("stars_hist") or {}),
            }
        )

    return {
        "total_answers": int(total_answers),
        "total_unique_responses": int(len(response_ids)),
        "general_stars_avg": round(sum(general_stars_values) / len(general_stars_values), 2) if general_stars_values else 0.0,
        "general_stars_hist": general_hist,
        "by_question": by_question,
    }


def _sanitize_base_url(base_url: str) -> str:
    clean = _safe_text(base_url)
    return clean.rstrip("/") if clean else DEFAULT_BASE_URL


def _build_http_params(tenant_id: str, token: str, selection: PeriodSelection) -> Dict[str, str]:
    params = {"tenant_id": tenant_id, "period": selection.period, "token": token}
    if selection.date_value:
        params["date"] = selection.date_value
    if selection.week_start_value:
        params["week_start"] = selection.week_start_value
    if selection.month_value:
        params["month"] = selection.month_value
    return params


def _build_redacted_http_params(tenant_id: str, selection: PeriodSelection) -> Dict[str, str]:
    return _build_http_params(tenant_id, "<redacted>", selection)


def _http_get_json(url: str, params: Dict[str, str]) -> Dict[str, Any]:
    response = requests.get(url, params=params, timeout=HTTP_TIMEOUT_SECONDS)
    response.raise_for_status()
    data = response.json()
    if not isinstance(data, dict):
        raise RuntimeError(f"Unexpected JSON payload from {url}")
    return data


def _approx_equal(left: Any, right: Any, tolerance: float) -> bool:
    return abs(_safe_float(left) - _safe_float(right)) <= tolerance


def _fmt_money(value: Any) -> str:
    return f"{_safe_float(value):.2f}"


def _fmt_value(value: Any) -> str:
    if isinstance(value, float):
        return f"{value:.2f}"
    return str(value)


def _compare_kpis(independent: Dict[str, Any], summary_payload: Dict[str, Any]) -> List[Dict[str, Any]]:
    failures: List[Dict[str, Any]] = []
    expected_kpis = independent.get("kpis") or {}
    summary_kpis = (summary_payload.get("kpis") or {})

    for field in ["orders_created", "orders_paid", "orders_unpaid", "unique_customers"]:
        expected = int(expected_kpis.get(field) or 0)
        actual = int(summary_kpis.get(field) or 0)
        if expected != actual:
            failures.append(
                {
                    "block": "kpis",
                    "field": field,
                    "independent": expected,
                    "summary": actual,
                    "orders_detail": "",
                    "delta": actual - expected,
                }
            )

    for field in ["sales_total", "avg_ticket", "avg_units_per_order"]:
        expected = _safe_float(expected_kpis.get(field) or 0.0)
        actual = _safe_float(summary_kpis.get(field) or 0.0)
        if not _approx_equal(expected, actual, AVERAGE_TOLERANCE):
            failures.append(
                {
                    "block": "kpis",
                    "field": field,
                    "independent": _fmt_money(expected),
                    "summary": _fmt_money(actual),
                    "orders_detail": "",
                    "delta": _fmt_money(actual - expected),
                }
            )
    return failures


def _compare_survey(independent: Dict[str, Any], summary_payload: Dict[str, Any]) -> List[Dict[str, Any]]:
    failures: List[Dict[str, Any]] = []
    summary_survey = summary_payload.get("survey_summary") or {}

    for field in ["total_answers", "total_unique_responses"]:
        expected = int(independent.get(field) or 0)
        actual = int(summary_survey.get(field) or 0)
        if expected != actual:
            failures.append(
                {
                    "block": "survey",
                    "field": field,
                    "independent": expected,
                    "summary": actual,
                    "orders_detail": "",
                    "delta": actual - expected,
                }
            )

    expected_avg = _safe_float(independent.get("general_stars_avg") or 0.0)
    actual_avg = _safe_float(summary_survey.get("general_stars_avg") or 0.0)
    if not _approx_equal(expected_avg, actual_avg, AVERAGE_TOLERANCE):
        failures.append(
            {
                "block": "survey",
                "field": "general_stars_avg",
                "independent": _fmt_money(expected_avg),
                "summary": _fmt_money(actual_avg),
                "orders_detail": "",
                "delta": _fmt_money(actual_avg - expected_avg),
            }
        )

    expected_hist = independent.get("general_stars_hist") or {}
    actual_hist = summary_survey.get("general_stars_hist") or {}
    for star in [1, 2, 3, 4, 5]:
        expected = int(expected_hist.get(star) or expected_hist.get(str(star)) or 0)
        actual = int(actual_hist.get(star) or actual_hist.get(str(star)) or 0)
        if expected != actual:
            failures.append(
                {
                    "block": "survey",
                    "field": f"general_stars_hist[{star}]",
                    "independent": expected,
                    "summary": actual,
                    "orders_detail": "",
                    "delta": actual - expected,
                }
            )
    return failures


def _compare_orders_detail(
    independent: Dict[str, Any],
    summary_payload: Dict[str, Any],
    detail_payload: Dict[str, Any],
) -> Tuple[List[Dict[str, Any]], Dict[str, List[Dict[str, Any]]]]:
    failures: List[Dict[str, Any]] = []
    diagnostics: Dict[str, List[Dict[str, Any]]] = {
        "extra_in_independent": [],
        "missing_in_independent": [],
    }
    summary_kpis = summary_payload.get("kpis") or {}
    independent_kpis = independent.get("kpis") or {}
    detail_orders = int(detail_payload.get("total_orders") or 0)
    detail_sales = _safe_float(detail_payload.get("total_paid_amount") or 0.0)

    summary_orders_paid = int(summary_kpis.get("orders_paid") or 0)
    summary_sales_total = _safe_float(summary_kpis.get("sales_total") or 0.0)
    independent_orders_paid = int(independent_kpis.get("orders_paid") or 0)
    independent_sales_total = _safe_float(independent_kpis.get("sales_total") or 0.0)

    if summary_orders_paid != detail_orders:
        failures.append(
            {
                "block": "orders_detail",
                "field": "summary.orders_paid vs detail.total_orders",
                "independent": summary_orders_paid,
                "summary": summary_orders_paid,
                "orders_detail": detail_orders,
                "delta": detail_orders - summary_orders_paid,
            }
        )
    if not _approx_equal(summary_sales_total, detail_sales, MONEY_TOLERANCE):
        failures.append(
            {
                "block": "orders_detail",
                "field": "summary.sales_total vs detail.total_paid_amount",
                "independent": _fmt_money(summary_sales_total),
                "summary": _fmt_money(summary_sales_total),
                "orders_detail": _fmt_money(detail_sales),
                "delta": _fmt_money(detail_sales - summary_sales_total),
            }
        )
    if independent_orders_paid != detail_orders:
        failures.append(
            {
                "block": "orders_detail",
                "field": "independent.orders_paid vs detail.total_orders",
                "independent": independent_orders_paid,
                "summary": summary_orders_paid,
                "orders_detail": detail_orders,
                "delta": detail_orders - independent_orders_paid,
            }
        )
    if not _approx_equal(independent_sales_total, detail_sales, MONEY_TOLERANCE):
        failures.append(
            {
                "block": "orders_detail",
                "field": "independent.sales_total vs detail.total_paid_amount",
                "independent": _fmt_money(independent_sales_total),
                "summary": _fmt_money(summary_sales_total),
                "orders_detail": _fmt_money(detail_sales),
                "delta": _fmt_money(detail_sales - independent_sales_total),
            }
        )

    independent_paid_orders = list(independent.get("independent_paid_orders") or [])
    independent_by_id = {
        _safe_text(order.get("order_id")): order
        for order in independent_paid_orders
        if _safe_text(order.get("order_id"))
    }
    detail_orders_list = list(detail_payload.get("orders") or [])
    detail_by_id = {
        _safe_text(order.get("order_id")): order
        for order in detail_orders_list
        if _safe_text(order.get("order_id"))
    }

    independent_order_ids = set(independent_by_id.keys())
    detail_order_ids = set(detail_by_id.keys())

    extra_ids = sorted(independent_order_ids - detail_order_ids)
    missing_ids = sorted(detail_order_ids - independent_order_ids)

    for order_id in extra_ids:
        order = independent_by_id.get(order_id) or {}
        diagnostics["extra_in_independent"].append(
            {
                "order_id": order_id,
                "created_at": _safe_text(order.get("created_at")),
                "tenant_id": _safe_text(order.get("tenant_id")),
                "status": _safe_text(order.get("status")),
                "total_amount": _fmt_money(order.get("total_amount")),
                "source": _safe_text(order.get("source")),
                "notes": _safe_text(order.get("notes")),
            }
        )

    for order_id in missing_ids:
        order = detail_by_id.get(order_id) or {}
        diagnostics["missing_in_independent"].append(
            {
                "order_id": order_id,
                "paid_at": _safe_text(order.get("paid_at")),
                "paid_amount": _fmt_money(order.get("paid_amount")),
                "customer_name": _safe_text(order.get("customer_name")),
                "items_summary": _safe_text(order.get("items_summary")),
            }
        )

    return failures, diagnostics


def _print_orders_detail_diagnostics(diagnostics: Dict[str, List[Dict[str, Any]]]) -> None:
    extra_rows = list(diagnostics.get("extra_in_independent") or [])
    missing_rows = list(diagnostics.get("missing_in_independent") or [])

    if extra_rows:
        print("EXTRA IN INDEPENDENT:")
        print("order_id | created_at | tenant_id | status | total_amount | source | notes")
        for row in extra_rows:
            print(
                f"{row['order_id']} | {row['created_at']} | {row['tenant_id']} | "
                f"{row['status']} | {row['total_amount']} | {row['source']} | {row['notes']}"
            )

    if missing_rows:
        print("MISSING IN INDEPENDENT:")
        print("order_id | paid_at | paid_amount | customer_name | items_summary")
        for row in missing_rows:
            print(
                f"{row['order_id']} | {row['paid_at']} | {row['paid_amount']} | "
                f"{row['customer_name']} | {row['items_summary']}"
            )


def _print_diff_table(failures: List[Dict[str, Any]]) -> None:
    print("Differences:")
    print("period_group | period_label | block | field | independent | summary | orders_detail | delta")
    for failure in failures:
        print(
            f"{failure['period_group']} | {failure['period_label']} | {failure['block']} | {failure['field']} | "
            f"{failure['independent']} | {failure['summary']} | {failure['orders_detail']} | {failure['delta']}"
        )


def _print_key_values(independent: Dict[str, Any], summary_payload: Dict[str, Any], detail_payload: Dict[str, Any]) -> None:
    independent_kpis = independent.get("kpis") or {}
    summary_kpis = summary_payload.get("kpis") or {}
    print("Key values:")
    print(
        f"- sales_total: independent={_fmt_money(independent_kpis.get('sales_total'))} | "
        f"summary={_fmt_money(summary_kpis.get('sales_total'))} | "
        f"orders_detail={_fmt_money(detail_payload.get('total_paid_amount'))}"
    )
    print(
        f"- orders_paid: independent={int(independent_kpis.get('orders_paid') or 0)} | "
        f"summary={int(summary_kpis.get('orders_paid') or 0)} | "
        f"orders_detail={int(detail_payload.get('total_orders') or 0)}"
    )


def _print_diagnostics(independent_orders: Dict[str, Any], independent_survey: Dict[str, Any]) -> None:
    warnings = list(independent_orders.get("warnings") or [])
    if warnings:
        print(f"Diagnostics: warnings={', '.join(sorted(set(warnings)))}")
    else:
        print("Diagnostics: warnings=none")

    print(
        f"Diagnostics: top_products={len(independent_orders.get('top_products') or [])} | "
        f"categories={len(independent_orders.get('categories') or [])} | "
        f"survey_questions={len(independent_survey.get('by_question') or [])}"
    )


def _fetch_backend_payloads(base_url: str, tenant_id: str, token: str, selection: PeriodSelection) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    params = _build_http_params(tenant_id, token, selection)
    summary_payload = _http_get_json(f"{base_url}/admin/dashboard/summary", params)
    detail_payload = _http_get_json(f"{base_url}/admin/dashboard/orders-detail", params)
    return summary_payload, detail_payload


def run_audit_case(
    *,
    index: int,
    total_cases: int,
    case: AuditCase,
    tenant_id: str,
    token: str,
    base_url: str,
    all_orders_rows: List[Dict[str, Any]],
    all_survey_rows: List[Dict[str, Any]],
    menu_idx: Dict[str, Dict[str, Any]],
) -> AuditRunResult:
    period_info = resolve_period(case.selection)
    tenant_orders_rows = _filter_rows_for_tenant(all_orders_rows, tenant_id)
    tenant_survey_rows = _filter_rows_for_tenant(all_survey_rows, tenant_id)
    orders_in_period = _filter_orders_for_period(tenant_orders_rows, period_info, case.selection.tenant_tz)
    survey_in_period = _filter_survey_for_period(tenant_survey_rows, period_info, case.selection.tenant_tz)

    independent_orders = compute_independent_orders_metrics(
        rows_in_period=orders_in_period,
        all_orders_rows=tenant_orders_rows,
        menu_idx=menu_idx,
        tenant_tz=case.selection.tenant_tz,
    )
    independent_survey = compute_independent_survey_metrics(survey_in_period)
    summary_payload, detail_payload = _fetch_backend_payloads(base_url, tenant_id, token, case.selection)

    kpi_failures = _compare_kpis(independent_orders, summary_payload)
    survey_failures = _compare_survey(independent_survey, summary_payload)
    orders_detail_failures, orders_detail_diagnostics = _compare_orders_detail(independent_orders, summary_payload, detail_payload)
    all_failures = kpi_failures + survey_failures + orders_detail_failures

    print()
    print(f"[{index}/{total_cases}] {case.label}")
    print(f"Range: {_period_range_text(period_info['start_local'], period_info['end_local'])}")
    print(f"Summary endpoint: {base_url}/admin/dashboard/summary")
    print(f"Orders-detail endpoint: {base_url}/admin/dashboard/orders-detail")
    print(f"Params: {json.dumps(_build_redacted_http_params(tenant_id, case.selection), ensure_ascii=False)}")
    print(f"KPI CHECK: {'PASS' if not kpi_failures else 'FAIL'}")
    print(f"SURVEY CHECK: {'PASS' if not survey_failures else 'FAIL'}")
    print(f"ORDERS DETAIL CHECK: {'PASS' if not orders_detail_failures else 'FAIL'}")
    print()
    _print_key_values(independent_orders, summary_payload, detail_payload)
    _print_diagnostics(independent_orders, independent_survey)

    enriched_failures: List[Dict[str, Any]] = []
    for failure in all_failures:
        item = dict(failure)
        item["period_group"] = case.group
        item["period_label"] = case.label
        enriched_failures.append(item)

    if enriched_failures:
        print()
        _print_diff_table(enriched_failures)
        if orders_detail_diagnostics.get("extra_in_independent") or orders_detail_diagnostics.get("missing_in_independent"):
            print()
            _print_orders_detail_diagnostics(orders_detail_diagnostics)
        return AuditRunResult(case=case, passed=False, failures=enriched_failures)
    return AuditRunResult(case=case, passed=True, failures=[])


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Independent audit script for dashboard stats.")
    parser.add_argument("--tenant-id", required=True)
    parser.add_argument("--token", required=True)
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL)
    parser.add_argument("--preset", choices=["demo-core", "full-history"])
    parser.add_argument("--period", choices=["today", "this_week", "month_to_date"])
    parser.add_argument("--date")
    parser.add_argument("--week-start")
    parser.add_argument("--month")
    parser.add_argument("--stop-on-fail", action="store_true")
    return parser


def _validate_individual_args(args: argparse.Namespace) -> None:
    if not args.period:
        raise SystemExit("You must provide --preset demo-core or an individual --period.")

    if args.period == "today":
        if args.week_start or args.month:
            raise SystemExit("period=today only supports --date.")
        return

    if args.period == "this_week":
        if args.date or args.month:
            raise SystemExit("period=this_week only supports --week-start.")
        if not args.week_start:
            raise SystemExit("period=this_week requires --week-start.")
        return

    if args.period == "month_to_date":
        if args.date or args.week_start:
            raise SystemExit("period=month_to_date only supports --month.")
        if not args.month:
            raise SystemExit("period=month_to_date requires --month.")
        return


def _build_cases(args: argparse.Namespace, tenant_tz: str) -> List[AuditCase]:
    if args.preset == "demo-core":
        return [
            AuditCase(
                group="Daily" if preset["period"] == "today" else "Weekly" if preset["period"] == "this_week" else "Monthly",
                label=f"{preset['period']} " + ("date=" + preset["date"] if "date" in preset else "week_start=" + preset["week_start"] if "week_start" in preset else "month=" + preset["month"]),
                selection=PeriodSelection(
                    period=str(preset["period"]),
                    tenant_tz=tenant_tz,
                    date_value=preset.get("date"),
                    week_start_value=preset.get("week_start"),
                    month_value=preset.get("month"),
                ),
            )
            for preset in PRESET_DEMO_CORE
        ]

    if args.preset == "full-history":
        now_local = datetime.now(ZoneInfo(tenant_tz))
        cases: List[AuditCase] = []

        for offset in range(29, -1, -1):
            target_day = (now_local - timedelta(days=offset)).date()
            cases.append(
                AuditCase(
                    group="Daily",
                    label=f"today date={target_day.isoformat()}",
                    selection=PeriodSelection(
                        period="today",
                        tenant_tz=tenant_tz,
                        date_value=target_day.isoformat(),
                    ),
                )
            )

        current_week_start = _local_week_start(now_local).date()
        for offset in range(11, -1, -1):
            target_week_start = current_week_start - timedelta(days=7 * offset)
            cases.append(
                AuditCase(
                    group="Weekly",
                    label=f"this_week week_start={target_week_start.isoformat()}",
                    selection=PeriodSelection(
                        period="this_week",
                        tenant_tz=tenant_tz,
                        week_start_value=target_week_start.isoformat(),
                    ),
                )
            )

        for offset in range(5, -1, -1):
            year, month = _shift_year_month(now_local.year, now_local.month, -offset)
            month_value = f"{year:04d}-{month:02d}"
            cases.append(
                AuditCase(
                    group="Monthly",
                    label=f"month_to_date month={month_value}",
                    selection=PeriodSelection(
                        period="month_to_date",
                        tenant_tz=tenant_tz,
                        month_value=month_value,
                    ),
                )
            )
        return cases

    _validate_individual_args(args)
    return [
        AuditCase(
            group="Daily" if args.period == "today" else "Weekly" if args.period == "this_week" else "Monthly",
            label=(
                f"{args.period} "
                + (f"date={args.date}" if args.date else f"week_start={args.week_start}" if args.week_start else f"month={args.month}" if args.month else "current")
            ),
            selection=PeriodSelection(
                period=str(args.period),
                tenant_tz=tenant_tz,
                date_value=_safe_text(args.date) or None,
                week_start_value=_safe_text(args.week_start) or None,
                month_value=_safe_text(args.month) or None,
            ),
        )
    ]


def _load_audit_inputs(tenant_id: str) -> Tuple[str, List[Dict[str, Any]], List[Dict[str, Any]], Dict[str, Dict[str, Any]]]:
    gc = get_gspread_client()
    tenant = get_tenant_or_404(tenant_id, gc=gc)
    tenant_tz = _safe_text(tenant.get("timezone")) or DEFAULT_TENANT_TZ
    spreadsheet_id = _safe_text(tenant.get("orders_sheet_id"))
    if not spreadsheet_id:
        raise SystemExit(f"Tenant '{tenant_id}' is missing orders_sheet_id.")

    spreadsheet = open_spreadsheet_by_key(gc, spreadsheet_id)
    _orders_title, _orders_headers, orders_rows = _read_worksheet_records(
        spreadsheet,
        ORDERS_WORKSHEET_CANDIDATES,
        ["order_id", "created_at", "customer_contact", "items", "status", "total_amount"],
    )
    _survey_title, _survey_headers, survey_rows = _read_worksheet_records(
        spreadsheet,
        [SURVEY_RESPONSES_WORKSHEET],
        ["response_id", "created_at", "question_id", "question_text", "answer_type", "answer_value"],
    )
    menu_idx = _load_menu_index_independent(spreadsheet)
    return tenant_tz, orders_rows, survey_rows, menu_idx


def main() -> int:
    parser = _build_parser()
    args = parser.parse_args()

    tenant_id = _safe_text(args.tenant_id)
    token = _safe_text(args.token)
    base_url = _sanitize_base_url(args.base_url)

    if not tenant_id:
        raise SystemExit("tenant-id is required.")
    if not token:
        raise SystemExit("token is required.")

    tenant_tz, orders_rows, survey_rows, menu_idx = _load_audit_inputs(tenant_id)
    cases = _build_cases(args, tenant_tz)

    print("AUDIT DASHBOARD STATS")
    print(f"Tenant: {tenant_id}")
    print(f"Base URL: {base_url}")
    print(f"Preset: {args.preset or 'none'}")

    if args.preset == "full-history":
        daily_count = sum(1 for case in cases if case.group == "Daily")
        weekly_count = sum(1 for case in cases if case.group == "Weekly")
        monthly_count = sum(1 for case in cases if case.group == "Monthly")
        print(f"Daily cases: {daily_count}")
        print(f"Weekly cases: {weekly_count}")
        print(f"Monthly cases: {monthly_count}")
        print(f"Total cases: {len(cases)}")

    failures = 0
    total_cases = len(cases)
    current_group = ""
    group_totals: Dict[str, int] = {"Daily": 0, "Weekly": 0, "Monthly": 0}
    group_passes: Dict[str, int] = {"Daily": 0, "Weekly": 0, "Monthly": 0}
    all_failures: List[Dict[str, Any]] = []

    for index, case in enumerate(cases, start=1):
        if case.group != current_group:
            current_group = case.group
            print()
            print(f"{case.group} audit started")

        result = run_audit_case(
            index=index,
            total_cases=total_cases,
            case=case,
            tenant_id=tenant_id,
            token=token,
            base_url=base_url,
            all_orders_rows=orders_rows,
            all_survey_rows=survey_rows,
            menu_idx=menu_idx,
        )
        group_totals[case.group] = group_totals.get(case.group, 0) + 1
        if result.passed:
            group_passes[case.group] = group_passes.get(case.group, 0) + 1
        else:
            failures += 1
            all_failures.extend(result.failures)
            if args.stop_on_fail:
                print()
                print("FINAL RESULT: AUDIT FAIL")
                return 1

    print()
    if args.preset == "full-history":
        total_passes = sum(group_passes.values())
        daily_status = "PASS" if group_passes.get("Daily", 0) == group_totals.get("Daily", 0) else "FAIL"
        weekly_status = "PASS" if group_passes.get("Weekly", 0) == group_totals.get("Weekly", 0) else "FAIL"
        monthly_status = "PASS" if group_passes.get("Monthly", 0) == group_totals.get("Monthly", 0) else "FAIL"
        total_status = "PASS" if total_passes == len(cases) else "FAIL"
        print("FULL-HISTORY SUMMARY")
        print(f"Daily: {group_passes.get('Daily', 0)}/{group_totals.get('Daily', 0)} {daily_status}")
        print(f"Weekly: {group_passes.get('Weekly', 0)}/{group_totals.get('Weekly', 0)} {weekly_status}")
        print(f"Monthly: {group_passes.get('Monthly', 0)}/{group_totals.get('Monthly', 0)} {monthly_status}")
        print(f"Total: {total_passes}/{len(cases)} {total_status}")

    if failures > 0:
        if all_failures:
            _print_diff_table(all_failures)
        print("FINAL RESULT: AUDIT FAIL")
        return 1

    print("FINAL RESULT: AUDIT PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
