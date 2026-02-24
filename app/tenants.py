# app/tenants.py

from typing import Any, Dict, Optional, Tuple, List
from datetime import datetime, timezone

from fastapi import HTTPException

from app.config import TENANTS_SHEET_NAME
from app.sheets import get_gspread_client, open_config_spreadsheet
from app.utils import now_iso_utc, to_bool, normalize


# =========================================================
# Cache simple en memoria + "self-heal"
# =========================================================
_TENANTS_CACHE: Dict[str, Dict[str, Any]] = {}   # key = tenant_id_normalizado
_TENANTS_CACHE_AT: Optional[str] = None


def tenants_cache_info() -> Dict[str, Any]:
    return {
        "cached_at": _TENANTS_CACHE_AT,
        "tenants_count": len(_TENANTS_CACHE),
        "tenant_ids": list(_TENANTS_CACHE.keys()),
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


def _norm_tenant_id(tenant_id: Any) -> str:
    """
    tenant_id NORMALIZADO para que:
    - no dependa de mayúsculas/minúsculas
    - no dependa de tildes
    - sea consistente con el resto del sistema
    """
    return normalize(tenant_id).replace(" ", "")


def load_tenants(gc=None, force: bool = False) -> Dict[str, Dict[str, Any]]:
    """
    Lee Tenants desde el spreadsheet de configuración (RESERVACIONES_CONFIG).

    Compatibilidad:
      - admin_bot_token + webhook_secret_admin (nuevo)
      - bot_token + webhook_secret (viejo fallback)

    IMPORTANTE:
      - Agregar columnas NO debe romper nada.
      - Si una columna no existe, get() devuelve "".
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
        i = headers_norm.index(k)
        return row[i] if i < len(row) else ""

    tenants: Dict[str, Dict[str, Any]] = {}

    for row in values[header_idx + 1:]:
        tid_raw = str(get(row, "tenant_id")).strip()
        if not tid_raw:
            continue

        tid = _norm_tenant_id(tid_raw)
        if not tid:
            continue

        active = to_bool(get(row, "active"))
        if not active:
            continue

        # tokens + secrets (compat)
        admin_bot_token = _pick_first_nonempty(get(row, "admin_bot_token"), get(row, "bot_token"), get(row, "bot_token_admin"))
        client_bot_token = _pick_first_nonempty(get(row, "client_bot_token"), get(row, "bot_token_client"))

        webhook_secret_admin = _pick_first_nonempty(get(row, "webhook_secret_admin"), get(row, "webhook_secret"))
        webhook_secret_client = _pick_first_nonempty(get(row, "webhook_secret_client"))

        tenants[tid] = {
            "tenant_id": tid,
            "tenant_id_raw": tid_raw,

            "name": get(row, "name"),
            "business_type": get(row, "business_type"),

            "orders_sheet_id": str(get(row, "orders_sheet_id")).strip(),
            "orders_enabled": to_bool(get(row, "orders_enabled")),
            "bookings_enabled": to_bool(get(row, "bookings_enabled")),

            "admin_bot_token": (admin_bot_token or "").strip(),
            "client_bot_token": (client_bot_token or "").strip(),
            "webhook_secret_admin": (webhook_secret_admin or "").strip(),
            "webhook_secret_client": (webhook_secret_client or "").strip(),

            "admin_chat_id": str(get(row, "admin_chat_id")).strip(),
            "timezone": (get(row, "timezone") or "America/La_Paz").strip(),
            "admin_whatsapp": str(get(row, "admin_whatsapp")).strip(),

            # ✅ QR fields (CLAVE para que no vuelva a romper)
            "payment_qr_file_id": str(get(row, "payment_qr_file_id")).strip(),
            "payment_qr_url": str(get(row, "payment_qr_url")).strip(),
            "payment_qr_link": str(get(row, "payment_qr_link")).strip(),
        }

    _TENANTS_CACHE = tenants
    _TENANTS_CACHE_AT = now_iso_utc()
    return _TENANTS_CACHE


def get_tenant_or_404(tenant_id: str, gc=None) -> Dict[str, Any]:
    """
    Self-heal:
    - Intenta con cache
    - Si no encuentra, fuerza reload UNA vez (por si cambiaste Sheets y el server no reinició)
    """
    tid = _norm_tenant_id(tenant_id)
    if not tid:
        raise HTTPException(status_code=400, detail="tenant_id is required")

    tenants = load_tenants(gc=gc, force=False)
    t = tenants.get(tid)
    if t:
        return t

    # 🔁 Self-heal reload
    tenants = load_tenants(gc=gc, force=True)
    t = tenants.get(tid)
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


# =========================================================
# ✅ Validator (base para /admin/diag)
# =========================================================
def validate_tenant_config(tenant: Dict[str, Any]) -> Dict[str, Any]:
    """
    No lanza exception. Devuelve diagnóstico claro para humanos.
    """
    errors: List[str] = []
    warnings: List[str] = []

    tid = tenant.get("tenant_id") or "?"
    orders_sheet_id = (tenant.get("orders_sheet_id") or "").strip()

    if not orders_sheet_id:
        errors.append("orders_sheet_id missing")

    # tokens/secrets
    if not (tenant.get("admin_bot_token") or "").strip():
        warnings.append("admin_bot_token missing (admin bot no funcionará)")

    if not (tenant.get("client_bot_token") or "").strip():
        warnings.append("client_bot_token missing (client bot no funcionará)")

    if not (tenant.get("webhook_secret_admin") or "").strip():
        warnings.append("webhook_secret_admin missing")

    if not (tenant.get("webhook_secret_client") or "").strip():
        warnings.append("webhook_secret_client missing")

    # QR (requerido si orders_enabled)
    orders_enabled = bool(tenant.get("orders_enabled"))
    qr_file_id = (tenant.get("payment_qr_file_id") or "").strip()
    qr_url = (tenant.get("payment_qr_url") or tenant.get("payment_qr_link") or "").strip()

    if orders_enabled:
        if not (qr_file_id or qr_url):
            errors.append("QR missing: set payment_qr_file_id or payment_qr_url/payment_qr_link")

    return {
        "tenant_id": tid,
        "orders_enabled": orders_enabled,
        "has_orders_sheet_id": bool(orders_sheet_id),
        "has_admin_bot_token": bool((tenant.get("admin_bot_token") or "").strip()),
        "has_client_bot_token": bool((tenant.get("client_bot_token") or "").strip()),
        "has_webhook_secret_admin": bool((tenant.get("webhook_secret_admin") or "").strip()),
        "has_webhook_secret_client": bool((tenant.get("webhook_secret_client") or "").strip()),
        "has_qr_file_id": bool(qr_file_id),
        "has_qr_url": bool(qr_url),
        "errors": errors,
        "warnings": warnings,
        "ok": len(errors) == 0,
    }
