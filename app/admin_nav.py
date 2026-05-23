# app/admin_nav.py

from app.telegram_keyboard import kb
from app.tenants import build_admin_dashboard_url


def admin_root_kb():
    return kb([
        [("⚙️ Panel", "admin_panel")],
    ])


def admin_panel_kb(user_role: str = "admin", tenant=None):
    """
    Panel rediseñado tipo mini app:
    - misma lógica / mismos callbacks
    - solo cambia naming visual
    """

    user_role = str(user_role or "").strip().lower()
    is_owner = user_role == "owner"
    dashboard_url = build_admin_dashboard_url(tenant or {}) if tenant else ""
    stats_button = ("📊 Estadísticas", "url", dashboard_url) if dashboard_url else ("📊 Estadísticas", "admin_stats")

    rows = []

    # BLOQUE 1: OPERACIÓN
    if not is_owner:
        rows.append([
            ("✅ Hacer pedido", "admin_order"),
            ("📦 Seguimiento de pedidos", "admin_tracking"),
        ])
        rows.append([
            ("📱 QR", "admin_payments"),
        ])
    else:
        rows.append([
            ("📦 Seguimiento de pedidos", "admin_tracking"),
            ("📱 QR", "admin_payments"),
        ])

    # BLOQUE 2: ANÁLISIS
    rows.append([
        stats_button,
        ("👥 Clientes", "admin_consumers"),
    ])

    rows.append([
        ("📝 Encuestas", "admin_surveys"),
        ("🎁 Promociones", "admin_promotions"),
    ])

    # BLOQUE 3: CONFIGURACIÓN
    rows.append([
        ("🍔 Menú", "admin_menu"),
        ("⏰ Horarios", "admin_hours"),
    ])

    rows.append([
        ("🎯 Objetivos de ventas", "admin_sales_goals"),
        ("🏪 Info general", "admin_business"),
    ])

    return kb(rows)


def admin_back_panel_kb(back_data: str):
    return kb([
        [("⬅️ Volver", back_data)],
        [("🧭 Panel", "admin_panel")],
    ])
