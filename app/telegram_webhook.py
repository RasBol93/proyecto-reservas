# app/telegram_webhook.py

import json
import urllib.request
from typing import Any, Dict

from fastapi import APIRouter, HTTPException

from app.config import TELEGRAM_API_BASE
from app.tenants import get_tenant_or_404, resolve_bot_by_secret
from app.sheets import get_gspread_client, open_spreadsheet_by_key
from app.menu import load_menu_index, group_menu_by_category, calc_total_amount
from app.orders import append_order_row, update_order_status, gen_order_id
from app.telegram_keyboard import kb, main_menu_kb
from app.utils import normalize

router = APIRouter()


# -------------------------
# Telegram API helpers
# -------------------------

def telegram_api_call(bot_token: str, method: str, payload: Dict[str, Any]) -> Dict[str, Any]:
    url = f"{TELEGRAM_API_BASE}/bot{bot_token}/{method}"
    data = json.dumps(payload).encode("utf-8")

    req = urllib.request.Request(
        url,
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )

    with urllib.request.urlopen(req, timeout=12) as resp:
        raw = resp.read().decode("utf-8")
        return json.loads(raw)


def telegram_send_text(bot_token: str, chat_id: int, text: str, reply_markup=None):
    payload = {"chat_id": chat_id, "text": text}
    if reply_markup:
        payload["reply_markup"] = reply_markup
    telegram_api_call(bot_token, "sendMessage", payload)


# -------------------------
# Webhook endpoint
# -------------------------

@router.post("/telegram/webhook/{tenant_id}/{secret}")
async def telegram_webhook(tenant_id: str, secret: str, update: Dict[str, Any]):

    gc = get_gspread_client()
    tenant = get_tenant_or_404(tenant_id)
    mode, bot_token = resolve_bot_by_secret(tenant, secret)

    if not bot_token:
        return {"ok": True}

    orders_sh = open_spreadsheet_by_key(gc, tenant["orders_sheet_id"])

    # CALLBACK
    if "callback_query" in update:
        cb = update["callback_query"]
        data = cb.get("data", "")
        chat_id = cb["message"]["chat"]["id"]

        # ADMIN: paid|tenant|order
        if data.startswith("paid|"):
            parts = data.split("|")
            order_id = parts[2]

            update_order_status(orders_sh, order_id, "PAID")

            telegram_send_text(
                bot_token,
                chat_id,
                f"✅ Pedido {order_id} marcado como PAID"
            )

            return {"ok": True}

        # CLIENT callbacks simples
        if data == "menu":
            menu_idx = load_menu_index(orders_sh)
            cats = group_menu_by_category(menu_idx)

            rows = []
            for c in cats:
                rows.append([(c, f"cat|{normalize(c)}")])

            rows.append([("⬅️ Volver", "home")])

            telegram_send_text(
                bot_token,
                chat_id,
                "📋 Elige una categoría:",
                kb(rows),
            )

            return {"ok": True}

        if data.startswith("cat|"):
            cat_norm = data.split("|")[1]
            menu_idx = load_menu_index(orders_sh)
            cats = group_menu_by_category(menu_idx)

            real_cat = None
            for c in cats:
                if normalize(c) == cat_norm:
                    real_cat = c
                    break

            if not real_cat:
                telegram_send_text(bot_token, chat_id, "Categoría no encontrada.")
                return {"ok": True}

            rows = []
            for it in cats[real_cat]:
                rows.append([(f"{it['name']} ({it['price']:.0f})", f"prd|{it['sku']}")])

            rows.append([("⬅️ Categorías", "menu")])

            telegram_send_text(
                bot_token,
                chat_id,
                f"🍽 {real_cat}",
                kb(rows),
            )

            return {"ok": True}

        if data.startswith("prd|"):
            sku = data.split("|")[1]
            rows = [
                [("1", f"qty|{sku}|1"), ("2", f"qty|{sku}|2")],
                [("3", f"qty|{sku}|3"), ("4", f"qty|{sku}|4")],
                [("⬅️ Volver", "menu")],
            ]

            telegram_send_text(
                bot_token,
                chat_id,
                "Selecciona cantidad:",
                kb(rows),
            )

            return {"ok": True}

        if data.startswith("qty|"):
            _, sku, qty = data.split("|")
            menu_idx = load_menu_index(orders_sh)

            items = [{"sku": sku, "qty": int(qty)}]
            total = calc_total_amount(items, menu_idx)
            order_id = gen_order_id()

            append_order_row(
                orders_sh,
                tenant_id,
                order_id,
                "Cliente Telegram",
                str(chat_id),
                items,
                "pickup",
                "ahora",
                "PENDING_PAYMENT",
                "telegram",
                total,
            )

            telegram_send_text(
                bot_token,
                chat_id,
                f"✅ Pedido creado\nID: {order_id}\nTotal: {total} BOB",
                main_menu_kb(),
            )

            return {"ok": True}

        return {"ok": True}

    # MENSAJE NORMAL
    if "message" in update:
        msg = update["message"]
        chat_id = msg["chat"]["id"]
        text = msg.get("text", "")

        if normalize(text) in ("start", "/start", "hola"):
            telegram_send_text(
                bot_token,
                chat_id,
                "Bienvenido 👋\nElige una opción:",
                main_menu_kb(),
            )

        return {"ok": True}

    return {"ok": True}
