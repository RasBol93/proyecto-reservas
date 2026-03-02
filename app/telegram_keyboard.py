# app/telegram_keyboard.py

from typing import Any, Dict, List, Tuple


TELEGRAM_CALLBACK_DATA_MAX = 64  # bytes aprox.


def _fits_callback_data(s: str) -> bool:
    try:
        return len(s.encode("utf-8")) <= TELEGRAM_CALLBACK_DATA_MAX
    except Exception:
        return len(s) <= TELEGRAM_CALLBACK_DATA_MAX


def kb(rows: List[List[Tuple[str, str]]]) -> Dict[str, Any]:
    """
    rows: [[(text, callback_data), ...], ...]
    """
    inline_keyboard = []
    for row in rows:
        r = []
        for (t, c) in row:
            c = str(c or "")
            if not _fits_callback_data(c):
                # Fail-fast: mejor explotar aquí que tener botones que Telegram rechaza silenciosamente
                raise ValueError(f"callback_data too long ({len(c.encode('utf-8'))} bytes): {c[:120]}")
            r.append({"text": str(t or ""), "callback_data": c})
        inline_keyboard.append(r)

    return {"inline_keyboard": inline_keyboard}


def main_menu_kb() -> Dict[str, Any]:
    # Solo botones que tu webhook ya entiende: menu y home
    return kb([
        [("📋 Ver Menú", "menu")],
        [("🏠 Inicio", "home")],
    ])
