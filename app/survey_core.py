# app/survey_core.py

import random
import string
from datetime import datetime
from typing import Any, Dict, List

from zoneinfo import ZoneInfo

from app.sheets import get_ws
from app.utils import log_event
from app.alerts import alert_sheet_error


SURVEY_SETTINGS_WS = "Survey_Settings"
SURVEY_CONFIG_WS = "Survey_Config"
SURVEY_RESPONSES_WS = "Survey_Responses"
SURVEY_COUPONS_WS = "Survey_Coupons"

SURVEY_SETTINGS_HEADERS = ["key", "value", "active"]
SURVEY_CONFIG_HEADERS = ["question_id", "order", "question_text", "type", "active"]
SURVEY_RESPONSES_HEADERS = [
    "response_id",
    "created_at",
    "survey_date",
    "tenant_id",
    "customer_phone",
    "customer_name",
    "question_id",
    "question_order",
    "question_text",
    "answer_type",
    "answer_value",
    "coupon_code",
]
SURVEY_COUPONS_HEADERS = [
    "coupon_code",
    "created_at",
    "tenant_id",
    "customer_phone",
    "reward_text",
    "used",
    "used_at",
]

SURVEY_ALLOWED_TYPES = {"text", "stars"}


def _safe_str(v: Any) -> str:
    return str(v or "").strip()


def _now_local(tenant_tz: str) -> datetime:
    return datetime.now(ZoneInfo(tenant_tz))


def _now_utc_iso() -> str:
    return datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")


def _survey_date_local(tenant_tz: str) -> str:
    return _now_local(tenant_tz).strftime("%Y-%m-%d")


def _normalize_phone(value: Any) -> str:
    raw = _safe_str(value)
    digits = "".join(ch for ch in raw if ch.isdigit())
    return digits


def _is_valid_phone(value: Any) -> bool:
    phone = _normalize_phone(value)
    return len(phone) >= 7


def _random_letters(n: int = 3) -> str:
    return "".join(random.choices(string.ascii_uppercase, k=n))


def _make_response_id(phone: str) -> str:
    base = datetime.utcnow().strftime("%Y%m%d%H%M%S")
    suffix = _random_letters(4)
    return f"srv_{base}_{phone}_{suffix}"


def _make_coupon_code(phone: str) -> str:
    phone_norm = _normalize_phone(phone)
    if not phone_norm:
        phone_norm = "SINNUMERO"
    return f"{phone_norm}{_random_letters(3)}"


def _ensure_ws(spreadsheet, title: str, headers: List[str]):
    try:
        ws = get_ws(spreadsheet, title)
        values = ws.get_all_values()
        if not values:
            ws.append_row(headers, value_input_option="USER_ENTERED")
        return ws
    except Exception:
        try:
            ws = spreadsheet.add_worksheet(title=title, rows=2000, cols=max(len(headers) + 2, 10))
            ws.append_row(headers, value_input_option="USER_ENTERED")
            return ws
        except Exception as e:
            log_event(
                "survey_ensure_ws_error",
                worksheet=title,
                error_type=type(e).__name__,
                error=str(e),
            )
            alert_sheet_error(
                tenant_id="",
                error=f"survey ensure ws failed for '{title}': {e}",
                extra_key="survey._ensure_ws",
            )
            raise


def ensure_survey_worksheets(orders_sh) -> Dict[str, Any]:
    return {
        "settings": _ensure_ws(orders_sh, SURVEY_SETTINGS_WS, SURVEY_SETTINGS_HEADERS),
        "config": _ensure_ws(orders_sh, SURVEY_CONFIG_WS, SURVEY_CONFIG_HEADERS),
        "responses": _ensure_ws(orders_sh, SURVEY_RESPONSES_WS, SURVEY_RESPONSES_HEADERS),
        "coupons": _ensure_ws(orders_sh, SURVEY_COUPONS_WS, SURVEY_COUPONS_HEADERS),
    }
