# app/telegram_webhook.py

import json
import urllib.request
from typing import Any, Dict, Optional

from fastapi import APIRouter, HTTPException

from app.config import TELEGRAM_API_BASE
from app.tenants import get_tenant_or_404, resolve_bot_by_secret
from app.sheets import get_gspread_client, open_spreadsheet_by_key
from app.menu import load_menu_index, group_menu_by_category, calc_total_amount
from app.orders import append_order_row, update_order_status, gen_order_id
from app.telegram_keyboard import kb, main_menu_kb
from app.utils import normalize, log_event

router = APIRouter()


# -------------------------
# Telegram API helpers
# -------------------------

def telegram_api_call(bot_token: str, method: str, payload: Dict[str, Any]) -> Dict[str, Any]:
    if not bot_token:
        raise RuntimeError("bot_token missing")

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


def telegram_send_text(
    bot_token: str,
    chat_id: int,
    text: str,
    reply_markup: Optional[Dict[str, Any]] = None,
    parse_mode: Optional[str] = None,
) -> None:
    payload: Dict[str, Any] = {"chat_id": chat_id, "text": text}
    if reply_markup:
        payload["reply_markup"] = reply_markup
    if parse_mode:
        payload["parse_mode"] = parse_mode

    try:
        res = telegram_api_call(bot_token, "sendMessage", payload)
        if not res.get("ok", True):
            log_event("telegram_send_failed", chat_id=chat_id, error=res.get("description") or res)
    except Exception as e:
        log_event("telegram_send_exception", chat_id=chat_id, error=str(e))


def telegram_answer_callback(bot_token: str, callback_query_id: str, text: str = "OK") -> None:
    try:
        res = telegram_api_call(bot_token, "answerCallbackQuery", {"callback_query_id": callback_query_id, "text": text})
        if not res.get("ok", True):
            log_event("telegram_ack_failed", error=res.get("description") or res)
    except Exception as e:
        log_event("telegram_ack_exception", error=str(e))


# -------------------------
# helpers tenant fields
# -------------------------

def get_admin_bot_token(tenant: Dict[str, Any]) -> str:
    # soporta nombres antiguos y nuevos
    return (tenant.get("admin_bot_token") or tenant.get("bot_token_admin") or "").strip()


def get_client_bot_token(tenant: Dict[str, Any]) -> str:
    return (tenant.get("client_bot_token") or tenant.get("bot_token_client") or "").strip()


def get_admin_chat_id(tenant: Dict[str, Any]) -> Optional[int]:
    raw = (tenant.get("admin_chat_id") or "").strip()
    if not raw:
        return None
    try:
        return int(raw)
    except Exception:
        return None


def notify_admin_new_order(tenant: Dict[str, Any], tenant_id: str, order_id: str, total: float, items: list) -> None:
    admin_token = get_admin_bot_token(tenant)
    admin_chat_id = get_admin_chat_id(tenant)

    if not admin_token or not admin_chat_id:
        log_event(
            "admin_notify_skipped",
            tenant_id=tenant_id,
            reason="missing admin_bot_token or admin_chat_id",
            admin_chat_id=str(admin_chat_id),
        )
        return

    # Botón de pago (callback_data <= 64 bytes; esto entra perfecto)
    pay_btn = kb([[("✅ Pagado", f"paid|{tenant_id}|{order_id}")]])

    # Mensaje simple (puedes enriquecer luego)
    txt = (
        f"🧾 *Nuevo pedido*\n"
        f"Tenant: `{tenant_id}`\n"
        f"ID: `{order_id}`\n"
        f"Total: *{total:.2f}* BOB\n"
        f"Items: {len(items)}\n\n"
        f"Pulsa ✅ Pagado cuando confirmes el pago."
    )

    telegram_send_text(admin_token, admin_chat_id, txt, reply_markup=pay_btn, parse_mode="Markdown")


# -------------------------
# Webhook endpoint
# -------------------------

