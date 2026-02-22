# app/sheets.py
import os
import json
from typing import Any, Dict, List, Optional, Tuple

import gspread
from google.oauth2.service_account import Credentials

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]

_gspread_client: Optional[gspread.Client] = None


def get_gspread_client() -> gspread.Client:
    """
    Crea y cachea un cliente de gspread usando el JSON de service account
    almacenado en la variable de entorno GCP_CREDENTIALS_JSON.
    """
    global _gspread_client

    if _gspread_client is not None:
        return _gspread_client

    raw = os.getenv("GCP_CREDENTIALS_JSON", "").strip()
    if not raw:
        raise RuntimeError("Falta la variable de entorno GCP_CREDENTIALS_JSON")

    info = json.loads(raw)
    creds = Credentials.from_service_account_info(info, scopes=SCOPES)
    _gspread_client = gspread.authorize(creds)
    return _gspread_client


def open_spreadsheet(spreadsheet_id: str) -> gspread.Spreadsheet:
    client = get_gspread_client()
    return client.open_by_key(spreadsheet_id)


def open_orders_spreadsheet(orders_sheet_id: str) -> gspread.Spreadsheet:
    """
    Mantengo este nombre porque tu código lo está importando.
    Abre el spreadsheet principal del tenant (donde está la pestaña ORDERS).
    """
    return open_spreadsheet(orders_sheet_id)


def open_worksheet(spreadsheet_id: str, worksheet_title: str):
    ss = open_spreadsheet(spreadsheet_id)
    return ss.worksheet(worksheet_title)


def read_records_manual(ws, header_row: int = 1) -> List[Dict[str, Any]]:
    """
    Lee una worksheet con headers en una fila específica.
    Devuelve list[dict] con keys = headers (fila header_row).
    """
    values = ws.get_all_values()
    if not values or len(values) < header_row:
        return []

    headers = [h.strip() for h in values[header_row - 1]]
    records: List[Dict[str, Any]] = []

    for row in values[header_row:]:
        # ignora filas totalmente vacías
        if not any(cell.strip() for cell in row):
            continue

        item = {}
        for i, key in enumerate(headers):
            if not key:
                continue
            item[key] = row[i].strip() if i < len(row) else ""
        records.append(item)

    return records
