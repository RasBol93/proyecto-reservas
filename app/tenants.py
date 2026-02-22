# app/tenants.py

from typing import Dict, Tuple, Optional
from fastapi import HTTPException

from app.config import TENANTS_SHEET_NAME
from app.sheets import open_spreadsheet_by_key


# -------------------------
# Cache en memoria
# -------------------------

_TENANTS_CACHE: Dict[str, Dict] = {}
_TENANTS_LOADED: bool = False


# -------------------------
# Loaders
# -------------------------

def load_tenants(gc, force: bool = False) -> Dict[str, Dict]:
    """
    Carga tenants desde Google Sheets y los cachea.
    """
    global _TENANTS_CACHE, _TENANTS_LOADED

    if _TENANTS_LOADED and not force:
        return _TENANTS_CACHE

    sh = open_spreadsheet_by_key(gc, TENANTS_SHEET_NAME)
    ws = sh.sheet1

    rows = ws.get_all_records()
    tenants: Dict[str, Dict] = {}

    for row in rows:
        tenant_id = str(row.get("tenant_id", "")).strip()
        if not tenant_id:
            continue

        tenants[tenant_id] = row

    _TENANTS_CACHE = tenants
    _TENANTS_LOADED = True
    return tenants


def tenants_cache_info() -> Dict[str, int]:
    return {
        "tenants_loaded": int(_TENANTS_LOADED),
        "tenants_count": len(_TENANTS_CACHE),
    }


# -------------------------
# Access helpers
# -------------------------

def get_tenant_or_404(gc, tenant_id: str) -> Dict:
    """
    Devuelve tenant o lanza 404.
    """
    tenant_id = (tenant_id or "").strip()
    if not tenant_id:
        raise HTTPException(status_code=400, detail="tenant_id missing")

    tenants = load_tenants(gc)

    tenant = tenants.get(tenant_id)
    if not tenant:
        raise HTTPException(status_code=404, detail=f"Tenant not found: {tenant_id}")

    return tenant


# -------------------------
# Telegram helpers
# -------------------------

def resolve_bot_by_secret(tenant: Dict, secret: str) -> Tuple[str, Optional[str]]:
    """
    Devuelve (mode, bot_token) según el secret recibido.

    mode: "admin" | "client" | "none"
    """

    secret = (secret or "").strip()
    if not secret:
        return ("none", None)

    # Admin bot
    admin_secret = str(tenant.get("admin_webhook_secret", "")).strip()
    if admin_secret and secret == admin_secret:
        token = str(tenant.get("admin_bot_token", "")).strip()
        return ("admin", token if token else None)

    # Client bot
    client_secret = str(tenant.get("client_webhook_secret", "")).strip()
    if client_secret and secret == client_secret:
        token = str(tenant.get("client_bot_token", "")).strip()
        return ("client", token if token else None)

    return ("none", None)
