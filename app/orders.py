# app/orders.py
"""
Orders persistence layer (Google Sheets)

Objetivos de estas mejoras:
- Idempotencia clara en updates (status y proof) para tolerar retries de Telegram/Render.
- Menos escrituras en Sheets (batch update cuando se puede).
- Robusto ante headers desplazados (fila 1 técnica, fila 2 etiquetas) y pestañas con nombres distintos.
- Diagnóstico más útil en logs cuando autodetecta pestañas o faltan headers.

Requisitos implícitos del sistema:
- Orders sheet debe tener (mínimo): order_id, tenant_id, status
- Ideal: Orders tab se llame "Orders", pero si no, se autodetecta por headers.
"""

import json
from typing import Any, Dict, List, Tuple, Optional

from fastapi import HTTPException

from app.sheets import get_ws
from app.utils import normalize, now_iso_utc, log_event


def gen_order_id() -> str:
    import secrets
    return secrets.token_hex(4)  # 8 chars hex


# ---------------------------------
# Worksheet resolution (robusto)
# ---------------------------------

REQUIRED_ORDERS_HEADERS_MIN = ["order_id", "tenant_id", "status"]


def _ws_has_required_headers(ws, required_headers: List[str], max_scan_rows: int = 30) -> bool:
    try:
        values = ws.get_all_values()
    except Exception:
        return False

    if not values:
        return False

    req = [normalize(h) for h in required_headers]
    scan = values[:max_scan_rows]

    for row in scan:
        row_norm = [normalize(x) for x in row]
        if all(h in row_norm for h in req):
            return True

    return False


def _find_orders_ws_by_headers(orders_sh) -> Optional[Any]:
    try:
        for ws in orders_sh.worksheets():
            if _ws_has_required_headers(ws, REQUIRED_ORDERS_HEADERS_MIN):
                return ws
    except Exception:
        return None
    return None


def _ensure_orders_ws(orders_sh):
    # 1) camino rápido
    try:
        return get_ws(orders_sh, "Orders")
    except Exception:
        pass

    # 2) fallback por headers
    ws = _find_orders_ws_by_headers(orders_sh)
    if ws is not None:
        try:
            log_event("orders_ws_autodetected", worksheet_title=getattr(ws, "title", "unknown"))
        except Exception:
            pass
        return ws

    raise HTTPException(
        status_code=500,
        detail=(
            "Orders worksheet not found. Expected a tab named 'Orders' "
            "or any tab with headers including: order_id, tenant_id, status"
        ),
    )


# ---------------------------------
# Header parsing (robusto)
# ---------------------------------

def _detect_header_row_0based(values: List[List[str]], required_headers: List[str], max_scan: int = 30) -> int:
    req = [normalize(h) for h in required_headers]
    scan = values[:max_scan]

    for idx, row in enumerate(scan):
        row_norm = [normalize(x) for x in row]
        if all(h in row_norm for h in req):
            return idx

    return 0


def _get_orders_header(ws, required: List[str]) -> Tuple[int, List[str], List[str], List[List[str]]]:
    try:
        values = ws.get_all_values()
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Cannot read Orders sheet: {e}")

    if not values:
        raise HTTPException(status_code=500, detail="Orders sheet is empty")

    header_idx_0 = _detect_header_row_0based(values, required_headers=required, max_scan=30)
    header_row_1based = header_idx_0 + 1

    headers_raw = values[header_idx_0]
    if not headers_raw:
        raise HTTPException(status_code=500, detail=f"Orders sheet missing headers in row {header_row_1based}")

    headers_norm = [normalize(h) for h in headers_raw]
    required_norm = [normalize(h) for h in required]

    missing = [h for h in required_norm if h not in headers_norm]
    if missing:
        raise HTTPException(
            status_code=500,
            detail=(
                f"Orders sheet missing required headers in row {header_row_1based}: {missing}. "
                f"Headers actuales: {headers_raw}"
            ),
        )

    return header_row_1based, headers_raw, headers_norm, values


def _col_index(headers_norm: List[str], col_name: str) -> Optional[int]:
    key = normalize(col_name)
    return headers_norm.index(key) if key in headers_norm else None


def _row_as_dict(headers_norm: List[str], row: List[Any]) -> Dict[str, Any]:
    out: Dict[str, Any] = {}
    for j, h in enumerate(headers_norm):
        out[h] = row[j] if j < len(row) else ""
    return out


def _find_row_by_order_id(values: List[List[Any]], header_row_1based: int, col_order_id: int, order_id: str) -> Optional[int]:
    oid_target = normalize(order_id)

    start_idx_0 = header_row_1based  # 1-based -> idx 0-based
    for i in range(start_idx_0, len(values)):
        row = values[i]
        oid = row[col_order_id] if col_order_id < len(row) else ""
        if normalize(oid) == oid_target:
            return i + 1
    return None


def _safe_str(v: Any) -> str:
    return str(v if v is not None else "")


# ---------------------------------
# Public API
# ---------------------------------

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
    total_amount: float,
) -> None:
    ws = _ensure_orders_ws(orders_sh)

    required = [
        "order_id", "created_at", "tenant_id", "customer_name", "customer_contact",
        "items", "notes", "delivery_type", "requested_time", "status", "source", "total_amount",
    ]
    header_row, headers_raw, headers_norm, _values = _get_orders_header(ws, required=required)

    payload_map: Dict[str, Any] = {
        "order_id": order_id,
        "created_at": now_iso_utc(),
        "tenant_id": tenant_id,
        "customer_name": customer_name,
        "customer_contact": customer_contact,
        "items": json.dumps(items, ensure_ascii=False),
        "notes": "",
        "delivery_type": delivery_type,
        "requested_time": requested_time,
        "status": status,
        "source": source,
        "total_amount": total_amount,

        # opcionales (si existen como headers)
        "payment_proof_file_id": "",
        "payment_proof_type": "",
        "payment_proof_caption": "",
        "payment_confirmed_at": "",
        "items_snapshot": "",
        "currency": "",
        "pricing_version": "",
    }

    row: List[Any] = []
    for h_raw in headers_raw:
        key = normalize(h_raw)
        row.append(payload_map.get(key, ""))

    try:
        ws.append_row(row, value_input_option="USER_ENTERED")
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed appending order row: {e}")


