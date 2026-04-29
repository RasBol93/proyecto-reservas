# app/sheets.py — versión optimizada con caché simple de client, spreadsheet y worksheet
# hardened incremental: misma estructura, mismos contratos, más robustez

import contextvars
import json
import threading
import time
import uuid
from collections import deque
from typing import Any, Dict, List, Optional, Tuple

import gspread

from app.config import ENV_GCP_CREDS_JSON, ENV_CONFIG_SPREADSHEET_ID, env_required
from app.utils import normalize, log_event, now_iso_utc
from app.alerts import alert_system_error, alert_sheet_error


# ----------------------------------------
# Caches simples en memoria
# ----------------------------------------

_GSPREAD_CLIENT_CACHE: gspread.Client | None = None
_SPREADSHEET_CACHE: Dict[str, gspread.Spreadsheet] = {}
_WORKSHEET_CACHE: Dict[Tuple[str, str], gspread.Worksheet] = {}
_SHEETS_REQUEST_CTX: contextvars.ContextVar[Optional[Dict[str, Any]]] = contextvars.ContextVar("sheets_request_ctx", default=None)
_RECENT_SHEETS_REQUESTS: deque[Dict[str, Any]] = deque(maxlen=200)
_RECENT_SHEETS_REQUESTS_LOCK = threading.Lock()
_RECENT_SHEETS_REQUESTS_RESET_AT: str = ""
_RECENT_SHEETS_REQUESTS_RESET_GENERATION: int = 0
_GSPREAD_INSTRUMENTATION_INSTALLED = False


# ----------------------------------------
# Retry policy simple
# ----------------------------------------

_SHEETS_RETRY_ATTEMPTS = 3
_SHEETS_RETRY_SLEEP_SECONDS = 0.35


def _safe_text(value: Any) -> str:
    try:
        return str(value or "").strip()
    except Exception:
        return ""


def _is_quota_429_error(exc: Exception) -> bool:
    msg = str(exc or "").lower()
    quota_signals = (
        "429",
        "quota",
        "quota exceeded",
        "rate limit",
        "too many requests",
    )
    return any(signal in msg for signal in quota_signals)


def start_sheets_request_context(
    *,
    path: str = "",
    flow_name: str = "",
    tenant_id: str = "",
    request_id: str = "",
) -> str:
    rid = _safe_text(request_id) or f"req_{uuid.uuid4().hex[:12]}"
    with _RECENT_SHEETS_REQUESTS_LOCK:
        reset_generation = int(_RECENT_SHEETS_REQUESTS_RESET_GENERATION)
    ctx = {
        "request_id": rid,
        "path": _safe_text(path),
        "flow_name": _safe_text(flow_name) or _safe_text(path) or "http_request",
        "tenant_id": _safe_text(tenant_id),
        "started_at_ts": time.time(),
        "sheet_reads": [],
        "sheet_reads_count": 0,
        "worksheets_touched": set(),
        "spreadsheets_touched": set(),
        "had_429": False,
        "serving_sources": set(),
        "recent_reset_generation": reset_generation,
    }
    _SHEETS_REQUEST_CTX.set(ctx)
    return rid


def set_sheets_observation_context(
    *,
    tenant_id: Optional[str] = None,
    flow_name: Optional[str] = None,
    path: Optional[str] = None,
) -> None:
    ctx = _SHEETS_REQUEST_CTX.get()
    if not isinstance(ctx, dict):
        return
    if tenant_id is not None and _safe_text(tenant_id):
        ctx["tenant_id"] = _safe_text(tenant_id)
    if flow_name is not None and _safe_text(flow_name):
        ctx["flow_name"] = _safe_text(flow_name)
    if path is not None and _safe_text(path):
        ctx["path"] = _safe_text(path)


def note_sheets_serving_source(source: str) -> None:
    clean_source = _safe_text(source)
    if not clean_source:
        return
    ctx = _SHEETS_REQUEST_CTX.get()
    if not isinstance(ctx, dict):
        return
    ctx.setdefault("serving_sources", set()).add(clean_source)


