# app/telegram_keyboard.py

from typing import Any, Dict, List, Tuple


TELEGRAM_CALLBACK_DATA_MAX = 64  # bytes aprox.


def _fits_callback_data(s: str) -> bool:
    try:
        return len(s.encode("utf-8")) <= TELEGRAM_CALLBACK_DATA_MAX
    except Exception:
        return len(s) <= TELEGRAM_CALLBACK_DATA_MAX


def kb(rows: List[List[Tuple[Any, ...]]]) -> Dict[str, Any]:
    """
    rows:
      - [[(text, callback_data), ...], ...]
      - [[(text, "url", href), ...], ...]
    """
    inline_keyboard = []
    for row in rows:
        rendered_row = []
        for btn in row:
            if len(btn) == 2:
                text, callback_data = btn
                callback_data = str(callback_data or "")
                if not _fits_callback_data(callback_data):
                    raise ValueError(
                        f"callback_data too long ({len(callback_data.encode('utf-8'))} bytes): {callback_data[:120]}"
                    )
                rendered_row.append({"text": str(text or ""), "callback_data": callback_data})
                continue

            if len(btn) == 3 and str(btn[1] or "").strip().lower() == "url":
                text, _kind, href = btn
                href = str(href or "").strip()
                if not href:
                    raise ValueError("url button requires a non-empty href")
                rendered_row.append({"text": str(text or ""), "url": href})
                continue

            raise ValueError(f"Unsupported keyboard button shape: {btn!r}")

        inline_keyboard.append(rendered_row)

    return {"inline_keyboard": inline_keyboard}


def main_menu_kb() -> Dict[str, Any]:
    # Solo botones que tu webhook ya entiende: menu y home
    return kb([
        [("📋 Ver Menú", "menu")],
        [("🏠 Inicio", "home")],
    ])