@router.post("/telegram/webhook/{tenant_id}/{secret}")
async def telegram_webhook(tenant_id: str, secret: str, update: Dict[str, Any]):
    tenant_id = (tenant_id or "").strip()
    if not tenant_id:
        return {"ok": True}

    gc = get_gspread_client()
    tenant = get_tenant_or_404(tenant_id, gc=gc)

    mode, bot_token = resolve_bot_by_secret(tenant, secret)
    if not bot_token:
        return {"ok": True}

    orders_sheet_id = (tenant.get("orders_sheet_id") or "").strip()
    if not orders_sheet_id:
        raise HTTPException(status_code=500, detail=f"orders_sheet_id missing for tenant: {tenant_id}")

    orders_sh = open_spreadsheet_by_key(gc, orders_sheet_id)

    # -------------------------
    # 1) CALLBACK QUERY
    # -------------------------
    cb = update.get("callback_query")
    if cb:
        data = (cb.get("data") or "").strip()
        cb_id = cb.get("id")
        chat_id = int(cb["message"]["chat"]["id"])

        if cb_id:
            telegram_answer_callback(bot_token, cb_id, "OK")

        # ADMIN: paid|tenant|order_id
        if mode == "admin" and data.startswith("paid|"):
            parts = data.split("|")
            if len(parts) != 3:
                return {"ok": True}

            cb_tenant_id = parts[1].strip()
            order_id = parts[2].strip()

            if cb_tenant_id != tenant_id:
                raise HTTPException(status_code=400, detail="Tenant mismatch in callback")

            update_order_status(orders_sh, order_id, "PAID")
            telegram_send_text(bot_token, chat_id, f"✅ Pedido {order_id} marcado como PAID")
            return {"ok": True}

        # CLIENT callbacks
        if mode == "client":

            if data in ("home", "menu"):
                if data == "home":
                    telegram_send_text(bot_token, chat_id, "Elige una opción:", main_menu_kb())
                    return {"ok": True}

                menu_idx = load_menu_index(orders_sh)
                cats = group_menu_by_category(menu_idx)

                if not cats:
                    telegram_send_text(bot_token, chat_id, "No hay menú activo.", main_menu_kb())
                    return {"ok": True}

                rows = []
                for c in sorted(cats.keys(), key=lambda x: normalize(x)):
                    rows.append([(c, f"cat|{normalize(c)}")])
                rows.append([("⬅️ Volver", "home")])

                telegram_send_text(bot_token, chat_id, "📋 Elige una categoría:", kb(rows))
                return {"ok": True}

            if data.startswith("cat|"):
                cat_norm = data.split("|", 1)[1].strip()

                menu_idx = load_menu_index(orders_sh)
                cats = group_menu_by_category(menu_idx)

                real_cat = None
                for c in cats.keys():
                    if normalize(c) == cat_norm:
                        real_cat = c
                        break

                if not real_cat:
                    telegram_send_text(bot_token, chat_id, "Categoría no encontrada.", main_menu_kb())
                    return {"ok": True}

                items = cats.get(real_cat, [])
                if not items:
                    telegram_send_text(bot_token, chat_id, "No hay productos activos.", main_menu_kb())
                    return {"ok": True}

                rows = []
                for it in items[:20]:
                    rows.append([(f"{it['name']} ({it['price']:.0f})", f"prd|{it['sku']}")])
                rows.append([("⬅️ Categorías", "menu")])

                telegram_send_text(bot_token, chat_id, f"🍽 {real_cat} — elige un producto:", kb(rows))
                return {"ok": True}

            if data.startswith("prd|"):
                sku = data.split("|", 1)[1].strip()

                rows = [
                    [("1", f"qty|{sku}|1"), ("2", f"qty|{sku}|2"), ("3", f"qty|{sku}|3"), ("4", f"qty|{sku}|4")],
                    [("⬅️ Volver", "menu")],
                ]
                telegram_send_text(bot_token, chat_id, "Selecciona cantidad:", kb(rows))
                return {"ok": True}

            if data.startswith("qty|"):
                parts = data.split("|")
                if len(parts) != 3:
                    return {"ok": True}

                _, sku, qty_s = parts
                try:
                    qty = int(qty_s)
                except Exception:
                    qty = 1

                menu_idx = load_menu_index(orders_sh)
                if sku not in menu_idx:
                    telegram_send_text(bot_token, chat_id, "Producto no disponible.", main_menu_kb())
                    return {"ok": True}

                items = [{"sku": sku, "qty": max(1, qty)}]
                total = calc_total_amount(items, menu_idx)
                order_id = gen_order_id()

                append_order_row(
                    orders_sh=orders_sh,
                    tenant_id=tenant_id,
                    order_id=order_id,
                    customer_name="Cliente Telegram",
                    customer_contact=str(chat_id),
                    items=items,
                    delivery_type="pickup",
                    requested_time="ahora",
                    status="PENDING_PAYMENT",
                    source="telegram",
                    total_amount=total,
                )

                # ✅ NUEVO: avisar al admin
                notify_admin_new_order(tenant, tenant_id, order_id, total, items)

                telegram_send_text(
                    bot_token,
                    chat_id,
                    f"✅ Pedido creado\nID: {order_id}\nTotal: {total:.2f} BOB\nEstado: PENDING_PAYMENT",
                    main_menu_kb(),
                )
                return {"ok": True}

        return {"ok": True}

    # -------------------------
    # 2) MENSAJE NORMAL
    # -------------------------
    msg = update.get("message") or update.get("edited_message")
    if msg:
        chat_id = int(msg["chat"]["id"])
        text = (msg.get("text") or "").strip()

        if mode == "client" and normalize(text) in ("start", "/start", "hola"):
            telegram_send_text(bot_token, chat_id, "Bienvenido 👋\nElige una opción:", main_menu_kb())
            return {"ok": True}

        if mode == "client":
            telegram_send_text(bot_token, chat_id, "Usa /start para ver el menú.", main_menu_kb())
        else:
            telegram_send_text(bot_token, chat_id, "OK admin ✅")

        return {"ok": True}

    return {"ok": True}
