# app/orders.py — versión optimizada simple y escalable para ORDERS
# hardened incremental: misma estructura, mismos contratos, más robustez

import json
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional
from zoneinfo import ZoneInfo

try:
    from gspread.exceptions import APIError, WorksheetNotFound
except Exception:  # pragma: no cover
    APIError = Exception  # type: ignore
    WorksheetNotFound = Exception  # type: ignore

from app.utils import log_event
from app.alerts import (
    alert_order_failed,
    alert_order_status_failed,
    alert_payment_proof_failed,
    alert_sheet_error,
)


# ----------------------------------------
# Helpers: worksheet + safe json
# ----------------------------------------

_ALLOWED_ORDER_STATUSES = {
    "PENDING_PAYMENT",
    "PAID",
}

_ALLOWED_PROOF_TYPES = {
    "photo",
    "document",
    "external_url",
}

_ORDERS_WS_CACHE: Dict[str, Any] = {}
_ORDERS_HEADER_CACHE: Dict[str, List[str]] = {}
_TRANSIENT_ORDERS_ERROR_SIGNALS = (
    "429",
    "quota",
    "rate limit",
    "timed out",
    "timeout",
    "temporarily unavailable",
    "service unavailable",
    "internal error",
    "bad gateway",
    "gateway timeout",
    "500",
    "502",
    "503",
    "504",
)


class OrdersReadTemporarilyUnavailable(RuntimeError):
    pass


def _normalize_status(value: Any) -> str:
    return str(value or "").strip().upper()


def _is_transient_orders_error_message(message: Any) -> bool:
    lower_msg = str(message or "").lower()
    return any(token in lower_msg for token in _TRANSIENT_ORDERS_ERROR_SIGNALS)


def _is_allowed_status(status: str) -> bool:
    return _normalize_status(status) in _ALLOWED_ORDER_STATUSES


def _is_valid_status_transition(current_status: str, new_status: str) -> bool:
    current_norm = _normalize_status(current_status)
    new_norm = _normalize_status(new_status)

    # idempotencia
    if current_norm == new_norm:
        return True

    # transición válida actual
    if current_norm == "PENDING_PAYMENT" and new_norm == "PAID":
        return True

    return False


def _get_orders_ws(orders_sh):
    """
    orders_sh: gspread Spreadsheet
    Busca nombres permitidos de la hoja de pedidos.
    Ya no cae silenciosamente a la primera hoja.
    """
    spreadsheet_key = _orders_spreadsheet_key(orders_sh)

    cached = _ORDERS_WS_CACHE.get(spreadsheet_key)
    if cached is not None:
        return cached

    for ws_name in ("ORDERS", "Orders", "orders"):
        try:
            ws = orders_sh.worksheet(ws_name)
            _ORDERS_WS_CACHE[spreadsheet_key] = ws
            return ws
        except WorksheetNotFound:
            continue
        except APIError as e:
            msg = str(e or "")
            if _is_transient_orders_error_message(msg):
                raise OrdersReadTemporarilyUnavailable(f"ORDERS worksheet temporarily unavailable: {msg}") from e
            raise RuntimeError(f"ORDERS worksheet access error: {msg}") from e
        except Exception as e:
            msg = str(e or "")
            lower_msg = msg.lower()
            if "worksheet" in lower_msg and "not found" in lower_msg:
                continue
            if _is_transient_orders_error_message(msg):
                raise OrdersReadTemporarilyUnavailable(f"ORDERS worksheet temporarily unavailable: {msg}") from e
            raise RuntimeError(f"ORDERS worksheet access error: {msg}") from e
    raise RuntimeError("ORDERS worksheet not found (accepted names: ORDERS, Orders, orders)")


def _orders_spreadsheet_key(orders_sh) -> str:
    try:
        sid = getattr(orders_sh, "id", None)
        if sid:
            return str(sid)
    except Exception:
        pass
    return str(id(orders_sh))


