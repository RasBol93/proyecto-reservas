# app/orders.py

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
        detail="Orders worksheet not found. Expected a tab named 'Orders' or any tab with headers including: order_id, tenant_id, status",
    )


# ---------------------------------
# Header parsing (robusto)
# ---------------------------------

def _detect_header_row_0based(values: List[List[str]], required_headers: List[str], max_scan: int = 30) -> int:
    """
    Encuentra fila de headers dentro de las primeras max_scan filas.
    Devuelve índice 0-based. Fallback: 0.
    """
    req = [normalize(h) for h in required_headers]
    scan = values[:max_scan]

    for idx, row in enumerate(scan):
        row_norm = [normalize(x) for x in row]
        if all(h in row_norm for h in req):
            return idx

    return 0


def _get_orders_header(ws, required: List[str]) -> Tuple[int, List[str], List[str], List[List[str]]]:
    """
    Devuelve:
      header_row_1based, headers_raw, headers_norm, values
    values se retorna para no llamar get_all_values() dos veces.
    """
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
    }

    # construir fila EXACTAMENTE con los headers detectados
    row: List[Any] = []
    for h_raw in headers_raw:
        key = normalize(h_raw)
        row.append(payload_map.get(key, ""))

    try:
        ws.append_row(row, value_input_option="USER_ENTERED")
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed appending order row: {e}")


def update_order_status(orders_sh, order_id: str, new_status: str) -> Dict[str, Any]:
    """
    Mejora: idempotencia explícita + retorno de bandera already.
    """
    ws = _ensure_orders_ws(orders_sh)

    required = ["order_id", "status"]
    header_row, headers_raw, headers_norm, values = _get_orders_header(ws, required=required)

    col_order_id = headers_norm.index("order_id")
    col_status = headers_norm.index("status")

    oid_target = normalize(order_id)
    old_status = ""
    found_row_1based = None

    # datos empiezan debajo del header_row
    start_idx_0 = header_row  # header_row es 1-based => start idx 0-based = header_row
    for i in range(start_idx_0, len(values)):
        row = values[i]
        oid = row[col_order_id] if col_order_id < len(row) else ""
        if normalize(oid) == oid_target:
            found_row_1based = i + 1
            old_status = row[col_status] if col_status < len(row) else ""
            break

    if not found_row_1based:
        return {"found": False}

    # idempotencia
    if normalize(old_status) == normalize(new_status):
        return {"found": True, "old_status": old_status, "row_1based": found_row_1based, "already": True}

    try:
        ws.update_cell(found_row_1based, col_status + 1, new_status)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed updating status: {e}")

    return {"found": True, "old_status": old_status, "row_1based": found_row_1based, "already": False}


def get_order_by_id(orders_sh, order_id: str) -> Optional[Dict[str, Any]]:
    ws = _ensure_orders_ws(orders_sh)

    required = ["order_id", "tenant_id", "customer_name", "customer_contact", "items", "status", "total_amount"]
    header_row, headers_raw, headers_norm, values = _get_orders_header(ws, required=required)

    col_order_id = headers_norm.index("order_id")
    oid_target = normalize(order_id)

    start_idx_0 = header_row
    for i in range(start_idx_0, len(values)):
        row = values[i]
        oid = row[col_order_id] if col_order_id < len(row) else ""
        if normalize(oid) == oid_target:
            data = _row_as_dict(headers_norm, row)
            data["_row_1based"] = i + 1
            data["_header_row"] = header_row
            return data

    return None


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

    # recorrer desde abajo hacia arriba, sin tocar header
    for i in range(len(values) - 1, header_row - 1, -1):
        row = values[i]
        c = row[col_contact] if col_contact < len(row) else ""
        s = row[col_status] if col_status < len(row) else ""
        if normalize(str(c)) == contact_target and normalize(str(s)) == status_target:
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
    """
    Mejora: actualiza solo si cambió (evita writes duplicados por retries).
    """
    ws = _ensure_orders_ws(orders_sh)

    required = ["order_id"]
    header_row, headers_raw, headers_norm, values = _get_orders_header(ws, required=required)

    col_oid = headers_norm.index("order_id")
    col_file = _col_index(headers_norm, "payment_proof_file_id")
    col_type = _col_index(headers_norm, "payment_proof_type")
    col_cap = _col_index(headers_norm, "payment_proof_caption")

    oid_target = normalize(order_id)
    found_row_1based = None
    found_row = None

    start_idx_0 = header_row
    for i in range(start_idx_0, len(values)):
        row = values[i]
        oid = row[col_oid] if col_oid < len(row) else ""
        if normalize(oid) == oid_target:
            found_row_1based = i + 1
            found_row = row
            break

    if not found_row_1based or found_row is None:
        return {"found": False}

    def _cell_value(col_idx: Optional[int]) -> str:
        if col_idx is None:
            return ""
        return str(found_row[col_idx] if col_idx < len(found_row) else "")

    cur_file = _cell_value(col_file)
    cur_type = _cell_value(col_type)
    cur_cap = _cell_value(col_cap)

    # Actualizar solo columnas que existan y que cambien
    try:
        if col_file is not None and str(cur_file) != str(proof_file_id):
            ws.update_cell(found_row_1based, col_file + 1, proof_file_id)
        if col_type is not None and str(cur_type) != str(proof_type):
            ws.update_cell(found_row_1based, col_type + 1, proof_type)
        if col_cap is not None and str(cur_cap) != str(proof_caption):
            ws.update_cell(found_row_1based, col_cap + 1, proof_caption)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed updating payment proof: {e}")

    return {"found": True, "row_1based": found_row_1based}
