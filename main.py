# =========================
# PROYECTO RESERVAS v2.0
# Admin + Client Telegram Bots
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
from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel, Field

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
    return str(v).strip().lower() in ("true","1","yes","y","si","sí","on")

def log_event(event, **fields):
    print(json.dumps({"ts": now_iso_utc(), "event": event, **fields}, ensure_ascii=False))

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
            raise HTTPException(429,"Rate limit exceeded")
        dq.append(now)

_rate = RateLimiter()

# =========================
# Sheets
# =========================

def get_gspread_client():
    creds = json.loads(os.getenv(ENV_GCP_CREDS_JSON))
    return gspread.service_account_from_dict(creds)

def get_config_spreadsheet(gc):
    return gc.open_by_key(os.getenv(ENV_CONFIG_SPREADSHEET_ID))

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
            "admin_bot_token": r.get("admin_bot_token","").strip(),
            "client_bot_token": r.get("client_bot_token","").strip(),
            "webhook_secret_admin": r.get("webhook_secret_admin","").strip(),
            "webhook_secret_client": r.get("webhook_secret_client","").strip(),
            "admin_chat_id": str(r.get("admin_chat_id","")).strip(),
            "orders_enabled": to_bool(r.get("orders_enabled")),
            "active": to_bool(r.get("active"))
        }

    _TENANTS_CACHE = tenants
    log_event("tenants_loaded", count=len(tenants))
    return tenants

def get_tenant(gc, tenant_id):
    tenants = load_tenants(gc)
    t = tenants.get(tenant_id)
    if not t or not t["active"]:
        raise HTTPException(404,"Tenant not found")
    return t

# =========================
# Telegram API
# =========================

def telegram_api_call(bot_token, method, payload):
    url = f"{TELEGRAM_API_BASE}/bot{bot_token}/{method}"
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers={"Content-Type":"application/json"})
    try:
        with urllib.request.urlopen(req) as resp:
            return json.loads(resp.read())
    except Exception as e:
        log_event("telegram_error", error=str(e))
        return {"ok":False}

# =========================
# Carrito en memoria
# =========================

USER_STATE = {}

def get_user_state(tenant_id, chat_id):
    key = f"{tenant_id}:{chat_id}"
    return USER_STATE.setdefault(key, {"cart":[]})

# =========================
# FastAPI
# =========================

app = FastAPI(title=APP_NAME)

@app.get("/")
def root():
    return {"ok":True}

@app.post("/admin/reload_tenants")
def reload(payload: dict):
    if payload.get("token") != os.getenv(ENV_ADMIN_TOKEN):
        raise HTTPException(403,"Invalid token")
    gc = get_gspread_client()
    load_tenants(gc, force=True)
    return {"ok":True}

# =========================
# CLIENT WEBHOOK
# =========================

@app.post("/telegram/client/webhook/{tenant_id}/{secret}")
async def telegram_client_webhook(tenant_id:str, secret:str, update:Dict):

    gc = get_gspread_client()
    tenant = get_tenant(gc, tenant_id)

    if secret != tenant["webhook_secret_client"]:
        raise HTTPException(403,"Invalid secret")

    bot_token = tenant["client_bot_token"]

    message = update.get("message")
    callback = update.get("callback_query")

    # ========= MENSAJE NORMAL =========
    if message:
        chat_id = message["chat"]["id"]
        text = message.get("text","")

        state = get_user_state(tenant_id, chat_id)

        if text.lower() in ("hola","/start"):
            telegram_api_call(bot_token,"sendMessage",{
                "chat_id":chat_id,
                "text":"Bienvenido 👋\nSelecciona una opción:",
                "reply_markup":{
                    "inline_keyboard":[
                        [{"text":"🍔 Hamburguesa Clásica","callback_data":"add|H01"}],
                        [{"text":"🛒 Ver carrito","callback_data":"cart"}]
                    ]
                }
            })
        return {"ok":True}

    # ========= CALLBACK =========
    if callback:
        data = callback.get("data")
        chat_id = callback["message"]["chat"]["id"]
        state = get_user_state(tenant_id, chat_id)

        if data.startswith("add|"):
            sku = data.split("|")[1]
            state["cart"].append({"sku":sku,"qty":1})
            telegram_api_call(bot_token,"answerCallbackQuery",{
                "callback_query_id":callback["id"],
                "text":"Agregado al carrito"
            })

        if data == "cart":
            items = "\n".join([f"{i['sku']} x{i['qty']}" for i in state["cart"]])
            telegram_api_call(bot_token,"sendMessage",{
                "chat_id":chat_id,
                "text":f"🛒 Carrito:\n{items}\n\nConfirmar?",
                "reply_markup":{
                    "inline_keyboard":[
                        [{"text":"✅ Confirmar","callback_data":"confirm"}]
                    ]
                }
            })

        if data == "confirm":
            order_id = str(int(time.time()))
            telegram_api_call(bot_token,"sendMessage",{
                "chat_id":chat_id,
                "text":f"🎉 Pedido creado!\nID: {order_id}"
            })

        return {"ok":True}

    return {"ok":True}
