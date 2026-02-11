import os
import json
from datetime import datetime
from typing import Dict, Any, List, Optional

from fastapi import FastAPI, HTTPException
import gspread
from google.oauth2.service_account import Credentials

# =========================
# CONFIG (ENV VARS)
# =========================
SHEET_ID = os.getenv("TENANTS_SHEET_ID", "").strip()  # sigue llamándose así por compatibilidad
GCP_CREDENTIALS_JSON = os.getenv("GCP_CREDENTIALS_JSON", "").strip()

TAB_TENANTS = os.getenv("TAB_TENANTS", "Tenants").strip()
TAB_CONTENT = os.getenv("TAB_CONTENT", "Content").strip()
TAB_RULES = os.getenv("TAB_RULES", "BookingRules").strip()
TAB_BOOKINGS = os.getenv("TAB_BOOKINGS", "Bookings").strip()
TAB_DEFAULTS = os.getenv("TAB_DEFAULTS", "Defaults").strip()

# Tabs dentro del sheet de pedidos del tenant (orders_sheet_id)
TAB_MENU = os.getenv("TAB_MENU", "Menu").strip()
TAB_ORDERS = os.getenv("TAB_ORDERS", "Orders").strip()

# Admin token opcional para endpoints sensibles (no lo usamos aún)
ADMIN_TOKEN = os.getenv("ADMIN_TOKEN", "").strip()

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive.readonly",
]

app = FastAPI()

# =========================
# GOOGLE SHEETS HELPERS
# =========================
def _require_env():
    if not SHEET_ID:
        raise RuntimeError("Missing TENANTS_SHEET_ID env var")
    if not GCP_CREDENTIALS_JSON:
        raise RuntimeError("Missing GCP_CREDENTIALS_JSON env var")


def get_gspread_client():
    _require_env()
    info = json.loads(GCP_CREDENTIALS_JSON)
    creds = Credentials.from_service_account_info(info, scopes=SCOPES)
    return gspread.authorize(creds)


def open_sheet_by_id(sheet_id: str):
    gc = get_gspread_client()
    return gc.open_by_key(sheet_id)


def open_main_sheet():
    return open_sheet_by_id(SHEET_ID)


def norm(s: Any) -> str:
    return str(s).strip()


def as_bool(v: Any) -> bool:
    """
    Convierte valores de Google Sheets / Python a boolean robusto.
    Acepta: True/False, "TRUE"/"FALSE", "true"/"false", 1/0, "1"/"0", "yes"/"no"
    """
    if isinstance(v, bool):
        return v
    if v is None:
        return False
    if isinstance(v, (int, float)):
        return v != 0
    s = str(v).strip().lower()
    return s in ("true", "1", "yes", "y", "si", "sí", "on")


def read_records(tab_name: str) -> List[Dict[str, Any]]:
    sh = open_main_sheet()
    ws = sh.worksheet(tab_name)
    # get_all_records() usa la fila 1 como encabezados
    return ws.get_all_records()


def read_records_manual_ws(ws: gspread.Worksheet, header_row: int = 1, data_start_row: int = 2) -> List[Dict[str, Any]]:
    """
    Lectura manual:
    - headers desde header_row
    - datos desde data_start_row
    Esto permite tener una fila 2 "en español" sin romper el parseo.
    """
    values = ws.get_all_values()
    if len(values) < header_row:
        return []

    headers = values[header_row - 1]
    headers_norm = [h.strip() for h in headers]

    out: List[Dict[str, Any]] = []
    for row in values[data_start_row - 1:]:
        # si la fila está completamente vacía, saltar
        if not any(cell.strip() for cell in row):
            continue

        rec: Dict[str, Any] = {}
        for i, key in enumerate(headers_norm):
            if not key:
                continue
            rec[key] = row[i].strip() if i < len(row) else ""
        out.append(rec)

    return out


def read_records_from_tenant_sheet(tenant_sheet_id: str, tab_name: str, header_row: int = 1, data_start_row: int = 2) -> List[Dict[str, Any]]:
    sh = open_sheet_by_id(tenant_sheet_id)
    ws = sh.worksheet(tab_name)
    return read_records_manual_ws(ws, header_row=header_row, data_start_row=data_start_row)

# =========================
# LOADERS
# =========================
def load_tenants() -> Dict[str, Dict[str, Any]]:
    rows = read_records(TAB_TENANTS)
    out: Dict[str, Dict[str, Any]] = {}
    for r in rows:
        tid = norm(r.get("tenant_id", "")).lower()
        if not tid:
            continue

        # Normalizamos flags para que TRUE/FALSE funcionen aunque vengan como bool
        r["bookings_enabled_bool"] = as_bool(r.get("bookings_enabled", False))
        r["orders_enabled_bool"] = as_bool(r.get("orders_enabled", False))
        r["active_bool"] = as_bool(r.get("active", True))

        out[tid] = r
    return out


def load_defaults(scope: Optional[str] = None) -> Dict[str, str]:
    """
    Defaults sheet:
      scope | key | value
    """
    rows = read_records(TAB_DEFAULTS)
    d: Dict[str, str] = {}
    for r in rows:
        sc = norm(r.get("scope", "")).lower()
        k = norm(r.get("key", ""))
        v = norm(r.get("value", ""))
        if not sc or not k:
            continue
        if scope and sc != scope.lower():
            continue
        d[k] = v
    return d


