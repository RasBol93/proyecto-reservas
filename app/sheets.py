# app/validators.py

from fastapi import Header, HTTPException
from typing import Optional
from app.config import ADMIN_TOKEN


def require_admin_token(x_admin_token: Optional[str] = Header(default=None)) -> None:
    """
    Valida que el header X-Admin-Token coincida con el ADMIN_TOKEN
    """
    if not x_admin_token:
        raise HTTPException(status_code=401, detail="Missing X-Admin-Token header")

    if x_admin_token != ADMIN_TOKEN:
        raise HTTPException(status_code=403, detail="Invalid admin token")


def validate_tenant_id(tenant_id: str) -> None:
    """
    Validación básica de tenant_id
    """
    if not tenant_id:
        raise HTTPException(status_code=400, detail="tenant_id is required")

    if len(tenant_id) < 3:
        raise HTTPException(status_code=400, detail="tenant_id too short")
