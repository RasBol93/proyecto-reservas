# app/tenants.py

from typing import Any, Dict, Optional, Tuple, List
import time

from fastapi import HTTPException

from app.config import TENANTS_SHEET_NAME
from app.sheets import get_gspread_client, open_config_spreadsheet
from app.utils import now_iso_utc, to_bool, normalize, log_event


# =========================================================
# Cache en memoria + TTL + self-heal
# =========================================================
_TENANTS_CACHE: Dict[str, Dict[str, Any]] = {}   # key = tenant_id_normalizado
_TENANTS_CACHE_AT: Optional[str] = None
_TENANTS_CACHE_AT_TS: Optional[float] = None  # epoch seconds

# TTL opcional (en segundos). 0 = sin TTL (solo self-heal por miss)
TENANTS_CACHE_TTL_SECONDS = 180  # 3 min


def tenants_cache_info() -> Dict[str, Any]:
    return {
        "cached_at": _TENANTS_CACHE_AT,
        "cached_at_ts": _TENANTS_CACHE_AT_TS,
        "tenants_count": len(_TENANTS_CACHE),
        "tenant_ids": list(_TENANTS_CACHE.keys()),
        "ttl_seconds": TENANTS_CACHE_TTL_SECONDS,
    }


# -------------------------
# Helpers
# -------------------------

def _pick_first_nonempty(*vals: Any) -> str:
    for v in vals:
        s = str(v or "").strip()
        if s:
            return s
    return ""


def _detect_header_row(values: List[List[str]], required_headers: List[str], max_scan: int = 10) -> int:
    """
    Soporta:
      - Fila 1: headers técnicos
      - Fila 2: traducción/etiquetas
    Encuentra la fila que contiene required_headers.
    Devuelve índice 0-based. Fallback: 0.
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
    tenant_id NORMALIZADO:
    - no depende de mayúsculas/minúsculas
    - no depende de tildes
    - consistente con todo el sistema
    """
    return normalize(tenant_id).replace(" ", "")


def _cache_is_fresh() -> bool:
    """
    Si TTL > 0, invalida cache cuando está viejo.
    """
    if not _TENANTS_CACHE:
        return False

    if TENANTS_CACHE_TTL_SECONDS <= 0:
        return True

    if _TENANTS_CACHE_AT_TS is None:
        return False

    age = time.time() - _TENANTS_CACHE_AT_TS
    return age <= TENANTS_CACHE_TTL_SECONDS


def _safe_str(v: Any) -> str:
    return str(v if v is not None else "")


def _build_row_getter(headers_norm: List[str]):
    """
    Devuelve una función get(row, key) que:
    - busca key normalizada en headers_norm
    - devuelve "" si no existe o si el índice supera el largo de la fila
    """
    idx_map: Dict[str, int] = {}
    for i, h in enumerate(headers_norm):
        if h and h not in idx_map:
            idx_map[h] = i

    def get(row: List[str], key: str) -> str:
        k = normalize(key)
        i = idx_map.get(k)
        if i is None:
            return ""
        return row[i] if i < len(row) else ""

    return get


def _get_tenants_ws(gc=None):
    if gc is None:
        gc = get_gspread_client()

    sh = open_config_spreadsheet(gc)

    try:
        ws = sh.worksheet(TENANTS_SHEET_NAME)
    except Exception as e:
        log_event("tenants_sheet_missing", error=str(e))
        raise HTTPException(status_code=500, detail=f"Missing worksheet '{TENANTS_SHEET_NAME}': {e}")

    return ws


def _get_tenants_values_and_header(ws) -> Tuple[List[List[str]], int, List[str], List[str]]:
    values = ws.get_all_values()
    if not values:
        return values, 0, [], []

    header_idx = _detect_header_row(
        values,
        required_headers=["tenant_id", "orders_sheet_id", "active"]
    )
    headers_raw = values[header_idx]
    headers_norm = [normalize(h) for h in headers_raw]
    return values, header_idx, headers_raw, headers_norm


def _find_header_col(headers_norm: List[str], key: str) -> Optional[int]:
    target = normalize(key)
    for i, h in enumerate(headers_norm):
        if h == target:
            return i
    return None


def _build_telegram_file_url(bot_token: str, file_id: str) -> str:
    """
    URL pública basada en Telegram file endpoint.
    Mantiene simplicidad y evita cambiar la estructura actual de TENANTS,
    que hoy usa payment_qr_url.
    """
    bot_token = _safe_str(bot_token).strip()
    file_id = _safe_str(file_id).strip()
    if not bot_token or not file_id:
        return ""
    return f"https://api.telegram.org/file/bot{bot_token}/{file_id}"


# -------------------------
# Main API
# -------------------------