def _orders_header_cache_key(ws) -> str:
    try:
        spreadsheet = getattr(ws, "spreadsheet", None)
        return _orders_spreadsheet_key(spreadsheet)
    except Exception:
        pass
    return str(id(ws))


def _now_iso_utc() -> str:
    return datetime.now(timezone.utc).isoformat()


def _safe_json_dumps(v: Any) -> str:
    try:
        return json.dumps(v, ensure_ascii=False)
    except Exception:
        return ""


def _safe_json_loads(s: Any) -> Any:
    if isinstance(s, (dict, list)):
        return s
    if not isinstance(s, str):
        return None
    t = s.strip()
    if not t:
        return None
    try:
        return json.loads(t)
    except Exception:
        return None


def _parse_iso_datetime(value: Any) -> Optional[datetime]:
    raw = str(value or "").strip()
    if not raw:
        return None

    candidates = [
        raw,
        raw.replace("Z", "+00:00"),
        raw.replace(" ", "T"),
        raw.replace(" ", "T").replace("Z", "+00:00"),
    ]
    for candidate in candidates:
        try:
            return datetime.fromisoformat(candidate)
        except Exception:
            continue
    return None


def _get_header(ws) -> List[str]:
    """
    Asumimos que fila 1 tiene headers técnicos.
    """
    return _load_orders_header(ws, strict=False)


def _load_orders_header(ws, *, strict: bool) -> List[str]:
    cache_key = _orders_header_cache_key(ws)
    cached = _ORDERS_HEADER_CACHE.get(cache_key)
    if cached:
        return cached

    if strict:
        try:
            hdr = ws.row_values(1)
        except Exception as e:
            if _is_transient_orders_error_message(e):
                raise OrdersReadTemporarilyUnavailable(f"ORDERS header temporarily unavailable: {e}") from e
            raise
    else:
        hdr = ws.row_values(1)

    clean_header = [h.strip() for h in hdr if str(h).strip()]
    if clean_header:
        _ORDERS_HEADER_CACHE[cache_key] = clean_header
    return clean_header


def _find_col_idx(header: List[str], col_name: str) -> Optional[int]:
    """
    Devuelve índice 0-based.
    """
    col_name = (col_name or "").strip()
    for i, h in enumerate(header):
        if h.strip() == col_name:
            return i
    return None


def _ensure_orders_column(ws, header: List[str], col_name: str) -> List[str]:
    current_idx = _find_col_idx(header, col_name)
    if current_idx is not None:
        return header

    clean_col_name = str(col_name or "").strip()
    next_col_1based = len(header) + 1
    ws.update_cell(1, next_col_1based, clean_col_name)

    updated_header = list(header) + [clean_col_name]
    _ORDERS_HEADER_CACHE[_orders_header_cache_key(ws)] = updated_header
    return updated_header


def _build_row_by_header(header: List[str], data: Dict[str, Any]) -> List[str]:
    """
    Construye una fila alineada al header.
    Convierte dict/list a JSON string.
    """
    row: List[str] = [""] * len(header)
    for k, v in (data or {}).items():
        idx = _find_col_idx(header, k)
        if idx is None:
            continue
        if isinstance(v, (dict, list)):
            row[idx] = _safe_json_dumps(v)
        elif v is None:
            row[idx] = ""
        else:
            row[idx] = str(v)
    return row


def _col_to_a1(col_1based: int) -> str:
    """
    1 -> A, 27 -> AA
    """
    result = ""
    n = int(col_1based)
    while n > 0:
        n, rem = divmod(n - 1, 26)
        result = chr(65 + rem) + result
    return result


def _cell_a1(row_1based: int, col_1based: int) -> str:
    return f"{_col_to_a1(col_1based)}{row_1based}"


def _range_a1(row_1based: int, start_col_1based: int, end_col_1based: int) -> str:
    return f"{_cell_a1(row_1based, start_col_1based)}:{_cell_a1(row_1based, end_col_1based)}"


