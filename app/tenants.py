from typing import Any, Dict, Optional

import gspread
from fastapi import HTTPException

from app.config import ENV_CONFIG_SPREADSHEET_ID
from app.sheets import read_records_manual
from app.utils import log_event, now_iso_utc, pick_first_nonempty, to_bool
import os

_TENANTS_CACHE: Dict[str, Dict[str, Any]] = {}
_TENANTS_CACHE_AT: Optional[str] = None


def get_config_spreadsheet(gc: gspread.Client) -> gspread.Spreadsheet:
    sid = os.getenv(ENV_CONFIG_SPREADSHEET_ID, "").strip()
    if not sid:
        raise RuntimeError(f"Missing env var: {ENV_CONFIG_SPREADSHEET_ID}")
    return gc.open_by_key(sid)


def load_tenants(gc: gspread.Client, force: bool = False) -> Dict[str, Dict[str, Any]]:
    global _TENANTS_CACHE, _TENANTS_CACHE_AT
    if _TENANTS_CACHE and not force:
        return _TENANTS_CACHE

    sh = get_config_spreadsheet(gc)
    ws = sh.worksheet("Tenants")
    records = read_records_manual(ws, required_headers=["tenant_id", "orders_sheet_id", "active"])

    tenants: Dict[str, Dict[str, Any]] = {}
    for r in records:
        tid = str(r.get("tenant_id", "")).strip()
        if not tid:
            continue

        admin_bot_token = pick_first_nonempty(r.get("admin_bot_token"), r.get("bot_token"))
        client_bot_token = pick_first_nonempty(r.get("client_bot_token"))

        webhook_secret_admin = pick_first_nonempty(r.get("webhook_secret_admin"), r.get("webhook_secret"))
        webhook_secret_client = pick_first_nonempty(r.get("webhook_secret_client"))

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

            "bot_token": admin_bot_token,
            "webhook_secret": webhook_secret_admin,

            "admin_chat_id": str(r.get("admin_chat_id", "")).strip(),
            "timezone": r.get("timezone", "America/La_Paz"),
            "active": to_bool(r.get("active", "")),
            "admin_whatsapp": (r.get("admin_whatsapp", "") or "").strip(),
        }

    _TENANTS_CACHE = tenants
    _TENANTS_CACHE_AT = now_iso_utc()
    log_event("tenants_loaded", cached_at=_TENANTS_CACHE_AT, tenants_count=len(tenants))
    return tenants


def get_cached_meta() -> Dict[str, Any]:
    return {"cached_at": _TENANTS_CACHE_AT, "tenants_count": len(_TENANTS_CACHE)}


def get_tenant_or_404(gc: gspread.Client, tenant_id: str) -> Dict[str, Any]:
    tenants = load_tenants(gc)
    t = tenants.get(tenant_id)
    if not t or not t.get("active", False):
        raise HTTPException(status_code=404, detail=f"Tenant not found or inactive: {tenant_id}")
    return t


def open_orders_spreadsheet(gc: gspread.Client, tenant: Dict[str, Any]) -> gspread.Spreadsheet:
    sid = tenant.get("orders_sheet_id", "").strip()
    if not sid:
        raise HTTPException(status_code=500, detail=f"Tenant {tenant['tenant_id']} missing orders_sheet_id")
    return gc.open_by_key(sid)
