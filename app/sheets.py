# app/tenants.py

from typing import Any, Dict, Optional

from fastapi import HTTPException

from app.config import TENANTS_SHEET_NAME
from app.sheets import get_gspread_client, open_config_spreadsheet
from app.utils import now_iso_utc, to_bool


# Cache simple en memoria
_TENANTS_CACHE: Dict[str, Dict[str, Any]] = {}
_TENANTS_CACHE_AT: Optional[str] = None


def tenants_cache_info() -> Dict[str, Any]:
    return {
        "cached_at": _TENANTS_CACHE_AT,
        "tenants_count": len(_TENANTS_CACHE),
    }


def _pick_first_nonempty(*vals: Any) -> str:
    for v in vals:
        s = str(v or "").strip()
        if s:
            return s
    return ""


def load_tenants(gc=None, force: bool = False) -> Dict[str, Dict[str, Any]]:
    """
    Lee Tenants desde el spreadsheet de configuración (RESERVACIONES_CONFIG).
    Soporta compatibilidad:
      - admin_bot_token + webhook_secret_admin (nuevo)
      - bot_token + webhook_secret (viejo fallback)
    """
    global _TENANTS_CACHE, _TENANTS_CACHE_AT

    if _TENANTS_CACHE and not force:
        return _TENANTS_CACHE

    if gc is None:
        gc = get_gspread_client()

    sh = open_config_spreadsheet(gc)
    ws = sh.worksheet(TENANTS_SHEET_NAME)

    values = ws.get_all_values()
    if not values:
        _TENANTS_CACHE = {}
        _TENANTS_CACHE_AT = now_iso_utc()
        return _TENANTS_CACHE

    headers = [h.strip().lower() for h in values[0]]
    tenants: Dict[str, Dict[str, Any]] = {}

    def get(row: list, key: str) -> str:
        key = key.strip().lower()
        if key not in headers:
            return ""
        idx = headers.index(key)
        return row[idx] if idx < len(row) else ""

    for row in values[1:]:
        tid = str(get(row, "tenant_id")).strip()
        if not tid:
            continue

        active = to_bool(get(row, "active"))
        if not active:
            continue

        admin_bot_token = _pick_first_nonempty(get(row, "admin_bot_token"), get(row, "bot_token"))
        client_bot_token = _pick_first_nonempty(get(row, "client_bot_token"))

        webhook_secret_admin = _pick_first_nonempty(get(row, "webhook_secret_admin"), get(row, "webhook_secret"))
        webhook_secret_client = _pick_first_nonempty(get(row, "webhook_secret_client"))

        tenants[tid] = {
            "tenant_id": tid,
            "name": get(row, "name"),
            "business_type": get(row, "business_type"),
            "orders_sheet_id": str(get(row, "orders_sheet_id")).strip(),
            "orders_enabled": to_bool(get(row, "orders_enabled")),
            "bookings_enabled": to_bool(get(row, "bookings_enabled")),
            "admin_bot_token": admin_bot_token,
            "client_bot_token": client_bot_token,
            "webhook_secret_admin": webhook_secret_admin,
            "webhook_secret_client": webhook_secret_client,
            "admin_chat_id": str(get(row, "admin_chat_id")).strip(),
            "timezone": (get(row, "timezone") or "America/La_Paz").strip(),
            "admin_whatsapp": str(get(row, "admin_whatsapp")).strip(),
        }

    _TENANTS_CACHE = tenants
    _TENANTS_CACHE_AT = now_iso_utc()
    return _TENANTS_CACHE


def get_tenant_or_404(tenant_id: str, gc=None) -> Dict[str, Any]:
    tenants = load_tenants(gc=gc)
    t = tenants.get((tenant_id or "").strip())
    if not t:
        raise HTTPException(status_code=404, detail=f"Tenant not found or inactive: {tenant_id}")
    return t


def resolve_bot_by_secret(tenant: Dict[str, Any], secret: str):
    s = (secret or "").strip()
    admin_secret = (tenant.get("webhook_secret_admin") or "").strip()
    client_secret = (tenant.get("webhook_secret_client") or "").strip()

    if admin_secret and s == admin_secret:
        return ("admin", (tenant.get("admin_bot_token") or "").strip())

    if client_secret and s == client_secret:
        return ("client", (tenant.get("client_bot_token") or "").strip())

    raise HTTPException(status_code=403, detail="Invalid webhook secret")
