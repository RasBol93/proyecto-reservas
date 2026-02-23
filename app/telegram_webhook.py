# app/telegram_webhook.py

import json
import urllib.request
from typing import Any, Dict, Optional, List, Tuple

from fastapi import APIRouter, HTTPException

from app.config import TELEGRAM_API_BASE
from app.tenants import get_tenant_or_404, resolve_bot_by_secret
from app.sheets import get_gspread_client, open_spreadsheet_by_key
from app.menu import load_menu_index, group_menu_by_category, calc_total_amount
from app.orders import append_order_row, update_order_status, gen_order_id
from app.telegram_keyboard import kb, main_menu_kb
from app.utils import normalize, log_event

router = APIRouter()

# =========================================================
# Estado en memoria (DEMO)
# - Si Render reinicia, se pierde.
# =========================================================
# key: (tenant_id, chat_id) -> {"cart":[{"sku":..., "qty":...}], "stage": "...", "tmp": {...}}
SESSIONS: Dict[Tuple[str, int], Dict[str, Any]] = {}


def get_sess(tenant_id: str, chat_id: int) -> Dict[str, Any]:
    key = (tenant_id, chat_id)
    if key not in SESSIONS:
        SESSIONS[key] = {"cart": [], "stage": "idle", "tmp": {}}
    return SESSIONS[key]


def clear_sess(tenant_id: str, chat_id: int) -> None:
    key = (tenant_id, chat_id)
    if key in SESSIONS:
        SESSIONS[key] = {"cart": [], "stage": "idle", "tmp": {}}


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
    return (tenant.get("admin_bot_token") or tenant.get("bot_token_admin") or "").strip()


def get_admin_chat_id(tenant: Dict[str, Any]) -> Optional[int]:
    raw = (tenant.get("admin_chat_id") or "").strip()
    if not raw:
        return None
    try:
        return int(raw)
    except Exception:
        return None


def fmt_cart_lines(cart: List[Dict[str, Any]], menu_idx: Dict[str, Any]) -> Tuple[str, float, int]:
    """
    Returns: (lines_text, total_amount, total_qty)
    """
    total_qty = 0
    lines = []
    items_for_total = []
    for it in cart:
        sku = it.get("sku")
        qty = int(it.get("qty") or 1)
        qty = max(1, qty)
        if sku not in menu_idx:
            continue
        total_qty += qty
        name = menu_idx[sku]["name"]
        price = float(menu_idx[sku]["price"])
        lines.append(f"- {qty} x {name} ({price:.0f})")
        items_for_total.append({"sku": sku, "qty": qty})

    total = calc_total_amount(items_for_total, menu_idx) if items_for_total else 0.0
    return ("\n".join(lines) if lines else "(vacío)"), total, total_qty


def notify_admin_new_order(
    tenant: Dict[str, Any],
    tenant_id: str,
    order_id: str,
    total: float,
    cart: List[Dict[str, Any]],
    menu_idx: Dict[str, Any],
    customer_name: str,
    customer_contact: str,
    requested_time: str,
) -> None:
    admin_token = get_admin_bot_token(tenant)
    admin_chat_id = get_admin_chat_id(tenant)

    if not admin_token or not admin_chat_id:
        log_event("admin_notify_skipped", tenant_id=tenant_id, reason="missing admin_bot_token or admin_chat_id")
        return

    lines_txt, _, total_qty = fmt_cart_lines(cart, menu_idx)

    pay_btn = kb([[("✅ Pagado", f"paid|{tenant_id}|{order_id}")]])

    txt = (
        f"🧾 *Nuevo pedido*\n"
        f"Tenant: `{tenant_id}`\n"
        f"ID: `{order_id}`\n"
        f"Cliente: *{customer_name}*\n"
        f"Contacto (chat_id): `{customer_contact}`\n"
        f"Hora recogida: *{requested_time}* (pendiente)\n"
        f"Cantidad total: *{total_qty}*\n"
        f"Total: *{total:.2f}* BOB\n\n"
        f"*Detalle:*\n{lines_txt}\n\n"
        f"Pulsa ✅ Pagado cuando confirmes el pago."
    )

    telegram_send_text(admin_token, admin_chat_id, txt, reply_markup=pay_btn, parse_mode="Markdown")


# -------------------------
# Client keyboards
# -------------------------