def update_order_status(orders_sh, order_id: str, new_status: str) -> Dict[str, Any]:
    ws = _ensure_orders_ws(orders_sh)

    required = ["order_id", "status"]
    header_row, headers_raw, headers_norm, values = _get_orders_header(ws, required=required)

    col_order_id = headers_norm.index("order_id")
    col_status = headers_norm.index("status")

    found_row_1based = _find_row_by_order_id(values, header_row, col_order_id, order_id)
    if not found_row_1based:
        return {"found": False}

    row_idx_0 = found_row_1based - 1
    row = values[row_idx_0] if row_idx_0 < len(values) else []
    old_status = row[col_status] if col_status < len(row) else ""

    if normalize(old_status) == normalize(new_status):
        return {
            "found": True,
            "old_status": old_status,
            "row_1based": found_row_1based,
            "already": True,
        }

    try:
        ws.update_cell(found_row_1based, col_status + 1, new_status)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed updating status: {e}")

    return {
        "found": True,
        "old_status": old_status,
        "row_1based": found_row_1based,
        "already": False,
    }


def get_order_by_id(orders_sh, order_id: str) -> Optional[Dict[str, Any]]:
    ws = _ensure_orders_ws(orders_sh)

    required = ["order_id", "tenant_id", "customer_name", "customer_contact", "items", "status", "total_amount"]
    header_row, headers_raw, headers_norm, values = _get_orders_header(ws, required=required)

    col_order_id = headers_norm.index("order_id")
    found_row_1based = _find_row_by_order_id(values, header_row, col_order_id, order_id)
    if not found_row_1based:
        return None

    row_idx_0 = found_row_1based - 1
    row = values[row_idx_0] if row_idx_0 < len(values) else []
    data = _row_as_dict(headers_norm, row)
    data["_row_1based"] = found_row_1based
    data["_header_row"] = header_row
    return data


def find_latest_pending_order_for_contact(
    orders_sh,
    customer_contact: str,
    status: str = "PENDING_PAYMENT",
) -> Optional[str]:
    ws = _ensure_orders_ws(orders_sh)

    required = ["order_id", "customer_contact", "status"]
    header_row, headers_raw, headers_norm, values = _get_orders_header(ws, required=required)

    col_oid = headers_norm.index("order_id")
    col_contact = headers_norm.index("customer_contact")
    col_status = headers_norm.index("status")

    contact_target = normalize(str(customer_contact))
    status_target = normalize(status)

    for i in range(len(values) - 1, header_row - 1, -1):
        row = values[i]
        c = row[col_contact] if col_contact < len(row) else ""
        s = row[col_status] if col_status < len(row) else ""
        if normalize(_safe_str(c)) == contact_target and normalize(_safe_str(s)) == status_target:
            oid = row[col_oid] if col_oid < len(row) else ""
            return oid.strip() if oid else None

    return None


def update_order_payment_proof(
    orders_sh,
    order_id: str,
    proof_file_id: str,
    proof_type: str,
    proof_caption: str = "",
) -> Dict[str, Any]:
    ws = _ensure_orders_ws(orders_sh)

    required = ["order_id"]
    header_row, headers_raw, headers_norm, values = _get_orders_header(ws, required=required)

    col_oid = headers_norm.index("order_id")
    col_file = _col_index(headers_norm, "payment_proof_file_id")
    col_type = _col_index(headers_norm, "payment_proof_type")
    col_cap = _col_index(headers_norm, "payment_proof_caption")

    found_row_1based = _find_row_by_order_id(values, header_row, col_oid, order_id)
    if not found_row_1based:
        return {"found": False}

    row_idx_0 = found_row_1based - 1
    row = values[row_idx_0] if row_idx_0 < len(values) else []

    def cur(col_idx: Optional[int]) -> str:
        if col_idx is None:
            return ""
        return _safe_str(row[col_idx] if col_idx < len(row) else "")

    cur_file = cur(col_file)
    cur_type = cur(col_type)
    cur_cap = cur(col_cap)

    updates: List[Tuple[int, str]] = []
    if col_file is not None and _safe_str(cur_file) != _safe_str(proof_file_id):
        updates.append((col_file + 1, proof_file_id))
    if col_type is not None and _safe_str(cur_type) != _safe_str(proof_type):
        updates.append((col_type + 1, proof_type))
    if col_cap is not None and _safe_str(cur_cap) != _safe_str(proof_caption):
        updates.append((col_cap + 1, proof_caption))

    if not updates:
        return {"found": True, "row_1based": found_row_1based, "changed": False}

    try:
        data = []
        for col_1based, value in updates:
            a1 = ws.cell(found_row_1based, col_1based).address
            data.append({"range": a1, "values": [[value]]})
        ws.batch_update(data)
    except Exception:
        try:
            for col_1based, value in updates:
                ws.update_cell(found_row_1based, col_1based, value)
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"Failed updating payment proof: {e}")

    return {"found": True, "row_1based": found_row_1based, "changed": True}


# =========================================================
# ✅ BACKWARD COMPATIBILITY (CLAVE)
# =========================================================
# Si algún archivo todavía importa append_order en vez de append_order_row,
# no se rompe el deploy.
append_order = append_order_row
