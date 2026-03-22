# app/orders.py

import json
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from app.utils import log_event


def _get_orders_ws(orders_sh):
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
    hdr = ws.row_values(1)
    return [h.strip() for h in hdr if str(h).strip()]


def _find_col_idx(header: List[str], col_name: str) -> Optional[int]:
    col_name = (col_name or "").strip()
    for i, h in enumerate(header):
        if h.strip() == col_name:
            return i
    return None


def _build_row_by_header(header: List[str], data: Dict[str, Any]) -> List[str]:
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


def gen_order_id() -> str:
    import secrets
    return secrets.token_hex(4)


def build_items_snapshot(items: List[Dict[str, Any]], menu_idx: Dict[str, Any]) -> List[Dict[str, Any]]:
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
) -> Dict[str, Any]:

    try:
        ws = _get_orders_ws(orders_sh)
        header = _get_header(ws)
        if not header:
            raise RuntimeError("ORDERS header row missing")

        data = {
            "order_id": order_id,
            "created_at": _now_iso_utc(),
            "tenant_id": tenant_id,
            "customer_name": customer_name,
            "customer_contact": customer_contact,
            "items": items,
            "items_snapshot": items_snapshot or "",
            "currency": currency,
            "pricing_version": pricing_version,
            "notes": notes,
            "delivery_type": delivery_type,
            "requested_time": requested_time,
            "status": status,
            "source": source,
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
            order_id=order_id,
            status=status,
            source=source,
            total_amount=total_amount,
            customer_contact=customer_contact,
        )

        return {"ok": True, "order_id": order_id}

    except Exception as e:
        log_event(
            "order_append_error",
            tenant_id=tenant_id,
            order_id=order_id,
            error_type=type(e).__name__,
            error=str(e),
        )
        return {"ok": False, "error": str(e)}


def append_order(*args, **kwargs):
    return append_order_row(*args, **kwargs)


def _iter_rows_as_dicts(ws) -> List[Dict[str, Any]]:
    header = _get_header(ws)
    if not header:
        return []
    values = ws.get_all_values()
    if len(values) <= 1:
        return []

    out: List[Dict[str, Any]] = []
    for r in values[1:]:
        if not any(str(x).strip() for x in r):
            continue
        d: Dict[str, Any] = {}
        for i, h in enumerate(header):
            d[h] = r[i] if i < len(r) else ""
        out.append(d)
    return out


def get_order_by_id(orders_sh, order_id: str) -> Optional[Dict[str, Any]]:
    ws = _get_orders_ws(orders_sh)
    rows = _iter_rows_as_dicts(ws)
    oid = (order_id or "").strip()
    for r in rows:
        if (r.get("order_id") or "").strip() == oid:
            return r
    return None


def find_latest_pending_order_for_contact(orders_sh, customer_contact: str, status: str = "PENDING_PAYMENT") -> Optional[str]:
    ws = _get_orders_ws(orders_sh)
    rows = _iter_rows_as_dicts(ws)

    contact = (customer_contact or "").strip()
    status = (status or "").strip()

    for r in reversed(rows):
        if (r.get("customer_contact") or "").strip() != contact:
            continue
        if (r.get("status") or "").strip() != status:
            continue
        return (r.get("order_id") or "").strip()
    return None


def _find_row_index_by_order_id(ws, order_id: str) -> Optional[int]:
    header = _get_header(ws)
    if not header:
        return None
    oid_col = _find_col_idx(header, "order_id")
    if oid_col is None:
        return None

    values = ws.get_all_values()
    for i in range(1, len(values)):
        row = values[i]
        cell = row[oid_col] if oid_col < len(row) else ""
        if str(cell).strip() == (order_id or "").strip():
            return i + 1
    return None


def update_order_status(orders_sh, order_id: str, new_status: str) -> Dict[str, Any]:
    try:
        ws = _get_orders_ws(orders_sh)
        header = _get_header(ws)
        ridx = _find_row_index_by_order_id(ws, order_id)

        if ridx is None:
            return {"ok": True, "found": False}

        status_col = _find_col_idx(header, "status")
        if status_col is None:
            raise RuntimeError("Missing status column")

        ws.update_cell(ridx, status_col + 1, str(new_status).strip())

        if str(new_status).strip() == "PAID":
            paid_col = _find_col_idx(header, "payment_confirmed_at")
            if paid_col is not None:
                ws.update_cell(ridx, paid_col + 1, _now_iso_utc())

        log_event("order_status_updated", order_id=order_id, status=new_status)

        return {"ok": True, "found": True}

    except Exception as e:
        log_event(
            "order_status_update_error",
            order_id=order_id,
            error_type=type(e).__name__,
            error=str(e),
        )
        return {"ok": False, "error": str(e)}


def update_order_payment_proof(
    orders_sh,
    order_id: str,
    proof_file_id: str,
    proof_type: str,
    proof_caption: str = "",
) -> Dict[str, Any]:

    try:
        ws = _get_orders_ws(orders_sh)
        header = _get_header(ws)
        ridx = _find_row_index_by_order_id(ws, order_id)

        if ridx is None:
            return {"ok": True, "found": False}

        fcol = _find_col_idx(header, "payment_proof_file_id")
        tcol = _find_col_idx(header, "payment_proof_type")
        ccol = _find_col_idx(header, "payment_proof_caption")

        if fcol is None or tcol is None:
            raise RuntimeError("Missing payment proof columns")

        ws.update_cell(ridx, fcol + 1, str(proof_file_id or "").strip())
        ws.update_cell(ridx, tcol + 1, str(proof_type or "").strip())

        if ccol is not None:
            ws.update_cell(ridx, ccol + 1, str(proof_caption or "").strip())

        log_event("order_proof_updated", order_id=order_id, proof_type=proof_type)

        return {"ok": True, "found": True}

    except Exception as e:
        log_event(
            "order_proof_update_error",
            order_id=order_id,
            error_type=type(e).__name__,
            error=str(e),
        )
        return {"ok": False, "error": str(e)}
