# app/admin_diag.py

import os
import re
from typing import Any, Dict, Optional, List, Tuple

from fastapi import APIRouter, HTTPException, Query

from app.sheets import get_gspread_client, open_spreadsheet_by_key, get_recent_sheets_request_summaries
from app.tenants import get_tenant_or_404, tenants_cache_info
from app.utils import normalize
from app.admin_settings import (
    ADMIN_SETTINGS_SHEET_NAME,
    REQUIRED_ADMIN_SETTINGS_HEADERS,
    resolve_business_status_dict,
)
from app.menu import load_menu_admin_index, group_menu_admin_by_category, get_menu_runtime_status
from app.orders import get_order_by_id
from app.alerts import send_test_alert

router = APIRouter(prefix="/admin/diag", tags=["admin"])


def _get_admin_token() -> str:
    return (os.getenv("ADMIN_TOKEN") or "").strip()


def _require_admin_token(token: str) -> None:
    admin_token = _get_admin_token()
    if not admin_token:
        raise HTTPException(status_code=503, detail="ADMIN_TOKEN no está configurado en variables de entorno (Render).")
    if (token or "").strip() != admin_token:
        raise HTTPException(status_code=401, detail="Invalid admin token")


def _mask(v: Optional[str]) -> str:
    v = (v or "").strip()
    if not v:
        return ""
    if len(v) <= 10:
        return "***"
    return v[:4] + "..." + v[-4:]


def _is_drive_uc_url(url: str) -> bool:
    url = (url or "").strip()
    if not url:
        return False
    return bool(re.search(r"^https://drive\.google\.com/uc\?export=download&id=", url))


def _drive_file_id_from_url(url: str) -> Optional[str]:
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
    url = (url or "").strip()
    if not url:
        return ""
    file_id = _drive_file_id_from_url(url)
    if file_id:
        return f"https://drive.google.com/uc?export=download&id={file_id}"
    return url


def _col_letter(n: int) -> str:
    s = ""
    while n > 0:
        n, r = divmod(n - 1, 26)
        s = chr(65 + r) + s
    return s


def _ws_sample_values(ws, max_rows: int = 30, max_cols: int = 30) -> List[List[str]]:
    end_col = _col_letter(max_cols)
    rng = f"A1:{end_col}{max_rows}"
    try:
        return ws.get(rng)
    except Exception:
        try:
            return ws.get_all_values()[:max_rows]
        except Exception:
            return []


def _ws_has_headers(ws, required_headers: List[str], max_scan_rows: int = 30) -> Tuple[bool, Optional[int], List[str]]:
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


def _check(check_id: str, status: str, details: str, suggested_fix: str = "") -> Dict[str, Any]:
    return {"id": check_id, "status": status, "details": details, "suggested_fix": suggested_fix}


def _is_token_shape_ok(tok: str) -> bool:
    tok = (tok or "").strip()
    if not tok:
        return False
    return (":" in tok) and (len(tok) >= 20)


