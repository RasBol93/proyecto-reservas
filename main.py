# =========================
# PROYECTO RESERVAS v2.2
# Admin + Client Telegram Bots + Sheets Orders
# (Cliente crea pedido REAL en Sheets + notifica Admin con botón ✅ Pagado)
# =========================

import os
import json
import re
import time
import urllib.request
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional
from collections import deque

import gspread
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

APP_NAME = "proyecto-reservas"

ENV_CONFIG_SPREADSHEET_ID = "RESERVACIONES_CONFIG"
ENV_GCP_CREDS_JSON = "GCP_CREDENTIALS_JSON"
ENV_ADMIN_TOKEN = "ADMIN_TOKEN"

TELEGRAM_API_BASE = "https://api.telegram.org"

# =========================
# Helpers / Validations
# =========================

TENANT_ID_RE = re.compile(r"^[a-z0-9_]{2,40}$")
ORDER_ID_RE = re.compile(r"^[a-f0-9]{8}$")

MAX_ITEMS_PER_ORDER = 30
MAX_NOTES_LEN = 500


def now_iso_utc() -> str:
    return datetime.now(timezone.utc).isoformat()


def normalize(s: Any) -> str:
    if s is None:
        return ""
    return str(s).strip().lower()


def to_bool(v: Any) -> bool:
    return str(v).strip().lower() in ("true", "1", "yes", "y", "si", "sí", "on")


def log_event(event: str, **fields: Any) -> None:
    # No loguees secretos
    safe = {k: v for k, v in fields.items() if "token" not in k and "secret" not in k and "creds" not in k}
    print(json.dumps({"ts": now_iso_utc(), "event": event, **safe}, ensure_ascii=False))


def validate_tenant_id(tenant_id: str) -> None:
    tid = (tenant_id or "").strip()
    if not TENANT_ID_RE.match(tid):
        raise HTTPException(status_code=422, detail="Invalid tenant_id format")


def gen_order_id() -> str:
    import secrets
    return secrets.token_hex(4)  # 8 hex


def validate_order_id(order_id: str) -> None:
    oid = (order_id or "").strip().lower()
    if not ORDER_ID_RE.match(oid):
        raise HTTPException(status_code=422, detail="Invalid order_id format")


# =========================
# Rate Limit (simple)
# =========================

class RateLimiter:
    def __init__(self):
        self.buckets: Dict[str, deque] = {}
        self.window = 60

    def hit(self, key: str, limit: int):
        now = time.time()
        dq = self.buckets.setdefault(key, deque())
        while dq and now - dq[0] > self.window:
            dq.popleft()
        if len(dq) >= limit:
            raise HTTPException(429, "Rate limit exceeded")
        dq.append(now)


_rate = RateLimiter()
RL_CLIENT_WEBHOOK_PER_MIN = 240
RL_ADMIN_WEBHOOK_PER_MIN = 240


# =========================
# Sheets
# =========================

def get_gspread_client() -> gspread.Client:
    raw = os.getenv(ENV_GCP_CREDS_JSON, "").strip()
    if not raw:
        raise RuntimeError(f"Missing env var: {ENV_GCP_CREDS_JSON}")
    creds = json.loads(raw)

    scopes = [
        "https://www.googleapis.com/auth/spreadsheets",
        "https://www.googleapis.com/auth/drive",
    ]
    return gspread.service_account_from_dict(creds, scopes=scopes)


def get_config_spreadsheet(gc: gspread.Client) -> gspread.Spreadsheet:
    sid = os.getenv(ENV_CONFIG_SPREADSHEET_ID, "").strip()
    if not sid:
        raise RuntimeError(f"Missing env var: {ENV_CONFIG_SPREADSHEET_ID}")
    return gc.open_by_key(sid)


_TENANTS_CACHE: Dict[str, Dict[str, Any]] = {}
_TENANTS_CACHE_AT: Optional[str] = None


