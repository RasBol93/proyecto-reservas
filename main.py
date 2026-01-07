import os
import json
import urllib.request
import urllib.parse
from fastapi import FastAPI, Request, Header, HTTPException

app = FastAPI()

# =========================
# 1) Config general
# =========================
PUBLIC_BASE_URL = os.getenv("PUBLIC_BASE_URL", "").strip()
ADMIN_TOKEN = os.getenv("ADMIN_TOKEN", "").strip()

# Botones (Reply Keyboard)
BTN_MENU = "📄 Ver menú"
BTN_FAQ = "❓ Preguntas frecuentes"
BTN_RESERVAR = "📅 Reservar"
BTN_AGENT = "👤 Hablar con alguien"
BTN_CANCEL = "✖️ Cancelar"

# Estado en memoria (simple)
# clave: (tenant, chat_id)
SESSIONS = {}  # (tenant, chat_id) -> {"step": str, "data": dict}

# =========================
# 2) Config por restaurante (tenant)
# =========================
def get_env(name: str, tenant: str, default: str = "") -> str:
    return os.getenv(f"{name}_{tenant.upper()}", os.getenv(name, default)).strip()

def tenant_config(tenant: str) -> dict:
    # tenant: "r1" o "r2"
    t = tenant.upper()
    return {
        "tenant": tenant,
        "token": get_env("TELEGRAM_BOT_TOKEN", t),
        "webhook_secret": get_env("WEBHOOK_SECRET", t),
        "menu_pdf_url": get_env("MENU_PDF_URL", t),
        "faq_text": get_env("FAQ_TEXT", t, default="Aún no se configuró el FAQ."),
        "admin_chat_id": get_env("ADMIN_CHAT_ID", t),  # string, luego lo parseamos
    }

def require_token(cfg: dict):
    if not cfg["token"]:
        raise RuntimeError(f"Missing TELEGRAM_BOT_TOKEN_{cfg['tenant'].upper()}")

# =========================
# 3) Telegram helpers (por tenant)
# =========================
def telegram_api(cfg: dict, method: str, payload: dict):
    require_token(cfg)
    url = f"https://api.telegram.org/bot{cfg['token']}/{method}"
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url, data=data, headers={"Content-Type": "application/json"}, method="POST"
    )
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.loads(r.read().decode("utf-8"))

def main_keyboard():
    return {
        "keyboard": [
            [{"text": BTN_MENU}, {"text": BTN_FAQ}],
            [{"text": BTN_RESERVAR}, {"text": BTN_AGENT}],
        ],
        "resize_keyboard": True,
        "one_time_keyboard": False,
    }

def reservation_keyboard():
    return {
        "keyboard": [[{"text": BTN_CANCEL}]],
        "resize_keyboard": True,
        "one_time_keyboard": False,
    }

def send_message(cfg: dict, chat_id: int, text: str, keyboard: dict | None = None):
    payload = {"chat_id": chat_id, "text": text}
    if keyboard:
        payload["reply_markup"] = keyboard
    try:
        telegram_api(cfg, "sendMessage", payload)
    except Exception as e:
        print(f"[ERROR] sendMessage ({cfg['tenant']}): {e}")

def send_document(cfg: dict, chat_id: int, doc_url: str, caption: str = ""):
    # Telegram sendDocument acepta URL pública en "document"
    payload = {"chat_id": chat_id, "document": doc_url}
    if caption:
        payload["caption"] = caption
    try:
        telegram_api(cfg, "sendDocument", payload)
    except Exception as e:
        print(f"[ERROR] sendDocument ({cfg['tenant']}): {e}")

def notify_admin(cfg: dict, text: str):
    admin_chat_id = cfg.get("admin_chat_id", "").strip()
    if not admin_chat_id:
        return
    try:
        cid = int(admin_chat_id)
        send_message(cfg, cid, text)
    except Exception:
        pass

# =========================
# 4) Health check
# =========================
@app.get("/")
def root():
    return {"status": "ok", "service": "proyecto-reservas", "bots": ["r1", "r2"]}

