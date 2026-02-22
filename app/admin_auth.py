# app/admin_auth.py

import os
from fastapi import HTTPException

from app.config import ENV_ADMIN_TOKEN


def require_admin_token(token: str) -> None:
    expected = os.getenv(ENV_ADMIN_TOKEN, "").strip()

    if not expected:
        raise HTTPException(status_code=500, detail="ADMIN_TOKEN is not configured in env")

    if (token or "").strip() != expected:
        raise HTTPException(status_code=403, detail="Invalid admin token")
