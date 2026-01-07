import os
import json
import urllib.request
import urllib.parse
from fastapi import FastAPI, Request, HTTPException

app = FastAPI()

# =========================
# 1) Config / Variables
# =========================
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
PUBLIC_BASE_URL = os.getenv("PUBLIC_BASE_URL", "")  # ej: https://proyecto-reservas-idwl.onrender.com
ADMIN_TOKEN = os.getenv("ADMIN_TOKEN", "")          # opcional pero recomendado

# =========================
# 2) Estado en memoria (simple)
#    OJO: esto se borra si Render reinicia.
# =========================
SESSIONS = {}  # chat_id -> dict con estado y datos

RESTAURANTS = ["R1", "R2"]

# Flujo de pasos de reserva
STEPS = [
    "choose_restaurant",
    "date",
    "time",
    "people",
    "name",
    "phone",
    "confirm",
]

# =========================
# 3) Utilidades Telegram
# =========================
def telegram_api(method: str, payload: dict):
    """Llama a la API de Telegram."""
    if not TELEGRAM_BOT_TOKEN:
        raise RuntimeError("Missing TELEGRAM_BOT_TOKEN")

    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/{method}"
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=15) as r:
        return json.loads(r.read().decode("utf-8"))

def telegram_send_message(chat_id: int, text: str):
    """Envía mensaje simple."""
    try:
        telegram_api("sendMessage", {"chat_id": chat_id, "text": text})
    except Exception as e:
        # No hacemos crash del webhook
        print(f"[ERROR] sendMessage failed: {e}")

def menu_text():
    return (
        "📌 Menú\n"
        "1) Reservar\n"
        "2) Ver disponibilidad\n"
        "3) Preguntas frecuentes\n"
        "4) Hablar con un agente\n\n"
        "Escribe el número (1-4)."
    )

def reset_session(chat_id: int):
    SESSIONS[chat_id] = {
        "step": None,
        "data": {
            "restaurant": None,
            "date": None,
            "time": None,
            "people": None,
            "name": None,
            "phone": None,
        }
    }

def start_reservation(chat_id: int):
    reset_session(chat_id)
    SESSIONS[chat_id]["step"] = "choose_restaurant"
    telegram_send_message(chat_id, "🍽️ ¿Para cuál restaurante quieres reservar?\nA) R1\nB) R2\n\nResponde A o B.")

def normalize(text: str) -> str:
    return (text or "").strip()

# =========================
# 4) Rutas de salud / debug
# =========================
@app.get("/")
def root():
    return {"status": "ok", "service": "proyecto-reservas"}

@app.get("/debug/env")
def debug_env():
    # Para verificar rápidamente si Render tiene las variables
    return {
        "has_TELEGRAM_BOT_TOKEN": bool(TELEGRAM_BOT_TOKEN),
        "PUBLIC_BASE_URL": PUBLIC_BASE_URL,
        "has_ADMIN_TOKEN": bool(ADMIN_TOKEN),
    }

# =========================
# 5) Admin: setWebhook desde el servidor (útil si tu red bloquea Telegram)
# =========================
def require_admin(token: str):
    if not ADMIN_TOKEN:
        raise HTTPException(status_code=400, detail="ADMIN_TOKEN not configured")
    if token != ADMIN_TOKEN:
        raise HTTPException(status_code=403, detail="Invalid admin token")

@app.post("/admin/telegram/setup")
async def admin_telegram_setup(request: Request):
    body = await request.json()
    token = body.get("token", "")
    require_admin(token)

    if not PUBLIC_BASE_URL:
        return {"ok": False, "error": "Missing PUBLIC_BASE_URL env var"}

    webhook_url = f"{PUBLIC_BASE_URL.rstrip('/')}/telegram/webhook"

    # setWebhook
    set_res = telegram_api("setWebhook", {"url": webhook_url})

    # getWebhookInfo
    info_res = telegram_api("getWebhookInfo", {})

    return {
        "ok": True,
        "webhook_url": webhook_url,
        "setWebhook": set_res,
        "getWebhookInfo": info_res,
        "note": "Si setWebhook ok=true, el webhook quedó configurado."
    }

