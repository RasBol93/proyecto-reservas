import os
import json
import urllib.request
from fastapi import FastAPI, Request

app = FastAPI()

# 1) Health check (para que tú veas que el server está vivo)
@app.get("/")
def root():
    return {"status": "ok", "service": "proyecto-reservas"}

def telegram_send_message(chat_id: int, text: str):
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    if not token:
        # Si falta el token, no hacemos nada (evita crasheos)
        return

    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = {"chat_id": chat_id, "text": text}

    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        urllib.request.urlopen(req, timeout=10).read()
    except Exception:
        # No tiramos error para que no falle el webhook
        pass

# 2) Webhook: aquí Telegram enviará cada mensaje del usuario
@app.post("/telegram/webhook")
async def telegram_webhook(request: Request):
    update = await request.json()

    # Estructura típica:
    # update["message"]["chat"]["id"]
    # update["message"]["text"]
    msg = update.get("message") or {}
    chat = msg.get("chat") or {}
    chat_id = chat.get("id")
    text = (msg.get("text") or "").strip()

    if chat_id:
        if text == "/start":
            telegram_send_message(chat_id, "Hola 👋 Soy el bot de reservas. Escribe 'reservar' o 'faq'.")
        else:
            telegram_send_message(chat_id, f"Recibido: {text}")

    return {"ok": True}
