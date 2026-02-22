# app/utils.py
import json
import re
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
    if s is None:
        return ""
    s = str(s).strip().lower()
    replacements = {
        "á": "a", "é": "e", "í": "i", "ó": "o", "ú": "u",
        "ä": "a", "ë": "e", "ï": "i", "ö": "o", "ü": "u",
        "ñ": "n",
    }
    for a, b in replacements.items():
        s = s.replace(a, b)
    # mantiene "_" porque \w lo incluye
    s = re.sub(r"[^\w\s-]", "", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s


def log_event(event: str, **fields: Any) -> None:
    # No loguear secretos (por si alguien pasa algo sensible)
    blocked = {"creds", "token", "GCP_CREDENTIALS_JSON", "ADMIN_TOKEN"}
    safe = {k: v for k, v in fields.items() if k not in blocked}
    print(json.dumps({"ts": now_iso_utc(), "event": event, **safe}, ensure_ascii=False))