def load_tenants(gc: gspread.Client, force: bool = False) -> Dict[str, Dict[str, Any]]:
    global _TENANTS_CACHE, _TENANTS_CACHE_AT
    if _TENANTS_CACHE and not force:
        return _TENANTS_CACHE

    sh = get_config_spreadsheet(gc)
    ws = sh.worksheet("Tenants")
    rows = ws.get_all_records()  # headers técnicos en fila 1

    tenants: Dict[str, Dict[str, Any]] = {}
    for r in rows:
        tid = (r.get("tenant_id") or "").strip()
        if not tid:
            continue

        tenants[tid] = {
            "tenant_id": tid,
            "orders_sheet_id": (r.get("orders_sheet_id") or "").strip(),
            "orders_enabled": to_bool(r.get("orders_enabled", "")),
            "active": to_bool(r.get("active", "")),
            "admin_chat_id": str(r.get("admin_chat_id", "")).strip(),

            # nombres nuevos (los tuyos)
            "admin_bot_token": (r.get("admin_bot_token", "") or "").strip(),
            "client_bot_token": (r.get("client_bot_token", "") or "").strip(),
            "webhook_secret_admin": (r.get("webhook_secret_admin", "") or "").strip(),
            "webhook_secret_client": (r.get("webhook_secret_client", "") or "").strip(),
        }

    _TENANTS_CACHE = tenants
    _TENANTS_CACHE_AT = now_iso_utc()
    log_event("tenants_loaded", count=len(tenants), cached_at=_TENANTS_CACHE_AT)
    return tenants


def get_tenant(gc: gspread.Client, tenant_id: str) -> Dict[str, Any]:
    tenants = load_tenants(gc)
    t = tenants.get(tenant_id)
    if not t or not t.get("active"):
        raise HTTPException(404, "Tenant not found or inactive")
    return t


def open_orders_spreadsheet(gc: gspread.Client, tenant: Dict[str, Any]) -> gspread.Spreadsheet:
    sid = (tenant.get("orders_sheet_id") or "").strip()
    if not sid:
        raise HTTPException(500, f"Tenant {tenant.get('tenant_id')} missing orders_sheet_id")
    try:
        return gc.open_by_key(sid)
    except gspread.exceptions.SpreadsheetNotFound:
        raise HTTPException(500, f"SpreadsheetNotFound orders_sheet_id={sid}. Share it with service account.")


def ensure_orders_headers(ws: gspread.Worksheet, required: List[str]) -> List[str]:
    values = ws.get_all_values()
    if not values or not values[0]:
        raise HTTPException(500, "Orders sheet missing headers in row 1")
    headers = values[0]
    headers_norm = [normalize(h) for h in headers]
    missing = [h for h in required if normalize(h) not in headers_norm]
    if missing:
        raise HTTPException(500, f"Orders sheet missing required headers in row 1: {missing}")
    return headers_norm


def load_menu_index(orders_sh: gspread.Spreadsheet) -> Dict[str, Dict[str, Any]]:
    """
    Lee pestaña Menu con headers técnicos:
      sku, name, price, active, category
    Solo active=TRUE
    """
    ws = orders_sh.worksheet("Menu")
    rows = ws.get_all_records()
    idx: Dict[str, Dict[str, Any]] = {}
    for r in rows:
        sku = (r.get("sku") or "").strip()
        if not sku:
            continue
        if not to_bool(r.get("active", "")):
            continue
        try:
            price = float(str(r.get("price", "")).strip())
        except Exception:
            continue
        idx[sku] = {
            "sku": sku,
            "name": r.get("name", ""),
            "price": price,
            "category": r.get("category", ""),
        }
    return idx


def calc_total_amount(items: List[Dict[str, Any]], menu_idx: Dict[str, Dict[str, Any]]) -> float:
    total = 0.0
    for it in items:
        sku = (it.get("sku") or "").strip()
        qty = int(it.get("qty", 0) or 0)
        if not sku or sku not in menu_idx:
            raise HTTPException(422, f"Unknown sku: {sku}")
        if qty <= 0:
            raise HTTPException(422, f"Invalid qty for {sku}")
        total += float(menu_idx[sku]["price"]) * qty
    return round(total, 2)


def append_order_row(
    orders_sh: gspread.Spreadsheet,
    tenant_id: str,
    order_id: str,
    customer_name: str,
    customer_contact: str,
    items: List[Dict[str, Any]],
    notes: str,
    delivery_type: str,
    requested_time: str,
    status: str,
    source: str,
    total_amount: float,
) -> None:
    ws = orders_sh.worksheet("Orders")
    ensure_orders_headers(ws, required=[
        "order_id", "created_at", "tenant_id", "customer_name", "customer_contact",
        "items", "notes", "delivery_type", "requested_time", "status", "source", "total_amount"
    ])

    payload = {
        "order_id": order_id,
        "created_at": now_iso_utc(),
        "tenant_id": tenant_id,
        "customer_name": customer_name,
        "customer_contact": customer_contact,
        "items": json.dumps(items, ensure_ascii=False),
        "notes": notes,
        "delivery_type": delivery_type,
        "requested_time": requested_time,
        "status": status,
        "source": source,
        "total_amount": total_amount,
    }

    headers_raw = ws.row_values(1)
    row = []
    for h in headers_raw:
        row.append(payload.get(normalize(h), ""))

    ws.append_row(row, value_input_option="USER_ENTERED")


