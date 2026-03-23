# app/telegram_api.py

import json
import time
import urllib.request
from typing import Any, Dict, Optional, List, Tuple

from app.config import TELEGRAM_API_BASE
from app.utils import log_event


def telegram_api_call(bot_token: str, method: str, payload: Dict[str, Any]) -> Dict[str, Any]:
    if not bot_token:
        raise RuntimeError("bot_token missing")

    url = f"{TELEGRAM_API_BASE}/bot{bot_token}/{method}"
    data = json.dumps(payload).encode("utf-8")

    req = urllib.request.Request(
        url,
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )

    with urllib.request.urlopen(req, timeout=20) as resp:
        raw = resp.read().decode("utf-8")
        return json.loads(raw)


def telegram_send_text(
    bot_token: str,
    chat_id: int,
    text: str,
    reply_markup: Optional[Dict[str, Any]] = None,
    parse_mode: Optional[str] = None,
) -> bool:
    payload: Dict[str, Any] = {"chat_id": chat_id, "text": text}
    if reply_markup:
        payload["reply_markup"] = reply_markup
    if parse_mode:
        payload["parse_mode"] = parse_mode

    try:
        res = telegram_api_call(bot_token, "sendMessage", payload)
        ok = bool(res.get("ok", False))
        if not ok:
            log_event("telegram_send_failed", chat_id=chat_id, error=res.get("description") or res)
        return ok
    except Exception as e:
        log_event("telegram_send_exception", chat_id=chat_id, error=str(e))
        return False


def telegram_send_photo(
    bot_token: str,
    chat_id: int,
    photo: str,
    caption: str = "",
    reply_markup: Optional[Dict[str, Any]] = None,
    parse_mode: Optional[str] = None,
) -> bool:
    payload: Dict[str, Any] = {"chat_id": chat_id, "photo": photo}
    if caption:
        payload["caption"] = caption
    if reply_markup:
        payload["reply_markup"] = reply_markup
    if parse_mode:
        payload["parse_mode"] = parse_mode

    try:
        res = telegram_api_call(bot_token, "sendPhoto", payload)
        ok = bool(res.get("ok", False))
        if not ok:
            log_event("telegram_send_photo_failed", chat_id=chat_id, error=res.get("description") or res)
        return ok
    except Exception as e:
        log_event("telegram_send_photo_exception", chat_id=chat_id, error=str(e))
        return False


def telegram_send_document(
    bot_token: str,
    chat_id: int,
    document: str,
    caption: str = "",
    reply_markup: Optional[Dict[str, Any]] = None,
    parse_mode: Optional[str] = None,
) -> bool:
    payload: Dict[str, Any] = {"chat_id": chat_id, "document": document}
    if caption:
        payload["caption"] = caption
    if reply_markup:
        payload["reply_markup"] = reply_markup
    if parse_mode:
        payload["parse_mode"] = parse_mode

    try:
        res = telegram_api_call(bot_token, "sendDocument", payload)
        ok = bool(res.get("ok", False))
        if not ok:
            log_event("telegram_send_document_failed", chat_id=chat_id, error=res.get("description") or res)
        return ok
    except Exception as e:
        log_event("telegram_send_document_exception", chat_id=chat_id, error=str(e))
        return False


def telegram_answer_callback(bot_token: str, callback_query_id: str, text: str = "OK") -> None:
    try:
        res = telegram_api_call(
            bot_token,
            "answerCallbackQuery",
            {"callback_query_id": callback_query_id, "text": text},
        )
        if not res.get("ok", True):
            log_event("telegram_ack_failed", error=res.get("description") or res)
    except Exception as e:
        log_event("telegram_ack_exception", error=str(e))


def reply_kb(button_rows: List[List[str]], resize: bool = True, one_time: bool = False) -> Dict[str, Any]:
    keyboard = [[{"text": txt} for txt in row] for row in button_rows]
    return {
        "keyboard": keyboard,
        "resize_keyboard": bool(resize),
        "one_time_keyboard": bool(one_time),
        "selective": False,
    }


def _multipart_encode(
    fields: Dict[str, str],
    file_field: str,
    filename: str,
    content_type: str,
    file_bytes: bytes,
) -> Tuple[bytes, str]:
    boundary = f"----tgBoundary{int(time.time() * 1000)}"
    parts: List[bytes] = []

    for k, v in fields.items():
        parts.append(f"--{boundary}\r\n".encode("utf-8"))
        parts.append(f'Content-Disposition: form-data; name="{k}"\r\n\r\n'.encode("utf-8"))
        parts.append((v or "").encode("utf-8"))
        parts.append(b"\r\n")

    parts.append(f"--{boundary}\r\n".encode("utf-8"))
    parts.append(f'Content-Disposition: form-data; name="{file_field}"; filename="{filename}"\r\n'.encode("utf-8"))
    parts.append(f"Content-Type: {content_type}\r\n\r\n".encode("utf-8"))
    parts.append(file_bytes)
    parts.append(b"\r\n")
    parts.append(f"--{boundary}--\r\n".encode("utf-8"))

    body = b"".join(parts)
    ctype = f"multipart/form-data; boundary={boundary}"
    return body, ctype


def telegram_get_file_path(bot_token: str, file_id: str) -> str:
    res = telegram_api_call(bot_token, "getFile", {"file_id": file_id})
    if not res.get("ok"):
        raise RuntimeError(f"getFile failed: {res}")
    return res["result"]["file_path"]


def telegram_download_file_bytes(bot_token: str, file_path: str) -> bytes:
    url = f"{TELEGRAM_API_BASE}/file/bot{bot_token}/{file_path}"
    with urllib.request.urlopen(url, timeout=30) as resp:
        return resp.read()


def telegram_send_file_bytes(
    bot_token: str,
    method: str,
    chat_id: int,
    file_field: str,
    filename: str,
    content_type: str,
    file_bytes: bytes,
    caption: str = "",
) -> bool:
    url = f"{TELEGRAM_API_BASE}/bot{bot_token}/{method}"
    fields = {"chat_id": str(chat_id)}
    if caption:
        fields["caption"] = caption

    body, ctype = _multipart_encode(fields, file_field, filename, content_type, file_bytes)
    req = urllib.request.Request(url, data=body, headers={"Content-Type": ctype}, method="POST")

    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            raw = resp.read().decode("utf-8")
            data = json.loads(raw)
            ok = bool(data.get("ok", False))
            if not ok:
                log_event("telegram_send_file_bytes_failed", error=data.get("description") or data)
            return ok
    except Exception as e:
        log_event("telegram_send_file_bytes_exception", error=str(e))
        return False


def telegram_send_alert(bot_token: str, chat_id: int, text: str) -> bool:
    try:
        alert_text = f"🚨 ALERTA SISTEMA\n\n{text}"
        return telegram_send_text(bot_token, chat_id, alert_text)
    except Exception as e:
        log_event("telegram_alert_exception", error=str(e))
        return False
