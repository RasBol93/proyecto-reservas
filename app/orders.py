# app/orders.py — versión optimizada simple y escalable para ORDERS
# hardened incremental: misma estructura, mismos contratos, más robustez

import json
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

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

def _get_orders_ws(orders_sh):
    """
    orders_sh: gspread Spreadsheet
    Preferimos worksheet 'ORDERS'. Si no existe, usamos la primera.
    """
    try:
        return orders_sh.worksheet("ORDERS")
    except Exception:
        return orders_sh.get_worksheet(0)


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


def _get_header(ws) -> List[str]:
    """
    Asumimos que fila 1 tiene headers técnicos.
    """
    hdr = ws.row_values(1)
    return [h.strip() for h in hdr if str(h).strip()]


def _find_col_idx(header: List[str], col_name: str) -> Optional[int]:
    """
    Devuelve índice 0-based.
    """
    col_name = (col_name or "").strip()
    for i, h in enumerate(header):
        if h.strip() == col_name:
            return i
    return None


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
    """
    try:
        ws = _get_orders_ws(orders_sh)
        header = _get_header(ws)
        if not header:
            raise RuntimeError("ORDERS header row missing")

        clean_order_id = str(order_id or "").strip()
        if not clean_order_id:
            raise RuntimeError("order_id missing")

        data = {
            "order_id": clean_order_id,
            "created_at": _now_iso_utc(),
            "tenant_id": str(tenant_id or "").strip(),
            "customer_name": str(customer_name or "").strip(),
            "customer_contact": str(customer_contact or "").strip(),
            "customer_telegram_chat_id": str(customer_telegram_chat_id or "").strip(),
            "items": items or [],
            "items_snapshot": items_snapshot or "",
            "currency": str(currency or "BOB").strip() or "BOB",
            "pricing_version": str(pricing_version or "v1").strip() or "v1",
            "notes": str(notes or "").strip(),
            "delivery_type": str(delivery_type or "").strip(),
            "requested_time": str(requested_time or "").strip(),
            "status": str(status or "").strip(),
            "source": str(source or "").strip(),
            "total_amount": total_amount,
            "payment_proof_file_id": "",
            "payment_confirmed_at": "",
            "payment_proof_type": "",
            "payment_proof_caption": "",
        }

        row = _build_row_by_header(header, data)
        ws.append_row(row, value_input_option="RAW")

        log_event(
            "order_appended",
            tenant_id=tenant_id,
            order_id=clean_order_id,
            status=status,
            source=source,
            total_amount=total_amount,
            customer_contact=customer_contact,
            customer_telegram_chat_id=customer_telegram_chat_id,
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
    ws = _get_orders_ws(orders_sh)
    header = _get_header(ws)
    if not header:
        return None

    ridx = _find_row_index_by_order_id(ws, order_id)
    if ridx is None:
        return None

    row = _safe_row_values(ws, ridx)
    if not row:
        return None
    return _row_to_dict(header, row)


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
    wanted_status = (status or "").strip()
    if not contact or not wanted_status:
        return None

    max_len = max(len(contact_vals), len(status_vals), len(order_vals))

    for row_idx in range(max_len - 1, 1, -1):
        cv = contact_vals[row_idx - 1] if row_idx - 1 < len(contact_vals) else ""
        sv = status_vals[row_idx - 1] if row_idx - 1 < len(status_vals) else ""
        ov = order_vals[row_idx - 1] if row_idx - 1 < len(order_vals) else ""

        if str(cv).strip() != contact:
            continue
        if str(sv).strip() != wanted_status:
            continue

        oid = str(ov).strip()
        if oid:
            return oid

    return None


# ----------------------------------------
# UPDATE helpers
# ----------------------------------------

def update_order_status(orders_sh, order_id: str, new_status: str) -> Dict[str, Any]:
    tenant_id_for_alert = ""

    try:
        ws = _get_orders_ws(orders_sh)
        header = _get_header(ws)
        if not header:
            raise RuntimeError("ORDERS header row missing")

        ridx = _find_row_index_by_order_id(ws, order_id)

        if ridx is None:
            return {"ok": True, "found": False}

        status_col = _find_col_idx(header, "status")
        if status_col is None:
            raise RuntimeError("Missing status column")

        tenant_col = _find_col_idx(header, "tenant_id")
        if tenant_col is not None:
            row = _safe_row_values(ws, ridx)
            if tenant_col < len(row):
                tenant_id_for_alert = str(row[tenant_col] or "").strip()

        clean_status = str(new_status or "").strip()
        if not clean_status:
            raise RuntimeError("new_status missing")

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
            status=clean_status,
        )

        return {"ok": True, "found": True}

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
) -> Dict[str, Any]:
    tenant_id_for_alert = ""

    try:
        ws = _get_orders_ws(orders_sh)
        header = _get_header(ws)
        if not header:
            raise RuntimeError("ORDERS header row missing")

        ridx = _find_row_index_by_order_id(ws, order_id)

        if ridx is None:
            return {"ok": True, "found": False}

        tenant_col = _find_col_idx(header, "tenant_id")
        if tenant_col is not None:
            row = _safe_row_values(ws, ridx)
            if tenant_col < len(row):
                tenant_id_for_alert = str(row[tenant_col] or "").strip()

        fcol = _find_col_idx(header, "payment_proof_file_id")
        tcol = _find_col_idx(header, "payment_proof_type")
        ccol = _find_col_idx(header, "payment_proof_caption")

        if fcol is None or tcol is None:
            raise RuntimeError("Missing payment proof columns")

        clean_file_id = str(proof_file_id or "").strip()
        clean_proof_type = str(proof_type or "").strip()
        clean_caption = str(proof_caption or "").strip()

        if not clean_file_id:
            raise RuntimeError("proof_file_id missing")
        if not clean_proof_type:
            raise RuntimeError("proof_type missing")

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

        return {"ok": True, "found": True}

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
