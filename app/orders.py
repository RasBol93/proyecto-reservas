# app/orders.py

import json
from typing import Any, Dict, List, Tuple, Optional

from fastapi import HTTPException

from app.sheets import get_ws, detect_header_row
from app.utils import normalize, now_iso_utc


def gen_order_id() -> str:
    import secrets
    return secrets.token_hex(4)  # 8 chars hex


def _ensure_ws(spreadsheet, title: str):
    try:
        return get_ws(spreadsheet, title)
    except Exception:
        raise HTTPException(status_code=500, detail=f"Worksheet '{title}' not found in tenant spreadsheet")


def _get_orders_header(ws, required: List[str]) -> Tuple[int, List[str], List[str]]:
    values = ws.get_all_values()
    if not values:
        raise HTTPException(status_code=500, detail="Orders sheet is empty")

    header_row = detect_header_row(values, required_headers=required)  # 1-based
    if header_row < 1 or header_row > len(values):
        header_row = 1

    headers_raw = values[header_row - 1]
    if not headers_raw:
        raise HTTPException(status_code=500, detail=f"Orders sheet missing headers in row {header_row}")

    headers_norm = [normalize(h) for h in headers_raw]
    required_norm = [normalize(h) for h in required]

    missing = [h for h in required_norm if h not in headers_norm]
    if missing:
        raise HTTPException(
            status_code=500,
            detail=(
                f"Orders sheet missing required headers in row {header_row}: {missing}. "
                f"Headers actuales: {headers_raw}"
            ),
        )

    return header_row, headers_raw, headers_norm


def _col_index(headers_norm: List[str], col_name: str) -> Optional[int]:
    """Devuelve índice 0-based si existe, else None"""
    key = normalize(col_name)
    return headers_norm.index(key) if key in headers_norm else None


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
    ws = _ensure_ws(orders_sh, "Orders")

    required = [
        "order_id", "created_at", "tenant_id", "customer_name", "customer_contact",
        "items", "notes", "delivery_type", "requested_time", "status", "source", "total_amount",
    ]
    header_row, headers_raw, headers_norm = _get_orders_header(ws, required=required)

    # OJO: agregamos también defaults para payment_proof_* si existen en headers
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
        "payment_proof_file_id": "",
        "payment_proof_type": "",
        "payment_proof_caption": "",
    }

    header_cells = ws.row_values(header_row)
    row: List[Any] = []
    for h_raw in header_cells:
        key = normalize(h_raw)
        row.append(payload_map.get(key, ""))

    ws.append_row(row, value_input_option="USER_ENTERED")


def update_order_status(orders_sh, order_id: str, new_status: str) -> Dict[str, Any]:
    ws = _ensure_ws(orders_sh, "Orders")

    required = ["order_id", "status"]
    header_row, headers_raw, headers_norm = _get_orders_header(ws, required=required)

    values = ws.get_all_values()
    if not values:
        return {"found": False}

    col_order_id = headers_norm.index("order_id")
    col_status = headers_norm.index("status")

    oid_target = normalize(order_id)
    old_status = ""
    found_row_1based = None

    start_idx = header_row
    for i in range(start_idx, len(values)):
        row = values[i]
        oid = row[col_order_id] if col_order_id < len(row) else ""
        if normalize(oid) == oid_target:
            found_row_1based = i + 1
            old_status = row[col_status] if col_status < len(row) else ""
            break

    if not found_row_1based:
        return {"found": False}

    if normalize(old_status) != normalize(new_status):
        ws.update_cell(found_row_1based, col_status + 1, new_status)

    return {"found": True, "old_status": old_status, "row_1based": found_row_1based}


def get_order_by_id(orders_sh, order_id: str) -> Optional[Dict[str, Any]]:
    ws = _ensure_ws(orders_sh, "Orders")

    required = ["order_id", "tenant_id", "customer_name", "customer_contact", "items", "status", "total_amount"]
    header_row, headers_raw, headers_norm = _get_orders_header(ws, required=required)

    values = ws.get_all_values()
    if not values:
        return None

    col_order_id = headers_norm.index("order_id")
    oid_target = normalize(order_id)

    start_idx = header_row
    for i in range(start_idx, len(values)):
        row = values[i]
        oid = row[col_order_id] if col_order_id < len(row) else ""
        if normalize(oid) == oid_target:
            data: Dict[str, Any] = {}
            for j, h in enumerate(headers_norm):
                data[h] = row[j] if j < len(row) else ""
            data["_row_1based"] = i + 1
            data["_header_row"] = header_row
            return data

    return None


def find_latest_pending_order_for_contact(orders_sh, customer_contact: str, status: str = "PENDING_PAYMENT") -> Optional[str]:
    """
    Busca el pedido más reciente (de abajo hacia arriba) para customer_contact con status dado.
    Devuelve order_id o None.
    """
    ws = _ensure_ws(orders_sh, "Orders")

    required = ["order_id", "customer_contact", "status"]
    header_row, headers_raw, headers_norm = _get_orders_header(ws, required=required)

    values = ws.get_all_values()
    if not values:
        return None

    col_oid = headers_norm.index("order_id")
    col_contact = headers_norm.index("customer_contact")
    col_status = headers_norm.index("status")

    contact_target = normalize(str(customer_contact))
    status_target = normalize(status)

    # recorrer desde abajo hacia arriba
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
    ws = _ensure_ws(orders_sh, "Orders")

    required = ["order_id"]
    header_row, headers_raw, headers_norm = _get_orders_header(ws, required=required)

    values = ws.get_all_values()
    if not values:
        return {"found": False}

    col_oid = headers_norm.index("order_id")
    col_file = _col_index(headers_norm, "payment_proof_file_id")
    col_type = _col_index(headers_norm, "payment_proof_type")
    col_cap = _col_index(headers_norm, "payment_proof_caption")

    oid_target = normalize(order_id)
    found_row_1based = None

    start_idx = header_row
    for i in range(start_idx, len(values)):
        row = values[i]
        oid = row[col_oid] if col_oid < len(row) else ""
        if normalize(oid) == oid_target:
            found_row_1based = i + 1
            break

    if not found_row_1based:
        return {"found": False}

    # Actualizar solo columnas que existan
    if col_file is not None:
        ws.update_cell(found_row_1based, col_file + 1, proof_file_id)
    if col_type is not None:
        ws.update_cell(found_row_1based, col_type + 1, proof_type)
    if col_cap is not None:
        ws.update_cell(found_row_1based, col_cap + 1, proof_caption)

    return {"found": True, "row_1based": found_row_1based}
