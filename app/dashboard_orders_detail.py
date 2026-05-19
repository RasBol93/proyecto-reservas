import json
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple

from zoneinfo import ZoneInfo

from app.menu import load_menu_index
from app.sheets import detect_header_row
from app.stats import resolve_period
from app.utils import normalize, log_event


ORDERS_WORKSHEET_CANDIDATES = ["ORDERS", "Orders", "orders"]
ALLOWED_PERIOD_KEYS = {"today", "this_week", "month_to_date"}


def _normalize_header_name(h: Any) -> str:
    s = str(h or "").strip()
    return normalize(s).replace(" ", "_")


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
    return dt.astimezone(ZoneInfo(tenant_tz))


def _fmt_local_date(dt: datetime) -> str:
    return dt.strftime("%d/%m/%Y")


def _period_range_text(period, tenant_tz: str) -> str:
    start_local = _to_local(period.start_utc.replace(tzinfo=ZoneInfo("UTC")), tenant_tz)
    end_local = _to_local((period.end_utc - timedelta(seconds=1)).replace(tzinfo=ZoneInfo("UTC")), tenant_tz)
    return f"{_fmt_local_date(start_local)} – {_fmt_local_date(end_local)}"


def _safe_float(v: Any) -> float:
    try:
        return float(str(v).replace(",", "."))
    except Exception:
        return 0.0


def _money_value(value: Any) -> Any:
    amount = round(_safe_float(value), 2)
    if abs(amount - round(amount)) < 0.000001:
        return int(round(amount))
    return amount


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
        except Exception:
            return []

    try:
        values = ws.get_all_values()
    except Exception as e:
        log_event("dashboard_orders_detail_get_all_values_failed", error=str(e))
        return []

    if not values:
        return []

    try:
        header_row_1based = detect_header_row(
            values,
            required_headers=["created_at", "customer_name", "customer_contact", "status", "total_amount"],
            max_scan=min(10, len(values)),
        )
    except Exception as e:
        log_event("dashboard_orders_detail_detect_header_failed", error=str(e))
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

    return records


def _parse_items_field(raw: Any) -> List[Dict[str, Any]]:
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


def _item_qty(it: Dict[str, Any]) -> int:
    try:
        qty = int(it.get("qty") or 1)
    except Exception:
        qty = 1
    return max(1, qty)


def _build_items_summary(order: Dict[str, Any], menu_idx: Dict[str, Any]) -> str:
    items_snapshot = _parse_items_field(order.get("items_snapshot"))
    items = items_snapshot or _parse_items_field(order.get("items"))

    aggregated: Dict[str, int] = {}
    for it in items:
        sku = str(it.get("sku") or "").strip()
        name = str(it.get("name") or "").strip()
        if not name and sku:
            menu_item = menu_idx.get(sku) or {}
            name = str(menu_item.get("name") or "").strip()
        label = name or sku
        if not label:
            continue

        qty = _item_qty(it)
        aggregated[label] = aggregated.get(label, 0) + qty

    if not aggregated:
        return ""

    parts: List[str] = []
    for name, qty in aggregated.items():
        if qty > 1:
            parts.append(f"{name} x{qty}")
        else:
            parts.append(name)
    return ", ".join(parts)


def build_dashboard_orders_detail(
    orders_sh,
    tenant_id: str,
    tenant_tz: str,
    period_key: str = "today",
    selected_date: Optional[str] = None,
    selected_week_start: Optional[str] = None,
    selected_month: Optional[str] = None,
) -> Dict[str, Any]:
    clean_period_key = str(period_key or "today").strip() or "today"
    if clean_period_key not in ALLOWED_PERIOD_KEYS:
        raise ValueError("Unsupported period")

    period = resolve_period(
        tenant_tz,
        clean_period_key,
        selected_date=selected_date,
        selected_week_start=selected_week_start,
        selected_month=selected_month,
    )
    rows = _load_orders_records(orders_sh)

    try:
        menu_idx = load_menu_index(orders_sh, force=False)
    except Exception:
        menu_idx = {}

    orders: List[Dict[str, Any]] = []
    total_paid_amount = 0.0
    currency = "BOB"

    for row in rows:
        created_dt = _parse_iso_dt_any(row.get("created_at"))
        if not created_dt:
            continue

        dt_utc = created_dt.astimezone(ZoneInfo("UTC")).replace(tzinfo=None)
        if not (period.start_utc <= dt_utc < period.end_utc):
            continue

        status = str(row.get("status") or "").strip()
        if normalize(status) != normalize("PAID"):
            continue

        paid_amount = round(_safe_float(row.get("total_amount") or 0), 2)
        total_paid_amount += paid_amount

        paid_dt = _parse_iso_dt_any(row.get("payment_confirmed_at")) or created_dt
        paid_local = _to_local(paid_dt, tenant_tz)

        row_currency = str(row.get("currency") or "").strip()
        if row_currency:
            currency = row_currency

        orders.append({
            "order_id": str(row.get("order_id") or "").strip(),
            "paid_at": paid_local.isoformat(),
            "date_label": paid_local.strftime("%d/%m/%Y"),
            "time_label": paid_local.strftime("%H:%M"),
            "customer_name": str(row.get("customer_name") or "").strip(),
            "customer_contact": str(row.get("customer_contact") or "").strip(),
            "items_summary": _build_items_summary(row, menu_idx),
            "paid_amount": _money_value(paid_amount),
            "currency": row_currency or currency or "BOB",
            "_sort_dt": paid_local,
        })

    orders.sort(key=lambda x: x.get("_sort_dt") or datetime.min.replace(tzinfo=ZoneInfo(tenant_tz)), reverse=True)
    for order in orders:
        order.pop("_sort_dt", None)

    return {
        "ok": True,
        "tenant_id": str(tenant_id or "").strip(),
        "period": {
            "key": clean_period_key,
            "label": str(period.label or "").strip(),
            "range_text": _period_range_text(period, tenant_tz),
        },
        "orders": orders,
        "total_orders": len(orders),
        "total_paid_amount": _money_value(total_paid_amount),
    }