def client_home_kb() -> Dict[str, Any]:
    # Puedes mejorar luego, pero por ahora:
    return kb([
        [("📋 Ver menú", "menu")],
        [("🛒 Ver carrito", "cart")],
    ])


def cart_kb(has_items: bool) -> Dict[str, Any]:
    rows = []
    if has_items:
        rows.append([("✅ Confirmar pedido", "cart_confirm")])
        rows.append([("🧹 Vaciar carrito", "cart_clear")])
    rows.append([("⬅️ Seguir comprando", "menu")])
    rows.append([("🏠 Inicio", "home")])
    return kb(rows)


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

    # =========================================================
    # 1) CALLBACK QUERY
    # =========================================================
    cb = update.get("callback_query")
    if cb:
        data = (cb.get("data") or "").strip()
        cb_id = cb.get("id")
        chat_id = int(cb["message"]["chat"]["id"])

        if cb_id:
            telegram_answer_callback(bot_token, cb_id, "OK")

        # -------------------------
        # ADMIN callbacks
        # -------------------------
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

        # -------------------------
        # CLIENT callbacks
        # -------------------------
        if mode == "client":
            sess = get_sess(tenant_id, chat_id)

            if data in ("home",):
                telegram_send_text(bot_token, chat_id, "Elige una opción:", client_home_kb())
                return {"ok": True}

            if data == "menu":
                menu_idx = load_menu_index(orders_sh)
                cats = group_menu_by_category(menu_idx)

                if not cats:
                    telegram_send_text(bot_token, chat_id, "No hay menú activo.", client_home_kb())
                    return {"ok": True}

                rows = []
                for c in sorted(cats.keys(), key=lambda x: normalize(x)):
                    rows.append([(c, f"cat|{normalize(c)}")])
                rows.append([("🛒 Carrito", "cart")])
                rows.append([("🏠 Inicio", "home")])

                telegram_send_text(bot_token, chat_id, "📋 Elige una categoría:", kb(rows))
                return {"ok": True}

            if data == "cart":
                menu_idx = load_menu_index(orders_sh)
                cart = sess.get("cart") or []
                lines_txt, total, total_qty = fmt_cart_lines(cart, menu_idx)

                has_items = total_qty > 0
                msg = (
                    f"🛒 *Tu carrito*\n"
                    f"Cantidad: *{total_qty}*\n"
                    f"Total: *{total:.2f}* BOB\n\n"
                    f"{lines_txt}"
                )
                telegram_send_text(bot_token, chat_id, msg, reply_markup=cart_kb(has_items), parse_mode="Markdown")
                return {"ok": True}

            if data == "cart_clear":
                sess["cart"] = []
                sess["stage"] = "idle"
                sess["tmp"] = {}
                telegram_send_text(bot_token, chat_id, "🧹 Carrito vaciado.", reply_markup=client_home_kb())
                return {"ok": True}

            if data == "cart_confirm":
                # Paso siguiente: pedir nombre (Etapa 1)
                cart = sess.get("cart") or []
                if not cart:
                    telegram_send_text(bot_token, chat_id, "Tu carrito está vacío.", reply_markup=client_home_kb())
                    return {"ok": True}

                sess["stage"] = "awaiting_name"
                telegram_send_text(bot_token, chat_id, "Perfecto. ¿Cuál es tu *nombre* para el pedido?", parse_mode="Markdown")
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
                    telegram_send_text(bot_token, chat_id, "Categoría no encontrada.", reply_markup=client_home_kb())
                    return {"ok": True}

                items = cats.get(real_cat, [])
                if not items:
                    telegram_send_text(bot_token, chat_id, "No hay productos activos.", reply_markup=client_home_kb())
                    return {"ok": True}

                rows = []
                for it in items[:20]:
                    rows.append([(f"{it['name']} ({it['price']:.0f})", f"prd|{it['sku']}")])
                rows.append([("🛒 Carrito", "cart")])
                rows.append([("⬅️ Categorías", "menu")])

                telegram_send_text(bot_token, chat_id, f"🍽 {real_cat} — elige un producto:", kb(rows))
                return {"ok": True}

            if data.startswith("prd|"):
                sku = data.split("|", 1)[1].strip()

                rows = [
                    [("1", f"qty|{sku}|1"), ("2", f"qty|{sku}|2"), ("3", f"qty|{sku}|3"), ("4", f"qty|{sku}|4")],
                    [("🛒 Carrito", "cart")],
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
                qty = max(1, qty)

                menu_idx = load_menu_index(orders_sh)
                if sku not in menu_idx:
                    telegram_send_text(bot_token, chat_id, "Producto no disponible.", reply_markup=client_home_kb())
                    return {"ok": True}

                # ✅ En vez de crear pedido: agregar al carrito
                cart = sess.get("cart") or []
                found = False
                for it in cart:
                    if it.get("sku") == sku:
                        it["qty"] = int(it.get("qty") or 0) + qty
                        found = True
                        break
                if not found:
                    cart.append({"sku": sku, "qty": qty})
                sess["cart"] = cart

                # Mostrar mini-resumen + opciones
                lines_txt, total, total_qty = fmt_cart_lines(cart, menu_idx)
                name = menu_idx[sku]["name"]
                telegram_send_text(
                    bot_token,
                    chat_id,
                    f"✅ Agregado al carrito: {qty} x {name}\n\nCantidad: {total_qty}\nTotal: {total:.2f} BOB",
                    reply_markup=kb([
                        [("🛒 Ver carrito", "cart")],
                        [("⬅️ Seguir comprando", "menu")],
                        [("🏠 Inicio", "home")],
                    ]),
                )
                return {"ok": True}

        return {"ok": True}

    # =========================================================
    # 2) MENSAJE NORMAL (texto)
    # =========================================================
    msg = update.get("message") or update.get("edited_message")
    if msg:
        chat_id = int(msg["chat"]["id"])
        text = (msg.get("text") or "").strip()

        # CLIENT: /start o saludo
        if mode == "client":
            sess = get_sess(tenant_id, chat_id)

            if normalize(text) in ("start", "/start", "hola"):
                clear_sess(tenant_id, chat_id)
                telegram_send_text(bot_token, chat_id, "Bienvenido 👋\nElige una opción:", client_home_kb())
                return {"ok": True}

            # ✅ Etapa 1: si estamos esperando nombre
            if sess.get("stage") == "awaiting_name":
                customer_name = text.strip()
                if not customer_name:
                    telegram_send_text(bot_token, chat_id, "Dime tu nombre, por favor.")
                    return {"ok": True}

                menu_idx = load_menu_index(orders_sh)
                cart = sess.get("cart") or []
                lines_txt, total, total_qty = fmt_cart_lines(cart, menu_idx)

                if total_qty <= 0:
                    telegram_send_text(bot_token, chat_id, "Tu carrito está vacío.", reply_markup=client_home_kb())
                    sess["stage"] = "idle"
                    return {"ok": True}

                order_id = gen_order_id()

                # (Etapa 2 después) por ahora dejamos requested_time como pendiente
                requested_time = "pendiente"

                append_order_row(
                    orders_sh=orders_sh,
                    tenant_id=tenant_id,
                    order_id=order_id,
                    customer_name=customer_name,
                    customer_contact=str(chat_id),
                    items=cart,
                    delivery_type="pickup",
                    requested_time=requested_time,
                    status="PENDING_PAYMENT",
                    source="telegram",
                    total_amount=total,
                )

                # Notificar admin (mensaje completo)
                notify_admin_new_order(
                    tenant=tenant,
                    tenant_id=tenant_id,
                    order_id=order_id,
                    total=total,
                    cart=cart,
                    menu_idx=menu_idx,
                    customer_name=customer_name,
                    customer_contact=str(chat_id),
                    requested_time=requested_time,
                )

                # Limpiar sesión
                clear_sess(tenant_id, chat_id)

                telegram_send_text(
                    bot_token,
                    chat_id,
                    f"✅ Pedido confirmado\nID: {order_id}\nCliente: {customer_name}\nCantidad: {total_qty}\nTotal: {total:.2f} BOB\n\n"
                    f"En el siguiente paso añadiremos la hora de recogida.",
                    reply_markup=client_home_kb(),
                )
                return {"ok": True}

            telegram_send_text(bot_token, chat_id, "Usa /start para ver el menú.", reply_markup=client_home_kb())
            return {"ok": True}

        # ADMIN: por ahora simple (puedes mejorar luego)
        telegram_send_text(bot_token, chat_id, "OK admin ✅")
        return {"ok": True}

    return {"ok": True}
