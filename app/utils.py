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

    try:
        s = str(s)
    except Exception:
        return ""

    s = s.strip().lower()
    if not s:
        return ""

    s = unicodedata.normalize("NFKD", s)
    s = "".join(ch for ch in s if not unicodedata.combining(ch))

    s = re.sub(r"[^\w\s-]", "", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s


def _is_sensitive_key(key: str) -> bool:
    k = str(key or "").strip().lower()

    blocked_exact = {
        "creds",
        "token",
        "authorization",
        "password",
        "gcp_credentials_json",
        "admin_token",
        "alert_bot_token",
        "alert_chat_id",
        "admin_bot_token",
        "client_bot_token",
        "owner_bot_token",
        "webhook_secret_admin",
        "webhook_secret_client",
        "webhook_secret_owner",
    }

    if k in blocked_exact:
        return True

    blocked_fragments = (
        "token",
        "secret",
        "password",
        "authorization",
        "credential",
        "cookie",
        "api_key",
        "apikey",
        "bearer",
    )

    return any(fragment in k for fragment in blocked_fragments)


def _safe_log_value(value: Any) -> Any:
    """
    Convierte valores a algo serializable y compacto para logging.
    """
    if value is None:
        return None

    if isinstance(value, (str, int, float, bool)):
        return value

    if isinstance(value, (list, tuple)):
        safe_items = []
        for item in value[:20]:
            safe_items.append(_safe_log_value(item))
        if len(value) > 20:
            safe_items.append(f"...(+{len(value) - 20} more)")
        return safe_items

    if isinstance(value, dict):
        out = {}
        count = 0
        for k, v in value.items():
            count += 1
            if count > 30:
                out["..."] = f"+{len(value) - 30} more keys"
                break
            k_str = str(k)
            if _is_sensitive_key(k_str):
                continue
            out[k_str] = _safe_log_value(v)
        return out

    try:
        return str(value)
    except Exception:
        return "<unserializable>"


def log_event(event: str, **fields: Any) -> None:
    safe = {}

    for k, v in fields.items():
        if _is_sensitive_key(k):
            continue
        safe[str(k)] = _safe_log_value(v)

    payload = {
        "ts": now_iso_utc(),
        "event": str(event or "").strip(),
        **safe,
    }

    try:
        print(json.dumps(payload, ensure_ascii=False, default=str))
    except Exception:
        try:
            fallback = {
                "ts": now_iso_utc(),
                "event": str(event or "").strip(),
                "log_error": "json_dump_failed",
            }
            print(json.dumps(fallback, ensure_ascii=False))
        except Exception:
            # último fallback: no romper el sistema por logging
            pass
