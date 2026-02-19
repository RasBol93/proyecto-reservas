# =========================
# PROYECTO RESERVAS v2.0.1
# Base v2.0 (funciona Telegram) + Guardado REAL en Sheets al confirmar
# =========================

import os
import json
import re
import time
import urllib.request
from datetime import datetime, timezone
from typing import Any, Dict, List
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
# Helpers
# =========================

def now_iso_utc():
    return datetime.now(timezone.utc).isoformat()

def normalize(s):
    if s is None:
        return ""
    return str(s).strip().lower()

def to_bool(v):
    return str(v).strip().lower() in ("true", "1", "yes", "y", "si", "sí", "on")

def log_event(event, **fields):
    # OJO: no loguear secretos si puedes evitarlos
    safe = {k: v for k, v in fields.items() if "token" not in k and "secret" not in k and "creds" not in k}
    print(json.dumps({"ts": now_iso_utc(), "event": event, **safe}, ensure_ascii=False))

# =========================
# Rate Limit
# =========================

class RateLimiter:
    def __init__(self):
        self.buckets = {}
        self.window = 60

    def hit(self, key, limit):
        now = time.time()
        dq = self.buckets.setdefault(key, deque())
        while dq and now - dq[0] > self.window:
            dq.popleft()
        if len(dq) >= limit:
            raise HTTPException(429, "Rate limit exceeded")
        dq.append(now)

_rate = RateLimiter()

# =========================
# Sheets
# =========================

def get_gspread_client():
    raw = os.getenv(ENV_GCP_CREDS_JSON, "")
    if not raw:
        raise RuntimeError("Missing env var: GCP_CREDENTIALS_JSON")
    creds = json.loads(raw)

    # Scopes recomendados (Drive+Sheets)
    scopes = [
        "https://www.googleapis.com/auth/spreadsheets",
        "https://www.googleapis.com/auth/drive",
    ]
    return gspread.service_account_from_dict(creds, scopes=scopes)

def get_config_spreadsheet(gc):
    sid = os.getenv(ENV_CONFIG_SPREADSHEET_ID, "")
    if not sid:
        raise RuntimeError("Missing env var: RESERVACIONES_CONFIG")
    return gc.open_by_key(sid)

_TENANTS_CACHE = {}

def load_tenants(gc, force=False):
    global _TENANTS_CACHE
    if _TENANTS_CACHE and not force:
        return _TENANTS_CACHE

    sh = get_config_spreadsheet(gc)
    ws = sh.worksheet("Tenants")
    rows = ws.get_all_records()

    tenants = {}
    for r in rows:
        tid = r.get("tenant_id")
        if not tid:
            continue
        tenants[tid] = {
            "tenant_id": tid,
            "orders_sheet_id": r.get("orders_sheet_id"),
            "admin_bot_token": (r.get("admin_bot_token", "") or "").strip(),
            "client_bot_token": (r.get("client_bot_token", "") or "").strip(),
            "webhook_secret_admin": (r.get("webhook_secret_admin", "") or "").strip(),
            "webhook_secret_client": (r.get("webhook_secret_client", "") or "").strip(),
            "admin_chat_id": str(r.get("admin_chat_id", "") or "").strip(),
            "orders_enabled": to_bool(r.get("orders_enabled")),
            "active": to_bool(r.get("active")),
        }

    _TENANTS_CACHE = tenants
    log_event("tenants_loaded", count=len(tenants))
    return tenants

def get_tenant(gc, tenant_id):
    tenants = load_tenants(gc)
    t = tenants.get(tenant_id)
    if not t or not t["active"]:
        raise HTTPException(404, "Tenant not found")
    return t

def open_orders_spreadsheet(gc, tenant):
    sid = (tenant.get("orders_sheet_id") or "").strip()
    if not sid:
        raise HTTPException(500, f"Tenant {tenant.get('tenant_id')} missing orders_sheet_id")
    try:
        return gc.open_by_key(sid)
    except gspread.exceptions.SpreadsheetNotFound:
        raise HTTPException(
            500,
            f"SpreadsheetNotFound: {sid}. Comparte el sheet con el service account (email del JSON)."
        )

def ensure_orders_headers(ws, required_headers):
    """
    - Si la hoja está vacía (sin headers), crea headers en fila 1.
    - Si ya hay headers, valida que existan los requeridos.
    """
    values = ws.get_all_values()
    if not values or len(values) == 0:
        ws.update("A1", [required_headers])
        return required_headers

    first_row = values[0] if values else []
    if not any(str(x).strip() for x in first_row):
        ws.update("A1", [required_headers])
        return required_headers

    headers_norm = [normalize(h) for h in first_row]
    missing = [h for h in required_headers if normalize(h) not in headers_norm]
    if missing:
        raise HTTPException(500, f"Orders sheet missing headers: {missing}. Headers actuales: {first_row}")
    return first_row

def append_order_row(orders_sh, order_payload):
    """
    order_payload ya viene con keys:
    order_id, created_at, tenant_id, customer_name, customer_contact, items, notes,
    delivery_type, requested_time, status, source, total_amount
    """
    ws = orders_sh.worksheet("Orders")

    REQUIRED = [
        "order_id", "created_at", "tenant_id", "customer_name", "customer_contact",
        "items", "notes", "delivery_type", "requested_time", "status", "source", "total_amount"
    ]

    headers = ensure_orders_headers(ws, REQUIRED)
    headers_norm = [normalize(h) for h in headers]

    row = []
    for h in headers_norm:
        row.append(order_payload.get(h, ""))

    ws.append_row(row, value_input_option="USER_ENTERED")