def _local_validate_tenant_config(tenant: Dict[str, Any]) -> Dict[str, Any]:
    errors: List[str] = []
    warnings: List[str] = []

    tid = tenant.get("tenant_id") or "?"
    orders_sheet_id = (tenant.get("orders_sheet_id") or "").strip()

    if not orders_sheet_id:
        errors.append("orders_sheet_id missing")

    admin_bot_token = (tenant.get("admin_bot_token") or "").strip()
    client_bot_token = (tenant.get("client_bot_token") or "").strip()
    secret_admin = (tenant.get("webhook_secret_admin") or "").strip()
    secret_client = (tenant.get("webhook_secret_client") or "").strip()

    if not admin_bot_token:
        warnings.append("admin_bot_token missing (admin bot no funcionará)")
    if not client_bot_token:
        warnings.append("client_bot_token missing (client bot no funcionará)")

    if admin_bot_token and ":" not in admin_bot_token:
        warnings.append("admin_bot_token shape looks wrong (expected ':')")
    if client_bot_token and ":" not in client_bot_token:
        warnings.append("client_bot_token shape looks wrong (expected ':')")

    if not secret_admin:
        warnings.append("webhook_secret_admin missing")
    if not secret_client:
        warnings.append("webhook_secret_client missing")

    if secret_admin and len(secret_admin) < 8:
        warnings.append("webhook_secret_admin too short (recommend >= 8)")
    if secret_client and len(secret_client) < 8:
        warnings.append("webhook_secret_client too short (recommend >= 8)")

    orders_enabled = bool(tenant.get("orders_enabled"))
    qr_file_id = (tenant.get("payment_qr_file_id") or "").strip()
    qr_url = (tenant.get("payment_qr_url") or tenant.get("payment_qr_link") or "").strip()

    if orders_enabled and not (qr_file_id or qr_url):
        errors.append("QR missing: set payment_qr_file_id or payment_qr_url/payment_qr_link")

    admin_chat_id = (tenant.get("admin_chat_id") or "").strip()
    if orders_enabled and not admin_chat_id:
        warnings.append("admin_chat_id missing (no podrás recibir notificaciones/confirmar pagos)")

    folder_id = (tenant.get("product_photos_drive_folder_id") or "").strip()
    if not folder_id:
        warnings.append("product_photos_drive_folder_id missing (no podrás subir fotos de productos a Drive)")

    return {
        "tenant_id": tid,
        "orders_enabled": orders_enabled,
        "has_orders_sheet_id": bool(orders_sheet_id),
        "has_admin_bot_token": bool(admin_bot_token),
        "has_client_bot_token": bool(client_bot_token),
        "has_webhook_secret_admin": bool(secret_admin),
        "has_webhook_secret_client": bool(secret_client),
        "has_admin_chat_id": bool(admin_chat_id),
        "has_qr_file_id": bool(qr_file_id),
        "has_qr_url": bool(qr_url),
        "has_product_photos_drive_folder_id": bool(folder_id),
        "errors": errors,
        "warnings": warnings,
        "ok": len(errors) == 0,
    }


def _safe_order_ws(sh):
    try:
        return sh.worksheet("ORDERS")
    except Exception:
        try:
            return sh.worksheet("Orders")
        except Exception:
            return sh.get_worksheet(0)


def _safe_count_data_rows(ws) -> int:
    try:
        values = ws.get_all_values()
        if not values:
            return 0
        return max(0, len(values) - 1)
    except Exception:
        return 0


def _safe_menu_runtime(orders_sh) -> Dict[str, Any]:
    try:
        menu_idx = load_menu_admin_index(orders_sh, force=False)
        cats = group_menu_admin_by_category(menu_idx)

        active_products = 0
        inactive_products = 0
        with_photo_url = 0
        with_photo_file_id = 0
        without_photo = 0

        for _, item in menu_idx.items():
            active = bool(item.get("active", False))
            if active:
                active_products += 1
            else:
                inactive_products += 1

            photo_url = str(item.get("photo_url") or "").strip()
            photo_file_id = str(item.get("photo_file_id") or "").strip()
            if photo_url:
                with_photo_url += 1
            elif photo_file_id:
                with_photo_file_id += 1
            else:
                without_photo += 1

        return {
            "ok": True,
            "categories_count": len(cats),
            "products_count": len(menu_idx),
            "active_products": active_products,
            "inactive_products": inactive_products,
            "with_photo_url": with_photo_url,
            "with_photo_file_id": with_photo_file_id,
            "without_photo": without_photo,
            "category_names": sorted(list(cats.keys()), key=lambda x: normalize(x))[:50],
        }
    except Exception as e:
        return {
            "ok": False,
            "error": str(e),
        }