# =========================
# 6) Tu webhook de Telegram
# =========================
@app.post("/telegram/webhook")
async def telegram_webhook(request: Request):
    update = await request.json()

    msg = update.get("message") or {}
    chat = msg.get("chat") or {}
    chat_id = chat.get("id")
    text = normalize(msg.get("text"))

    # Logs útiles (los ves en Render -> Logs)
    print(f"[IN] chat_id={chat_id} text={text}")

    if not chat_id:
        return {"ok": True}

    # Asegura sesión
    if chat_id not in SESSIONS:
        reset_session(chat_id)

    # Comandos base
    if text.lower() in ["/start", "menu"]:
        telegram_send_message(chat_id, "Hola 👋 Soy el bot de reservas.")
        telegram_send_message(chat_id, menu_text())
        SESSIONS[chat_id]["step"] = None
        return {"ok": True}

    if text.lower() in ["cancelar", "cancel", "stop"]:
        reset_session(chat_id)
        telegram_send_message(chat_id, "✅ Listo. Cancelé el proceso.")
        telegram_send_message(chat_id, menu_text())
        return {"ok": True}

    # Si está en un flujo de reserva, seguimos el flujo
    step = SESSIONS[chat_id].get("step")

    if step:
        handle_reservation_step(chat_id, text)
        return {"ok": True}

    # Si no está en flujo, interpretamos menú
    if text in ["1", "reservar", "reserva"]:
        start_reservation(chat_id)
        return {"ok": True}

    if text in ["2", "disponibilidad", "ver disponibilidad"]:
        telegram_send_message(
            chat_id,
            "📅 Disponibilidad (demo):\n"
            "- R1: Hoy 19:00 / 20:00\n"
            "- R2: Hoy 18:30 / 21:00\n\n"
            "Si quieres reservar, escribe 1."
        )
        return {"ok": True}

    if text in ["3", "faq", "preguntas", "preguntas frecuentes"]:
        telegram_send_message(
            chat_id,
            "❓ Preguntas frecuentes (demo):\n"
            "- Horario: 12:00 a 23:00\n"
            "- Reservas: hasta 10 personas por mesa (consultar si más)\n"
            "- Cancelaciones: hasta 2 horas antes\n\n"
            "Para reservar, escribe 1."
        )
        return {"ok": True}

    if text in ["4", "agente", "hablar con un agente"]:
        telegram_send_message(
            chat_id,
            "👤 Ok. Para hablar con un agente, escribe tu nombre y tu consulta.\n"
            "(En la siguiente versión aquí enviamos aviso al staff.)"
        )
        return {"ok": True}

    # Default si no entendió
    telegram_send_message(chat_id, "No entendí 😅")
    telegram_send_message(chat_id, menu_text())
    return {"ok": True}


def handle_reservation_step(chat_id: int, text: str):
    """Máquina de estados simple para guiar la reserva."""
    session = SESSIONS[chat_id]
    step = session["step"]
    data = session["data"]

    # Paso 1: elegir restaurante
    if step == "choose_restaurant":
        t = text.lower()
        if t in ["a", "r1", "1"]:
            data["restaurant"] = "R1"
        elif t in ["b", "r2", "2"]:
            data["restaurant"] = "R2"
        else:
            telegram_send_message(chat_id, "Por favor responde A (R1) o B (R2).")
            return

        session["step"] = "date"
        telegram_send_message(chat_id, f"✅ Perfecto. Restaurante: {data['restaurant']}\n\n📅 ¿Qué fecha? (Ej: 2026-01-10)")
        return

    # Paso 2: fecha
    if step == "date":
        # Validación simple (puedes mejorar después)
        if len(text) < 8:
            telegram_send_message(chat_id, "Fecha inválida. Ejemplo correcto: 2026-01-10")
            return
        data["date"] = text
        session["step"] = "time"
        telegram_send_message(chat_id, "🕒 ¿A qué hora? (Ej: 19:30)")
        return

    # Paso 3: hora
    if step == "time":
        if ":" not in text:
            telegram_send_message(chat_id, "Hora inválida. Ejemplo: 19:30")
            return
        data["time"] = text
        session["step"] = "people"
        telegram_send_message(chat_id, "👥 ¿Para cuántas personas?")
        return

    # Paso 4: personas
    if step == "people":
        try:
            n = int(text)
            if n <= 0:
                raise ValueError()
        except ValueError:
            telegram_send_message(chat_id, "Escribe un número válido (ej: 2, 4, 6).")
            return

        data["people"] = n
        session["step"] = "name"
        telegram_send_message(chat_id, "🧑 ¿A nombre de quién?")
        return

    # Paso 5: nombre
    if step == "name":
        if len(text) < 2:
            telegram_send_message(chat_id, "Escribe un nombre válido.")
            return
        data["name"] = text
        session["step"] = "phone"
        telegram_send_message(chat_id, "📞 ¿Tu teléfono? (solo número o con +)")
        return

    # Paso 6: teléfono
    if step == "phone":
        # Validación suave
        cleaned = text.replace(" ", "")
        if len(cleaned) < 6:
            telegram_send_message(chat_id, "Teléfono muy corto. Intenta de nuevo.")
            return
        data["phone"] = text
        session["step"] = "confirm"

        summary = (
            "✅ Confirma tu reserva:\n"
            f"- Restaurante: {data['restaurant']}\n"
            f"- Fecha: {data['date']}\n"
            f"- Hora: {data['time']}\n"
            f"- Personas: {data['people']}\n"
            f"- Nombre: {data['name']}\n"
            f"- Teléfono: {data['phone']}\n\n"
            "Responde: SI para confirmar o NO para cancelar."
        )
        telegram_send_message(chat_id, summary)
        return

    # Paso 7: confirmación
    if step == "confirm":
        t = text.lower()
        if t in ["si", "sí", "s", "ok", "confirmar"]:
            # Aquí luego guardaremos en Google Sheets / base de datos
            telegram_send_message(chat_id, "🎉 ¡Listo! Tu reserva fue registrada (demo).")
            telegram_send_message(chat_id, menu_text())
            reset_session(chat_id)
            return

        if t in ["no", "n", "cancelar"]:
            telegram_send_message(chat_id, "✅ Ok, cancelé la reserva.")
            telegram_send_message(chat_id, menu_text())
            reset_session(chat_id)
            return

        telegram_send_message(chat_id, "Responde SI para confirmar o NO para cancelar.")
        return
