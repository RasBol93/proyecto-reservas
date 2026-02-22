# app/sheets.py

import os
import json
from typing import Any

import gspread

from app.config import ENV_GCP_CREDS_JSON


def get_gspread_client() -> gspread.Client:
    """
    Crea un cliente de gspread usando el JSON (service account) guardado en la env var.
    Env var esperada: GCP_CREDENTIALS_JSON
    """
    creds_raw = (os.getenv(ENV_GCP_CREDS_JSON, "") or "").strip()
    if not creds_raw:
        raise RuntimeError(f"Missing env var: {ENV_GCP_CREDS_JSON}")

    info = json.loads(creds_raw)

    scopes = [
        "https://www.googleapis.com/auth/spreadsheets",
        "https://www.googleapis.com/auth/drive",
    ]

    return gspread.service_account_from_dict(info, scopes=scopes)


def open_spreadsheet_by_key(gc: gspread.Client, spreadsheet_id: str) -> gspread.Spreadsheet:
    """
    Abre un Spreadsheet por su key/id.
    """
    sid = (spreadsheet_id or "").strip()
    if not sid:
        raise RuntimeError("spreadsheet_id is required to open spreadsheet")
    return gc.open_by_key(sid)


def get_worksheet(sh: gspread.Spreadsheet, title: str) -> gspread.Worksheet:
    """
    Obtiene una worksheet por título.
    """
    t = (title or "").strip()
    if not t:
        raise RuntimeError("worksheet title is required")
    return sh.worksheet(t)


def get_all_values(ws: gspread.Worksheet) -> list[list[Any]]:
    """
    Wrapper simple por si luego quieres interceptar logs/errores.
    """
    return ws.get_all_values()