def load_booking_rules_for_tenant(tenant_id: str) -> Dict[str, str]:
    """
    BookingRules sheet:
      tenant_id | rule_key | value
    """
    rows = read_records(TAB_RULES)
    rules: Dict[str, str] = {}
    tid = tenant_id.lower().strip()
    for r in rows:
        r_tid = norm(r.get("tenant_id", "")).lower()
        if r_tid != tid:
            continue
        key = norm(r.get("rule_key", ""))
        val = norm(r.get("value", ""))
        if key:
            rules[key] = val
    return rules


def get_effective_rules(tenant_id: str) -> Dict[str, Any]:
    """
    Priority:
      BookingRules(tenant) > Defaults(scope=booking_rule)
    """
    defaults = load_defaults(scope="booking_rule")
    overrides = load_booking_rules_for_tenant(tenant_id)

    effective = dict(defaults)
    effective.update(overrides)
    return effective


def load_content_for_tenant(tenant_id: str) -> Dict[str, Dict[str, str]]:
    """
    Content sheet:
      tenant_id | content_key | type | value
    Returns dict: {content_key: {"type":..., "value":...}}
    """
    rows = read_records(TAB_CONTENT)
    out: Dict[str, Dict[str, str]] = {}
    tid = tenant_id.lower().strip()
    for r in rows:
        r_tid = norm(r.get("tenant_id", "")).lower()
        if r_tid != tid:
            continue
        ck = norm(r.get("content_key", ""))
        tp = norm(r.get("type", "")).lower()
        val = norm(r.get("value", ""))
        if not ck:
            continue
        out[ck] = {"type": tp, "value": val}
    return out

# =========================
# ENDPOINTS (E2.1)
# =========================
@app.get("/")
def root():
    return {"status": "ok", "service": "proyecto-reservas", "ts": datetime.utcnow().isoformat()}


@app.get("/debug/tenants")
def debug_tenants():
    try:
        tenants = load_tenants()
        return {
            "ok": True,
            "count": len(tenants),
            "tenant_ids": sorted(list(tenants.keys())),
            "sample": tenants.get("resto_demo")  # útil para ver flags
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/debug/defaults")
def debug_defaults():
    try:
        defaults = load_defaults(scope="booking_rule")
        return {"ok": True, "count": len(defaults), "defaults": defaults}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/debug/rules/{tenant_id}")
def debug_rules(tenant_id: str):
    try:
        tenants = load_tenants()
        tid = tenant_id.lower().strip()
        if tid not in tenants:
            raise HTTPException(status_code=404, detail=f"Unknown tenant_id: {tid}")

        defaults = load_defaults(scope="booking_rule")
        overrides = load_booking_rules_for_tenant(tid)
        effective = dict(defaults)
        effective.update(overrides)

        return {
            "ok": True,
            "tenant_id": tid,
            "defaults_count": len(defaults),
            "overrides_count": len(overrides),
            "overrides": overrides,
            "effective": effective,
        }
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/debug/content/{tenant_id}")
def debug_content(tenant_id: str):
    try:
        tenants = load_tenants()
        tid = tenant_id.lower().strip()
        if tid not in tenants:
            raise HTTPException(status_code=404, detail=f"Unknown tenant_id: {tid}")

        content = load_content_for_tenant(tid)
        return {"ok": True, "tenant_id": tid, "count": len(content), "content": content}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# =========================
# ORDERS: MENU (E2.2)
# =========================
@app.get("/menu")
def get_menu(tenant_id: str):
    """
    Lee el menú del tenant desde su orders_sheet_id.
    Respeta tu diseño:
      - Fila 1: headers en inglés (sku,name,price,active,category)
      - Fila 2: explicación en español (se ignora)
      - Fila 3+: data real
    """
    tenants = load_tenants()
    tid = tenant_id.lower().strip()

    if tid not in tenants:
        raise HTTPException(status_code=404, detail=f"Unknown tenant_id: {tid}")

    t = tenants[tid]

    if not t.get("active_bool", True):
        raise HTTPException(status_code=400, detail=f"Tenant inactive: {tid}")

    if not t.get("orders_enabled_bool", False):
        raise HTTPException(status_code=400, detail=f"Orders not enabled for tenant: {tid}")

    orders_sheet_id = norm(t.get("orders_sheet_id", ""))
    if not orders_sheet_id:
        raise HTTPException(status_code=400, detail=f"Missing orders_sheet_id for tenant: {tid}")

    # Menu: headers fila 1, datos desde fila 3
    rows = read_records_from_tenant_sheet(
        tenant_sheet_id=orders_sheet_id,
        tab_name=TAB_MENU,
        header_row=1,
        data_start_row=3
    )

    # Filtrar solo active=TRUE (pero soporta bool/string)
    items = []
    for r in rows:
        if as_bool(r.get("active", False)):
            items.append({
                "sku": norm(r.get("sku", "")),
                "name": norm(r.get("name", "")),
                "price": float(norm(r.get("price", "0")) or 0),
                "category": norm(r.get("category", "")),
            })

    # Agrupar por categoría
    categories: Dict[str, List[Dict[str, Any]]] = {}
    for it in items:
        cat = it.get("category") or "Otros"
        categories.setdefault(cat, []).append(it)

    return {
        "ok": True,
        "tenant_id": tid,
        "total_items": len(items),
        "categories": [{"name": k, "items": v} for k, v in categories.items()],
        "items": items,
    }
