# app/admin_diag.py

import os
from typing import Any, Dict, Optional

from fastapi import APIRouter, HTTPException, Query

from app.sheets import get_gspread_client
from app.tenants import get_tenant_or_404

router = APIRouter(prefix="/admin/diag", tags=["admin"])


def _get_admin_token() -> str:
    return (os.getenv("ADMIN_TOKEN") or "").strip()


def _require_admin_token(token: str) -> None:
    admin_token = _get_admin_token()

    # NO rompemos deploy si falta: respondemos claro
    if not admin_token:
        raise HTTPException(
            status_code=503,
            detail="ADMIN_TOKEN no está configurado en variables de entorno (Render).",
        )

    if (token or "").strip() != admin_token:
        raise HTTPException(status_code=401, detail="Invalid admin token")


@router.get("/tenant")
def diag_tenant(
    tenant_id: str = Query(..., description="tenant_id (ej: resto_demo)"),
    token: str = Query(..., description="ADMIN_TOKEN"),
) -> Dict[str, Any]:
    _require_admin_token(token)

    gc = get_gspread_client()
    tenant = get_tenant_or_404(tenant_id, gc=gc)

    def mask(v: Optional[str]) -> str:
        v = (v or "").strip()
        if not v:
            return ""
        if len(v) <= 10:
            return "***"
        return v[:4] + "..." + v[-4:]

    return {
        "ok": True,
        "tenant_id": tenant.get("tenant_id"),
        "tenant_id_raw": tenant.get("tenant_id_raw"),
        "tenant_keys": sorted(list(tenant.keys())),
        "orders_sheet_id": tenant.get("orders_sheet_id"),
        "admin_chat_id": tenant.get("admin_chat_id"),
        "timezone": tenant.get("timezone"),
        "qr": {
            "payment_qr_file_id_present": bool((tenant.get("payment_qr_file_id") or "").strip()),
            "payment_qr_url_present": bool((tenant.get("payment_qr_url") or "").strip() or (tenant.get("payment_qr_link") or "").strip()),
            "payment_qr_file_id_masked": mask(tenant.get("payment_qr_file_id")),
            "payment_qr_url": (tenant.get("payment_qr_url") or tenant.get("payment_qr_link") or "").strip()[:200],
        },
        "tokens_present": {
            "admin_bot_token_present": bool((tenant.get("admin_bot_token") or "").strip()),
            "client_bot_token_present": bool((tenant.get("client_bot_token") or "").strip()),
            "webhook_secret_admin_present": bool((tenant.get("webhook_secret_admin") or "").strip()),
            "webhook_secret_client_present": bool((tenant.get("webhook_secret_client") or "").strip()),
        },
        "tokens_masked": {
            "admin_bot_token": mask(tenant.get("admin_bot_token")),
            "client_bot_token": mask(tenant.get("client_bot_token")),
            "webhook_secret_admin": mask(tenant.get("webhook_secret_admin")),
            "webhook_secret_client": mask(tenant.get("webhook_secret_admin")),
        },
    }