def get_current_sheets_request_summary_preview() -> Optional[Dict[str, Any]]:
    ctx = _SHEETS_REQUEST_CTX.get()
    if not isinstance(ctx, dict):
        return None

    duration_ms = max(0, int((time.time() - float(ctx.get("started_at_ts") or time.time())) * 1000))
    return {
        "request_id": _safe_text(ctx.get("request_id")),
        "path": _safe_text(ctx.get("path")),
        "flow_name": _safe_text(ctx.get("flow_name")),
        "tenant_id": _safe_text(ctx.get("tenant_id")),
        "status_code": 0,
        "total_sheet_reads": int(ctx.get("sheet_reads_count") or 0),
        "worksheets_touched": sorted([w for w in ctx.get("worksheets_touched", set()) if _safe_text(w)]),
        "spreadsheets_touched": sorted([s for s in ctx.get("spreadsheets_touched", set()) if _safe_text(s)]),
        "duration_ms": duration_ms,
        "had_429": bool(ctx.get("had_429")),
        "serving_sources": sorted([s for s in ctx.get("serving_sources", set()) if _safe_text(s)]),
        "reads": list(ctx.get("sheet_reads") or []),
        "error": "",
    }


def summarize_logical_sheet_reads(summary: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    if not isinstance(summary, dict):
        return {
            "logical_sheet_reads_count": 0,
            "logical_units": [],
        }

    reads = summary.get("reads") or []
    if not isinstance(reads, list):
        reads = []

    sheets_with_full_reads = set()
    for read in reads:
        if not isinstance(read, dict):
            continue
        operation = _safe_text(read.get("operation"))
        if operation != "worksheet.get_all_values":
            continue
        sheets_with_full_reads.add((
            _safe_text(read.get("spreadsheet_id")),
            _safe_text(read.get("worksheet")),
        ))

    logical_units: List[Dict[str, Any]] = []
    for read in reads:
        if not isinstance(read, dict):
            continue

        operation = _safe_text(read.get("operation"))
        spreadsheet_id = _safe_text(read.get("spreadsheet_id"))
        worksheet = _safe_text(read.get("worksheet"))
        sheet_key = (spreadsheet_id, worksheet)

        if operation in {"client.open_by_key", "spreadsheet.worksheet_lookup", "spreadsheet.worksheets"}:
            continue

        if not operation.startswith("worksheet."):
            continue

        logical_operation = ""
        if operation == "worksheet.get_all_values":
            logical_operation = "read_all_values"
        elif operation == "worksheet.get":
            if sheet_key in sheets_with_full_reads:
                continue
            logical_operation = "read_range"
        elif operation == "worksheet.row_values":
            logical_operation = "read_row_values"
        elif operation == "worksheet.col_values":
            logical_operation = "read_col_values"
        else:
            # Conservatively count unknown worksheet-level reads as one logical unit
            # keyed by sheet + operation, instead of discarding potentially real work.
            suffix = operation.split(".", 1)[1].strip() if "." in operation else ""
            logical_operation = f"read_{suffix.replace('.', '_')}" if suffix else "read_worksheet_operation"

        logical_units.append({
            "spreadsheet_id": spreadsheet_id,
            "worksheet": worksheet,
            "logical_operation": logical_operation,
        })

    return {
        "logical_sheet_reads_count": len(logical_units),
        "logical_units": logical_units,
    }


def enrich_sheets_request_summary_with_logical_counts(summary: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    if not isinstance(summary, dict):
        return None

    enriched = dict(summary)
    logical = summarize_logical_sheet_reads(summary)
    enriched["logical_sheet_reads_count"] = int(logical.get("logical_sheet_reads_count") or 0)
    enriched["logical_sheet_read_units"] = list(logical.get("logical_units") or [])
    return enriched


def finish_sheets_request_context(
    *,
    status_code: Optional[int] = None,
    error: str = "",
) -> Optional[Dict[str, Any]]:
    ctx = _SHEETS_REQUEST_CTX.get()
    if not isinstance(ctx, dict):
        return None

    duration_ms = max(0, int((time.time() - float(ctx.get("started_at_ts") or time.time())) * 1000))
    summary = {
        "request_id": _safe_text(ctx.get("request_id")),
        "path": _safe_text(ctx.get("path")),
        "flow_name": _safe_text(ctx.get("flow_name")),
        "tenant_id": _safe_text(ctx.get("tenant_id")),
        "status_code": int(status_code or 0) if status_code is not None else 0,
        "total_sheet_reads": int(ctx.get("sheet_reads_count") or 0),
        "worksheets_touched": sorted([w for w in ctx.get("worksheets_touched", set()) if _safe_text(w)]),
        "spreadsheets_touched": sorted([s for s in ctx.get("spreadsheets_touched", set()) if _safe_text(s)]),
        "duration_ms": duration_ms,
        "had_429": bool(ctx.get("had_429")),
        "serving_sources": sorted([s for s in ctx.get("serving_sources", set()) if _safe_text(s)]),
        "reads": list(ctx.get("sheet_reads") or []),
        "error": _safe_text(error),
    }

    with _RECENT_SHEETS_REQUESTS_LOCK:
        _RECENT_SHEETS_REQUESTS.append({
            "summary": summary,
            "reset_generation": int(ctx.get("recent_reset_generation") or 0),
        })

    try:
        log_event(
            "sheets_request_summary",
            request_id=summary["request_id"],
            path=summary["path"],
            flow_name=summary["flow_name"],
            tenant_id=summary["tenant_id"],
            status_code=summary["status_code"],
            total_sheet_reads=summary["total_sheet_reads"],
            worksheets_touched=summary["worksheets_touched"],
            duration_ms=summary["duration_ms"],
            had_429=summary["had_429"],
            serving_sources=summary["serving_sources"],
            error=summary["error"],
        )
    except Exception:
        pass

    _SHEETS_REQUEST_CTX.set(None)
    return summary


def get_recent_sheets_request_summaries(
    *,
    limit: int = 20,
    min_reads: int = 0,
    had_429_only: bool = False,
) -> List[Dict[str, Any]]:
    clean_limit = max(1, min(int(limit or 20), 100))
    clean_min_reads = max(0, int(min_reads or 0))

    with _RECENT_SHEETS_REQUESTS_LOCK:
        items = list(_RECENT_SHEETS_REQUESTS)

    items.reverse()
    filtered: List[Dict[str, Any]] = []
    for item in items:
        summary = item.get("summary") if isinstance(item, dict) else None
        if not isinstance(summary, dict):
            continue
        if int(summary.get("total_sheet_reads") or 0) < clean_min_reads:
            continue
        if had_429_only and not bool(summary.get("had_429")):
            continue
        filtered.append(summary)
        if len(filtered) >= clean_limit:
            break

    return filtered


def reset_recent_sheets_request_summaries() -> Dict[str, Any]:
    global _RECENT_SHEETS_REQUESTS_RESET_AT, _RECENT_SHEETS_REQUESTS_RESET_GENERATION
    with _RECENT_SHEETS_REQUESTS_LOCK:
        cleared_count = len(_RECENT_SHEETS_REQUESTS)
        _RECENT_SHEETS_REQUESTS.clear()
        _RECENT_SHEETS_REQUESTS_RESET_GENERATION += 1
        _RECENT_SHEETS_REQUESTS_RESET_AT = now_iso_utc()
        reset_generation = int(_RECENT_SHEETS_REQUESTS_RESET_GENERATION)
        reset_at = _RECENT_SHEETS_REQUESTS_RESET_AT

    try:
        log_event(
            "sheets_recent_reset",
            cleared_count=cleared_count,
            reset_at=reset_at,
            reset_generation=reset_generation,
        )
    except Exception:
        pass

    return {
        "cleared_count": cleared_count,
        "reset_at": reset_at,
    }


def get_recent_sheets_request_summaries_since_reset(
    *,
    limit: int = 20,
    min_reads: int = 0,
    had_429_only: bool = False,
) -> Dict[str, Any]:
    clean_limit = max(1, min(int(limit or 20), 100))
    clean_min_reads = max(0, int(min_reads or 0))

    with _RECENT_SHEETS_REQUESTS_LOCK:
        items = list(_RECENT_SHEETS_REQUESTS)
        current_generation = int(_RECENT_SHEETS_REQUESTS_RESET_GENERATION)
        reset_at = _RECENT_SHEETS_REQUESTS_RESET_AT

    items.reverse()
    filtered: List[Dict[str, Any]] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        if int(item.get("reset_generation") or 0) < current_generation:
            continue
        summary = item.get("summary")
        if not isinstance(summary, dict):
            continue
        if int(summary.get("total_sheet_reads") or 0) < clean_min_reads:
            continue
        if had_429_only and not bool(summary.get("had_429")):
            continue
        filtered.append(summary)
        if len(filtered) >= clean_limit:
            break

    return {
        "reset_at": str(reset_at or "").strip(),
        "requests": filtered,
    }


def _record_sheets_read(
    *,
    spreadsheet_id: str,
    worksheet: str,
    operation: str,
    duration_ms: int,
    ok: bool,
    error: str = "",
    is_429: bool = False,
) -> None:
    ctx = _SHEETS_REQUEST_CTX.get()
    request_id = ""
    path = ""
    flow_name = ""
    tenant_id = ""

    if isinstance(ctx, dict):
        request_id = _safe_text(ctx.get("request_id"))
        path = _safe_text(ctx.get("path"))
        flow_name = _safe_text(ctx.get("flow_name"))
        tenant_id = _safe_text(ctx.get("tenant_id"))
        ctx["sheet_reads_count"] = int(ctx.get("sheet_reads_count") or 0) + 1
        if _safe_text(worksheet):
            ctx.setdefault("worksheets_touched", set()).add(_safe_text(worksheet))
        if _safe_text(spreadsheet_id):
            ctx.setdefault("spreadsheets_touched", set()).add(_safe_text(spreadsheet_id))
        if is_429:
            ctx["had_429"] = True

        read_item = {
            "spreadsheet_id": _safe_text(spreadsheet_id),
            "worksheet": _safe_text(worksheet),
            "operation": _safe_text(operation),
            "duration_ms": int(duration_ms or 0),
            "ok": bool(ok),
            "is_429": bool(is_429),
            "error": _safe_text(error),
        }
        ctx.setdefault("sheet_reads", []).append(read_item)

    try:
        log_event(
            "sheets_read",
            request_id=request_id,
            path=path,
            flow_name=flow_name,
            tenant_id=tenant_id,
            spreadsheet_id=_safe_text(spreadsheet_id),
            worksheet=_safe_text(worksheet),
            operation=_safe_text(operation),
            duration_ms=int(duration_ms or 0),
            ok=bool(ok),
            is_429=bool(is_429),
            error=_safe_text(error),
        )
    except Exception:
        pass


def _wrap_gspread_method(cls, method_name: str, operation_name: str, metadata_getter):
    original = getattr(cls, method_name, None)
    if original is None or getattr(original, "_sheet_obs_wrapped", False):
        return

    def _wrapped(self, *args, **kwargs):
        started_at = time.time()
        ok = False
        error = ""
        is_429 = False
        try:
            result = original(self, *args, **kwargs)
            ok = True
            return result
        except Exception as e:
            error = str(e)
            is_429 = _is_quota_429_error(e)
            raise
        finally:
            duration_ms = max(0, int((time.time() - started_at) * 1000))
            spreadsheet_id, worksheet = metadata_getter(self, *args, **kwargs)
            _record_sheets_read(
                spreadsheet_id=_safe_text(spreadsheet_id),
                worksheet=_safe_text(worksheet),
                operation=operation_name,
                duration_ms=duration_ms,
                ok=ok,
                error=error,
                is_429=is_429,
            )

    setattr(_wrapped, "_sheet_obs_wrapped", True)
    setattr(cls, method_name, _wrapped)


def _install_gspread_read_instrumentation() -> None:
    global _GSPREAD_INSTRUMENTATION_INSTALLED
    if _GSPREAD_INSTRUMENTATION_INSTALLED:
        return

    _wrap_gspread_method(
        gspread.Client,
        "open_by_key",
        "client.open_by_key",
        lambda _self, spreadsheet_id, *args, **kwargs: (_safe_text(spreadsheet_id), ""),
    )
    _wrap_gspread_method(
        gspread.Spreadsheet,
        "worksheet",
        "spreadsheet.worksheet_lookup",
        lambda self, title, *args, **kwargs: (_safe_text(getattr(self, "id", "")), _safe_text(title)),
    )
    _wrap_gspread_method(
        gspread.Spreadsheet,
        "worksheets",
        "spreadsheet.worksheets",
        lambda self, *args, **kwargs: (_safe_text(getattr(self, "id", "")), ""),
    )
    _wrap_gspread_method(
        gspread.Worksheet,
        "get_all_values",
        "worksheet.get_all_values",
        lambda self, *args, **kwargs: (_safe_text(getattr(getattr(self, "spreadsheet", None), "id", "")), _safe_text(getattr(self, "title", ""))),
    )
    _wrap_gspread_method(
        gspread.Worksheet,
        "row_values",
        "worksheet.row_values",
        lambda self, *args, **kwargs: (_safe_text(getattr(getattr(self, "spreadsheet", None), "id", "")), _safe_text(getattr(self, "title", ""))),
    )
    _wrap_gspread_method(
        gspread.Worksheet,
        "col_values",
        "worksheet.col_values",
        lambda self, *args, **kwargs: (_safe_text(getattr(getattr(self, "spreadsheet", None), "id", "")), _safe_text(getattr(self, "title", ""))),
    )
    _wrap_gspread_method(
        gspread.Worksheet,
        "get",
        "worksheet.get",
        lambda self, *args, **kwargs: (_safe_text(getattr(getattr(self, "spreadsheet", None), "id", "")), _safe_text(getattr(self, "title", ""))),
    )

    _GSPREAD_INSTRUMENTATION_INSTALLED = True


_install_gspread_read_instrumentation()


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