def _safe_orders_runtime(orders_sh, order_id: str = "") -> Dict[str, Any]:
    try:
        ws = _safe_order_ws(orders_sh)
        total_rows = _safe_count_data_rows(ws)

        out: Dict[str, Any] = {
            "ok": True,
            "orders_rows_count": total_rows,
        }

        oid = (order_id or "").strip()
        if oid:
            order = get_order_by_id(orders_sh, oid)
            out["lookup_order_id"] = oid
            out["order_found"] = bool(order)
            if order:
                out["order"] = {
                    "order_id": order.get("order_id"),
                    "tenant_id": order.get("tenant_id"),
                    "customer_name": order.get("customer_name"),
                    "customer_contact": order.get("customer_contact"),
                    "status": order.get("status"),
                    "source": order.get("source"),
                    "created_at": order.get("created_at"),
                    "requested_time": order.get("requested_time"),
                    "total_amount": order.get("total_amount"),
                    "payment_proof_type": order.get("payment_proof_type"),
                    "payment_proof_file_id_present": bool((order.get("payment_proof_file_id") or "").strip()),
                    "payment_confirmed_at": order.get("payment_confirmed_at"),
                }
        return out
    except Exception as e:
        return {
            "ok": False,
            "error": str(e),
        }


@router.get("/tenant")
def diag_tenant(tenant_id: str = Query(...), token: str = Query(...)) -> Dict[str, Any]:
    _require_admin_token(token)
    gc = get_gspread_client()
    tenant = get_tenant_or_404(tenant_id, gc=gc)

    qr_url_raw = (tenant.get("payment_qr_url") or tenant.get("payment_qr_link") or "").strip()
    qr_url_norm = _normalize_public_qr_url(qr_url_raw)
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
            "payment_qr_url_present": bool(qr_url_norm),
            "payment_qr_file_id_masked": _mask(qr_file_id),
            "payment_qr_url_preview": qr_url_norm[:200],
            "payment_qr_url_is_drive_uc": _is_drive_uc_url(qr_url_norm),
            "payment_qr_url_normalized": qr_url_norm[:200],
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
            "webhook_secret_client": _mask(tenant.get("webhook_secret_client")),
        },
    }