def load_tenants(gc=None, force: bool = False) -> Dict[str, Dict[str, Any]]:
    """
    Lee Tenants desde el spreadsheet de configuración.

    Compatibilidad tokens/secrets:
      - admin_bot_token + webhook_secret_admin (nuevo)
      - bot_token + webhook_secret (viejo fallback)
      - bot_token_admin / bot_token_client (alternos)
      - owner_bot_token + webhook_secret_owner
    """
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
            raise HTTPException(status_code=500, detail=f"Missing worksheet '{TENANTS_SHEET_NAME}': {e}")

        values = ws.get_all_values()
        if not values:
            _TENANTS_CACHE = {}
            _TENANTS_CACHE_AT = now_iso_utc()
            _TENANTS_CACHE_AT_TS = time.time()
            return _TENANTS_CACHE

        header_idx = _detect_header_row(
            values,
            required_headers=["tenant_id", "orders_sheet_id", "active"]
        )
        headers_raw = values[header_idx]
        headers_norm = [normalize(h) for h in headers_raw]
        get = _build_row_getter(headers_norm)

        tenants: Dict[str, Dict[str, Any]] = {}
        skipped: Dict[str, int] = {
            "missing_tenant_id": 0,
            "inactive": 0,
            "missing_orders_sheet_id": 0,
        }

        for row in values[header_idx + 1:]:
            tid_raw = _safe_str(get(row, "tenant_id")).strip()
            if not tid_raw:
                skipped["missing_tenant_id"] += 1
                continue

            tid = _norm_tenant_id(tid_raw)
            if not tid:
                skipped["missing_tenant_id"] += 1
                continue

            active = to_bool(get(row, "active"))
            if not active:
                skipped["inactive"] += 1
                continue

            orders_sheet_id = _safe_str(get(row, "orders_sheet_id")).strip()
            if not orders_sheet_id:
                skipped["missing_orders_sheet_id"] += 1
                continue

            admin_bot_token = _pick_first_nonempty(
                get(row, "admin_bot_token"),
                get(row, "bot_token"),
                get(row, "bot_token_admin"),
            )
            client_bot_token = _pick_first_nonempty(
                get(row, "client_bot_token"),
                get(row, "bot_token_client"),
            )
            owner_bot_token = _pick_first_nonempty(
                get(row, "owner_bot_token"),
            )

            webhook_secret_admin = _pick_first_nonempty(
                get(row, "webhook_secret_admin"),
                get(row, "webhook_secret"),
            )
            webhook_secret_client = _pick_first_nonempty(
                get(row, "webhook_secret_client")
            )
            webhook_secret_owner = _pick_first_nonempty(
                get(row, "webhook_secret_owner")
            )

            tenant_obj: Dict[str, Any] = {
                "tenant_id": tid,
                "tenant_id_raw": tid_raw,

                "name": get(row, "name"),
                "business_type": get(row, "business_type"),

                "orders_sheet_id": orders_sheet_id,
                "orders_enabled": to_bool(get(row, "orders_enabled")),
                "bookings_enabled": to_bool(get(row, "bookings_enabled")),

                "admin_bot_token": _safe_str(admin_bot_token).strip(),
                "client_bot_token": _safe_str(client_bot_token).strip(),
                "owner_bot_token": _safe_str(owner_bot_token).strip(),

                "webhook_secret_admin": _safe_str(webhook_secret_admin).strip(),
                "webhook_secret_client": _safe_str(webhook_secret_client).strip(),
                "webhook_secret_owner": _safe_str(webhook_secret_owner).strip(),

                "admin_chat_id": _safe_str(get(row, "admin_chat_id")).strip(),
                "owner_chat_id": _safe_str(get(row, "owner_chat_id")).strip(),
                "owner_enabled": to_bool(get(row, "owner_enabled")),

                "timezone": (_safe_str(get(row, "timezone")) or "America/La_Paz").strip(),
                "admin_whatsapp": _safe_str(get(row, "admin_whatsapp")).strip(),
                "admin_username": _safe_str(get(row, "admin_username")).strip(),

                # QR
                "payment_qr_file_id": _safe_str(get(row, "payment_qr_file_id")).strip(),
                "payment_qr_url": _safe_str(get(row, "payment_qr_url")).strip(),
                "payment_qr_link": _safe_str(get(row, "payment_qr_link")).strip(),

                # fotos de productos
                "product_photos_drive_folder_id": _safe_str(get(row, "product_photos_drive_folder_id")).strip(),
            }

            tenants[tid] = tenant_obj

        _TENANTS_CACHE = tenants
        _TENANTS_CACHE_AT = now_iso_utc()
        _TENANTS_CACHE_AT_TS = time.time()

        try:
            log_event(
                "tenants_loaded",
                tenants_count=len(tenants),
                header_row_1based=header_idx + 1,
                skipped=skipped,
                ttl_seconds=TENANTS_CACHE_TTL_SECONDS,
            )
        except Exception:
            pass

        return _TENANTS_CACHE

    except HTTPException:
        raise
    except Exception as e:
        log_event(
            "tenants_load_error",
            error_type=type(e).__name__,
            error=str(e),
        )
        raise HTTPException(status_code=500, detail=f"Error loading tenants: {e}")


