# app/tenants.py

from typing import Any, Dict, Optional, Tuple

from fastapi import HTTPException

from app.config import TENANTS_SHEET_NAME
from app.sheets import get_gspread_client, open_config_spreadsheet
from app.utils import now_iso_utc, to_bool, normalize


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


def _detect_header_row(values: list, required_headers: list, max_scan: int = 10) -> int:
    """
    Soporta:
      - Fila 1: headers técnicos
      - Fila 2: traducción/etiquetas
    Encuentra la fila que contiene required_headers.
    Devuelve índice 0-based.
    """
    req = [normalize(h) for h in required_headers]
    scan = values[:max_scan]
    for idx, row in enumerate(scan):
        row_norm = [normalize(x) for x in row]
        if all(h in row_norm for h in req):
            return idx
    return 0


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

    try:
        ws = sh.worksheet(TENANTS_SHEET_NAME)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Missing worksheet '{TENANTS_SHEET_NAME}': {e}")

    values = ws.get_all_values()
    if not values:
        _TENANTS_CACHE = {}
        _TENANTS_CACHE_AT = now_iso_utc()
        return _TENANTS_CACHE

    header_idx = _detect_header_row(values, required_headers=["tenant_id", "orders_sheet_id", "active"])
    headers_raw = values[header_idx]
    headers_norm = [normalize(h) for h in headers_raw]

    def get(row: list, key: str) -> str:
        k = normalize(key)
        if k not in headers_norm:
            return ""
        idx = headers_norm.index(k)
        return row[idx] if idx < len(row) else ""

    tenants: Dict[str, Dict[str, Any]] = {}

    for row in values[header_idx + 1:]:
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


def get_tenant_or_404(*args, gc=None) -> Dict[str, Any]:
    """
    Compatibilidad con ambas llamadas:
      - get_tenant_or_404(tenant_id, gc=gc)
      - get_tenant_or_404(gc, tenant_id)
    """
    if len(args) == 2:
        # forma vieja: (gc, tenant_id)
        gc_local = args[0]
        tenant_id = args[1]
    elif len(args) == 1:
        tenant_id = args[0]
        gc_local = gc
    else:
        raise HTTPException(status_code=500, detail="get_tenant_or_404() invalid arguments")

    tenants = load_tenants(gc=gc_local)
    t = tenants.get((tenant_id or "").strip())
    if not t:
        raise HTTPException(status_code=404, detail=f"Tenant not found or inactive: {tenant_id}")
    return t


def resolve_bot_by_secret(tenant: Dict[str, Any], secret: str) -> Tuple[str, str]:
    s = (secret or "").strip()
    admin_secret = (tenant.get("webhook_secret_admin") or "").strip()
    client_secret = (tenant.get("webhook_secret_client") or "").strip()

    if admin_secret and s == admin_secret:
        return ("admin", (tenant.get("admin_bot_token") or "").strip())

    if client_secret and s == client_secret:
        return ("client", (tenant.get("client_bot_token") or "").strip())

    raise HTTPException(status_code=403, detail="Invalid webhook secret")