@router.get("/tenant_full")
def tenant_full(tenant_id: str = Query(...), token: str = Query(...)) -> Dict[str, Any]:
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

    gc = get_gspread_client()
    try:
        tenant = get_tenant_or_404(tenant_id, gc=gc)
        add(_check("tenant_found_active", "OK", "Tenant encontrado y activo."))
    except HTTPException as e:
        add(_check("tenant_found_active", "FAIL", f"Tenant no válido: {e.detail}", "Revisar tenant_id y columna active en TENANTS."))
        return {"ok": False, "tenant_id": tenant_id, "summary": summary, "blockers": blockers, "checks": checks}

    raw = (tenant.get("tenant_id_raw") or "").strip()
    norm = (tenant.get("tenant_id") or "").strip()
    if raw and normalize(raw).replace(" ", "") != norm:
        add(_check("tenant_id_normalization", "WARN", f"tenant_id_raw='{raw}' normaliza distinto.", "Evita espacios/tildes raras en tenant_id."))
    else:
        add(_check("tenant_id_normalization", "OK", "tenant_id consistente (raw vs normalizado)."))

    orders_enabled = bool(tenant.get("orders_enabled", False))
    add(_check(
        "orders_enabled",
        "OK" if orders_enabled else "WARN",
        f"orders_enabled={'true' if orders_enabled else 'false'}",
        "Si este tenant debe vender, pon orders_enabled=TRUE." if not orders_enabled else "",
    ))

    admin_bot_token = (tenant.get("admin_bot_token") or "").strip()
    client_bot_token = (tenant.get("client_bot_token") or "").strip()
    secret_admin = (tenant.get("webhook_secret_admin") or "").strip()
    secret_client = (tenant.get("webhook_secret_client") or "").strip()

    add(_check(
        "admin_bot_token_present",
        "OK" if admin_bot_token else "FAIL",
        "admin_bot_token presente." if admin_bot_token else "admin_bot_token missing.",
        "Completa admin_bot_token en TENANTS." if not admin_bot_token else "",
    ))

    add(_check(
        "client_bot_token_present",
        "OK" if client_bot_token else "FAIL",
        "client_bot_token presente." if client_bot_token else "client_bot_token missing.",
        "Completa client_bot_token en TENANTS." if not client_bot_token else "",
    ))

    add(_check(
        "webhook_secret_admin_present",
        "OK" if secret_admin else "FAIL",
        "webhook_secret_admin presente." if secret_admin else "webhook_secret_admin missing.",
        "Completa webhook_secret_admin en TENANTS." if not secret_admin else "",
    ))

    add(_check(
        "webhook_secret_client_present",
        "OK" if secret_client else "FAIL",
        "webhook_secret_client presente." if secret_client else "webhook_secret_client missing.",
        "Completa webhook_secret_client en TENANTS." if not secret_client else "",
    ))

    add(_check(
        "admin_bot_token_shape",
        "OK" if (admin_bot_token and _is_token_shape_ok(admin_bot_token)) else ("WARN" if admin_bot_token else "WARN"),
        "admin_bot_token parece correcto." if admin_bot_token else "Sin token para validar.",
        "Verifica token BotFather." if (admin_bot_token and not _is_token_shape_ok(admin_bot_token)) else "",
    ))

    add(_check(
        "client_bot_token_shape",
        "OK" if (client_bot_token and _is_token_shape_ok(client_bot_token)) else ("WARN" if client_bot_token else "WARN"),
        "client_bot_token parece correcto." if client_bot_token else "Sin token para validar.",
        "Verifica token BotFather." if (client_bot_token and not _is_token_shape_ok(client_bot_token)) else "",
    ))

    admin_chat_id_raw = (tenant.get("admin_chat_id") or "").strip()
    if not admin_chat_id_raw:
        add(_check("admin_chat_id_present", "WARN", "admin_chat_id vacío.", "Usa /id en el bot admin y pega ese número en TENANTS."))
    else:
        try:
            int(admin_chat_id_raw)
            add(_check("admin_chat_id_present", "OK", "admin_chat_id presente y numérico."))
        except Exception:
            add(_check("admin_chat_id_present", "FAIL", f"admin_chat_id no es numérico: '{admin_chat_id_raw}'", "Debe ser un número (chat_id Telegram)."))

    tz = (tenant.get("timezone") or "").strip()
    add(_check(
        "timezone_present",
        "OK" if tz else "WARN",
        f"timezone='{tz}'" if tz else "timezone vacío.",
        "Recomendado: America/La_Paz" if not tz else "",
    ))

    orders_sheet_id = (tenant.get("orders_sheet_id") or "").strip()
    if not orders_sheet_id:
        add(_check("orders_sheet_id_present", "FAIL", "orders_sheet_id missing.", "Completa orders_sheet_id en TENANTS."))
        return {"ok": False, "tenant_id": tenant_id, "summary": summary, "blockers": blockers, "checks": checks}
    add(_check("orders_sheet_id_present", "OK", "orders_sheet_id presente."))

    qr_file_id = (tenant.get("payment_qr_file_id") or "").strip()
    qr_url_raw = (tenant.get("payment_qr_url") or tenant.get("payment_qr_link") or "").strip()
    qr_url_norm = _normalize_public_qr_url(qr_url_raw)

    if qr_file_id:
        add(_check("payment_qr_source", "OK", "QR por file_id (preferido)."))
    elif qr_url_norm:
        add(_check("payment_qr_source", "OK", "QR por URL pública."))
        add(_check(
            "payment_qr_url_format",
            "OK" if _is_drive_uc_url(qr_url_norm) else "WARN",
            "QR URL formato OK." if _is_drive_uc_url(qr_url_norm) else "QR URL no es drive uc?export=download&id=...",
            "Convierte a formato uc?export=download&id=<ID>." if not _is_drive_uc_url(qr_url_norm) else "",
        ))
    else:
        add(_check(
            "payment_qr_source",
            "WARN",
            "No hay QR configurado.",
            "Configura payment_qr_file_id o payment_qr_url/payment_qr_link en TENANTS.",
        ))

    try:
        sh = open_spreadsheet_by_key(gc, orders_sheet_id)
        add(_check("orders_sheet_open", "OK", "Spreadsheet de tenant abre correctamente."))
    except Exception as e:
        add(_check("orders_sheet_open", "FAIL", f"No se pudo abrir spreadsheet: {e}", "Comparte el sheet con la service account y verifica el ID."))
        return {"ok": False, "tenant_id": tenant_id, "summary": summary, "blockers": blockers, "checks": checks}

    orders_required = ["order_id", "created_at", "tenant_id", "customer_name", "customer_contact", "items", "status", "total_amount"]
    menu_required = ["sku", "name", "price", "active", "category"]
    admin_settings_required = REQUIRED_ADMIN_SETTINGS_HEADERS

    orders_diag = _find_ws_by_name_or_headers(sh, "Orders", required_headers=orders_required)
    if not orders_diag.get("found"):
        add(_check("orders_ws_found", "FAIL", "Orders worksheet no encontrada.", "Crea 'Orders' o una hoja con esos headers."))
    else:
        add(_check("orders_ws_found", "OK", f"Orders encontrada: '{orders_diag.get('title')}' ({orders_diag.get('method')})."))
        add(_check(
            "orders_headers_ok",
            "OK" if orders_diag.get("headers_ok") else "FAIL",
            f"Headers Orders OK (fila {orders_diag.get('header_row')})." if orders_diag.get("headers_ok") else "Headers Orders incompletos.",
            "Revisa headers técnicos exactos." if not orders_diag.get("headers_ok") else "",
        ))

    menu_diag = _find_ws_by_name_or_headers(sh, "Menu", required_headers=menu_required)
    if not menu_diag.get("found"):
        add(_check("menu_ws_found", "FAIL", "Menu worksheet no encontrada.", "Crea 'Menu' o una hoja con esos headers."))
    else:
        add(_check("menu_ws_found", "OK", f"Menu encontrada: '{menu_diag.get('title')}' ({menu_diag.get('method')})."))
        add(_check(
            "menu_headers_ok",
            "OK" if menu_diag.get("headers_ok") else "FAIL",
            f"Headers Menu OK (fila {menu_diag.get('header_row')})." if menu_diag.get("headers_ok") else "Headers Menu incompletos.",
            "Revisa headers técnicos exactos." if not menu_diag.get("headers_ok") else "",
        ))

    admin_settings_diag = _find_ws_by_name_or_headers(sh, ADMIN_SETTINGS_SHEET_NAME, required_headers=admin_settings_required)
    if not admin_settings_diag.get("found"):
        add(_check(
            "admin_settings_ws_found",
            "FAIL",
            "AdminSettings worksheet no encontrada.",
            "Crea 'AdminSettings' con headers: key,value,active,scope,updated_at,updated_by,notes",
        ))
    else:
        add(_check(
            "admin_settings_ws_found",
            "OK",
            f"AdminSettings encontrada: '{admin_settings_diag.get('title')}' ({admin_settings_diag.get('method')}).",
        ))
        add(_check(
            "admin_settings_headers_ok",
            "OK" if admin_settings_diag.get("headers_ok") else "FAIL",
            f"Headers AdminSettings OK (fila {admin_settings_diag.get('header_row')})." if admin_settings_diag.get("headers_ok") else "Headers AdminSettings incompletos.",
            "Revisa headers técnicos exactos." if not admin_settings_diag.get("headers_ok") else "",
        ))

    ok = (summary["FAIL"] == 0)

    seen = set()
    quick_fix = []
    for c in checks:
        sf = (c.get("suggested_fix") or "").strip()
        if sf and sf not in seen:
            seen.add(sf)
            quick_fix.append(sf)

    return {
        "ok": ok,
        "tenant_id": tenant.get("tenant_id"),
        "tenant_id_raw": tenant.get("tenant_id_raw"),
        "summary": summary,
        "blockers": blockers,
        "quick_fix": quick_fix[:20],
        "checks": checks,
        "worksheets": {
            "orders": orders_diag,
            "menu": menu_diag,
            "admin_settings": admin_settings_diag,
        },
        "qr_debug": {
            "payment_qr_url_raw": qr_url_raw[:200],
            "payment_qr_url_normalized": qr_url_norm[:200],
            "payment_qr_url_is_drive_uc": _is_drive_uc_url(qr_url_norm),
            "payment_qr_file_id_present": bool(qr_file_id),
        },
    }


