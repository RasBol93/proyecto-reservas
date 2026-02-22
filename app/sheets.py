# app/sheets.py
import json
import os
from typing import Any, Dict

import gspread
from google.oauth2.service_account import Credentials


SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]


def get_gspread_client() -> gspread.Client:
    """
    Crea un cliente de gspread usando la variable de entorno GCP_CREDENTIALS_JSON
    (service account).
    """
    raw = os.getenv("GCP_CREDENTIALS_JSON", "").strip()
    if not raw:
        raise RuntimeError("Missing env var: GCP_CREDENTIALS_JSON")

    info = json.loads(raw)
    creds = Credentials.from_service_account_info(info, scopes=SCOPES)
    return gspread.authorize(creds)


def open_orders_spreadsheet(gc: gspread.Client, tenant: Dict[str, Any]) -> gspread.Spreadsheet:
    """
    Abre el Spreadsheet de pedidos del tenant.
    Espera que tenant tenga la key: orders_sheet_id
    """
    sheet_id = str(tenant.get("orders_sheet_id", "")).strip()
    if not sheet_id:
        raise RuntimeError("Tenant missing orders_sheet_id")
    return gc.open_by_key(sheet_id)