def update_order_status(orders_sh: gspread.Spreadsheet, order_id: str, new_status: str) -> Dict[str, Any]:
    ws = orders_sh.worksheet("Orders")
    values = ws.get_all_values()
    if not values:
        return {"found": False}

    headers_norm = [normalize(h) for h in values[0]]
    if "order_id" not in headers_norm or "status" not in headers_norm:
        raise HTTPException(500, "Orders sheet must have order_id and status columns")

    col_oid = headers_norm.index("order_id") + 1
    col_status = headers_norm.index("status") + 1

    for r_idx in range(2, len(values) + 1):
        oid = (ws.cell(r_idx, col_oid).value or "").strip()
        if oid == order_id:
            old_status = ws.cell(r_idx, col_status).value or ""
            if normalize(old_status) != normalize(new_status):
                ws.update_cell(r_idx, col_status, new_status)
            return {"found": True, "old_status": old_status}

    return {"found": False}


# =========================
# Telegram API
# =========================

def telegram_api_call(bot_token: str, method: str, payload: Dict[str, Any]) -> Dict[str, Any]:
    if not bot_token:
        return {"ok": False, "error": "missing_bot_token"}

    url = f"{TELEGRAM_API_BASE}/bot{bot_token}/{method}"
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=12) as resp:
            raw = resp.read().decode("utf-8")
            return json.loads(raw)
    except Exception as e:
        log_event("telegram_error", method=method, error=str(e))
        return {"ok": False, "error": str(e)}


def send_admin_order_message(
    tenant: Dict[str, Any],
    order_id: str,
    items: List[Dict[str, Any]],
    total_amount: float,
    customer_name: str,
    customer_contact: str,
    notes: str,
    delivery_type: str,
    requested_time: str,
) -> None:
    bot_token = (tenant.get("admin_bot_token") or "").strip()
    admin_chat_id = (tenant.get("admin_chat_id") or "").strip()
    if not bot_token or not admin_chat_id:
        log_event("admin_notify_skip", tenant_id=tenant.get("tenant_id"),
                  has_token=bool(bot_token), has_admin=bool(admin_chat_id))
        return

    lines = []
    lines.append("🧾 *Nuevo pedido*")
    lines.append(f"Tenant: `{tenant.get('tenant_id')}`")
    lines.append(f"Order ID: `{order_id}`")
    lines.append(f"Total: *{total_amount} BOB*")
    lines.append("")
    lines.append("*Items:*")
    for it in items:
        lines.append(f"• `{it['sku']}` x{it['qty']}")
    lines.append("")
    lines.append(f"Cliente: {customer_name}")
    lines.append(f"Contacto: {customer_contact}")
    lines.append(f"Tipo: {delivery_type}")
    lines.append(f"Hora: {requested_time}")
    if notes:
        lines.append(f"Notas: {notes}")

    text = "\n".join(lines)
    callback_data = f"paid|{tenant['tenant_id']}|{order_id}"

    telegram_api_call(bot_token, "sendMessage", {
        "chat_id": int(admin_chat_id),
        "text": text,
        "parse_mode": "Markdown",
        "reply_markup": {
            "inline_keyboard": [
                [{"text": "✅ Pagado", "callback_data": callback_data}]
            ]
        }
    })


# =========================
# Carrito en memoria (demo)
# =========================

USER_STATE: Dict[str, Dict[str, Any]] = {}


def get_user_state(tenant_id: str, chat_id: int) -> Dict[str, Any]:
    key = f"{tenant_id}:{chat_id}"
    return USER_STATE.setdefault(key, {"cart": []})


# =========================
# FastAPI
# =========================

app = FastAPI(title=APP_NAME, version="2.2.0")


@app.get("/")
def root():
    return {"ok": True, "service": APP_NAME}


class AdminTokenIn(BaseModel):
    token: str


@app.post("/admin/reload_tenants")
def reload_tenants(payload: AdminTokenIn):
    expected = os.getenv(ENV_ADMIN_TOKEN, "").strip()
    if not expected:
        raise HTTPException(500, "ADMIN_TOKEN not configured")
    if (payload.token or "").strip() != expected:
        raise HTTPException(403, "Invalid token")

    gc = get_gspread_client()
    load_tenants(gc, force=True)
    return {"ok": True, "cached_at": _TENANTS_CACHE_AT, "tenants_count": len(_TENANTS_CACHE)}