@router.get("/business_status")
def business_status(tenant_id: str = Query(...), token: str = Query(...)) -> Dict[str, Any]:
    _require_admin_token(token)

    gc = get_gspread_client()
    tenant = get_tenant_or_404(tenant_id, gc=gc)

    orders_sheet_id = (tenant.get("orders_sheet_id") or "").strip()
    if not orders_sheet_id:
        raise HTTPException(status_code=500, detail="orders_sheet_id missing for tenant")

    tenant_tz = (tenant.get("timezone") or "America/La_Paz").strip()
    sh = open_spreadsheet_by_key(gc, orders_sheet_id)

    data = resolve_business_status_dict(sh, tenant_tz=tenant_tz)

    return {
        "ok": True,
        "tenant_id": tenant.get("tenant_id"),
        "timezone": tenant_tz,
        "business_status": data,
    }


@router.get("/runtime")
def runtime_diag(
    tenant_id: str = Query(...),
    token: str = Query(...),
    order_id: str = Query(default=""),
) -> Dict[str, Any]:
    _require_admin_token(token)

    gc = get_gspread_client()
    tenant = get_tenant_or_404(tenant_id, gc=gc)
    cfg = _local_validate_tenant_config(tenant)

    orders_sheet_id = (tenant.get("orders_sheet_id") or "").strip()
    if not orders_sheet_id:
        raise HTTPException(status_code=500, detail="orders_sheet_id missing for tenant")

    tenant_tz = (tenant.get("timezone") or "America/La_Paz").strip()
    sh = open_spreadsheet_by_key(gc, orders_sheet_id)

    business = resolve_business_status_dict(sh, tenant_tz=tenant_tz)
    menu_runtime = _safe_menu_runtime(sh)
    orders_runtime = _safe_orders_runtime(sh, order_id=order_id)

    runtime_summary = {
        "tenant_ok": True,
        "tenant_config_ok": bool(cfg.get("ok")),
        "business_accepts_orders_now": bool(business.get("accepts_orders_now")),
        "menu_ok": bool(menu_runtime.get("ok")),
        "orders_ok": bool(orders_runtime.get("ok")),
    }

    return {
        "ok": True,
        "tenant_id": tenant.get("tenant_id"),
        "tenant_id_raw": tenant.get("tenant_id_raw"),
        "timezone": tenant_tz,
        "runtime_summary": runtime_summary,
        "tenant_cache": tenants_cache_info(),
        "tenant_config": cfg,
        "business_status": business,
        "menu_runtime": menu_runtime,
        "orders_runtime": orders_runtime,
    }


