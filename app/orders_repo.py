import json
from typing import Any, Dict, List

import gspread
from fastapi import HTTPException

from app.utils import normalize, now_iso_utc


def ensure_orders_headers(ws: gspread.Worksheet, required: List[str]) -> List[str]:
    values = ws.get_all_values()
    if not values or not values[0]:
        raise HTTPException(status_code=500, detail="Orders sheet is empty or missing headers in row 1")

    headers = values[0]
    headers_norm = [normalize(h) for h in headers]

    missing = [h for h in required if normalize(h) not in headers_norm]
    if missing:
        raise HTTPException(
            status_code=500,
            detail=f"Orders sheet missing required headers in row 1: {missing}. Headers actuales: {headers}"
        )
    return headers_norm


def append_order_row(
    orders_sh: gspread.Spreadsheet,
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
):
    ws = orders_sh.worksheet("Orders")
    ensure_orders_headers(
        ws,
        required=[
            "order_id", "created_at", "tenant_id", "customer_name", "customer_contact",
            "items", "notes", "delivery_type", "requested_time", "status", "source", "total_amount"
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

    header_raw = ws.row_values(1)
    row: List[Any] = []
    for h_raw in header_raw:
        h = normalize(h_raw)
        row.append(payload_map.get(h, ""))

    ws.append_row(row, value_input_option="USER_ENTERED")


def update_order_status(orders_sh: gspread.Spreadsheet, order_id: str, new_status: str) -> Dict[str, Any]:
    ws = orders_sh.worksheet("Orders")
    values = ws.get_all_values()
    if not values:
        return {"found": False}

    headers = values[0]
    headers_norm = [normalize(h) for h in headers]

    if "order_id" not in headers_norm or "status" not in headers_norm:
        raise HTTPException(status_code=500, detail="Orders sheet must have order_id and status headers in row 1")

    col_order_id = headers_norm.index("order_id") + 1
    col_status = headers_norm.index("status") + 1

    for r_idx in range(2, len(values) + 1):
        oid = ws.cell(r_idx, col_order_id).value
        if str(oid).strip() == order_id:
            old_status = ws.cell(r_idx, col_status).value or ""
            if normalize(old_status) != normalize(new_status):
                ws.update_cell(r_idx, col_status, new_status)
            return {"found": True, "old_status": old_status}

    return {"found": False}
