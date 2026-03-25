# app/admin_consumers.py

from app.telegram_api import telegram_send_text
from app.consumer_db import (
    consumer_periods_inline_kb,
    consumer_filters_inline_kb,
    build_consumers_report_pages,
    resolve_consumer_period,
)


def _send_consumers_menu(bot_token: str, chat_id: int, tenant_id: str) -> bool:
    return telegram_send_text(
        bot_token,
        chat_id,
        "👥 BASE DE CONSUMIDORES\n\nElige un período:",
        reply_markup=consumer_periods_inline_kb(tenant_id),
    )


def _send_consumers_filters(bot_token: str, chat_id: int, tenant_id: str, period_key: str, tenant_tz: str) -> bool:
    period = resolve_consumer_period(period_key, tenant_tz)
    return telegram_send_text(
        bot_token,
        chat_id,
        (
            "👥 BASE DE CONSUMIDORES\n\n"
            f"Período elegido: {period.label}\n\n"
            "Ahora elige qué lista quieres ver:"
        ),
        reply_markup=consumer_filters_inline_kb(tenant_id, period_key),
    )


def _send_consumers_report(
    bot_token: str,
    chat_id: int,
    tenant_id: str,
    orders_sh,
    tenant_tz: str,
    period_key: str,
    filter_key: str,
) -> bool:
    pages = build_consumers_report_pages(
        orders_sh=orders_sh,
        tenant_tz=tenant_tz,
        period_key=period_key,
        filter_key=filter_key,
    )

    if not pages:
        pages = ["No encontré resultados."]

    for idx, page in enumerate(pages):
        if idx == len(pages) - 1:
            telegram_send_text(
                bot_token,
                chat_id,
                page,
                reply_markup=consumer_filters_inline_kb(tenant_id, period_key),
            )
        else:
            telegram_send_text(bot_token, chat_id, page)

    return True
