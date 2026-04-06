# app/admin_nav.py

from app.telegram_keyboard import kb


def admin_root_kb():
    return kb([
        [("⚙️ Panel", "admin_panel")],
    ])


def admin_panel_kb(user_role: str = "admin"):
    """
    Panel rediseñado tipo mini app:
    - misma lógica / mismos callbacks
    - solo cambia naming visual
    """

    user_role = str(user_role or "").strip().lower()
    is_owner = user_role == "owner"

    rows = []

    # BLOQUE 1: OPERACIÓN
    if not is_owner:
        rows.append([
            ("🧺 Crear pedido", "admin_order"),
            ("📱 QR", "admin_payments"),
        ])
    else:
        rows.append([
            ("📱 QR", "admin_payments"),
        ])

    # BLOQUE 2: ANÁLISIS
    rows.append([
        ("📊 Estadísticas", "admin_stats"),
        ("👥 Clientes", "admin_consumers"),
    ])

    rows.append([
        ("📝 Encuestas", "admin_surveys"),
    ])

    # BLOQUE 3: CONFIGURACIÓN
    rows.append([
        ("🍔 Menú", "admin_menu"),
        ("⏰ Horarios", "admin_hours"),
    ])

    return kb(rows)


def admin_back_panel_kb(back_data: str):
    return kb([
        [("⬅️ Volver", back_data)],
        [("🧭 Panel", "admin_panel")],
    ])
