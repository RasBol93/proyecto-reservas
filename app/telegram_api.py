import json
import urllib.request
from typing import Any, Dict, Optional

from app.config import TELEGRAM_API_BASE
from app.utils import log_event


def telegram_api_call(bot_token: str, method: str, payload: Dict[str, Any]) -> Dict[str, Any]:
    if not bot_token:
        raise RuntimeError("bot_token missing")

    url = f"{TELEGRAM_API_BASE}/bot{bot_token}/{method}"
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"}, method="POST")

    try:
        with urllib.request.urlopen(req, timeout=12) as resp:
            raw = resp.read().decode("utf-8")
            return json.loads(raw)
    except Exception as e:
        log_event("telegram_api_error", method=method, error=str(e))
        return {"ok": False, "error": str(e)}


def telegram_send_text(bot_token: str, chat_id: int, text: str, reply_markup: Optional[Dict[str, Any]] = None) -> None:
    payload: Dict[str, Any] = {"chat_id": chat_id, "text": text}
    if reply_markup:
        payload["reply_markup"] = reply_markup
    res = telegram_api_call(bot_token, "sendMessage", payload)
    log_event("telegram_send_text", ok=res.get("ok", False))


def telegram_answer_callback(bot_token: str, callback_query_id: str, text: str) -> None:
    res = telegram_api_call(bot_token, "answerCallbackQuery", {"callback_query_id": callback_query_id, "text": text})
    log_event("telegram_answer_callback", ok=res.get("ok", False))
