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


def open_sheet():
    gc = get_gspread_client()
    return gc.open_by_key(SHEET_ID)


def read_records(tab_name: str) -> List[Dict[str, Any]]:
    sh = open_sheet()
    ws = sh.worksheet(tab_name)
    # get_all_records() usa la fila 1 como encabezados
    return ws.get_all_records()


def norm(s: Any) -> str:
    return str(s).strip()


# =========================
# LOADERS
# =========================
def load_tenants() -> Dict[str, Dict[str, Any]]:
    rows = read_records(TAB_TENANTS)
    out = {}
    for r in rows:
        tid = norm(r.get("tenant_id", "")).lower()
        if not tid:
            continue
        out[tid] = r
    return out


def load_defaults(scope: Optional[str] = None) -> Dict[str, str]:
    """
    Defaults sheet:
      scope | key | value
    """
    rows = read_records(TAB_DEFAULTS)
    d = {}
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
    rules = {}
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
    out = {}
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

