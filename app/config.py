# app/config.py

import os

# =========================
# App metadata
# =========================

APP_NAME = "proyecto-reservas"
APP_VERSION = "2.0.2-modular-stable"

# =========================
# Environment variable names
# (estos son los NOMBRES de las env vars)
# =========================

ENV_CONFIG_SPREADSHEET_ID = "RESERVACIONES_CONFIG"
ENV_GCP_CREDS_JSON = "GCP_CREDENTIALS_JSON"
ENV_ADMIN_TOKEN = "ADMIN_TOKEN"

# =========================
# Google Sheets config
# =========================

TENANTS_SHEET_NAME = "Tenants"

# =========================
# Limits
# =========================

MAX_ITEMS_PER_ORDER = 30
MAX_NAME_LEN = 80
MAX_CONTACT_LEN = 30
MAX_REQUESTED_TIME_LEN = 60
MAX_SOURCE_LEN = 20

ALLOWED_DELIVERY_TYPES = {"pickup"}
ALLOWED_SOURCES = {"api", "swagger", "telegram", "whatsapp"}

# =========================
# Rate limits (por tenant, por minuto)
# =========================

RL_MENU_PER_MIN = 120
RL_CREATE_PER_MIN = 60
RL_MARKPAID_PER_MIN = 60

# =========================
# Telegram
# =========================

TELEGRAM_API_BASE = "https://api.telegram.org"

# =========================
# Helpers
# =========================

def env_required(name: str) -> str:
    """
    Lee una variable de entorno y lanza error si no existe.
    """
    value = os.getenv(name, "").strip()
    if not value:
        raise RuntimeError(f"Missing env var: {name}")
    return value
