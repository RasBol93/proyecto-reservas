# app/tenants.py

import os
from typing import Any, Dict, Optional, Tuple

from fastapi import HTTPException

from app.config import ENV_CONFIG_SPREADSHEET_ID
from app.sheets import (
    get_gspread_client,
    open_spreadsheet_by_key,
    get_ws,
    read_records_manual,
    to_bool,
)


# Cache simple en memoria
_TENANTS_CACHE: Dict[str, Dict[str, Any]] = {}
_TENANTS_CACHE_AT: Optional[str] = None


def _now_iso_utc() -> str:
    # import local para evitar ciclos
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat()


def _pick_first_nonempty(*vals: Any) -> str:
    for v in vals:
        s = str(v or "").strip()
        if s:
            return s
    return ""


def load_tenants(force: bool = False) -> Dict[str, Dict[str, Any]]:
    """
    Lee Tenants desde RESERVACIONES_CONFIG.

    Soporta nombres nuevos y compatibilidad con los viejos:
      Nuevos:
        admin_bot_token, webhook_secret_admin,
        client_bot_token, webhook_secret_client
      Viejos (fallback):
        bot_token, webhook_secret
    """
    global _TENANTS_CACHE, _TENANTS_CACHE_AT

    if _TENANTS_CACHE and not force:
        return _TENANTS_CACHE

    cfg_id = os.getenv(ENV_CONFIG_SPREADSHEET_ID, "").strip()
    if not cfg_id:
        raise RuntimeError(f"Missing env var: {ENV_CONFIG_SPREADSHEET_ID}")

    gc = get_gspread_client()
    sh = open_spreadsheet_by_key(gc, cfg_id)
    ws = get_ws(sh, "Tenants")

    records = read_records_manual(ws, required_headers=["tenant_id", "orders_sheet_id", "active"])

    tenants: Dict[str, Dict[str, Any]] = {}
    for r in records:
        tid = str(r.get("tenant_id", "")).strip()
        if not tid:
            continue

        admin_bot_token = _pick_first_nonempty(r.get("admin_bot_token"), r.get("bot_token"))
        client_bot_token = _pick_first_nonempty(r.get("client_bot_token"))

        webhook_secret_admin = _pick_first_nonempty(r.get("webhook_secret_admin"), r.get("webhook_secret"))
        webhook_secret_client = _pick_first_nonempty(r.get("webhook_secret_client"))

        tenants[tid] = {
            "tenant_id": tid,
            "name": r.get("name", ""),
            "business_type": r.get("business_type", ""),
            "orders_sheet_id": str(r.get("orders_sheet_id", "")).strip(),

            "bookings_enabled": to_bool(r.get("bookings_enabled", "")),
            "orders_enabled": to_bool(r.get("orders_enabled", "")),

            "admin_bot_token": admin_bot_token,
            "client_bot_token": client_bot_token,

            "webhook_secret_admin": webhook_secret_admin,
            "webhook_secret_client": webhook_secret_client,

            # Compat vieja: para no romper imports legacy
            "bot_token": admin_bot_token,
            "webhook_secret": webhook_secret_admin,

            "admin_chat_id": str(r.get("admin_chat_id", "")).strip(),
            "timezone": r.get("timezone", "America/La_Paz"),
            "active": to_bool(r.get("active", "")),
            "admin_whatsapp": (r.get("admin_whatsapp", "") or "").strip(),
        }

    _TENANTS_CACHE = tenants
    _TENANTS_CACHE_AT = _now_iso_utc()
    return tenants


def tenants_cache_meta() -> Dict[str, Any]:
    return {"cached_at": _TENANTS_CACHE_AT, "tenants_count": len(_TENANTS_CACHE)}


def get_tenant_or_404(tenant_id: str) -> Dict[str, Any]:
    tenants = load_tenants(force=False)
    t = tenants.get(tenant_id)
    if not t or not t.get("active", False):
        raise HTTPException(status_code=404, detail=f"Tenant not found or inactive: {tenant_id}")
    return t


def resolve_bot_by_secret(tenant: Dict[str, Any], secret: str) -> Tuple[str, str]:
    """
    Devuelve ("admin"|"client", bot_token) según el secret recibido.
    """
    s = (secret or "").strip()
    admin_secret = (tenant.get("webhook_secret_admin", "") or "").strip()
    client_secret = (tenant.get("webhook_secret_client", "") or "").strip()

    if admin_secret and s == admin_secret:
        return ("admin", (tenant.get("admin_bot_token", "") or "").strip())

    if client_secret and s == client_secret:
        return ("client", (tenant.get("client_bot_token", "") or "").strip())

    raise HTTPException(status_code=403, detail="Invalid webhook secret")