def _batch_write_cells(ws, updates: List[Dict[str, str]]) -> None:
    """
    updates: [{"row":2,"col":3,"value":"x"}, ...]
    Hace un solo batch_update si hay varias celdas.
    """
    if not updates:
        return

    data = []
    for u in updates:
        row = int(u["row"])
        col = int(u["col"])
        value = str(u.get("value", ""))
        data.append({
            "range": _cell_a1(row, col),
            "values": [[value]],
        })

    ws.batch_update(data, value_input_option="RAW")


def _row_to_dict(header: List[str], row: List[Any]) -> Dict[str, Any]:
    d: Dict[str, Any] = {}
    for i, h in enumerate(header):
        d[h] = row[i] if i < len(row) else ""
    return d


def _safe_row_values(ws, row_index: int) -> List[Any]:
    try:
        return ws.row_values(row_index)
    except Exception:
        return []


def _safe_col_values(ws, col_index_1based: int) -> List[Any]:
    try:
        return ws.col_values(col_index_1based)
    except Exception:
        return []


def _find_next_empty_row(ws, header: List[str]) -> int:
    """
    Busca la siguiente fila vacía real usando solo el ancho técnico del header.
    Esto evita que append_row se vaya a la derecha por rangos extraños de Google Sheets.
    """
    header_len = len(header or [])
    if header_len <= 0:
        return 2

    order_id_col = _find_col_idx(header, "order_id")
    if order_id_col is not None:
        values = _safe_col_values(ws, order_id_col + 1)
        if not values or len(values) == 1:
            return 2

        for idx_0based, cell in enumerate(values[1:], start=2):
            if not str(cell).strip():
                return idx_0based

        return len(values) + 1

    try:
        values = ws.get_all_values()
    except Exception:
        values = []

    # fila 1 = header técnico; empezamos a revisar desde fila 2
    if not values or len(values) == 1:
        return 2

    for idx_0based, row in enumerate(values[1:], start=2):
        slice_row = row[:header_len]
        if not any(str(cell).strip() for cell in slice_row):
            return idx_0based

    return len(values) + 1


def _write_full_row(ws, row_index_1based: int, row_values: List[str]) -> None:
    """
    Escribe la fila completa explícitamente desde columna A hasta el ancho del header.
    """
    if not row_values:
        return
    end_col = len(row_values)
    ws.update(
        _range_a1(row_index_1based, 1, end_col),
        [row_values],
        value_input_option="RAW",
    )


# ----------------------------------------
# ID generator
# ----------------------------------------

def gen_order_id() -> str:
    import secrets
    return secrets.token_hex(4)


# ----------------------------------------
# Pricing snapshot
# ----------------------------------------

def build_items_snapshot(items: List[Dict[str, Any]], menu_idx: Dict[str, Any]) -> List[Dict[str, Any]]:
    """
    items: [{"sku": "...", "qty": 2}, ...]
    menu_idx: {sku: {"name":..., "price":...}, ...}
    output:
      [{"sku","name","qty","unit_price","line_total"}, ...]
    """
    snapshot: List[Dict[str, Any]] = []
    for it in items or []:
        sku = str(it.get("sku") or "").strip()
        if not sku:
            continue

        try:
            qty = int(it.get("qty") or 1)
        except Exception:
            qty = 1
        qty = max(1, qty)

        if sku not in menu_idx:
            snapshot.append({
                "sku": sku,
                "name": sku,
                "qty": qty,
                "unit_price": 0,
                "line_total": 0,
            })
            continue

        name = str(menu_idx[sku].get("name") or sku).strip()
        try:
            unit_price = float(menu_idx[sku].get("price") or 0)
        except Exception:
            unit_price = 0.0

        line_total = float(unit_price) * float(qty)

        snapshot.append({
            "sku": sku,
            "name": name,
            "qty": qty,
            "unit_price": unit_price,
            "line_total": line_total,
        })
    return snapshot


# ----------------------------------------
# CREATE
# ----------------------------------------

