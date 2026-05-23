# app/config.py

import os

APP_NAME = "proyecto-reservas"
APP_VERSION = "2.0.2-modular-stable"

# =========================================================
# ENV VARS
# =========================================================

ENV_CONFIG_SPREADSHEET_ID = "RESERVACIONES_CONFIG"
ENV_GCP_CREDS_JSON = "GCP_CREDENTIALS_JSON"
ENV_ADMIN_TOKEN = "ADMIN_TOKEN"
ENV_DASHBOARD_APP_BASE_URL = "DASHBOARD_APP_BASE_URL"
ENV_R2_ACCOUNT_ID = "R2_ACCOUNT_ID"
ENV_R2_ACCESS_KEY_ID = "R2_ACCESS_KEY_ID"
ENV_R2_SECRET_ACCESS_KEY = "R2_SECRET_ACCESS_KEY"
ENV_R2_BUCKET_NAME = "R2_BUCKET_NAME"
ENV_R2_PUBLIC_BASE_URL = "R2_PUBLIC_BASE_URL"

# 👉 NUEVO (ALERTAS)
ENV_ALERT_BOT_TOKEN = "ALERT_BOT_TOKEN"
ENV_ALERT_CHAT_ID = "ALERT_CHAT_ID"


# =========================================================
# SHEETS
# =========================================================

TENANTS_SHEET_NAME = "Tenants"
TENANTS_SHEET = TENANTS_SHEET_NAME


# =========================================================
# LIMITS
# =========================================================

MAX_ITEMS_PER_ORDER = 30
MAX_NAME_LEN = 80
MAX_CONTACT_LEN = 30
MAX_REQUESTED_TIME_LEN = 60
MAX_SOURCE_LEN = 20


# =========================================================
# ENUMS
# =========================================================

ALLOWED_DELIVERY_TYPES = {"pickup"}
ALLOWED_SOURCES = {"api", "swagger", "telegram", "whatsapp", "webapp"}


# =========================================================
# RATE LIMITS
# =========================================================

RL_MENU_PER_MIN = 120
RL_CREATE_PER_MIN = 60
RL_MARKPAID_PER_MIN = 60


# =========================================================
# TELEGRAM
# =========================================================

TELEGRAM_API_BASE = "https://api.telegram.org"


# =========================================================
# HELPERS
# =========================================================

def env_required(name: str) -> str:
    v = os.getenv(name, "").strip()
    if not v:
        raise RuntimeError(f"Missing env var: {name}")
    return v


def env_optional(name: str, default: str = "") -> str:
    return os.getenv(name, default).strip()


# =========================================================
# ALERT CONFIG (NUEVO)
# =========================================================

def get_alert_config():
    """
    Devuelve configuración de alertas (si existe).
    """
    token = env_optional(ENV_ALERT_BOT_TOKEN)
    chat_id = env_optional(ENV_ALERT_CHAT_ID)

    return {
        "enabled": bool(token and chat_id),
        "bot_token": token,
        "chat_id": int(chat_id) if chat_id.isdigit() else None,
    }
