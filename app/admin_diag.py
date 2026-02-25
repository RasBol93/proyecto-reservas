# app/admin_diag.py

import os
import re
from typing import Any, Dict, Optional, List, Tuple

from fastapi import APIRouter, HTTPException, Query

from app.sheets import get_gspread_client, open_spreadsheet_by_key
from app.tenants import get_tenant_or_404
from app.utils import normalize

router = APIRouter(prefix="/admin/diag", tags=["admin"])


# ---------------------------------
# Admin token
# ---------------------------------

def _get_admin_token() -> str:
    return (os.getenv("ADMIN_TOKEN") or "").strip()


def _require_admin_token(token: str) -> None:
    admin_token = _get_admin_token()

    if not admin_token:
        raise HTTPException(
            status_code=503,
            detail="ADMIN_TOKEN no está configurado en variables de entorno (Render).",
        )

    if (token or "").strip() != admin_token:
        raise HTTPException(status_code=401, detail="Invalid admin token")


def _mask(v: Optional[str]) -> str:
    v = (v or "").strip()
    if not v:
        return ""
    if len(v) <= 10:
        return "***"
    return v[:4] + "..." + v[-4:]


# ---------------------------------
# URL helpers
# ---------------------------------

def _is_drive_uc_url(url: str) -> bool:
    url = (url or "").strip()
    if not url:
        return False
    return bool(re.search(r"^https://drive\.google\.com/uc\?export=download&id=", url))


def _drive_file_id_from_url(url: str) -> Optional[str]:
    """
    Extrae file_id de links Drive típicos:
      - https://drive.google.com/file/d/<ID>/view?...
      - https://drive.google.com/open?id=<ID>
      - https://drive.google.com/uc?id=<ID>&export=download
      - https://drive.google.com/uc?export=download&id=<ID>
    """
    if not url:
        return None
    m = re.search(r"/file/d/([a-zA-Z0-9_-]+)", url)
    if m:
        return m.group(1)
    m = re.search(r"[?&]id=([a-zA-Z0-9_-]+)", url)
    if m:
        return m.group(1)
    return None


def _normalize_public_qr_url(url: str) -> str:
    """
    Telegram suele aceptar mejor:
      https://drive.google.com/uc?export=download&id=<ID>
    """
    url = (url or "").strip()
    if not url:
        return ""
    file_id = _drive_file_id_from_url(url)
    if file_id:
        return f"https://drive.google.com/uc?export=download&id={file_id}"
    return url


# ---------------------------------
# Sheets helpers (ligeros)
# ---------------------------------

def _col_letter(n: int) -> str:
    """1 -> A, 2 -> B, 27 -> AA"""
    s = ""
    while n > 0:
        n, r = divmod(n - 1, 26)
        s = chr(65 + r) + s
    return s


def _ws_sample_values(ws, max_rows: int = 30, max_cols: int = 30) -> List[List[str]]:
    """
    Lee solo un bloque chico (mucho más rápido que get_all_values en sheets grandes).
    """
    end_col = _col_letter(max_cols)
    rng = f"A1:{end_col}{max_rows}"
    try:
        return ws.get(rng)  # gspread: devuelve lista de filas
    except Exception:
        # fallback
        try:
            return ws.get_all_values()[:max_rows]
        except Exception:
            return []


def _ws_has_headers(ws, required_headers: List[str], max_scan_rows: int = 30) -> Tuple[bool, Optional[int], List[str]]:
    """
    Retorna: (ok, header_row_1based, headers_raw_encontrados)
    Busca headers en las primeras max_scan_rows filas.
    """
    values = _ws_sample_values(ws, max_rows=max_scan_rows, max_cols=60)
    if not values:
        return (False, None, [])

    req = [normalize(h) for h in required_headers]

    for idx0, row in enumerate(values):
        row_norm = [normalize(x) for x in row]
        if all(h in row_norm for h in req):
            return (True, idx0 + 1, row)

    return (False, None, values[0] if values else [])


def _find_ws_by_name_or_headers(sh, preferred_title: str, required_headers: List[str]) -> Dict[str, Any]:
    """
    - Primero intenta por nombre exacto.
    - Si falla, busca por headers en otras hojas.
    """
    # 1) por nombre
    try:
        ws = sh.worksheet(preferred_title)
        ok, header_row, headers_raw = _ws_has_headers(ws, required_headers=required_headers)
        return {
            "found": True,
            "method": "by_name",
            "title": ws.title,
            "headers_ok": ok,
            "header_row": header_row,
            "headers_raw": headers_raw,
        }
    except Exception:
        pass

    # 2) por headers
    try:
        for ws in sh.worksheets():
            ok, header_row, headers_raw = _ws_has_headers(ws, required_headers=required_headers)
            if ok:
                return {
                    "found": True,
                    "method": "by_headers",
                    "title": ws.title,
                    "headers_ok": True,
                    "header_row": header_row,
                    "headers_raw": headers_raw,
                }
        return {"found": False, "method": "not_found"}
    except Exception as e:
        return {"found": False, "method": "error", "error": str(e)}