def get_tenant_or_404(tenant_id: str, gc=None) -> Dict[str, Any]:
    """
    Self-heal:
    - intenta con cache
    - si no encuentra, fuerza reload una vez
    """
    tid = _norm_tenant_id(tenant_id)
    if not tid:
        raise HTTPException(status_code=400, detail="tenant_id is required")

    tenants = load_tenants(gc=gc, force=False)
    t = tenants.get(tid)
    if t:
        return t

    tenants = load_tenants(gc=gc, force=True)
    t = tenants.get(tid)
    if not t:
        raise HTTPException(status_code=404, detail=f"Tenant not found or inactive: {tenant_id}")

    try:
        log_event("tenant_self_heal_reload", tenant_id=tid)
    except Exception:
        pass

    return t


def resolve_bot_by_secret(tenant: Dict[str, Any], secret: str) -> Tuple[str, str]:
    """
    Devuelve (mode, bot_token) o lanza 403.
    """
    s = (secret or "").strip()
    admin_secret = (tenant.get("webhook_secret_admin") or "").strip()
    client_secret = (tenant.get("webhook_secret_client") or "").strip()
    owner_secret = (tenant.get("webhook_secret_owner") or "").strip()

    if admin_secret and s == admin_secret:
        token = (tenant.get("admin_bot_token") or "").strip()
        if not token:
            raise HTTPException(status_code=500, detail="admin_bot_token missing for tenant")
        return ("admin", token)

    if client_secret and s == client_secret:
        token = (tenant.get("client_bot_token") or "").strip()
        if not token:
            raise HTTPException(status_code=500, detail="client_bot_token missing for tenant")
        return ("client", token)

    if owner_secret and s == owner_secret:
        token = (tenant.get("owner_bot_token") or "").strip()
        if not token:
            raise HTTPException(status_code=500, detail="owner_bot_token missing for tenant")
        return ("admin", token)

    raise HTTPException(status_code=403, detail="Invalid webhook secret")


# =========================================================
# QR updater
# =========================================================

def update_tenant_payment_qr(tenant_id: str, file_id: str, gc=None) -> bool:
    """
    Actualiza payment_qr_url para un tenant en la hoja TENANTS
    y refresca cache en memoria.

    IMPORTANTE:
    Tu hoja real usa payment_qr_url, no payment_qr_file_id.
    """
    tid = _norm_tenant_id(tenant_id)
    file_id = _safe_str(file_id).strip()

    if not tid or not file_id:
        return False

    try:
        # Necesitamos tenant para sacar el admin_bot_token y construir la URL
        tenant = get_tenant_or_404(tid, gc=gc)
        admin_bot_token = _safe_str(tenant.get("admin_bot_token")).strip()
        payment_qr_url = _build_telegram_file_url(admin_bot_token, file_id)

        if not payment_qr_url:
            log_event(
                "tenant_payment_qr_update_missing_token_or_url",
                tenant_id=tid,
                has_admin_bot_token=bool(admin_bot_token),
            )
            return False

        ws = _get_tenants_ws(gc=gc)
        values, header_idx, headers_raw, headers_norm = _get_tenants_values_and_header(ws)

        if not values or not headers_raw:
            return False

        tenant_col = _find_header_col(headers_norm, "tenant_id")
        qr_url_col = _find_header_col(headers_norm, "payment_qr_url")

        if tenant_col is None or qr_url_col is None:
            log_event(
                "tenant_payment_qr_update_missing_column",
                tenant_id=tid,
                has_tenant_col=(tenant_col is not None),
                has_qr_url_col=(qr_url_col is not None),
            )
            return False

        target_sheet_row: Optional[int] = None

        for row_idx_0b, row in enumerate(values[header_idx + 1:], start=header_idx + 1):
            raw_tid = row[tenant_col] if tenant_col < len(row) else ""
            if _norm_tenant_id(raw_tid) == tid:
                target_sheet_row = row_idx_0b + 1  # gspread 1-based
                break

        if target_sheet_row is None:
            log_event("tenant_payment_qr_update_not_found", tenant_id=tid)
            return False

        ws.update_cell(target_sheet_row, qr_url_col + 1, payment_qr_url)

        # Refrescar cache
        load_tenants(gc=gc, force=True)

        try:
            log_event(
                "tenant_payment_qr_updated",
                tenant_id=tid,
                sheet_row=target_sheet_row,
                target_column="payment_qr_url",
            )
        except Exception:
            pass

        return True

    except Exception as e:
        log_event(
            "tenant_payment_qr_update_error",
            tenant_id=tid,
            error_type=type(e).__name__,
            error=str(e),
        )
        return False


