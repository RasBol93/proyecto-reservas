# app/stats.py — versión UX mejorada tipo app

import json
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple

from app.utils import normalize
from app.menu import load_menu_index


# =========================
# Helpers
# =========================

def _parse_iso_dt(s: str) -> Optional[datetime]:
    try:
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        return datetime.fromisoformat(s)
    except:
        return None


def _fmt_date(d: datetime) -> str:
    return d.strftime("%d-%m-%Y")


def _weekday_es(dt: datetime) -> str:
    return ["Lun","Mar","Mié","Jue","Vie","Sáb","Dom"][dt.weekday()]


def _hour_bucket(dt: datetime) -> str:
    return dt.strftime("%H:00")


def _parse_items(v: Any) -> List[Dict[str, Any]]:
    if isinstance(v, list):
        return v
    if isinstance(v, str):
        try:
            return json.loads(v)
        except:
            return []
    return []


# =========================
# MAIN
# =========================

def build_stats_report_text(orders_sh, tenant_id: str, tenant_tz: str, period):

    ws = orders_sh.worksheet("Orders")
    values = ws.get_all_values()

    if not values:
        return "📊 Sin datos"

    header = [normalize(x) for x in values[0]]

    def c(name):
        return header.index(normalize(name))

    i_created = c("created_at")
    i_status = c("status")
    i_total = c("total_amount")
    i_items = c("items")

    menu_idx = load_menu_index(orders_sh)

    # =========================
    # Métricas base
    # =========================

    orders_created = 0
    orders_paid = 0
    total_sales = 0
    total_units = 0

    # nuevas stats
    weekday_stats = {}
    hour_stats = {}

    for row in values[1:]:
        dt = _parse_iso_dt(row[i_created])
        if not dt:
            continue

        if not (period.start_utc <= dt <= period.end_utc):
            continue

        orders_created += 1

        is_paid = normalize(row[i_status]) == "paid"
        if not is_paid:
            continue

        orders_paid += 1

        # ventas
        try:
            total_sales += float(row[i_total])
        except:
            pass

        # items
        items = _parse_items(row[i_items])

        order_units = 0

        for it in items:
            qty = int(it.get("qty", 1))
            order_units += qty

        total_units += order_units

        # =========================
        # día de la semana
        # =========================
        d = _weekday_es(dt)
        if d not in weekday_stats:
            weekday_stats[d] = {"orders":0,"units":0,"sales":0}

        weekday_stats[d]["orders"] += 1
        weekday_stats[d]["units"] += order_units
        weekday_stats[d]["sales"] += float(row[i_total] or 0)

        # =========================
        # horas
        # =========================
        h = _hour_bucket(dt)
        if h not in hour_stats:
            hour_stats[h] = 0

        hour_stats[h] += float(row[i_total] or 0)

    # =========================
    # cálculos
    # =========================

    ticket = total_sales / orders_paid if orders_paid else 0
    units_avg = total_units / orders_paid if orders_paid else 0

    # ordenar días
    weekday_order = ["Lun","Mar","Mié","Jue","Vie","Sáb","Dom"]

    # top horas
    top_hours = sorted(hour_stats.items(), key=lambda x: x[1], reverse=True)[:3]

    # =========================
    # TEXTO (UX tipo app)
    # =========================

    lines = []

    lines.append("📊 ESTADÍSTICAS")
    lines.append("")

    lines.append(f"📅 { _fmt_date(period.start_utc) } a { _fmt_date(period.end_utc) }")
    lines.append("")

    # RESUMEN
    lines.append("🔹 RESUMEN")
    lines.append(f"Pedidos pagados: {orders_paid}")
    lines.append(f"Ventas: Bs {total_sales:.2f}")
    lines.append(f"Ticket promedio: Bs {ticket:.2f}")
    lines.append(f"Unidades promedio: {units_avg:.1f}")
    lines.append("")

    # CONVERSIÓN
    lines.append("🔹 EMBUDO")
    lines.append(f"Pedidos creados: {orders_created}")
    lines.append(f"Pedidos pagados: {orders_paid}")
    lines.append("")

    # DÍAS
    lines.append("🔹 VENTAS POR DÍA")
    for d in weekday_order:
        if d in weekday_stats:
            v = weekday_stats[d]
            lines.append(
                f"{d}: {v['orders']} pedidos | {v['units']} unidades | Bs {v['sales']:.2f}"
            )
    lines.append("")

    # HORAS
    lines.append("🔹 MEJORES HORAS")
    for h, s in top_hours:
        lines.append(f"{h} → Bs {s:.2f}")
    lines.append("")

    lines.append("— — —")

    return "\n".join(lines)
