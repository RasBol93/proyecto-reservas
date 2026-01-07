import os
import json
import urllib.request
import urllib.parse
from fastapi import FastAPI, Request, Header, HTTPException

app = FastAPI()

# =========================================================
# CONFIG (pensado para múltiples clientes a futuro)
# =========================================================
# Hoy: 1 cliente (default). Mañana: puedes tener "h360", "cliente2", etc.
#
# Variables recomendadas en Render:
# - CLIENT_ID=default
# - TELEGRAM_BOT_TOKEN=xxxxx
# - PUBLIC_BASE_URL=https://proyecto-reservas-idwl.onrender.com
# - WEBHOOK_SECRET=una_clave_larga (opcional pero MUY recomendado)
#
# Para múltiples clientes (futuro):
# - CLIENT_ID=h360
# - TELEGRAM_BOT_TOKEN_H360=...
# - PUBLIC_BASE_URL_H360=...
# - WEBHOOK_SECRET_H360=...
#
# Este código ya soporta ambos esquemas.

def _client_id() -> str:
    return os.getenv("CLIENT_ID", "default").strip() or "default"

def _get_env_for_client(base_name: str, client_id: str) -> str:
    """
    Busca variables con este orden:
    1) BASE_NAME_{CLIENT_ID en mayúsculas}   ej TELEGRAM_BOT_TOKEN_H360
    2) BASE_NAME (fallback)                  ej TELEGRAM_BOT_TOKEN
    """
    suffix = client_id.upper().replace("-", "_")
    v = os.getenv(f"{base_name}_{suffix}")
    if v:
        return v
    return os.getenv(base_name, "")

def _get_token(client_id: str) -> str:
    return _get_env_for_client("TELEGRAM_BOT_TOKEN", client_id).strip()

def _get_public_base_url(client_id: str) -> str:
    return _get_env_for_client("PUBLIC_BASE_URL", client_id).strip().rstrip("/")

def _get_webhook_secret(client_id: str) -> str:
    # Opcional, pero recomendado para que no cualquiera te llame el webhook
    return _get_env_for_client("WEBHOOK_SECRET", client_id).strip()

# =========================================================
# HELPERS TELEGRAM
# =========================================================
def _tg_api(client_id: str, method: str, data: dict):
    token = _get_token(client_id)
    if not token:
        return {"ok": False, "error": f"Missing TELEGRAM_BOT_TOKEN for client '{client_id}'"}

    url = f"https://api.telegram.org/bot{token}/{method}"
    body = urllib.parse.urlencode(data).encode("utf-8")

    req = urllib.request.Request(url, data=body, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except Exception as e:
        return {"ok": False, "error": f"Telegram API error: {type(e).__name__}: {str(e)}"}

def telegram_send_message(client_id: str, chat_id: int, text: str):
    # Usamos sendMessage con form-encoded (simple y robusto)
    return _tg_api(client_id, "sendMessage", {"chat_id": str(chat_id), "text": text})

# =========================================================
# ENDPOINTS
# =========================================================

# 1) Health check
@app.get("/")
def root():
    return {"status": "ok", "service": "proyecto-reservas", "client_id": _client_id()}

# 2) Configurar el webhook desde Render (evita que tu navegador llame api.telegram.org)
@app.get("/setup-webhook")
def setup_webhook():
    client_id = _client_id()
    base_url = _get_public_base_url(client_id)
    if not base_url:
        return {"ok": False, "error": "Missing PUBLIC_BASE_URL env var"}

    webhook_url = f"{base_url}/telegram/webhook"
    secret = _get_webhook_secret(client_id)

    payload = {"url": webhook_url}
    # Header de seguridad: Telegram lo enviará en cada update si lo seteas aquí
    if secret:
        payload["secret_token"] = secret

    set_res = _tg_api(client_id, "setWebhook", payload)
    info_res = _tg_api(client_id, "getWebhookInfo", {})

    return {
        "ok": True,
        "client_id": client_id,
        "webhook_url": webhook_url,
        "setWebhook": set_res,
        "getWebhookInfo": info_res,
        "note": "Si setWebhook ok=true, el webhook quedó configurado.",
    }

# 3) Ver estado del webhook sin cambiar nada
@app.get("/debug/webhook")
def debug_webhook():
    client_id = _client_id()
    info_res = _tg_api(client_id, "getWebhookInfo", {})
    return {"client_id": client_id, "getWebhookInfo": info_res}

# 4) Webhook receptor: aquí Telegram enviará cada mensaje del usuario
@app.post("/telegram/webhook")
async def telegram_webhook(
    request: Request,
    x_telegram_bot_api_secret_token: str | None = Header(default=None),
):
    client_id = _client_id()

    # Si configuraste WEBHOOK_SECRET, validamos que venga el header correcto
    secret = _get_webhook_secret(client_id)
    if secret:
        if not x_telegram_bot_api_secret_token or x_telegram_bot_api_secret_token != secret:
            raise HTTPException(status_code=401, detail="Invalid webhook secret token")

    update = await request.json()

    msg = update.get("message") or {}
    chat = msg.get("chat") or {}
    chat_id = chat.get("id")
    text = (msg.get("text") or "").strip()

    if chat_id:
        if text == "/start":
            telegram_send_message(
                client_id,
                chat_id,
                "Hola 👋 Soy el bot de reservas.\nEscribe: 'reservar' o 'faq'."
            )
        elif text.lower() == "faq":
            telegram_send_message(
                client_id,
                chat_id,
                "FAQ:\n1) Check-in 15:00\n2) Check-out 11:00\n(Esto es demo; luego lo conectamos a tu lógica real)"
            )
        elif text.lower() == "reservar":
            telegram_send_message(
                client_id,
                chat_id,
                "Perfecto ✅ Para reservar dime: fecha de ingreso, fecha de salida y cantidad de personas."
            )
        else:
            telegram_send_message(client_id, chat_id, f"Recibido: {text}")

    return {"ok": True}