# =========================================================
# Validator
# =========================================================

def validate_tenant_config(tenant: Dict[str, Any]) -> Dict[str, Any]:
    errors: List[str] = []
    warnings: List[str] = []

    tid = tenant.get("tenant_id") or "?"
    orders_sheet_id = (tenant.get("orders_sheet_id") or "").strip()

    if not orders_sheet_id:
        errors.append("orders_sheet_id missing")

    admin_bot_token = (tenant.get("admin_bot_token") or "").strip()
    client_bot_token = (tenant.get("client_bot_token") or "").strip()
    owner_bot_token = (tenant.get("owner_bot_token") or "").strip()

    secret_admin = (tenant.get("webhook_secret_admin") or "").strip()
    secret_client = (tenant.get("webhook_secret_client") or "").strip()
    secret_owner = (tenant.get("webhook_secret_owner") or "").strip()

    if not admin_bot_token:
        warnings.append("admin_bot_token missing (admin bot no funcionará)")
    if not client_bot_token:
        warnings.append("client_bot_token missing (client bot no funcionará)")
    if bool(tenant.get("owner_enabled")) and not owner_bot_token:
        warnings.append("owner_bot_token missing (owner bot no funcionará)")

    if admin_bot_token and ":" not in admin_bot_token:
        warnings.append("admin_bot_token shape looks wrong (expected ':')")
    if client_bot_token and ":" not in client_bot_token:
        warnings.append("client_bot_token shape looks wrong (expected ':')")
    if owner_bot_token and ":" not in owner_bot_token:
        warnings.append("owner_bot_token shape looks wrong (expected ':')")

    if not secret_admin:
        warnings.append("webhook_secret_admin missing")
    if not secret_client:
        warnings.append("webhook_secret_client missing")
    if bool(tenant.get("owner_enabled")) and not secret_owner:
        warnings.append("webhook_secret_owner missing")

    if secret_admin and len(secret_admin) < 8:
        warnings.append("webhook_secret_admin too short (recommend >= 8)")
    if secret_client and len(secret_client) < 8:
        warnings.append("webhook_secret_client too short (recommend >= 8)")
    if secret_owner and len(secret_owner) < 8:
        warnings.append("webhook_secret_owner too short (recommend >= 8)")

    orders_enabled = bool(tenant.get("orders_enabled"))
    qr_file_id = (tenant.get("payment_qr_file_id") or "").strip()
    qr_url = (tenant.get("payment_qr_url") or tenant.get("payment_qr_link") or "").strip()

    if orders_enabled and not (qr_file_id or qr_url):
        errors.append("QR missing: set payment_qr_file_id or payment_qr_url/payment_qr_link")

    admin_chat_id = (tenant.get("admin_chat_id") or "").strip()
    if orders_enabled and not admin_chat_id:
        warnings.append("admin_chat_id missing (no podrás recibir notificaciones/confirmar pagos)")

    if bool(tenant.get("owner_enabled")):
        owner_chat_id = (tenant.get("owner_chat_id") or "").strip()
        if not owner_chat_id:
            warnings.append("owner_chat_id missing (owner no podrá recibir notificaciones)")

    folder_id = (tenant.get("product_photos_drive_folder_id") or "").strip()
    if not folder_id:
        warnings.append("product_photos_drive_folder_id missing (no podrás subir fotos de productos a Drive)")

    return {
        "tenant_id": tid,
        "orders_enabled": orders_enabled,
        "has_orders_sheet_id": bool(orders_sheet_id),

        "has_admin_bot_token": bool(admin_bot_token),
        "has_client_bot_token": bool(client_bot_token),
        "has_owner_bot_token": bool(owner_bot_token),

        "has_webhook_secret_admin": bool(secret_admin),
        "has_webhook_secret_client": bool(secret_client),
        "has_webhook_secret_owner": bool(secret_owner),

        "has_admin_chat_id": bool(admin_chat_id),
        "has_owner_chat_id": bool((tenant.get("owner_chat_id") or "").strip()),

        "has_qr_file_id": bool(qr_file_id),
        "has_qr_url": bool(qr_url),
        "has_product_photos_drive_folder_id": bool(folder_id),

        "errors": errors,
        "warnings": warnings,
        "ok": len(errors) == 0,
    }