# =========================
# CLIENT WEBHOOK
# =========================

@app.post("/telegram/client/webhook/{tenant_id}/{secret}")
async def telegram_client_webhook(tenant_id: str, secret: str, update: Dict[str, Any]):
    validate_tenant_id(tenant_id)
    _rate.hit(f"client:{tenant_id}", RL_CLIENT_WEBHOOK_PER_MIN)

    gc = get_gspread_client()
    tenant = get_tenant(gc, tenant_id)

    # secret client
    if secret.strip() != (tenant.get("webhook_secret_client") or "").strip():
        raise HTTPException(403, "Invalid secret")

    if not tenant.get("orders_enabled"):
        return {"ok": True}

    bot_token = (tenant.get("client_bot_token") or "").strip()
    if not bot_token:
        log_event("client_missing_token", tenant_id=tenant_id)
        return {"ok": True}

    message = update.get("message")
    callback = update.get("callback_query")

    # ========= MENSAJE NORMAL =========
    if message:
        chat_id = int(message["chat"]["id"])
        text = (message.get("text") or "").strip()

        state = get_user_state(tenant_id, chat_id)

        if normalize(text) in ("hola", "/start", "start"):
            state["cart"] = []  # reset demo
            telegram_api_call(bot_token, "sendMessage", {
                "chat_id": chat_id,
                "text": "Bienvenido 👋\nSelecciona una opción:",
                "reply_markup": {
                    "inline_keyboard": [
                        [{"text": "🍔 Hamburguesa Clásica", "callback_data": "add|H01"}],
                        [{"text": "🛒 Ver carrito", "callback_data": "cart"}]
                    ]
                }
            })
        return {"ok": True}

    # ========= CALLBACK =========
    if callback:
        data = (callback.get("data") or "").strip()
        chat_id = int(callback["message"]["chat"]["id"])
        state = get_user_state(tenant_id, chat_id)

        # Acknowledge rápido
        telegram_api_call(bot_token, "answerCallbackQuery", {
            "callback_query_id": callback["id"],
            "text": "OK"
        })

        if data.startswith("add|"):
            sku = data.split("|", 1)[1].strip()

            found = False
            for it in state["cart"]:
                if it["sku"] == sku:
                    it["qty"] += 1
                    found = True
                    break
            if not found:
                if len(state["cart"]) >= MAX_ITEMS_PER_ORDER:
                    telegram_api_call(bot_token, "sendMessage", {"chat_id": chat_id, "text": "Carrito lleno."})
                    return {"ok": True}
                state["cart"].append({"sku": sku, "qty": 1})

            telegram_api_call(bot_token, "sendMessage", {"chat_id": chat_id, "text": f"✅ Agregado {sku} al carrito"})
            return {"ok": True}

        if data == "cart":
            if not state["cart"]:
                telegram_api_call(bot_token, "sendMessage", {"chat_id": chat_id, "text": "Tu carrito está vacío."})
                return {"ok": True}

            items_txt = "\n".join([f"{i['sku']} x{i['qty']}" for i in state["cart"]])
            telegram_api_call(bot_token, "sendMessage", {
                "chat_id": chat_id,
                "text": f"🛒 Carrito:\n{items_txt}\n\n¿Confirmar?",
                "reply_markup": {
                    "inline_keyboard": [
                        [{"text": "✅ Confirmar", "callback_data": "confirm"}],
                        [{"text": "🗑 Vaciar", "callback_data": "clear"}],
                    ]
                }
            })
            return {"ok": True}

        if data == "clear":
            state["cart"] = []
            telegram_api_call(bot_token, "sendMessage", {"chat_id": chat_id, "text": "Carrito vaciado."})
            return {"ok": True}

        if data == "confirm":
            if not state["cart"]:
                telegram_api_call(bot_token, "sendMessage", {"chat_id": chat_id, "text": "Carrito vacío. Agrega algo primero."})
                return {"ok": True}

            # Crear pedido REAL en Sheets + notificar admin
            try:
                orders_sh = open_orders_spreadsheet(gc, tenant)
                menu_idx = load_menu_index(orders_sh)

                items = state["cart"]
                total_amount = calc_total_amount(items, menu_idx)

                order_id = gen_order_id()

                # Demo: nombre/contacto “Telegram”
                customer_name = "Cliente Telegram"
                customer_contact = str(chat_id)  # demo: chat_id como contacto
                notes = ""
                delivery_type = "pickup"
                requested_time = "ahora"

                append_order_row(
                    orders_sh=orders_sh,
                    tenant_id=tenant_id,
                    order_id=order_id,
                    customer_name=customer_name,
                    customer_contact=customer_contact,
                    items=items,
                    notes=notes,
                    delivery_type=delivery_type,
                    requested_time=requested_time,
                    status="PENDING_PAYMENT",
                    source="telegram_client",
                    total_amount=total_amount,
                )

                log_event("order_created_from_client", tenant_id=tenant_id, order_id=order_id, total_amount=total_amount)

                # Notificar admin con botón Pagado
                send_admin_order_message(
                    tenant=tenant,
                    order_id=order_id,
                    items=items,
                    total_amount=total_amount,
                    customer_name=customer_name,
                    customer_contact=customer_contact,
                    notes=notes,
                    delivery_type=delivery_type,
                    requested_time=requested_time,
                )

                # Limpiar carrito
                state["cart"] = []

                telegram_api_call(bot_token, "sendMessage", {
                    "chat_id": chat_id,
                    "text": f"🎉 Pedido creado!\nOrder ID: {order_id}\nTotal: {total_amount} BOB\n\nAhora espera confirmación de pago."
                })
                return {"ok": True}

            except HTTPException as he:
                log_event("client_confirm_http_exception", tenant_id=tenant_id, detail=str(he.detail))
                telegram_api_call(bot_token, "sendMessage", {"chat_id": chat_id, "text": f"❌ Error: {he.detail}"})
                return {"ok": True}

            except Exception as e:
                log_event("client_confirm_exception", tenant_id=tenant_id, error=str(e))
                telegram_api_call(bot_token, "sendMessage", {"chat_id": chat_id, "text": "❌ Error creando el pedido. Reintenta."})
                return {"ok": True}

        return {"ok": True}

    return {"ok": True}