# ---------------------------------
# Check framework (OK/WARN/FAIL)
# ---------------------------------

def _check(check_id: str, status: str, details: str, suggested_fix: str = "") -> Dict[str, Any]:
    return {
        "id": check_id,
        "status": status,  # OK | WARN | FAIL
        "details": details,
        "suggested_fix": suggested_fix,
    }


def _is_token_shape_ok(tok: str) -> bool:
    """
    Heurística: tokens de bot Telegram suelen tener ":".
    No es validación real, solo warning.
    """
    tok = (tok or "").strip()
    if not tok:
        return False
    return (":" in tok) and (len(tok) >= 20)


# ---------------------------------
# Endpoints
# ---------------------------------

@router.get("/tenant")
def diag_tenant(
    tenant_id: str = Query(..., description="tenant_id (ej: resto_demo)"),
    token: str = Query(..., description="ADMIN_TOKEN"),
) -> Dict[str, Any]:
    _require_admin_token(token)

    gc = get_gspread_client()
    tenant = get_tenant_or_404(tenant_id, gc=gc)

    qr_url_raw = (tenant.get("payment_qr_url") or tenant.get("payment_qr_link") or "").strip()
    qr_url = _normalize_public_qr_url(qr_url_raw)
    qr_file_id = (tenant.get("payment_qr_file_id") or "").strip()

    return {
        "ok": True,
        "tenant_id": tenant.get("tenant_id"),
        "tenant_id_raw": tenant.get("tenant_id_raw"),
        "tenant_keys": sorted(list(tenant.keys())),
        "orders_sheet_id": tenant.get("orders_sheet_id"),
        "admin_chat_id": tenant.get("admin_chat_id"),
        "timezone": tenant.get("timezone"),
        "qr": {
            "payment_qr_file_id_present": bool(qr_file_id),
            "payment_qr_url_present": bool(qr_url),
            "payment_qr_file_id_masked": _mask(qr_file_id),
            "payment_qr_url_preview": qr_url[:200],
            "payment_qr_url_is_drive_uc": _is_drive_uc_url(qr_url),
        },
        "tokens_present": {
            "admin_bot_token_present": bool((tenant.get("admin_bot_token") or "").strip()),
            "client_bot_token_present": bool((tenant.get("client_bot_token") or "").strip()),
            "webhook_secret_admin_present": bool((tenant.get("webhook_secret_admin") or "").strip()),
            "webhook_secret_client_present": bool((tenant.get("webhook_secret_client") or "").strip()),
        },
        "tokens_masked": {
            "admin_bot_token": _mask(tenant.get("admin_bot_token")),
            "client_bot_token": _mask(tenant.get("client_bot_token")),
            "webhook_secret_admin": _mask(tenant.get("webhook_secret_admin")),
            "webhook_secret_client": _mask(tenant.get("webhook_secret_client")),  # ✅ corregido
        },
    }


