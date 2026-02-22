# app/orders.py

import json
from typing import Any, Dict, List

from fastapi import HTTPException

from app.sheets import get_ws
from app.utils import normalize, now_iso_utc


def gen_order_id() -> str:
    import secrets
    return secrets.token_hex(4)  # 8 chars hex


def _ensure_ws(spreadsheet, title: str):
    """
    Obtiene worksheet por título. Si no existe, lanza error claro.
    """
    try:
        return get_ws(spreadsheet, title)
    except Exception:
        raise HTTPException(status_code=500, detail=f"Worksheet '{title}' not found in tenant spreadsheet")


def ensure_orders_headers(ws, required: List[str]) -> List[str]:
    values = ws.get_all_values()
    if not values or not values[0]:
        raise HTTPException(status_code=500, detail="Orders sheet is empty or missing headers in row 1")

    headers_raw = values[0]
    headers_norm = [normalize(h) for h in headers_raw]

    required_norm = [normalize(h) for h in required]
    missing = [h for h in required_norm if h not in headers_norm]
    if missing:
        raise HTTPException(
            status_code=500,
            detail=f"Orders sheet missing required headers in row 1: {missing}. Headers actuales: {headers_raw}",
        )

    return headers_norm


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

    ensure_orders_headers(
        ws,
        required=[
            "order_id", "created_at", "tenant_id", "customer_name", "customer_contact",
            "items", "notes", "delivery_type", "requested_time", "status", "source", "total_amount",
        ],
    )

    created_at = now_iso_utc()

    payload_map: Dict[str, Any] = {
        "order_id": order_id,
        "created_at": created_at,
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
    }

    # Mantener el orden EXACTO de columnas según la fila 1 del Sheet
    header_raw = ws.row_values(1)
    row: List[Any] = []
    for h_raw in header_raw:
        h = normalize(h_raw)
        row.append(payload_map.get(h, ""))

    ws.append_row(row, value_input_option="USER_ENTERED")


def update_order_status(orders_sh, order_id: str, new_status: str) -> Dict[str, Any]:
    ws = _ensure_ws(orders_sh, "Orders")

    values = ws.get_all_values()
    if not values or not values[0]:
        return {"found": False}

    headers_raw = values[0]
    headers_norm = [normalize(h) for h in headers_raw]

    if "order_id" not in headers_norm or "status" not in headers_norm:
        raise HTTPException(status_code=500, detail="Orders sheet must have order_id and status headers in row 1")

    col_order_id = headers_norm.index("order_id")  # 0-based en values
    col_status = headers_norm.index("status")      # 0-based en values

    oid_target = normalize(order_id)

    # Buscar en values (rápido, 0 llamadas extra)
    found_row_index_1based = None
    old_status = ""
    for i in range(1, len(values)):  # desde fila 2 (índice 1)
        row = values[i]
        oid = row[col_order_id] if col_order_id < len(row) else ""
        if normalize(oid) == oid_target:
            found_row_index_1based = i + 1  # convertir a 1-based para Sheets
            old_status = row[col_status] if col_status < len(row) else ""
            break

    if not found_row_index_1based:
        return {"found": False}

    # Solo actualizar si cambia
    if normalize(old_status) != normalize(new_status):
        ws.update_cell(found_row_index_1based, col_status + 1, new_status)

    return {"found": True, "old_status": old_status}
