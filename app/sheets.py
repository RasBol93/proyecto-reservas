# app/sheets.py

import json
from typing import Any, Dict, List

import gspread

from app.config import ENV_GCP_CREDS_JSON, ENV_CONFIG_SPREADSHEET_ID, env_required
from app.utils import normalize


# -------------------------
# Gspread client
# -------------------------

def get_gspread_client() -> gspread.Client:
    """
    Crea un cliente de gspread usando el JSON de service account guardado en env.
    Env esperado: GCP_CREDENTIALS_JSON (string JSON completo)
    """
    creds_json = env_required(ENV_GCP_CREDS_JSON)
    info = json.loads(creds_json)

    scopes = [
        "https://www.googleapis.com/auth/spreadsheets",
        "https://www.googleapis.com/auth/drive",
    ]
    return gspread.service_account_from_dict(info, scopes=scopes)


def open_spreadsheet_by_key(gc: gspread.Client, spreadsheet_id: str) -> gspread.Spreadsheet:
    """
    Abre cualquier spreadsheet por ID (key).
    """
    sid = (spreadsheet_id or "").strip()
    if not sid:
        raise RuntimeError("Missing spreadsheet_id")
    return gc.open_by_key(sid)


def open_config_spreadsheet(gc: gspread.Client) -> gspread.Spreadsheet:
    """
    Abre el spreadsheet de configuración (Tenants, etc.)
    Env esperado: RESERVACIONES_CONFIG (spreadsheet id)
    """
    config_id = env_required(ENV_CONFIG_SPREADSHEET_ID)
    return gc.open_by_key(config_id)


# -------------------------
# Worksheet helpers
# -------------------------

def get_ws(spreadsheet: gspread.Spreadsheet, title: str) -> gspread.Worksheet:
    """
    Devuelve una worksheet por nombre.
    """
    return spreadsheet.worksheet(title)


# -------------------------
# Reading helpers (headers desplazados)
# -------------------------

def detect_header_row(values: List[List[Any]], required_headers: List[str], max_scan: int = 10) -> int:
    """
    Soporta el patrón típico:
      - fila 1: headers técnicos (EN)
      - fila 2: traducción / etiquetas (ES)
    Detecta la fila de headers técnicos buscando required_headers.
    Retorna índice 1-based.
    """
    req = [normalize(h) for h in required_headers]
    scan = values[:max_scan]

    for idx, row in enumerate(scan, start=1):
        row_norm = [normalize(x) for x in row]
        if all(h in row_norm for h in req):
            return idx

    # fallback: fila 1
    return 1


def read_records_manual(ws: gspread.Worksheet, required_headers: List[str]) -> List[Dict[str, Any]]:
    """
    Lee una worksheet y devuelve una lista de dicts usando headers normalizados.
    Detecta automáticamente la fila de headers.
    """
    values = ws.get_all_values()
    if not values:
        return []

    header_row = detect_header_row(values, required_headers=required_headers)
    headers = values[header_row - 1]
    headers_norm = [normalize(h) for h in headers]

    records: List[Dict[str, Any]] = []

    for row in values[header_row:]:
        if not any(str(x).strip() for x in row):
            continue

        d: Dict[str, Any] = {}
        for i, h in enumerate(headers_norm):
            if not h:
                continue
            d[h] = row[i] if i < len(row) else ""
        records.append(d)

    return records
