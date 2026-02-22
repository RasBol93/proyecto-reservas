# app/orders.py

import json
from typing import Any, Dict, List, Tuple

from fastapi import HTTPException

from app.sheets import get_ws, detect_header_row
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


def _get_orders_header(ws, required: List[str]) -> Tuple[int, List[str], List[str]]:
    """
    Devuelve:
      - header_row (1-based)
      - headers_raw (tal cual en el sheet)
      - headers_norm (normalizados)
    Detecta automáticamente la fila de headers (para soportar fila 1 o fila 2).
    """
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
    }

    # Mantener EXACTO el orden de columnas según la fila real de headers detectada
    # (no asumas fila 1)
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

    col_order_id = headers_norm.index("order_id")  # 0-based en la fila header
    col_status = headers_norm.index("status")      # 0-based

    oid_target = normalize(order_id)
    old_status = ""
    found_row_1based = None

    # data empieza después de header_row
    start_idx = header_row  # porque values es 0-based y header_row es 1-based
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

    return {"found": True, "old_status": old_status}
