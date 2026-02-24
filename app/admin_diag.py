# app/admin_diag.py

from typing import Any, Dict, Optional
from fastapi import APIRouter, HTTPException, Query

from app.config import ADMIN_TOKEN
from app.sheets import get_gspread_client, open_spreadsheet_by_key
from app.tenants import get_tenant_or_404, validate_tenant_config, load_tenants
from app.utils import log_event

router = APIRouter()


def _require_admin(token: str) -> None:
    if not ADMIN_TOKEN or token != ADMIN_TOKEN:
        raise HTTPException(status_code=401, detail="invalid admin token")


@router.get("/admin/diag/tenant")
def diag_tenant(
    tenant_id: str = Query(...),
    token: str = Query(...),
    force_reload: bool = Query(False),
) -> Dict[str, Any]:
    """
    Diagnóstico humano por tenant:
    - valida config (tokens/secrets/qr/sheet_id)
    - valida acceso a spreadsheet (Sheets)
    - valida worksheets requeridas (ORDERS, Menu)
    """
    _require_admin(token)

    gc = get_gspread_client()

    # opcional: forzar recarga del cache de tenants
    if force_reload:
        load_tenants(gc=gc, force=True)

    tenant = get_tenant_or_404(tenant_id, gc=gc)
    cfg = validate_tenant_config(tenant)

    checks: Dict[str, Any] = {"config": cfg, "sheets": {}, "worksheets": {}}

    # --- Check acceso al spreadsheet ---
    orders_sheet_id = (tenant.get("orders_sheet_id") or "").strip()
    if not orders_sheet_id:
        checks["sheets"]["ok"] = False
        checks["sheets"]["error"] = "orders_sheet_id missing"
        return {
            "ok": False,
            "tenant_id": tenant.get("tenant_id"),
            "checks": checks,
        }

    try:
        sh = open_spreadsheet_by_key(gc, orders_sheet_id)
        checks["sheets"]["ok"] = True
        checks["sheets"]["title"] = sh.title
    except Exception as e:
        checks["sheets"]["ok"] = False
        checks["sheets"]["error"] = f"cannot open spreadsheet: {e}"
        log_event("diag_open_spreadsheet_failed", tenant_id=tenant.get("tenant_id"), error=str(e))
        return {
            "ok": False,
            "tenant_id": tenant.get("tenant_id"),
            "checks": checks,
        }

    # --- Check worksheets esperadas ---
    # Ajusta nombres si en tu proyecto se llaman distinto
    expected = ["ORDERS", "Menu"]
    existing_titles = [ws.title for ws in sh.worksheets()]
    checks["worksheets"]["existing"] = existing_titles
    missing = [t for t in expected if t not in existing_titles]
    checks["worksheets"]["missing_expected"] = missing
    checks["worksheets"]["ok"] = len(missing) == 0

    # Resultado final
    ok = bool(cfg.get("ok")) and bool(checks["sheets"]["ok"]) and bool(checks["worksheets"]["ok"])

    return {
        "ok": ok,
        "tenant_id": tenant.get("tenant_id"),
        "tenant_name": tenant.get("name"),
        "checks": checks,
        "summary": (
            "OK ✅" if ok else
            f"FALTA: {', '.join(cfg.get('errors', []) + (['missing worksheets'] if missing else []))}"
        )
    }