def append_order_row(
    orders_sh,
    tenant_id: str,
    order_id: str,
    customer_name: str,
    customer_contact: str,
    items: List[Dict[str, Any]],
    delivery_type: str,
    requested_time: str,
    status: str,
    source: str,
    total_amount: Any,
    items_snapshot: Optional[List[Dict[str, Any]]] = None,
    currency: str = "BOB",
    pricing_version: str = "v1",
    notes: str = "",
    customer_telegram_chat_id: str = "",
) -> Dict[str, Any]:
    """
    Inserta una fila en ORDERS alineada a headers.
    Escribe explícitamente en la siguiente fila vacía, sin usar append_row.
    """
    try:
        ws = _get_orders_ws(orders_sh)
        header = _get_header(ws)
        if not header:
            raise RuntimeError("ORDERS header row missing")

        clean_order_id = str(order_id or "").strip()
        if not clean_order_id:
            raise RuntimeError("order_id missing")

        clean_status = _normalize_status(status)
        if not _is_allowed_status(clean_status):
            raise RuntimeError(f"invalid initial status: {clean_status}")

        clean_items = items if isinstance(items, list) else []
        if not clean_items:
            raise RuntimeError("items missing")

        try:
            total_amount_num = float(total_amount)
        except Exception:
            raise RuntimeError("invalid total_amount")

        if total_amount_num < 0:
            raise RuntimeError("total_amount must be >= 0")

        data = {
            "order_id": clean_order_id,
            "created_at": _now_iso_utc(),
            "tenant_id": str(tenant_id or "").strip(),
            "customer_name": str(customer_name or "").strip(),
            "customer_contact": str(customer_contact or "").strip(),
            "customer_telegram_chat_id": str(customer_telegram_chat_id or "").strip(),
            "items": clean_items,
            "items_snapshot": items_snapshot or "",
            "currency": str(currency or "BOB").strip() or "BOB",
            "pricing_version": str(pricing_version or "v1").strip() or "v1",
            "notes": str(notes or "").strip(),
            "delivery_type": str(delivery_type or "").strip(),
            "requested_time": str(requested_time or "").strip(),
            "status": clean_status,
            "source": str(source or "").strip(),
            "total_amount": total_amount_num,
            "payment_proof_file_id": "",
            "payment_confirmed_at": "",
            "payment_proof_type": "",
            "payment_proof_caption": "",
        }

        row = _build_row_by_header(header, data)
        next_row = _find_next_empty_row(ws, header)
        _write_full_row(ws, next_row, row)

        log_event(
            "order_appended",
            tenant_id=tenant_id,
            order_id=clean_order_id,
            status=clean_status,
            source=source,
            total_amount=total_amount_num,
            customer_contact=customer_contact,
            customer_telegram_chat_id=customer_telegram_chat_id,
            row_index=next_row,
        )

        return {"ok": True, "order_id": clean_order_id}

    except Exception as e:
        log_event(
            "order_append_error",
            tenant_id=tenant_id,
            order_id=order_id,
            error_type=type(e).__name__,
            error=str(e),
        )
        alert_order_failed(
            tenant_id=tenant_id,
            order_id=order_id,
            error=str(e),
        )
        alert_sheet_error(
            tenant_id=tenant_id,
            error=str(e),
            extra_key="append_order_row",
        )
        return {"ok": False, "error": str(e)}


def append_order(*args, **kwargs):
    return append_order_row(*args, **kwargs)


# ----------------------------------------
# READ helpers
# ----------------------------------------

def _find_row_index_by_order_id(ws, order_id: str) -> Optional[int]:
    """
    Devuelve row index 1-based en la sheet (incluye header en fila 1).
    Lee solo la columna order_id.
    """
    header = _get_header(ws)
    if not header:
        return None

    oid_col = _find_col_idx(header, "order_id")
    if oid_col is None:
        return None

    col_values = _safe_col_values(ws, oid_col + 1)
    target = (order_id or "").strip()
    if not target:
        return None

    # fila 1 = header
    for i in range(1, len(col_values)):
        if str(col_values[i]).strip() == target:
            return i + 1

    return None


