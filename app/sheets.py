# app/sheets.py

import json
import gspread

from app.config import ENV_GCP_CREDS_JSON, ENV_CONFIG_SPREADSHEET_ID, env_required


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
