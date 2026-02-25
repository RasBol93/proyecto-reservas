# app/utils.py

import json
import re
import unicodedata
from datetime import datetime, timezone
from typing import Any


def now_iso_utc() -> str:
    return datetime.now(timezone.utc).isoformat()


def to_bool(v: Any) -> bool:
    if isinstance(v, bool):
        return v
    if v is None:
        return False
    s = str(v).strip().lower()
    return s in ("true", "1", "yes", "y", "si", "sí", "on")


def normalize(s: Any) -> str:
    """
    Normaliza para matching robusto:
    - lower
    - sin tildes/diacríticos (unicode)
    - quita puntuación rara pero mantiene "_" "-" y espacios
    """
    if s is None:
        return ""
    s = str(s).strip().lower()

    # quita tildes/diacríticos
    s = unicodedata.normalize("NFKD", s)
    s = "".join(ch for ch in s if not unicodedata.combining(ch))

    # permite: letras/números/_/-/espacios
    s = re.sub(r"[^\w\s-]", "", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s


def log_event(event: str, **fields: Any) -> None:
    # bloquea keys sensibles
    blocked_keys = {
        "creds", "token", "authorization", "password",
        "GCP_CREDENTIALS_JSON", "ADMIN_TOKEN",
        "admin_bot_token", "client_bot_token",
        "webhook_secret_admin", "webhook_secret_client",
    }
    safe = {}
    for k, v in fields.items():
        if k in blocked_keys:
            continue
        safe[k] = v
    print(json.dumps({"ts": now_iso_utc(), "event": event, **safe}, ensure_ascii=False))
