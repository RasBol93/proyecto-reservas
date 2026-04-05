# app/sheets.py — versión optimizada con caché simple de client, spreadsheet y worksheet
# hardened incremental: misma estructura, mismos contratos, más robustez

import json
import time
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
# Retry policy simple
# ----------------------------------------

_SHEETS_RETRY_ATTEMPTS = 3
_SHEETS_RETRY_SLEEP_SECONDS = 0.35


def _sleep_before_retry(attempt_index: int) -> None:
    try:
        # backoff simple y corto: 0.35, 0.70, 1.05...
        time.sleep(_SHEETS_RETRY_SLEEP_SECONDS * max(1, attempt_index))
    except Exception:
        pass


def _should_retry_exception(exc: Exception) -> bool:
    msg = str(exc or "").lower()

    retry_signals = (
        "timed out",
        "timeout",
        "temporarily unavailable",
        "connection reset",
        "connection aborted",
        "connection refused",
        "remote end closed connection",
        "service unavailable",
        "internal error",
        "bad gateway",
        "gateway timeout",
        "rate limit",
        "quota",
        "429",
        "500",
        "502",
        "503",
        "504",
    )

    return any(signal in msg for signal in retry_signals)


def _call_with_retry(fn, *, op_name: str, log_fields: Dict[str, Any] | None = None):
    last_exc: Exception | None = None
    extra = dict(log_fields or {})

    for attempt in range(1, _SHEETS_RETRY_ATTEMPTS + 1):
        try:
            return fn()
        except Exception as e:
            last_exc = e

            try:
                log_event(
                    "sheets_retryable_error",
                    op_name=op_name,
                    attempt=attempt,
                    max_attempts=_SHEETS_RETRY_ATTEMPTS,
                    retry=bool(attempt < _SHEETS_RETRY_ATTEMPTS and _should_retry_exception(e)),
                    error_type=type(e).__name__,
                    error=str(e),
                    **extra,
                )
            except Exception:
                pass

            if attempt >= _SHEETS_RETRY_ATTEMPTS or not _should_retry_exception(e):
                break

            _sleep_before_retry(attempt)

    if last_exc is not None:
        raise last_exc

    raise RuntimeError(f"{op_name} failed without exception")


# ----------------------------------------
# Client
# ----------------------------------------

def get_gspread_client() -> gspread.Client:
    """
    Crea (o reutiliza) un cliente de gspread usando el JSON de service account guardado en env.
    Env esperado: GCP_CREDENTIALS_JSON (string JSON completo)
    """
    global _GSPREAD_CLIENT_CACHE

    if _GSPREAD_CLIENT_CACHE is not None:
        return _GSPREAD_CLIENT_CACHE

    try:
        creds_json = env_required(ENV_GCP_CREDS_JSON)
        if not str(creds_json or "").strip():
            raise RuntimeError("Empty GCP credentials JSON")

        info = json.loads(creds_json)
        if not isinstance(info, dict):
            raise RuntimeError("Invalid GCP credentials JSON: expected object")

        scopes = [
            "https://www.googleapis.com/auth/spreadsheets",
            "https://www.googleapis.com/auth/drive",
        ]

        def _create_client():
            return gspread.service_account_from_dict(info, scopes=scopes)

        client = _call_with_retry(
            _create_client,
            op_name="gspread.service_account_from_dict",
        )

        _GSPREAD_CLIENT_CACHE = client
        return _GSPREAD_CLIENT_CACHE

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
    """
    Abre cualquier spreadsheet por ID (key), con caché simple en memoria.
    """
    try:
        sid = (spreadsheet_id or "").strip()
        if not sid:
            raise RuntimeError("Missing spreadsheet_id")

        cached = _SPREADSHEET_CACHE.get(sid)
        if cached is not None:
            return cached

        def _open():
            return gc.open_by_key(sid)

        sh = _call_with_retry(
            _open,
            op_name="gc.open_by_key",
            log_fields={"spreadsheet_id": sid},
        )

        if sh is None:
            raise RuntimeError(f"Spreadsheet not found: {sid}")

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
    """
    Abre el spreadsheet de configuración (Tenants, etc.)
    Env esperado: RESERVACIONES_CONFIG (spreadsheet id)
    """
    try:
        config_id = env_required(ENV_CONFIG_SPREADSHEET_ID).strip()
        if not config_id:
            raise RuntimeError("Missing config spreadsheet id")

        cached = _SPREADSHEET_CACHE.get(config_id)
        if cached is not None:
            return cached

        def _open():
            return gc.open_by_key(config_id)

        sh = _call_with_retry(
            _open,
            op_name="gc.open_by_key_config",
            log_fields={"spreadsheet_id": config_id},
        )

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
    """
    Obtiene una worksheet por título, con caché simple por spreadsheet+title.
    """
    try:
        t = (title or "").strip()
        if not t:
            raise RuntimeError("Missing worksheet title")

        s_key = _spreadsheet_cache_key(spreadsheet)
        cache_key = (s_key, t)

        cached = _WORKSHEET_CACHE.get(cache_key)
        if cached is not None:
            return cached

        def _get():
            return spreadsheet.worksheet(t)

        ws = _call_with_retry(
            _get,
            op_name="spreadsheet.worksheet",
            log_fields={"worksheet_title": t, "spreadsheet_key": s_key},
        )

        if ws is None:
            raise RuntimeError(f"Worksheet not found: {t}")

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
    """
    Limpia cachés.
    - Si no recibe spreadsheet_id: limpia todo.
    - Si recibe spreadsheet_id: limpia spreadsheet + worksheets asociadas.
    """
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


def invalidate_all_sheet_caches() -> None:
    """
    Alias explícito para invalidación global.
    No rompe contratos existentes y hace el código más legible
    cuando queramos invalidar TODO el layer de sheets.
    """
    invalidate_sheet_caches(None)


# ----------------------------------------
# Header helpers
# ----------------------------------------

def detect_header_row(values: List[List[Any]], required_headers: List[str], max_scan: int = 10) -> int:
    """
    Soporta el patrón:
      - fila 1: headers técnicos (EN)
      - fila 2: traducción / etiquetas (ES)
    Detecta la fila de headers técnicos buscando required_headers normalizados.
    """
    try:
        if not values:
            return 1

        req = [normalize(h) for h in required_headers if str(h or "").strip()]
        if not req:
            return 1

        scan = values[:max_scan] if max_scan > 0 else values

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
    """
    Lee registros detectando automáticamente la fila de headers técnicos.
    Devuelve lista de dicts con keys normalizadas (lower, sin tildes, etc.).
    """
    try:
        def _get_values():
            return ws.get_all_values()

        values = _call_with_retry(
            _get_values,
            op_name="worksheet.get_all_values",
            log_fields={"worksheet_title": getattr(ws, "title", "")},
        )

        if not values:
            return []

        header_row = detect_header_row(values, required_headers=required_headers)
        if header_row < 1 or header_row > len(values):
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
                d[h] = row[i] if i < len(row) else ""
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
