# app/tenants.py (HARDENED)

from typing import Any, Dict, Optional, Tuple, List
import time
from fastapi import HTTPException

from app.config import TENANTS_SHEET_NAME
from app.sheets import get_gspread_client, open_config_spreadsheet
from app.utils import now_iso_utc, to_bool, normalize, log_event


_TENANTS_CACHE: Dict[str, Dict[str, Any]] = {}
_TENANTS_CACHE_AT: Optional[str] = None
_TENANTS_CACHE_AT_TS: Optional[float] = None

TENANTS_CACHE_TTL_SECONDS = 180


def tenants_cache_info() -> Dict[str, Any]:
    return {
        "cached_at": _TENANTS_CACHE_AT,
        "cached_at_ts": _TENANTS_CACHE_AT_TS,
        "tenants_count": len(_TENANTS_CACHE),
        "ttl_seconds": TENANTS_CACHE_TTL_SECONDS,
    }


def _norm_tenant_id(tenant_id: Any) -> str:
    return normalize(tenant_id).replace(" ", "")


def _cache_is_fresh() -> bool:
    if not _TENANTS_CACHE:
        return False

    if TENANTS_CACHE_TTL_SECONDS <= 0:
        return True

    if _TENANTS_CACHE_AT_TS is None:
        return False

    return (time.time() - _TENANTS_CACHE_AT_TS) <= TENANTS_CACHE_TTL_SECONDS


# =========================================================
# LOAD TENANTS (CRÍTICO)
# =========================================================

def load_tenants(gc=None, force: bool = False) -> Dict[str, Dict[str, Any]]:
    global _TENANTS_CACHE, _TENANTS_CACHE_AT, _TENANTS_CACHE_AT_TS

    try:
        if not force and _cache_is_fresh():
            return _TENANTS_CACHE

        if gc is None:
            gc = get_gspread_client()

        sh = open_config_spreadsheet(gc)

        try:
            ws = sh.worksheet(TENANTS_SHEET_NAME)
        except Exception as e:
            log_event("tenants_sheet_missing", error=str(e))
            raise HTTPException(status_code=500, detail="Tenants sheet missing")

        values = ws.get_all_values()

        if not values:
            log_event("tenants_empty")
            _TENANTS_CACHE = {}
            return _TENANTS_CACHE

        header = values[0]
        headers_norm = [normalize(h) for h in header]

        tenants = {}
        skipped = 0

        for row in values[1:]:

            try:
                tid_raw = str(row[headers_norm.index("tenant_id")]).strip()
            except Exception:
                skipped += 1
                continue

            if not tid_raw:
                skipped += 1
                continue

            tid = _norm_tenant_id(tid_raw)

            try:
                active = to_bool(row[headers_norm.index("active")])
            except Exception:
                active = False

            if not active:
                continue

            try:
                orders_sheet_id = str(row[headers_norm.index("orders_sheet_id")]).strip()
            except Exception:
                orders_sheet_id = ""

            if not orders_sheet_id:
                skipped += 1
                continue

            tenant_obj = {
                "tenant_id": tid,
                "tenant_id_raw": tid_raw,
                "orders_sheet_id": orders_sheet_id,
                "admin_bot_token": "",
                "client_bot_token": "",
                "webhook_secret_admin": "",
                "webhook_secret_client": "",
                "admin_chat_id": "",
                "timezone": "America/La_Paz",
            }

            tenants[tid] = tenant_obj

        _TENANTS_CACHE = tenants
        _TENANTS_CACHE_AT = now_iso_utc()
        _TENANTS_CACHE_AT_TS = time.time()

        log_event("tenants_loaded", tenants=len(tenants), skipped=skipped)

        return tenants

    except Exception as e:
        log_event(
            "tenants_load_error",
            error_type=type(e).__name__,
            error=str(e),
        )
        raise


# =========================================================
# GET TENANT
# =========================================================

def get_tenant_or_404(tenant_id: str, gc=None) -> Dict[str, Any]:
    try:
        tid = _norm_tenant_id(tenant_id)

        if not tid:
            raise HTTPException(status_code=400, detail="tenant_id required")

        tenants = load_tenants(gc=gc, force=False)
        t = tenants.get(tid)

        if t:
            return t

        # self-heal
        tenants = load_tenants(gc=gc, force=True)
        t = tenants.get(tid)

        if not t:
            log_event("tenant_not_found", tenant_id=tenant_id)
            raise HTTPException(status_code=404, detail="tenant not found")

        log_event("tenant_self_heal", tenant_id=tid)
        return t

    except Exception as e:
        log_event(
            "tenant_lookup_error",
            tenant_id=tenant_id,
            error=str(e),
        )
        raise


# =========================================================
# SECRET RESOLUTION
# =========================================================

def resolve_bot_by_secret(tenant: Dict[str, Any], secret: str) -> Tuple[str, str]:
    try:
        s = (secret or "").strip()

        if s == (tenant.get("webhook_secret_admin") or "").strip():
            token = tenant.get("admin_bot_token")
            if not token:
                raise HTTPException(status_code=500, detail="admin token missing")
            return ("admin", token)

        if s == (tenant.get("webhook_secret_client") or "").strip():
            token = tenant.get("client_bot_token")
            if not token:
                raise HTTPException(status_code=500, detail="client token missing")
            return ("client", token)

        log_event("invalid_secret", tenant_id=tenant.get("tenant_id"))
        raise HTTPException(status_code=403, detail="invalid secret")

    except Exception as e:
        log_event("secret_resolve_error", error=str(e))
        raise