# =========================
# 5) Setup webhooks (2 bots) — desde Render
# =========================
@app.get("/setup-webhooks")
def setup_webhooks():
    if not PUBLIC_BASE_URL:
        return {"ok": False, "error": "Missing PUBLIC_BASE_URL env var"}

    results = {}

    for tenant in ["r1", "r2"]:
        cfg = tenant_config(tenant)
        if not cfg["token"]:
            results[tenant] = {"ok": False, "error": f"Missing TELEGRAM_BOT_TOKEN_{tenant.upper()}"}
            continue

        webhook_url = f"{PUBLIC_BASE_URL}/telegram/webhook/{tenant}"
        payload = {"url": webhook_url}

        # Si hay secret, lo usamos (Telegram lo enviará en header X-Telegram-Bot-Api-Secret-Token)
        if cfg["webhook_secret"]:
            payload["secret_token"] = cfg["webhook_secret"]

        try:
            set_res = telegram_api(cfg, "setWebhook", payload)
            info_res = telegram_api(cfg, "getWebhookInfo", {})
            results[tenant] = {
                "ok": True,
                "webhook_url": webhook_url,
                "setWebhook": set_res,
                "getWebhookInfo": info_res,
            }
        except Exception as e:
            results[tenant] = {"ok": False, "error": str(e), "webhook_url": webhook_url}

    return {"ok": True, "results": results}

# =========================
# 6) Webhook handlers (uno por bot)
# =========================
@app.post("/telegram/webhook/r1")
async def telegram_webhook_r1(
    request: Request,
    x_telegram_bot_api_secret_token: str | None = Header(default=None),
):
    return await handle_update("r1", request, x_telegram_bot_api_secret_token)

@app.post("/telegram/webhook/r2")
async def telegram_webhook_r2(
    request: Request,
    x_telegram_bot_api_secret_token: str | None = Header(default=None),
):
    return await handle_update("r2", request, x_telegram_bot_api_secret_token)

# =========================
# 7) Lógica común (por tenant)
# =========================
def session_key(tenant: str, chat_id: int):
    return (tenant, chat_id)

def reset_session(tenant: str, chat_id: int):
    SESSIONS.pop(session_key(tenant, chat_id), None)

def get_session(tenant: str, chat_id: int):
    return SESSIONS.get(session_key(tenant, chat_id))

def set_session(tenant: str, chat_id: int, step: str, data: dict):
    SESSIONS[session_key(tenant, chat_id)] = {"step": step, "data": data}

