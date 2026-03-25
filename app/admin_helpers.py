# app/admin_helpers.py

from typing import Any, Dict
import re


def _safe_str(v: Any) -> str:
    return str(v or "").strip()


def _safe_client_chat_id_from_order(order: Dict[str, Any]) -> str:
    chat_id = str(order.get("customer_telegram_chat_id") or "").strip()
    if chat_id and chat_id.isdigit():
        return chat_id

    fallback = str(order.get("customer_contact") or "").strip()
    if fallback and fallback.isdigit():
        return fallback

    return ""


def _extract_slot_hhmm(requested_time: str) -> str:
    s = _safe_str(requested_time)
    if not s:
        return ""
    m = re.search(r"(\d{1,2}:\d{2})", s)
    return m.group(1) if m else ""
