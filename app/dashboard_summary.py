from datetime import datetime
from typing import Any, Dict, List

from app.consumer_db import aggregate_consumers
from app.stats import resolve_period, build_stats_summary_data
from app.survey_analytics import build_survey_analytics


def _utc_iso() -> str:
    return datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")


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
    stats_data = build_stats_summary_data(
        orders_sh=orders_sh,
        tenant_id=tenant_id,
        tenant_tz=tenant_tz,
        period=period,
    )

    try:
        _, consumers, _ = aggregate_consumers(
            orders_sh=orders_sh,
            tenant_tz=tenant_tz,
            period_key=period_key,
            min_orders=1,
        )
    except Exception:
        consumers = []

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
        "sales_by_day": list(stats_data.get("sales_by_day") or []),
        "sales_by_hour": list(stats_data.get("sales_by_hour") or []),
        "top_products": list(stats_data.get("top_products") or []),
        "categories": list(stats_data.get("categories") or []),
        "customers_summary": {
            "total_customers": len(consumers),
            "repeat_customers": repeat_customers,
            "top_customers": _serialize_top_customers(consumers),
        },
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
