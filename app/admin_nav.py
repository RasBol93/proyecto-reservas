# app/admin_nav.py

from app.telegram_keyboard import kb


def admin_root_kb():
    return kb([
        [("🧭 Panel admin", "admin_panel")],
    ])


def admin_panel_kb():
    return kb([
        [("📊 Estadísticas", "admin_stats")],
        [("👥 Base de consumidores", "admin_consumers")],
        [("📝 Encuestas", "admin_surveys")],  # 👈 NUEVO
        [("➕ Crear pedido manual", "admin_order")],
        [("⚙️ Config días y horarios", "admin_hours")],
        [("🍔 Config menú y precios", "admin_menu")],
    ])


def admin_back_panel_kb(back_data: str):
    return kb([
        [("⬅️ Volver", back_data)],
        [("🧭 Panel admin", "admin_panel")],
    ])
