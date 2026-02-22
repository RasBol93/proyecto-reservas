# app/telegram_keyboard.py

from typing import Any, Dict, List, Tuple


def kb(rows: List[List[Tuple[str, str]]]) -> Dict[str, Any]:
    """
    rows: [[(text, callback_data), ...], ...]
    """
    return {
        "inline_keyboard": [
            [{"text": t, "callback_data": c} for (t, c) in row]
            for row in rows
        ]
    }


def main_menu_kb() -> Dict[str, Any]:
    return kb([
        [("📋 Ver Menú", "menu")],
        [("📍 Ver Ubicación", "loc")],
        [("❓ Preguntas Frecuentes", "faq")],
        [("🛒 Carrito", "cart")],
    ])
