# app/tenants.py

import json
from pathlib import Path
from typing import Any, Dict, Optional, Tuple, List
import time

from fastapi import HTTPException

from app.config import TENANTS_SHEET_NAME, ENV_CONFIG_SPREADSHEET_ID, env_required
from app.sheets import get_gspread_client, open_config_spreadsheet, invalidate_sheet_caches, set_sheets_observation_context
from app.utils import now_iso_utc, to_bool, normalize, log_event


# =========================================================
# Cache en memoria + TTL + self-heal
# =========================================================
_TENANTS_CACHE: Dict[str, Dict[str, Any]] = {}   # key = tenant_id_normalizado
_TENANTS_CACHE_AT: Optional[str] = None
_TENANTS_CACHE_AT_TS: Optional[float] = None  # epoch seconds

# TTL opcional (en segundos). 0 = sin TTL (solo self-heal por miss)
TENANTS_CACHE_TTL_SECONDS = 180  # 3 min
TENANTS_SNAPSHOT_MAX_AGE_SECONDS = 86400
TENANTS_SNAPSHOT_VERSION = 1
TENANTS_SNAPSHOT_DIRNAME = ".tenants_snapshots"


def tenants_cache_info() -> Dict[str, Any]:
    return {
        "cached_at": _TENANTS_CACHE_AT,
        "cached_at_ts": _TENANTS_CACHE_AT_TS,
        "tenants_count": len(_TENANTS_CACHE),
        "tenant_ids": list(_TENANTS_CACHE.keys()),
        "ttl_seconds": TENANTS_CACHE_TTL_SECONDS,
    }


def invalidate_tenants_cache() -> None:
    global _TENANTS_CACHE, _TENANTS_CACHE_AT, _TENANTS_CACHE_AT_TS
    _TENANTS_CACHE = {}
    _TENANTS_CACHE_AT = None
    _TENANTS_CACHE_AT_TS = None


def _project_root() -> Path:
    return Path(__file__).resolve().parent.parent


def _snapshot_dir() -> Path:
    return _project_root() / TENANTS_SNAPSHOT_DIRNAME


def _snapshot_path(config_spreadsheet_id: str) -> Path:
    clean_id = str(config_spreadsheet_id or "").strip() or "config"
    safe_id = normalize(clean_id).replace(" ", "_") or "config"
    return _snapshot_dir() / f"{safe_id}.json"


def _read_snapshot_payload(path: Path) -> Optional[Dict[str, Any]]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None
    if not isinstance(payload, dict):
        return None
    return payload


def _load_tenants_snapshot(config_spreadsheet_id: str) -> Optional[Tuple[float, Dict[str, Dict[str, Any]]]]:
    path = _snapshot_path(config_spreadsheet_id)
    if not path.exists():
        return None

    payload = _read_snapshot_payload(path)
    if payload is None:
        return None
    if int(payload.get("version") or 0) != TENANTS_SNAPSHOT_VERSION:
        return None
    if str(payload.get("config_spreadsheet_id") or "").strip() != str(config_spreadsheet_id or "").strip():
        return None

    try:
        generated_at_ts = float(payload.get("generated_at_ts") or 0)
    except Exception:
        return None
    if generated_at_ts <= 0:
        return None
    if (time.time() - generated_at_ts) > TENANTS_SNAPSHOT_MAX_AGE_SECONDS:
        return None

    tenants = payload.get("tenants")
    if not isinstance(tenants, dict):
        return None

    return generated_at_ts, tenants


def _persist_tenants_snapshot(config_spreadsheet_id: str, tenants: Dict[str, Dict[str, Any]], *, ts: Optional[float] = None) -> None:
    snapshot_ts = float(ts if ts is not None else time.time())
    payload = {
        "version": TENANTS_SNAPSHOT_VERSION,
        "config_spreadsheet_id": str(config_spreadsheet_id or "").strip(),
        "generated_at_ts": snapshot_ts,
        "tenants": tenants,
    }

    snapshot_dir = _snapshot_dir()
    snapshot_dir.mkdir(parents=True, exist_ok=True)

    path = _snapshot_path(config_spreadsheet_id)
    temp_path = path.with_suffix(path.suffix + ".tmp")
    temp_path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    temp_path.replace(path)


def invalidate_all_tenant_related_caches() -> None:
    """
    Invalida cache de tenants y también el layer de sheets/config.
    Útil cuando cambia configuración viva.
    """
    invalidate_tenants_cache()
    invalidate_sheet_caches(None)


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
    if not values:
        return 0

    req = [normalize(h) for h in required_headers if h]
    scan = values[:max_scan] if max_scan > 0 else values

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
    try:
        return str(v if v is not None else "")
    except Exception:
        return ""


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
    if header_idx < 0 or header_idx >= len(values):
        return values, 0, [], []

    headers_raw = values[header_idx] if header_idx < len(values) else []
    headers_norm = [normalize(h) for h in headers_raw]
    return values, header_idx, headers_raw, headers_norm


