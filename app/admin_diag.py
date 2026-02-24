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
# Helpers de diagnóstico Sheets
# ---------------------------------

def _ws_has_headers(ws, required_headers: List[str], max_scan_rows: int = 30) -> Tuple[bool, Optional[int], List[str]]:
    """
    Retorna: (ok, header_row_1based, headers_raw_encontrados)
    Busca headers en las primeras max_scan_rows filas.
    """
    try:
        values = ws.get_all_values()
    except Exception:
        return (False, None, [])

    if not values:
        return (False, None, [])

    req = [normalize(h) for h in required_headers]
    scan = values[:max_scan_rows]

    for idx0, row in enumerate(scan):
        row_norm = [normalize(x) for x in row]
        if all(h in row_norm for h in req):
            return (True, idx0 + 1, row)

    return (False, None, [])


def _find_ws_by_name_or_headers(sh, preferred_title: str, required_headers: List[str]) -> Dict[str, Any]:
    """
    - Primero intenta por nombre exacto.
    - Si falla, busca por headers.
    Devuelve dict con info detallada.
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


def _is_drive_uc_url(url: str) -> bool:
    url = (url or "").strip()
    if not url:
        return False
    return bool(re.search(r"^https://drive\.google\.com/uc\?export=download&id=", url))


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

    qr_url = (tenant.get("payment_qr_url") or tenant.get("payment_qr_link") or "").strip()
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
            "webhook_secret_client": _mask(tenant.get("webhook_secret_client")),  # ✅ bug corregido
        },
    }


@router.get("/healthcheck")
def healthcheck_tenant(
    tenant_id: str = Query(..., description="tenant_id (ej: resto_demo)"),
    token: str = Query(..., description="ADMIN_TOKEN"),
) -> Dict[str, Any]:
    """
    Healthcheck “1 clic” para soporte a 100 restaurantes:
    - valida tenant + tokens
    - abre spreadsheet
    - valida tabs Orders/Menu por nombre o por headers
    - valida headers mínimos
    - valida QR configurado
    """
    _require_admin_token(token)

    gc = get_gspread_client()
    tenant = get_tenant_or_404(tenant_id, gc=gc)

    problems: List[str] = []
    warnings: List[str] = []

    # ---- tenant fields
    orders_sheet_id = (tenant.get("orders_sheet_id") or "").strip()
    if not orders_sheet_id:
        problems.append("orders_sheet_id missing")

    admin_bot_token = (tenant.get("admin_bot_token") or "").strip()
    client_bot_token = (tenant.get("client_bot_token") or "").strip()
    secret_admin = (tenant.get("webhook_secret_admin") or "").strip()
    secret_client = (tenant.get("webhook_secret_client") or "").strip()

    if not admin_bot_token:
        problems.append("admin_bot_token missing")
    if not client_bot_token:
        problems.append("client_bot_token missing")
    if not secret_admin:
        problems.append("webhook_secret_admin missing")
    if not secret_client:
        problems.append("webhook_secret_client missing")

    qr_file_id = (tenant.get("payment_qr_file_id") or "").strip()
    qr_url = (tenant.get("payment_qr_url") or tenant.get("payment_qr_link") or "").strip()

    if not qr_file_id and not qr_url:
        problems.append("QR missing: set payment_qr_file_id or payment_qr_url/payment_qr_link")
    if qr_url and not _is_drive_uc_url(qr_url):
        warnings.append("QR url is set but not Drive uc?export=download&id=... (may fail on Telegram)")

    # ---- abrir spreadsheet
    sh = None
    if orders_sheet_id:
        try:
            sh = open_spreadsheet_by_key(gc, orders_sheet_id)
        except Exception as e:
            problems.append(f"cannot open orders_sheet_id: {e}")

    # ---- validar Orders tab (por nombre o headers)
    orders_diag = {"found": False}
    if sh:
        orders_diag = _find_ws_by_name_or_headers(
            sh,
            preferred_title="Orders",
            required_headers=["order_id", "created_at", "tenant_id", "customer_name", "customer_contact", "items", "status", "total_amount"],
        )
        if not orders_diag.get("found"):
            problems.append("Orders worksheet not found (by name or headers)")
        elif not orders_diag.get("headers_ok"):
            problems.append(f"Orders headers missing/invalid in tab '{orders_diag.get('title')}'")

    # ---- validar Menu tab (por nombre o headers)
    menu_diag = {"found": False}
    if sh:
        menu_diag = _find_ws_by_name_or_headers(
            sh,
            preferred_title="Menu",
            required_headers=["sku", "name", "price", "active", "category"],
        )
        if not menu_diag.get("found"):
            problems.append("Menu worksheet not found (by name or headers)")
        elif not menu_diag.get("headers_ok"):
            problems.append(f"Menu headers missing/invalid in tab '{menu_diag.get('title')}'")

    ok = (len(problems) == 0)

    return {
        "ok": ok,
        "tenant_id": tenant.get("tenant_id"),
        "tenant_id_raw": tenant.get("tenant_id_raw"),
        "problems": problems,
        "warnings": warnings,
        "checks": {
            "orders_sheet_id_present": bool(orders_sheet_id),
            "tokens_present": {
                "admin_bot_token": bool(admin_bot_token),
                "client_bot_token": bool(client_bot_token),
                "webhook_secret_admin": bool(secret_admin),
                "webhook_secret_client": bool(secret_client),
            },
            "qr_present": {
                "payment_qr_file_id": bool(qr_file_id),
                "payment_qr_url": bool(qr_url),
                "payment_qr_url_is_drive_uc": _is_drive_uc_url(qr_url),
            },
            "worksheets": {
                "orders": orders_diag,
                "menu": menu_diag,
            },
        },
    }
