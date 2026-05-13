from datetime import datetime
from typing import Any, Dict, List, Optional

from app.admin_settings import get_admin_setting_value, load_admin_settings
from app.consumer_db import build_dashboard_customer_metrics
from app.stats import (
    resolve_period,
    load_stats_source_data,
    build_stats_summary_data,
    build_kpi_comparisons,
)
from app.survey_analytics import build_survey_analytics


def _utc_iso() -> str:
    return datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")


def _money_value(value: Any) -> Any:
    try:
        amount = round(float(value or 0.0), 2)
    except Exception:
        amount = 0.0

    if abs(amount - round(amount)) < 0.000001:
        return int(round(amount))
    return amount


def _parse_sales_goal_amount(settings_map: Dict[str, Dict[str, Any]], key: str) -> Optional[float]:
    raw_value = str(get_admin_setting_value(settings_map, key, "") or "").strip()
    if not raw_value:
        return None

    try:
        amount = float(raw_value.replace(",", "."))
    except Exception:
        return None

    if amount <= 0:
        return None

    return amount


def _build_sales_goal_payload(
    settings_map: Dict[str, Dict[str, Any]],
    period_key: str,
    current_amount: Any,
) -> Dict[str, Any]:
    goal_key_by_period = {
        "today": "daily_sales_goal_amount",
        "this_week": "weekly_sales_goal_amount",
        "month_to_date": "monthly_sales_goal_amount",
    }

    current_amount_num = round(float(current_amount or 0.0), 2)
    target_key = goal_key_by_period.get(str(period_key or "").strip(), "")
    target_amount = _parse_sales_goal_amount(settings_map, target_key) if target_key else None

    if target_amount is None:
        return {
            "period": period_key,
            "target_amount": None,
            "current_amount": _money_value(current_amount_num),
            "remaining_amount": None,
            "achievement_percent": None,
            "status": "not_configured",
        }

    remaining_amount = round(max(target_amount - current_amount_num, 0.0), 2)
    achievement_percent = round((current_amount_num / target_amount) * 100, 2)

    return {
        "period": period_key,
        "target_amount": _money_value(target_amount),
        "current_amount": _money_value(current_amount_num),
        "remaining_amount": _money_value(remaining_amount),
        "achievement_percent": achievement_percent,
        "status": "achieved" if current_amount_num >= target_amount else "behind",
    }


def _serialize_top_customers(consumers: List[Dict[str, Any]], limit: int = 5) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    for c in (consumers or [])[:limit]:
        last_purchase_dt = c.get("last_purchase_dt")
        last_purchase_at = ""
        if last_purchase_dt is not None:
            try:
                last_purchase_at = last_purchase_dt.isoformat()
            except Exception:
                last_purchase_at = str(last_purchase_dt or "")

        out.append({
            "name": str(c.get("name") or "").strip(),
            "contact": str(c.get("contact") or "").strip(),
            "orders_count": int(c.get("orders_count") or 0),
            "total_spent": round(float(c.get("total_spent") or 0.0), 2),
            "last_purchase_at": last_purchase_at,
            "products_text": str(c.get("products_text") or "").strip(),
        })
    return out


def build_dashboard_summary_data(
    orders_sh,
    tenant: Dict[str, Any],
    tenant_id: str,
    tenant_tz: str,
    period_key: str = "today",
) -> Dict[str, Any]:
    period = resolve_period(tenant_tz, period_key)
    stats_source = load_stats_source_data(orders_sh)
    stats_data = build_stats_summary_data(
        orders_sh=orders_sh,
        tenant_id=tenant_id,
        tenant_tz=tenant_tz,
        period=period,
        source_data=stats_source,
    )
    kpi_comparisons = build_kpi_comparisons(
        orders_sh=orders_sh,
        tenant_id=tenant_id,
        tenant_tz=tenant_tz,
        period_key=period_key,
        period=period,
        current_summary=stats_data,
        source_data=stats_source,
    )
    try:
        settings_map = load_admin_settings(orders_sh, force=False)
    except Exception:
        settings_map = {}

    try:
        _, consumers, _, customer_order_type_distribution, top_recurrent_consumers = build_dashboard_customer_metrics(
            orders_sh=orders_sh,
            tenant_tz=tenant_tz,
            period_key=period_key,
        )
    except Exception:
        consumers = []
        customer_order_type_distribution = []
        top_recurrent_consumers = []

    repeat_customers = 0
    for c in consumers:
        try:
            if int(c.get("orders_count") or 0) > 1:
                repeat_customers += 1
        except Exception:
            continue

    try:
        survey_summary = build_survey_analytics(
            orders_sh=orders_sh,
            tenant_tz=tenant_tz,
            period_key=period_key,
        )
    except Exception:
        survey_summary = {
            "period_label": "",
            "period_range_text": "",
            "total_answers": 0,
            "total_unique_responses": 0,
            "general_stars_avg": 0.0,
            "general_stars_hist": {1: 0, 2: 0, 3: 0, 4: 0, 5: 0},
            "by_question": [],
        }

    return {
        "ok": True,
        "tenant": {
            "tenant_id": str(tenant.get("tenant_id") or tenant_id).strip(),
            "restaurant_name": str(
                tenant.get("restaurant_name")
                or tenant.get("name")
                or tenant_id
            ).strip(),
        },
        "period": {
            "key": period_key,
            "label": str((stats_data.get("period") or {}).get("label") or period.label).strip(),
            "range_text": str((stats_data.get("period") or {}).get("range_text") or "").strip(),
        },
        "kpis": dict(stats_data.get("kpis") or {}),
        "kpi_comparisons": dict(kpi_comparisons or {}),
        "sales_by_day": list(stats_data.get("sales_by_day") or []),
        "sales_by_hour": list(stats_data.get("sales_by_hour") or []),
        "top_products": list(stats_data.get("top_products") or []),
        "categories": list(stats_data.get("categories") or []),
        "order_item_count_distribution": list(stats_data.get("order_item_count_distribution") or []),
        "top_order_combinations": list(stats_data.get("top_order_combinations") or []),
        "sales_goal": _build_sales_goal_payload(
            settings_map=settings_map,
            period_key=period_key,
            current_amount=(stats_data.get("kpis") or {}).get("sales_total", 0),
        ),
        "customers_summary": {
            "total_customers": len(consumers),
            "repeat_customers": repeat_customers,
            "top_customers": _serialize_top_customers(consumers),
        },
        "customer_order_type_distribution": list(customer_order_type_distribution or []),
        "top_recurrent_customers": _serialize_top_customers(top_recurrent_consumers, limit=3),
        "survey_summary": {
            "total_answers": int(survey_summary.get("total_answers") or 0),
            "total_unique_responses": int(survey_summary.get("total_unique_responses") or 0),
            "general_stars_avg": float(survey_summary.get("general_stars_avg") or 0.0),
            "general_stars_hist": dict(survey_summary.get("general_stars_hist") or {}),
            "by_question": list(survey_summary.get("by_question") or []),
        },
        "insights": list(stats_data.get("insights") or []),
        "metadata": {
            "generated_at": _utc_iso(),
            "source": "sheets",
            "tenant_id": str(tenant.get("tenant_id") or tenant_id).strip(),
        },
    }