def _find_header_col(headers_norm: List[str], key: str) -> Optional[int]:
    target = normalize(key)
    for i, h in enumerate(headers_norm):
        if h == target:
            return i
    return None


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

    snapshot_cached = None
    try:
        config_spreadsheet_id = env_required(ENV_CONFIG_SPREADSHEET_ID).strip()

        if force:
            invalidate_tenants_cache()

        if not force and _cache_is_fresh():
            return _TENANTS_CACHE

        snapshot_cached = None if force else _load_tenants_snapshot(config_spreadsheet_id)
        if not force and snapshot_cached is not None:
            snapshot_ts, snapshot_tenants = snapshot_cached
            _TENANTS_CACHE = snapshot_tenants
            _TENANTS_CACHE_AT = now_iso_utc()
            _TENANTS_CACHE_AT_TS = snapshot_ts
            try:
                log_event(
                    "tenants_loaded_from_snapshot",
                    tenants_count=len(snapshot_tenants),
                    snapshot_age_seconds=max(0, int(time.time() - snapshot_ts)),
                    snapshot_max_age_seconds=TENANTS_SNAPSHOT_MAX_AGE_SECONDS,
                )
            except Exception:
                pass
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
        if header_idx < 0 or header_idx >= len(values):
            raise HTTPException(status_code=500, detail="Invalid TENANTS header row")

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

        loaded_at_ts = time.time()
        _TENANTS_CACHE = tenants
        _TENANTS_CACHE_AT = now_iso_utc()
        _TENANTS_CACHE_AT_TS = loaded_at_ts
        try:
            _persist_tenants_snapshot(config_spreadsheet_id, tenants, ts=loaded_at_ts)
        except Exception as e:
            try:
                log_event(
                    "tenants_snapshot_write_failed",
                    error_type=type(e).__name__,
                    error=str(e),
                )
            except Exception:
                pass

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
        if snapshot_cached is not None:
            snapshot_ts, snapshot_tenants = snapshot_cached
            _TENANTS_CACHE = snapshot_tenants
            _TENANTS_CACHE_AT = now_iso_utc()
            _TENANTS_CACHE_AT_TS = snapshot_ts
            try:
                log_event(
                    "tenants_load_failed_serving_snapshot",
                    error_type=type(e).__name__,
                    error=str(e),
                    snapshot_age_seconds=max(0, int(time.time() - snapshot_ts)),
                )
            except Exception:
                pass
            return _TENANTS_CACHE

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
    set_sheets_observation_context(tenant_id=tid)

    tenants = load_tenants(gc=gc, force=False)
    t = tenants.get(tid)
    if t:
        set_sheets_observation_context(tenant_id=tid)
        return t

    tenants = load_tenants(gc=gc, force=True)
    t = tenants.get(tid)
    if not t:
        raise HTTPException(status_code=404, detail=f"Tenant not found or inactive: {tenant_id}")

    try:
        log_event("tenant_self_heal_reload", tenant_id=tid)
    except Exception:
        pass

    set_sheets_observation_context(tenant_id=tid)
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

def update_tenant_payment_qr(tenant_id: str, qr_url: str, gc=None) -> bool:
    """
    Actualiza payment_qr_url para un tenant en la hoja TENANTS
    y refresca cache en memoria.
    """
    tid = _norm_tenant_id(tenant_id)
    qr_url = _safe_str(qr_url).strip()

    if not tid or not qr_url:
        return False

    try:
        ws = _get_tenants_ws(gc=gc)
        values, header_idx, headers_raw, headers_norm = _get_tenants_values_and_header(ws)

        if not values or not headers_raw:
            return False

        tenant_col = _find_header_col(headers_norm, "tenant_id")
        qr_col = _find_header_col(headers_norm, "payment_qr_url")

        if tenant_col is None or qr_col is None:
            log_event(
                "tenant_payment_qr_update_missing_column",
                tenant_id=tid,
                has_tenant_col=(tenant_col is not None),
                has_qr_col=(qr_col is not None),
            )
            return False

        target_sheet_row: Optional[int] = None

        for row_idx_0b, row in enumerate(values[header_idx + 1:], start=header_idx + 1):
            raw_tid = row[tenant_col] if tenant_col < len(row) else ""
            if _norm_tenant_id(raw_tid) == tid:
                target_sheet_row = row_idx_0b + 1  # gspread is 1-based
                break

        if target_sheet_row is None:
            log_event("tenant_payment_qr_update_not_found", tenant_id=tid)
            return False

        ws.update_cell(target_sheet_row, qr_col + 1, qr_url)

        # Refrescar cache de configuración
        invalidate_all_tenant_related_caches()
        load_tenants(gc=gc, force=True)

        try:
            log_event(
                "tenant_payment_qr_updated",
                tenant_id=tid,
                sheet_row=target_sheet_row,
                updated_field="payment_qr_url",
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