def get_order_by_id(orders_sh, order_id: str) -> Optional[Dict[str, Any]]:
    ctx = get_order_context_by_id(orders_sh, order_id)
    if not ctx:
        return None
    return dict(ctx.get("order") or {})


def _strict_col_values(ws, col_index_1based: int) -> List[Any]:
    try:
        return ws.col_values(col_index_1based)
    except Exception as e:
        if _is_transient_orders_error_message(e):
            raise OrdersReadTemporarilyUnavailable(f"ORDERS column temporarily unavailable: {e}") from e
        raise


def _strict_row_values(ws, row_index: int) -> List[Any]:
    try:
        return ws.row_values(row_index)
    except Exception as e:
        if _is_transient_orders_error_message(e):
            raise OrdersReadTemporarilyUnavailable(f"ORDERS row temporarily unavailable: {e}") from e
        raise


def _strict_header(ws) -> List[str]:
    return _load_orders_header(ws, strict=True)


def _build_order_context(ws, header: List[str], row_index: int, row: List[Any]) -> Dict[str, Any]:
    return {
        "ws": ws,
        "header": header,
        "row_index": int(row_index),
        "row": row,
        "order": _row_to_dict(header, row),
    }


def _resolve_order_context(ws, order_id: str, *, strict: bool) -> Optional[Dict[str, Any]]:
    header = _strict_header(ws) if strict else _get_header(ws)
    if not header:
        return None

    oid_col = _find_col_idx(header, "order_id")
    if oid_col is None:
        return None

    col_values = _strict_col_values(ws, oid_col + 1) if strict else _safe_col_values(ws, oid_col + 1)
    target = (order_id or "").strip()
    if not target:
        return None

    row_index = None
    for i in range(1, len(col_values)):
        if str(col_values[i]).strip() == target:
            row_index = i + 1
            break

    if row_index is None:
        return None

    row = _strict_row_values(ws, row_index) if strict else _safe_row_values(ws, row_index)
    if not row:
        return None

    return _build_order_context(ws, header, row_index, row)


def get_order_context_by_id(orders_sh, order_id: str) -> Optional[Dict[str, Any]]:
    ws = _get_orders_ws(orders_sh)
    return _resolve_order_context(ws, order_id, strict=False)


def get_order_context_by_id_strict(orders_sh, order_id: str) -> Optional[Dict[str, Any]]:
    ws = _get_orders_ws(orders_sh)
    return _resolve_order_context(ws, order_id, strict=True)


def get_order_by_id_strict(orders_sh, order_id: str) -> Optional[Dict[str, Any]]:
    ctx = get_order_context_by_id_strict(orders_sh, order_id)
    if not ctx:
        return None
    return dict(ctx.get("order") or {})


def list_paid_pending_delivery_orders(orders_sh, tenant_id: str) -> List[Dict[str, Any]]:
    ws = _get_orders_ws(orders_sh)
    header = _get_header(ws)
    if not header:
        return []

    try:
        values = ws.get_all_values()
    except Exception:
        return []

    tenant_col = _find_col_idx(header, "tenant_id")
    status_col = _find_col_idx(header, "status")
    delivered_col = _find_col_idx(header, "delivered_at")

    if status_col is None:
        return []

    clean_tenant_id = str(tenant_id or "").strip()
    pending: List[Dict[str, Any]] = []

    for row_index, row in enumerate(values[1:], start=2):
        order = _row_to_dict(header, row)

        row_tenant_id = str(order.get("tenant_id") or "").strip() if tenant_col is not None else ""
        if tenant_col is not None and row_tenant_id != clean_tenant_id:
            continue

        if _normalize_status(order.get("status")) != "PAID":
            continue

        delivered_at = str(order.get("delivered_at") or "").strip() if delivered_col is not None else ""
        if delivered_at:
            continue

        pending.append({
            "row_index": int(row_index),
            "order": order,
        })

    return pending


