# app/client_ui.py

from typing import Dict, List, Any

from app.telegram_api import telegram_send_text, telegram_send_photo
from app.telegram_keyboard import kb
from app.content import (
    build_start_text,
    load_content_map,
    has_location,
    has_faq,
    has_survey,
)


def build_dynamic_home_kb(content_map: Dict[str, str]):
    rows = [
        [("📋 Ver menú", "menu")],
        [("🛒 Ver carrito", "cart")],
    ]

    if has_location(content_map):
        rows.append([("📍 Ubicación", "location")])

    rows.append([("⏰ Horarios", "hours")])

    if has_faq(content_map):
        rows.append([("❓ FAQ", "faq")])

    if has_survey(content_map):
        rows.append([("📝 Encuesta", "survey")])

    return kb(rows)


def _send_home(bot_token: str, chat_id: int, orders_sh) -> bool:
    content_map = load_content_map(orders_sh)
    return telegram_send_text(
        bot_token,
        chat_id,
        build_start_text(orders_sh, content_map=content_map),
        build_dynamic_home_kb(content_map),
    )


def _send_category_products(
    bot_token: str,
    chat_id: int,
    real_cat: str,
    items: List[Dict[str, Any]],
) -> None:
    with_photo = []
    without_photo = []

    for it in items:
        photo_url = str(it.get("photo_url") or "").strip()
        photo_file_id = str(it.get("photo_file_id") or "").strip()
        if photo_url or photo_file_id:
            with_photo.append(it)
        else:
            without_photo.append(it)

    telegram_send_text(bot_token, chat_id, f"🍽 {real_cat}")

    for it in with_photo:
        photo_url = str(it.get("photo_url") or "").strip()
        photo_file_id = str(it.get("photo_file_id") or "").strip()
        price_txt = f"{float(it['price']):.0f}"

        reply_markup = kb([
            [(f"⬆️ {it['name']} — Bs {price_txt}", f"prd|{it['sku']}")],
        ])

        if photo_url:
            telegram_send_photo(
                bot_token,
                chat_id,
                photo_url,
                caption="",
                reply_markup=reply_markup,
            )
        elif photo_file_id:
            telegram_send_photo(
                bot_token,
                chat_id,
                photo_file_id,
                caption="",
                reply_markup=reply_markup,
            )

    if without_photo:
        rows = []
        for it in without_photo[:25]:
            rows.append([(f"{it['name']} — Bs {float(it['price']):.0f}", f"prd|{it['sku']}")])

        telegram_send_text(
            bot_token,
            chat_id,
            "Productos sin foto:",
            kb(rows),
        )

    telegram_send_text(
        bot_token,
        chat_id,
        "Otras opciones",
        kb([
            [("🛒 Carrito", "cart")],
            [("⬅️ Categorías", "menu")],
            [("🏠 Inicio", "home")],
        ]),
    )