@router.get("/menu_snapshot")
def menu_snapshot_diag(
    tenant_id: str = Query(...),
    token: str = Query(...),
) -> Dict[str, Any]:
    _require_admin_token(token)

    gc = get_gspread_client()
    tenant = get_tenant_or_404(tenant_id, gc=gc)

    orders_sheet_id = (tenant.get("orders_sheet_id") or "").strip()
    if not orders_sheet_id:
        raise HTTPException(status_code=500, detail="orders_sheet_id missing for tenant")

    sh = open_spreadsheet_by_key(gc, orders_sheet_id)

    return {
        "ok": True,
        "tenant_id": tenant.get("tenant_id"),
        "menu_snapshot": get_menu_runtime_status(sh),
    }


@router.get("/sheets_recent")
def sheets_recent_diag(
    token: str = Query(...),
    limit: int = Query(default=20),
    min_reads: int = Query(default=1),
    had_429_only: bool = Query(default=False),
) -> Dict[str, Any]:
    _require_admin_token(token)

    requests = get_recent_sheets_request_summaries(
        limit=limit,
        min_reads=min_reads,
        had_429_only=had_429_only,
    )

    return {
        "ok": True,
        "count": len(requests),
        "requests": requests,
    }


@router.get("/test_alert")
def test_alert(token: str = Query(...), message: str = Query(default="Prueba manual desde /admin/diag/test_alert")) -> Dict[str, Any]:
    _require_admin_token(token)
    result = send_test_alert(message=message)
    return {
        "ok": bool(result.get("ok")),
        "result": result,
    }


@router.get("/healthcheck")
def healthcheck_tenant(tenant_id: str = Query(...), token: str = Query(...)) -> Dict[str, Any]:
    return tenant_full(tenant_id=tenant_id, token=token)
