import json
import re
from datetime import datetime, timezone
from typing import Any, Dict

from fastapi import HTTPException

from app.config import (
    ENV_ADMIN_TOKEN,
    MAX_CONTACT_LEN,
    MAX_REQUESTED_TIME_LEN,
    MAX_SOURCE_LEN,
    ALLOWED_SOURCES,
    ALLOWED_DELIVERY_TYPES,
    TENANT_ID_RE,
    ORDER_ID_RE,
)

import os


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
    s = re.sub(r"[^\w\s-]", "", s)   # mantiene "_" porque \w lo incluye
    s = re.sub(r"\s+", " ", s).strip()
    return s


def log_event(event: str, **fields: Any) -> None:
    safe = {k: v for k, v in fields.items() if k not in ("creds", "token", "GCP_CREDENTIALS_JSON", "ADMIN_TOKEN")}
    print(json.dumps({"ts": now_iso_utc(), "event": event, **safe}, ensure_ascii=False))


def validate_tenant_id(tenant_id: str) -> None:
    tid = (tenant_id or "").strip()
    if not TENANT_ID_RE.match(tid):
        raise HTTPException(status_code=422, detail="Invalid tenant_id format")


def validate_order_id(order_id: str) -> None:
    oid = (order_id or "").strip().lower()
    if not ORDER_ID_RE.match(oid):
        raise HTTPException(status_code=422, detail="Invalid order_id format")


def validate_delivery_type(v: str) -> str:
    dv = (v or "pickup").strip().lower()
    if dv not in ALLOWED_DELIVERY_TYPES:
        raise HTTPException(status_code=422, detail=f"delivery_type must be one of {sorted(ALLOWED_DELIVERY_TYPES)}")
    return dv


def validate_source(v: str) -> str:
    sv = (v or "api").strip().lower()
    if len(sv) > MAX_SOURCE_LEN:
        raise HTTPException(status_code=422, detail="source too long")
    if sv not in ALLOWED_SOURCES:
        raise HTTPException(status_code=422, detail=f"source must be one of {sorted(ALLOWED_SOURCES)}")
    return sv


def validate_requested_time(v: str) -> str:
    rv = (v or "ahora").strip()
    if len(rv) > MAX_REQUESTED_TIME_LEN:
        raise HTTPException(status_code=422, detail="requested_time too long")
    return rv


def validate_contact(contact: str) -> None:
    c = (contact or "").strip()
    if len(c) > MAX_CONTACT_LEN:
        raise HTTPException(status_code=422, detail="customer_contact too long")
    if not re.match(r"^\+?\d{3,20}$", c):
        raise HTTPException(status_code=422, detail="customer_contact must be digits (optionally starting with +)")


def require_admin_token(token: str) -> None:
    expected = os.getenv(ENV_ADMIN_TOKEN, "").strip()
    if not expected:
        raise HTTPException(status_code=500, detail="ADMIN_TOKEN is not configured in env")
    if (token or "").strip() != expected:
        raise HTTPException(status_code=403, detail="Invalid admin token")


def pick_first_nonempty(*vals: Any) -> str:
    for v in vals:
        s = (v or "")
        s = str(s).strip()
        if s:
            return s
    return ""