@router.get("/tenant_full")
def tenant_full(
    tenant_id: str = Query(..., description="tenant_id (ej: resto_demo)"),
    token: str = Query(..., description="ADMIN_TOKEN"),
) -> Dict[str, Any]:
    """
    Diagnóstico robusto (para operar 100 restaurantes):
    - ~20 checks
    - salida estándar OK/WARN/FAIL
    - suggested_fix por check
    """
    _require_admin_token(token)

    checks: List[Dict[str, Any]] = []
    blockers: List[str] = []
    summary = {"OK": 0, "WARN": 0, "FAIL": 0}

    def add(c: Dict[str, Any]) -> None:
        checks.append(c)
        st = c["status"]
        if st in summary:
            summary[st] += 1
        if st == "FAIL":
            blockers.append(c["id"])

    # ---- (1) tenant load
    gc = get_gspread_client()
    try:
        tenant = get_tenant_or_404(tenant_id, gc=gc)
        add(_check("tenant_found_active", "OK", "Tenant encontrado y activo."))
    except HTTPException as e:
        add(_check("tenant_found_active", "FAIL", f"Tenant no válido: {e.detail}", "Revisar tenant_id y columna active en TENANTS."))
        return {"ok": False, "tenant_id": tenant_id, "summary": summary, "blockers": blockers, "checks": checks}

    # ---- (2) tenant_id normalization
    raw = (tenant.get("tenant_id_raw") or "").strip()
    norm = (tenant.get("tenant_id") or "").strip()
    if raw and normalize(raw).replace(" ", "") != norm:
        add(_check(
            "tenant_id_normalization",
            "WARN",
            f"tenant_id_raw='{raw}' normaliza a '{normalize(raw).replace(' ', '')}', pero tenant_id cacheado es '{norm}'.",
            "Evita espacios/tildes raras en tenant_id para que sea estable."
        ))
    else:
        add(_check("tenant_id_normalization", "OK", "tenant_id consistente (raw vs normalizado)."))

    # ---- (3) flags
    orders_enabled = bool(tenant.get("orders_enabled", False))
    if orders_enabled:
        add(_check("orders_enabled", "OK", "orders_enabled=true"))
    else:
        add(_check("orders_enabled", "WARN", "orders_enabled=false", "Si este tenant debe vender, pon orders_enabled=TRUE."))

    # ---- (4) tokens + secrets
    admin_bot_token = (tenant.get("admin_bot_token") or "").strip()
    client_bot_token = (tenant.get("client_bot_token") or "").strip()
    secret_admin = (tenant.get("webhook_secret_admin") or "").strip()
    secret_client = (tenant.get("webhook_secret_client") or "").strip()

    add(_check("admin_bot_token_present", "OK" if admin_bot_token else "FAIL",
               "admin_bot_token presente." if admin_bot_token else "admin_bot_token missing.",
               "Completa admin_bot_token en TENANTS." if not admin_bot_token else ""))

    add(_check("client_bot_token_present", "OK" if client_bot_token else "FAIL",
               "client_bot_token presente." if client_bot_token else "client_bot_token missing.",
               "Completa client_bot_token en TENANTS." if not client_bot_token else ""))

    add(_check("webhook_secret_admin_present", "OK" if secret_admin else "FAIL",
               "webhook_secret_admin presente." if secret_admin else "webhook_secret_admin missing.",
               "Completa webhook_secret_admin en TENANTS." if not secret_admin else ""))

    add(_check("webhook_secret_client_present", "OK" if secret_client else "FAIL",
               "webhook_secret_client presente." if secret_client else "webhook_secret_client missing.",
               "Completa webhook_secret_client en TENANTS." if not secret_client else ""))

    # ---- (5) token shape heuristic
    if admin_bot_token and not _is_token_shape_ok(admin_bot_token):
        add(_check("admin_bot_token_shape", "WARN", "admin_bot_token tiene forma rara (heurística).", "Verifica que sea el token real del BotFather."))
    else:
        add(_check("admin_bot_token_shape", "OK" if admin_bot_token else "WARN", "admin_bot_token parece correcto." if admin_bot_token else "Sin token para validar."))

    if client_bot_token and not _is_token_shape_ok(client_bot_token):
        add(_check("client_bot_token_shape", "WARN", "client_bot_token tiene forma rara (heurística).", "Verifica que sea el token real del BotFather."))
    else:
        add(_check("client_bot_token_shape", "OK" if client_bot_token else "WARN", "client_bot_token parece correcto." if client_bot_token else "Sin token para validar."))

    # ---- (6) admin_chat_id
    admin_chat_id_raw = (tenant.get("admin_chat_id") or "").strip()
    if not admin_chat_id_raw:
        add(_check("admin_chat_id_present", "WARN", "admin_chat_id vacío.", "Usa /id en el bot admin y pega ese número en TENANTS."))
    else:
        try:
            int(admin_chat_id_raw)
            add(_check("admin_chat_id_present", "OK", "admin_chat_id presente y numérico."))
        except Exception:
            add(_check("admin_chat_id_present", "FAIL", f"admin_chat_id no es numérico: '{admin_chat_id_raw}'", "Debe ser un número (chat_id Telegram)."))

    # ---- (7) timezone
    tz = (tenant.get("timezone") or "").strip()
    if tz:
        add(_check("timezone_present", "OK", f"timezone='{tz}'"))
    else:
        add(_check("timezone_present", "WARN", "timezone vacío.", "Recomendado: America/La_Paz"))

    # ---- (8) orders_sheet_id
    orders_sheet_id = (tenant.get("orders_sheet_id") or "").strip()
    if not orders_sheet_id:
        add(_check("orders_sheet_id_present", "FAIL", "orders_sheet_id missing.", "Completa orders_sheet_id en TENANTS."))
        return {"ok": False, "tenant_id": tenant_id, "summary": summary, "blockers": blockers, "checks": checks}
    add(_check("orders_sheet_id_present", "OK", "orders_sheet_id presente."))

    # ---- (9) QR config
    qr_file_id = (tenant.get("payment_qr_file_id") or "").strip()
    qr_url_raw = (tenant.get("payment_qr_url") or tenant.get("payment_qr_link") or "").strip()
    qr_url = _normalize_public_qr_url(qr_url_raw)

    if qr_file_id:
        add(_check("payment_qr_source", "OK", "QR por file_id (preferido)."))
    elif qr_url:
        add(_check("payment_qr_source", "OK", "QR por URL pública."))
        if not _is_drive_uc_url(qr_url):
            add(_check("payment_qr_url_format", "WARN", "QR URL no es drive uc?export=download&id=...", "Convierte a formato uc?export=download&id=<ID> para máxima compatibilidad."))
        else:
            add(_check("payment_qr_url_format", "OK", "QR URL tiene formato drive uc correcto."))
    else:
        add(_check("payment_qr_source", "WARN", "No hay QR configurado.", "Configura payment_qr_file_id o payment_qr_url/payment_qr_link en TENANTS."))

    # ---- (10) Open spreadsheet
    try:
        sh = open_spreadsheet_by_key(gc, orders_sheet_id)
        add(_check("orders_sheet_open", "OK", "Spreadsheet de tenant abre correctamente."))
    except Exception as e:
        add(_check("orders_sheet_open", "FAIL", f"No se pudo abrir spreadsheet: {e}", "Comparte el sheet con la service account y verifica el ID."))
        return {"ok": False, "tenant_id": tenant_id, "summary": summary, "blockers": blockers, "checks": checks}

    # ---- (11) Worksheets + headers
    orders_required = ["order_id", "created_at", "tenant_id", "customer_name", "customer_contact", "items", "status", "total_amount"]
    menu_required = ["sku", "name", "price", "active", "category"]

    orders_diag = _find_ws_by_name_or_headers(sh, "Orders", required_headers=orders_required)
    if not orders_diag.get("found"):
        add(_check("orders_ws_found", "FAIL", "Orders worksheet no encontrada.", "Crea una pestaña 'Orders' o asegúrate que exista una con esos headers."))
    else:
        add(_check("orders_ws_found", "OK", f"Orders encontrada: '{orders_diag.get('title')}' ({orders_diag.get('method')})."))
        if orders_diag.get("headers_ok"):
            add(_check("orders_headers_ok", "OK", f"Headers Orders OK (fila {orders_diag.get('header_row')})."))
        else:
            add(_check("orders_headers_ok", "FAIL", f"Headers Orders incompletos en '{orders_diag.get('title')}'.", "Revisa la fila de headers técnicos y nombres exactos."))

    menu_diag = _find_ws_by_name_or_headers(sh, "Menu", required_headers=menu_required)
    if not menu_diag.get("found"):
        add(_check("menu_ws_found", "FAIL", "Menu worksheet no encontrada.", "Crea una pestaña 'Menu' o asegúrate que exista una con esos headers."))
    else:
        add(_check("menu_ws_found", "OK", f"Menu encontrada: '{menu_diag.get('title')}' ({menu_diag.get('method')})."))
        if menu_diag.get("headers_ok"):
            add(_check("menu_headers_ok", "OK", f"Headers Menu OK (fila {menu_diag.get('header_row')})."))
        else:
            add(_check("menu_headers_ok", "FAIL", f"Headers Menu incompletos en '{menu_diag.get('title')}'.", "Revisa la fila de headers técnicos y nombres exactos."))

    ok = (summary["FAIL"] == 0)
    quick_fix = [c["suggested_fix"] for c in checks if c["status"] == "FAIL" and c["suggested_fix"]]
    quick_fix += [c["suggested_fix"] for c in checks if c["status"] == "WARN" and c["suggested_fix"]]

    return {
        "ok": ok,
        "tenant_id": tenant.get("tenant_id"),
        "tenant_id_raw": tenant.get("tenant_id_raw"),
        "summary": summary,
        "blockers": blockers,
        "quick_fix": quick_fix[:15],
        "checks": checks,
        "worksheets": {"orders": orders_diag, "menu": menu_diag},
    }


# Mantengo /healthcheck por compatibilidad (alias simple)
@router.get("/healthcheck")
def healthcheck_tenant(
    tenant_id: str = Query(..., description="tenant_id (ej: resto_demo)"),
    token: str = Query(..., description="ADMIN_TOKEN"),
) -> Dict[str, Any]:
    return tenant_full(tenant_id=tenant_id, token=token)
