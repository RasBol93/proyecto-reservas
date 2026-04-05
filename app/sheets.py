# app/sheets.py — versión hardened (robustez + seguridad sin romper compatibilidad)

import json
from typing import Any, Dict, List, Tuple

import gspread

from app.config import ENV_GCP_CREDS_JSON, ENV_CONFIG_SPREADSHEET_ID, env_required
from app.utils import normalize, log_event
from app.alerts import alert_system_error, alert_sheet_error


# ----------------------------------------
# Caches simples en memoria
# ----------------------------------------

_GSPREAD_CLIENT_CACHE: gspread.Client | None = None
_SPREADSHEET_CACHE: Dict[str, gspread.Spreadsheet] = {}
_WORKSHEET_CACHE: Dict[Tuple[str, str], gspread.Worksheet] = {}


# ----------------------------------------
# Client
# ----------------------------------------

def get_gspread_client() -> gspread.Client:
    global _GSPREAD_CLIENT_CACHE

    if _GSPREAD_CLIENT_CACHE is not None:
        return _GSPREAD_CLIENT_CACHE

    try:
        creds_json = env_required(ENV_GCP_CREDS_JSON)
        info = json.loads(creds_json)

        scopes = [
            "https://www.googleapis.com/auth/spreadsheets",
            "https://www.googleapis.com/auth/drive",
        ]

        client = gspread.service_account_from_dict(info, scopes=scopes)

        _GSPREAD_CLIENT_CACHE = client
        return client

    except Exception as e:
        log_event(
            "gspread_client_error",
            error_type=type(e).__name__,
            error=str(e),
        )
        alert_system_error(
            error=str(e),
            module="sheets.get_gspread_client",
        )
        raise


# ----------------------------------------
# Spreadsheet
# ----------------------------------------

def open_spreadsheet_by_key(gc: gspread.Client, spreadsheet_id: str) -> gspread.Spreadsheet:
    try:
        sid = (spreadsheet_id or "").strip()
        if not sid:
            raise RuntimeError("Missing spreadsheet_id")

        if sid in _SPREADSHEET_CACHE:
            return _SPREADSHEET_CACHE[sid]

        sh = gc.open_by_key(sid)

        if sh is None:
            raise RuntimeError("Spreadsheet not found")

        _SPREADSHEET_CACHE[sid] = sh
        return sh

    except Exception as e:
        log_event(
            "open_spreadsheet_by_key_error",
            spreadsheet_id=(spreadsheet_id or "").strip(),
            error_type=type(e).__name__,
            error=str(e),
        )
        alert_sheet_error(
            tenant_id="",
            error=str(e),
            extra_key="open_spreadsheet_by_key",
        )
        raise


def open_config_spreadsheet(gc: gspread.Client) -> gspread.Spreadsheet:
    try:
        config_id = env_required(ENV_CONFIG_SPREADSHEET_ID).strip()
        if not config_id:
            raise RuntimeError("Missing config spreadsheet id")

        if config_id in _SPREADSHEET_CACHE:
            return _SPREADSHEET_CACHE[config_id]

        sh = gc.open_by_key(config_id)

        if sh is None:
            raise RuntimeError("Config spreadsheet not found")

        _SPREADSHEET_CACHE[config_id] = sh
        return sh

    except Exception as e:
        log_event(
            "open_config_spreadsheet_error",
            error_type=type(e).__name__,
            error=str(e),
        )
        alert_system_error(
            error=str(e),
            module="sheets.open_config_spreadsheet",
        )
        raise


# ----------------------------------------
# Worksheet
# ----------------------------------------

def _spreadsheet_cache_key(spreadsheet: gspread.Spreadsheet) -> str:
    try:
        sid = getattr(spreadsheet, "id", None)
        if sid:
            return str(sid)
    except Exception:
        pass
    return str(id(spreadsheet))


def get_ws(spreadsheet: gspread.Spreadsheet, title: str) -> gspread.Worksheet:
    try:
        t = (title or "").strip()
        if not t:
            raise RuntimeError("Missing worksheet title")

        s_key = _spreadsheet_cache_key(spreadsheet)
        cache_key = (s_key, t)

        if cache_key in _WORKSHEET_CACHE:
            return _WORKSHEET_CACHE[cache_key]

        ws = spreadsheet.worksheet(t)

        if ws is None:
            raise RuntimeError(f"Worksheet '{t}' not found")

        _WORKSHEET_CACHE[cache_key] = ws
        return ws

    except Exception as e:
        log_event(
            "get_ws_error",
            worksheet_title=(title or "").strip(),
            error_type=type(e).__name__,
            error=str(e),
        )
        alert_sheet_error(
            tenant_id="",
            error=f"worksheet '{title}' not found or not accessible: {e}",
            extra_key="get_ws",
        )
        raise


def invalidate_sheet_caches(spreadsheet_id: str | None = None) -> None:
    global _GSPREAD_CLIENT_CACHE

    if spreadsheet_id is None:
        _SPREADSHEET_CACHE.clear()
        _WORKSHEET_CACHE.clear()
        return

    sid = str(spreadsheet_id).strip()
    if not sid:
        return

    _SPREADSHEET_CACHE.pop(sid, None)

    keys_to_delete = [k for k in _WORKSHEET_CACHE.keys() if k[0] == sid]
    for k in keys_to_delete:
        _WORKSHEET_CACHE.pop(k, None)


# ----------------------------------------
# Header helpers
# ----------------------------------------

def detect_header_row(values: List[List[Any]], required_headers: List[str], max_scan: int = 10) -> int:
    try:
        if not values:
            return 1

        req = [normalize(h) for h in required_headers if h]
        scan = values[:max_scan]

        for idx, row in enumerate(scan, start=1):
            row_norm = [normalize(x) for x in row]
            if all(h in row_norm for h in req):
                return idx

        return 1

    except Exception as e:
        log_event(
            "detect_header_row_error",
            error_type=type(e).__name__,
            error=str(e),
        )
        alert_system_error(
            error=str(e),
            module="sheets.detect_header_row",
        )
        return 1


# ----------------------------------------
# Record readers
# ----------------------------------------

def read_records_manual(ws: gspread.Worksheet, required_headers: List[str]) -> List[Dict[str, Any]]:
    try:
        values = ws.get_all_values()
        if not values:
            return []

        header_row = detect_header_row(values, required_headers=required_headers)

        if header_row <= 0 or header_row > len(values):
            return []

        headers = values[header_row - 1]
        headers_norm = [normalize(h) for h in headers]

        records: List[Dict[str, Any]] = []

        for row in values[header_row:]:
            if not any(str(x).strip() for x in row):
                continue

            d: Dict[str, Any] = {}

            for i, h in enumerate(headers_norm):
                if not h:
                    continue

                try:
                    d[h] = row[i] if i < len(row) else ""
                except Exception:
                    d[h] = ""

            records.append(d)

        return records

    except Exception as e:
        ws_title = ""
        try:
            ws_title = getattr(ws, "title", "")
        except Exception:
            ws_title = ""

        log_event(
            "read_records_manual_error",
            worksheet_title=ws_title,
            error_type=type(e).__name__,
            error=str(e),
        )
        alert_sheet_error(
            tenant_id="",
            error=f"read_records_manual failed on worksheet '{ws_title}': {e}",
            extra_key="read_records_manual",
        )
        raise