async def handle_update(tenant: str, request: Request, secret_header: str | None):
    cfg = tenant_config(tenant)

    # Validar secret_token si está configurado
    expected_secret = cfg.get("webhook_secret", "").strip()
    if expected_secret:
        if not secret_header or secret_header != expected_secret:
            raise HTTPException(status_code=401, detail="Invalid webhook secret token")

    update = await request.json()
    msg = update.get("message") or {}
    chat = msg.get("chat") or {}
    chat_id = chat.get("id")
    text = (msg.get("text") or "").strip()

    if not chat_id:
        return {"ok": True}

    # /start => menú principal (botones)
    if text == "/start":
        reset_session(tenant, chat_id)
        send_message(
            cfg,
            chat_id,
            "¡Hola! 👋 Soy el bot de reservas.\nElige una opción:",
            keyboard=main_keyboard(),
        )
        return {"ok": True}

    # Botones principales
    if text == BTN_MENU:
        if not cfg["menu_pdf_url"]:
            send_message(cfg, chat_id, "Aún no está configurado el PDF del menú.", keyboard=main_keyboard())
        else:
            send_document(cfg, chat_id, cfg["menu_pdf_url"], caption="Aquí tienes el menú 📄")
            send_message(cfg, chat_id, "¿Deseas algo más?", keyboard=main_keyboard())
        return {"ok": True}

    if text == BTN_FAQ:
        send_message(cfg, chat_id, cfg["faq_text"], keyboard=main_keyboard())
        return {"ok": True}

    if text == BTN_AGENT:
        send_message(cfg, chat_id, "Perfecto. Escribe tu mensaje y te contactará un encargado.", keyboard=main_keyboard())
        # Notificación al admin (por tenant)
        notify_admin(cfg, f"[{tenant.upper()}] Un cliente pidió hablar con alguien. ChatID: {chat_id}")
        return {"ok": True}

    if text == BTN_RESERVAR:
        # Inicia flujo de reserva SIN elegir restaurante (tenant ya está fijado por el bot)
        set_session(tenant, chat_id, "ASK_DATE", {})
        send_message(cfg, chat_id, "Perfecto. ¿Para qué fecha? (formato: YYYY-MM-DD)", keyboard=reservation_keyboard())
        return {"ok": True}

    # Cancelar en cualquier punto
    if text == BTN_CANCEL:
        reset_session(tenant, chat_id)
        send_message(cfg, chat_id, "Reserva cancelada. ¿Qué deseas hacer ahora?", keyboard=main_keyboard())
        return {"ok": True}

    # Flujo de reserva (simple, como en el PDF: fecha, hora, personas, nombre, teléfono, confirmación)
    s = get_session(tenant, chat_id)
    if not s:
        # Si no está en flujo, devolvemos al menú
        send_message(cfg, chat_id, "Elige una opción:", keyboard=main_keyboard())
        return {"ok": True}

    step = s["step"]
    data = s["data"]

    if step == "ASK_DATE":
        data["date"] = text
        set_session(tenant, chat_id, "ASK_TIME", data)
        send_message(cfg, chat_id, "¿A qué hora? (ej: 19:30)", keyboard=reservation_keyboard())
        return {"ok": True}

    if step == "ASK_TIME":
        data["time"] = text
        set_session(tenant, chat_id, "ASK_PEOPLE", data)
        send_message(cfg, chat_id, "¿Para cuántas personas?", keyboard=reservation_keyboard())
        return {"ok": True}

    if step == "ASK_PEOPLE":
        data["people"] = text
        set_session(tenant, chat_id, "ASK_NAME", data)
        send_message(cfg, chat_id, "¿A nombre de quién?", keyboard=reservation_keyboard())
        return {"ok": True}

    if step == "ASK_NAME":
        data["name"] = text
        set_session(tenant, chat_id, "ASK_PHONE", data)
        send_message(cfg, chat_id, "¿Tu número de teléfono?", keyboard=reservation_keyboard())
        return {"ok": True}

    if step == "ASK_PHONE":
        data["phone"] = text
        set_session(tenant, chat_id, "CONFIRM", data)
        resumen = (
            f"Confirma tu reserva:\n"
            f"📅 Fecha: {data['date']}\n"
            f"🕒 Hora: {data['time']}\n"
            f"👥 Personas: {data['people']}\n"
            f"👤 Nombre: {data['name']}\n"
            f"📞 Tel: {data['phone']}\n\n"
            f"Responde: SI o NO"
        )
        send_message(cfg, chat_id, resumen, keyboard=reservation_keyboard())
        return {"ok": True}

    if step == "CONFIRM":
        if text.strip().lower() in ["si", "sí"]:
            # Aquí luego conectamos Google Calendar por tenant.
            # Por ahora confirmamos y notificamos admin del tenant.
            send_message(cfg, chat_id, "✅ Reserva confirmada. ¡Gracias!", keyboard=main_keyboard())
            notify_admin(
                cfg,
                f"✅ Nueva reserva [{tenant.upper()}]\n"
                f"{data['date']} {data['time']} - {data['people']} pax\n"
                f"Nombre: {data['name']} - Tel: {data['phone']}"
            )
            reset_session(tenant, chat_id)
        else:
            send_message(cfg, chat_id, "Reserva no confirmada. Si deseas, vuelve a empezar.", keyboard=main_keyboard())
            reset_session(tenant, chat_id)
        return {"ok": True}

    # fallback
    send_message(cfg, chat_id, "Elige una opción:", keyboard=main_keyboard())
    return {"ok": True}
