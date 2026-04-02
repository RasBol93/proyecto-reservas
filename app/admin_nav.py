# app/admin_nav.py

from app.telegram_keyboard import kb


def admin_root_kb():
    return kb([
        [("⚙️ Panel", "admin_panel")],
    ])


def admin_panel_kb(user_role: str = "admin"):
    rows = [
        [("📊 Estadísticas", "admin_stats")],
        [("👥 Base de consumidores", "admin_consumers")],
        [("📝 Encuestas", "admin_surveys")],
    ]

    if str(user_role or "").strip().lower() != "owner":
        rows.append([("➕ Crear pedido manual", "admin_order")])

    rows.extend([
        [("⚙️ Config días y horarios", "admin_hours")],
        [("🍔 Config menú y precios", "admin_menu")],
        [("💳 Pagos", "admin_payments")],
    ])

    return kb(rows)


def admin_back_panel_kb(back_data: str):
    return kb([
        [("⬅️ Volver", back_data)],
        [("⚙️ Panel", "admin_panel")],
    ])