def mark_order_delivered(
    orders_sh,
    tenant_id: str,
    order_id: str,
    tenant_tz: str,
    order_ctx: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    tenant_id_for_alert = str(tenant_id or "").strip()

    try:
        clean_order_id = str(order_id or "").strip()
        if not clean_order_id:
            return {"ok": True, "found": False}

        use_ctx = order_ctx if isinstance(order_ctx, dict) else None
        if use_ctx is not None:
            ctx_order_id = str(((use_ctx.get("order") or {}).get("order_id")) or "").strip()
            if ctx_order_id and ctx_order_id != clean_order_id:
                use_ctx = None

        if use_ctx is None:
            use_ctx = get_order_context_by_id(orders_sh, clean_order_id)

        if not use_ctx:
            return {"ok": True, "found": False}

        ws = use_ctx["ws"]
        header = list(use_ctx["header"])
        row_index = int(use_ctx["row_index"])
        row = list(use_ctx.get("row") or [])

        tenant_col = _find_col_idx(header, "tenant_id")
        if tenant_col is not None and tenant_col < len(row):
            row_tenant_id = str(row[tenant_col] or "").strip()
            if row_tenant_id != tenant_id_for_alert:
                return {"ok": True, "found": False}

        status_col = _find_col_idx(header, "status")
        if status_col is None:
            raise RuntimeError("Missing status column")

        current_status = _normalize_status(row[status_col] if status_col < len(row) else "")
        if current_status != "PAID":
            return {"ok": False, "found": True, "error": "order is not paid"}

        header = _ensure_orders_column(ws, header, "delivered_at")
        delivered_col = _find_col_idx(header, "delivered_at")
        if delivered_col is None:
            raise RuntimeError("Missing delivered_at column")

        current_delivered_at = row[delivered_col] if delivered_col < len(row) else ""
        current_delivered_at = str(current_delivered_at or "").strip()
        if current_delivered_at:
            return {
                "ok": True,
                "found": True,
                "already_delivered": True,
                "delivered_at": current_delivered_at,
            }

        delivered_at_value = datetime.now(ZoneInfo(str(tenant_tz or "America/La_Paz"))).isoformat()
        _batch_write_cells(ws, [
            {"row": row_index, "col": delivered_col + 1, "value": delivered_at_value},
        ])

        log_event(
            "order_delivered_marked",
            tenant_id=tenant_id_for_alert,
            order_id=clean_order_id,
            delivered_at=delivered_at_value,
        )

        return {
            "ok": True,
            "found": True,
            "already_delivered": False,
            "delivered_at": delivered_at_value,
        }

    except Exception as e:
        log_event(
            "order_delivered_mark_error",
            tenant_id=tenant_id_for_alert,
            order_id=order_id,
            error_type=type(e).__name__,
            error=str(e),
        )
        alert_sheet_error(
            tenant_id=tenant_id_for_alert,
            error=str(e),
            extra_key="mark_order_delivered",
        )
        return {"ok": False, "error": str(e)}


def find_latest_pending_order_for_contact(
    orders_sh,
    customer_contact: str,
    status: str = "PENDING_PAYMENT",
) -> Optional[str]:
    """
    Lee solo las columnas necesarias: customer_contact, status, order_id.
    """
    ws = _get_orders_ws(orders_sh)
    header = _get_header(ws)
    if not header:
        return None

    i_contact = _find_col_idx(header, "customer_contact")
    i_status = _find_col_idx(header, "status")
    i_order_id = _find_col_idx(header, "order_id")

    if i_contact is None or i_status is None or i_order_id is None:
        return None

    contact_vals = _safe_col_values(ws, i_contact + 1)
    status_vals = _safe_col_values(ws, i_status + 1)
    order_vals = _safe_col_values(ws, i_order_id + 1)

    contact = (customer_contact or "").strip()
    wanted_status = _normalize_status(status)
    if not contact or not wanted_status:
        return None

    max_len = max(len(contact_vals), len(status_vals), len(order_vals))

    for row_idx in range(max_len - 1, 1, -1):
        cv = contact_vals[row_idx - 1] if row_idx - 1 < len(contact_vals) else ""
        sv = status_vals[row_idx - 1] if row_idx - 1 < len(status_vals) else ""
        ov = order_vals[row_idx - 1] if row_idx - 1 < len(order_vals) else ""

        if str(cv).strip() != contact:
            continue
        if _normalize_status(sv) != wanted_status:
            continue

        oid = str(ov).strip()
        if oid:
            return oid

    return None


# ----------------------------------------
# UPDATE helpers
# ----------------------------------------

def update_order_status(orders_sh, order_id: str, new_status: str, order_ctx: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    tenant_id_for_alert = ""

    try:
        clean_order_id = str(order_id or "").strip()
        if not clean_order_id:
            return {"ok": True, "found": False}

        use_ctx = order_ctx if isinstance(order_ctx, dict) else None
        if use_ctx is not None:
            ctx_order_id = str(((use_ctx.get("order") or {}).get("order_id")) or "").strip()
            if ctx_order_id and ctx_order_id != clean_order_id:
                use_ctx = None

        if use_ctx is None:
            use_ctx = get_order_context_by_id(orders_sh, clean_order_id)

        if not use_ctx:
            return {"ok": True, "found": False}

        ws = use_ctx["ws"]
        header = use_ctx["header"]
        ridx = int(use_ctx["row_index"])
        row = list(use_ctx.get("row") or [])

        status_col = _find_col_idx(header, "status")
        if status_col is None:
            raise RuntimeError("Missing status column")

        tenant_col = _find_col_idx(header, "tenant_id")

        if tenant_col is not None and tenant_col < len(row):
            tenant_id_for_alert = str(row[tenant_col] or "").strip()

        clean_status = _normalize_status(new_status)
        if not clean_status:
            raise RuntimeError("new_status missing")
        if not _is_allowed_status(clean_status):
            raise RuntimeError(f"invalid target status: {clean_status}")

        current_status = ""
        if status_col < len(row):
            current_status = _normalize_status(row[status_col])

        if current_status and not _is_valid_status_transition(current_status, clean_status):
            raise RuntimeError(f"invalid status transition: {current_status} -> {clean_status}")

        # idempotencia: si ya está en el estado destino, no reescribir
        if current_status == clean_status:
            log_event(
                "order_status_update_idempotent",
                tenant_id=tenant_id_for_alert,
                order_id=order_id,
                status=clean_status,
            )
            return {"ok": True, "found": True, "already_in_status": True}

        updates = [
            {"row": ridx, "col": status_col + 1, "value": clean_status},
        ]

        if clean_status == "PAID":
            paid_col = _find_col_idx(header, "payment_confirmed_at")
            if paid_col is not None:
                updates.append({
                    "row": ridx,
                    "col": paid_col + 1,
                    "value": _now_iso_utc(),
                })

        _batch_write_cells(ws, updates)

        log_event(
            "order_status_updated",
            tenant_id=tenant_id_for_alert,
            order_id=order_id,
            from_status=current_status,
            status=clean_status,
        )

        return {"ok": True, "found": True, "already_in_status": False}

    except Exception as e:
        log_event(
            "order_status_update_error",
            tenant_id=tenant_id_for_alert,
            order_id=order_id,
            error_type=type(e).__name__,
            error=str(e),
        )
        alert_order_status_failed(
            tenant_id=tenant_id_for_alert,
            order_id=order_id,
            new_status=new_status,
            error=str(e),
        )
        alert_sheet_error(
            tenant_id=tenant_id_for_alert,
            error=str(e),
            extra_key="update_order_status",
        )
        return {"ok": False, "error": str(e)}


def update_order_payment_proof(
    orders_sh,
    order_id: str,
    proof_file_id: str,
    proof_type: str,
    proof_caption: str = "",
    order_ctx: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    tenant_id_for_alert = ""

    try:
        clean_order_id = str(order_id or "").strip()
        if not clean_order_id:
            return {"ok": True, "found": False}

        use_ctx = order_ctx if isinstance(order_ctx, dict) else None
        if use_ctx is not None:
            ctx_order_id = str(((use_ctx.get("order") or {}).get("order_id")) or "").strip()
            if ctx_order_id and ctx_order_id != clean_order_id:
                use_ctx = None

        if use_ctx is None:
            use_ctx = get_order_context_by_id(orders_sh, clean_order_id)

        if not use_ctx:
            return {"ok": True, "found": False}

        ws = use_ctx["ws"]
        header = use_ctx["header"]
        ridx = int(use_ctx["row_index"])
        row = list(use_ctx.get("row") or [])

        tenant_col = _find_col_idx(header, "tenant_id")

        if tenant_col is not None and tenant_col < len(row):
            tenant_id_for_alert = str(row[tenant_col] or "").strip()

        fcol = _find_col_idx(header, "payment_proof_file_id")
        tcol = _find_col_idx(header, "payment_proof_type")
        ccol = _find_col_idx(header, "payment_proof_caption")
        status_col = _find_col_idx(header, "status")

        if fcol is None or tcol is None:
            raise RuntimeError("Missing payment proof columns")

        clean_file_id = str(proof_file_id or "").strip()
        clean_proof_type = str(proof_type or "").strip().lower()
        clean_caption = str(proof_caption or "").strip()

        if not clean_file_id:
            raise RuntimeError("proof_file_id missing")
        if clean_proof_type not in _ALLOWED_PROOF_TYPES:
            raise RuntimeError(f"invalid proof_type: {clean_proof_type}")

        current_status = ""
        if status_col is not None and status_col < len(row):
            current_status = _normalize_status(row[status_col])

        # si en el futuro hay más estados, esto evita meter comprobantes en estados absurdos
        if current_status and current_status not in _ALLOWED_ORDER_STATUSES:
            raise RuntimeError(f"invalid current order status for proof update: {current_status}")

        current_file_id = row[fcol] if fcol < len(row) else ""
        current_proof_type = row[tcol] if tcol < len(row) else ""
        current_caption = row[ccol] if (ccol is not None and ccol < len(row)) else ""

        # idempotencia
        if (
            str(current_file_id or "").strip() == clean_file_id
            and str(current_proof_type or "").strip().lower() == clean_proof_type
            and str(current_caption or "").strip() == clean_caption
        ):
            log_event(
                "order_proof_update_idempotent",
                tenant_id=tenant_id_for_alert,
                order_id=order_id,
                proof_type=clean_proof_type,
            )
            return {"ok": True, "found": True, "already_same": True}

        updates = [
            {"row": ridx, "col": fcol + 1, "value": clean_file_id},
            {"row": ridx, "col": tcol + 1, "value": clean_proof_type},
        ]

        if ccol is not None:
            updates.append({
                "row": ridx,
                "col": ccol + 1,
                "value": clean_caption,
            })

        _batch_write_cells(ws, updates)

        log_event(
            "order_proof_updated",
            tenant_id=tenant_id_for_alert,
            order_id=order_id,
            proof_type=clean_proof_type,
        )

        return {"ok": True, "found": True, "already_same": False}

    except Exception as e:
        log_event(
            "order_proof_update_error",
            tenant_id=tenant_id_for_alert,
            order_id=order_id,
            error_type=type(e).__name__,
            error=str(e),
        )
        alert_payment_proof_failed(
            tenant_id=tenant_id_for_alert,
            order_id=order_id,
            error=str(e),
        )
        alert_sheet_error(
            tenant_id=tenant_id_for_alert,
            error=str(e),
            extra_key="update_order_payment_proof",
        )
        return {"ok": False, "error": str(e)}