# =========================
# ADMIN WEBHOOK (botón Pagado)
# =========================

@app.post("/telegram/admin/webhook/{tenant_id}/{secret}")
async def telegram_admin_webhook(tenant_id: str, secret: str, update: Dict[str, Any]):
    validate_tenant_id(tenant_id)
    _rate.hit(f"admin:{tenant_id}", RL_ADMIN_WEBHOOK_PER_MIN)

    gc = get_gspread_client()
    tenant = get_tenant(gc, tenant_id)

    # secret admin
    if secret.strip() != (tenant.get("webhook_secret_admin") or "").strip():
        raise HTTPException(403, "Invalid secret")

    bot_token = (tenant.get("admin_bot_token") or "").strip()
    expected_admin_chat_id = (tenant.get("admin_chat_id") or "").strip()

    cb = update.get("callback_query")
    if not cb:
        return {"ok": True}

    from_id = str((cb.get("from") or {}).get("id", "")).strip()
    if expected_admin_chat_id and from_id != expected_admin_chat_id:
        log_event("admin_callback_forbidden", tenant_id=tenant_id, from_id=from_id)
        raise HTTPException(403, "Not allowed")

    data = (cb.get("data") or "").strip()
    parts = data.split("|")
    if len(parts) != 3 or parts[0] != "paid":
        return {"ok": True}

    cb_tenant = parts[1].strip()
    order_id = parts[2].strip().lower()

    if cb_tenant != tenant_id:
        raise HTTPException(400, "Tenant mismatch")

    validate_order_id(order_id)

    orders_sh = open_orders_spreadsheet(gc, tenant)
    result = update_order_status(orders_sh, order_id, "PAID")
    if not result.get("found"):
        log_event("admin_paid_not_found", tenant_id=tenant_id, order_id=order_id)
        if bot_token:
            telegram_api_call(bot_token, "answerCallbackQuery", {
                "callback_query_id": cb.get("id"),
                "text": "No encontré ese pedido en Sheets."
            })
        return {"ok": True}

    old_status = str(result.get("old_status", "") or "")
    already_paid = normalize(old_status) == "paid"

    if bot_token:
        telegram_api_call(bot_token, "answerCallbackQuery", {
            "callback_query_id": cb.get("id"),
            "text": "✅ Marcado como PAID" if not already_paid else "✅ Ya estaba PAID"
        })

    log_event("admin_mark_paid_ok", tenant_id=tenant_id, order_id=order_id, old_status=old_status, already_paid=already_paid)
    return {"ok": True, "order_id": order_id, "already_paid": already_paid}