# =========================
# Telegram API
# =========================

def telegram_api_call(bot_token, method, payload):
    url = f"{TELEGRAM_API_BASE}/bot{bot_token}/{method}"
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=12) as resp:
            raw = resp.read().decode("utf-8")
            return json.loads(raw)
    except Exception as e:
        log_event("telegram_error", method=method, error=str(e))
        return {"ok": False, "error": str(e)}

# =========================
# Carrito en memoria
# =========================

USER_STATE = {}

def get_user_state(tenant_id, chat_id):
    key = f"{tenant_id}:{chat_id}"
    return USER_STATE.setdefault(key, {"cart": []})

# =========================
# FastAPI
# =========================

app = FastAPI(title=APP_NAME)

@app.get("/")
def root():
    return {"ok": True}

@app.post("/admin/reload_tenants")
def reload(payload: dict):
    if payload.get("token") != os.getenv(ENV_ADMIN_TOKEN):
        raise HTTPException(403, "Invalid token")
    gc = get_gspread_client()
    load_tenants(gc, force=True)
    return {"ok": True}

# =========================
# CLIENT WEBHOOK
# =========================

@app.post("/telegram/client/webhook/{tenant_id}/{secret}")
async def telegram_client_webhook(tenant_id: str, secret: str, update: Dict):

    gc = get_gspread_client()
    tenant = get_tenant(gc, tenant_id)

    if secret != tenant["webhook_secret_client"]:
        raise HTTPException(403, "Invalid secret")

    bot_token = tenant["client_bot_token"]
    if not bot_token:
        log_event("client_missing_token", tenant_id=tenant_id)
        return {"ok": True}

    message = update.get("message")
    callback = update.get("callback_query")

    # ========= MENSAJE NORMAL =========
    if message:
        chat_id = message["chat"]["id"]
        text = message.get("text", "")

        state = get_user_state(tenant_id, chat_id)

        if normalize(text) in ("hola", "/start", "start"):
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
        data = callback.get("data", "")
        chat_id = callback["message"]["chat"]["id"]
        state = get_user_state(tenant_id, chat_id)

        # Acknowledge rápido para que no quede “cargando”
        telegram_api_call(bot_token, "answerCallbackQuery", {
            "callback_query_id": callback["id"],
            "text": "OK"
        })

        if data.startswith("add|"):
            sku = data.split("|")[1]
            state["cart"].append({"sku": sku, "qty": 1})
            telegram_api_call(bot_token, "sendMessage", {
                "chat_id": chat_id,
                "text": f"✅ Agregado {sku} al carrito"
            })
            return {"ok": True}

        if data == "cart":
            if not state["cart"]:
                telegram_api_call(bot_token, "sendMessage", {
                    "chat_id": chat_id,
                    "text": "Tu carrito está vacío."
                })
                return {"ok": True}

            items = "\n".join([f"{i['sku']} x{i['qty']}" for i in state["cart"]])
            telegram_api_call(bot_token, "sendMessage", {
                "chat_id": chat_id,
                "text": f"🛒 Carrito:\n{items}\n\nConfirmar?",
                "reply_markup": {
                    "inline_keyboard": [
                        [{"text": "✅ Confirmar", "callback_data": "confirm"}]
                    ]
                }
            })
            return {"ok": True}

        if data == "confirm":
            # ✅ Aquí está el cambio: GUARDAR EN SHEETS
            try:
                order_id = str(int(time.time()))
                created_at = now_iso_utc()

                # Demo simple (después lo hacemos “pro”)
                customer_name = "Cliente Telegram"
                customer_contact = str(chat_id)
                notes = ""
                delivery_type = "pickup"
                requested_time = "ahora"
                status = "PENDING_PAYMENT"
                source = "telegram_client"
                total_amount = ""  # aún no calculamos (sin menú/price en esta base)

                order_payload = {
                    "order_id": order_id,
                    "created_at": created_at,
                    "tenant_id": tenant_id,
                    "customer_name": customer_name,
                    "customer_contact": customer_contact,
                    "items": json.dumps(state["cart"], ensure_ascii=False),
                    "notes": notes,
                    "delivery_type": delivery_type,
                    "requested_time": requested_time,
                    "status": status,
                    "source": source,
                    "total_amount": total_amount,
                }

                orders_sh = open_orders_spreadsheet(gc, tenant)
                append_order_row(orders_sh, order_payload)

                log_event("order_saved_in_sheets", tenant_id=tenant_id, order_id=order_id)

                # Limpia carrito SOLO si guardó OK
                state["cart"] = []

                telegram_api_call(bot_token, "sendMessage", {
                    "chat_id": chat_id,
                    "text": f"🎉 Pedido creado y guardado ✅\nID: {order_id}"
                })

            except HTTPException as he:
                log_event("sheets_http_error", tenant_id=tenant_id, detail=str(he.detail))
                telegram_api_call(bot_token, "sendMessage", {
                    "chat_id": chat_id,
                    "text": f"❌ No pude guardar en Sheets: {he.detail}"
                })

            except Exception as e:
                log_event("sheets_error", tenant_id=tenant_id, error=str(e))
                telegram_api_call(bot_token, "sendMessage", {
                    "chat_id": chat_id,
                    "text": "❌ Error guardando en Sheets. Revisa permisos del service account."
                })

            return {"ok": True}

        return {"ok": True}

    return {"ok": True}
