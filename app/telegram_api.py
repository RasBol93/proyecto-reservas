# app/telegram_api.py

import json
import time
import urllib.request
import urllib.error
from typing import Any, Dict, Optional, List, Tuple

from app.config import TELEGRAM_API_BASE
from app.utils import log_event


# -------------------------
# Retry / timeouts
# -------------------------

_TELEGRAM_API_TIMEOUT_SECONDS = 20
_TELEGRAM_FILE_TIMEOUT_SECONDS = 30
_TELEGRAM_RETRY_ATTEMPTS = 3
_TELEGRAM_RETRY_SLEEP_SECONDS = 0.35


def _sleep_before_retry(attempt_index: int) -> None:
    try:
        time.sleep(_TELEGRAM_RETRY_SLEEP_SECONDS * max(1, attempt_index))
    except Exception:
        pass


def _extract_http_status_from_exception(exc: Exception) -> Optional[int]:
    try:
        if isinstance(exc, urllib.error.HTTPError):
            return int(exc.code)
    except Exception:
        pass
    return None


def _should_retry_exception(exc: Exception) -> bool:
    status = _extract_http_status_from_exception(exc)
    if status in (429, 500, 502, 503, 504):
        return True

    msg = str(exc or "").lower()
    retry_signals = (
        "timed out",
        "timeout",
        "temporarily unavailable",
        "connection reset",
        "connection aborted",
        "connection refused",
        "remote end closed connection",
        "service unavailable",
        "bad gateway",
        "gateway timeout",
        "rate limit",
        "429",
        "500",
        "502",
        "503",
        "504",
    )
    return any(signal in msg for signal in retry_signals)


def _http_error_body(exc: Exception) -> str:
    try:
        if isinstance(exc, urllib.error.HTTPError) and exc.fp:
            raw = exc.read().decode("utf-8", errors="replace")
            return raw[:1000]
    except Exception:
        pass
    return ""


def _call_with_retry(fn, *, op_name: str, log_fields: Optional[Dict[str, Any]] = None):
    last_exc: Exception | None = None
    extra = dict(log_fields or {})

    for attempt in range(1, _TELEGRAM_RETRY_ATTEMPTS + 1):
        try:
            return fn()
        except Exception as e:
            last_exc = e
            try:
                log_event(
                    "telegram_retryable_error",
                    op_name=op_name,
                    attempt=attempt,
                    max_attempts=_TELEGRAM_RETRY_ATTEMPTS,
                    retry=bool(attempt < _TELEGRAM_RETRY_ATTEMPTS and _should_retry_exception(e)),
                    http_status=_extract_http_status_from_exception(e),
                    error_type=type(e).__name__,
                    error=str(e),
                    error_body=_http_error_body(e),
                    **extra,
                )
            except Exception:
                pass

            if attempt >= _TELEGRAM_RETRY_ATTEMPTS or not _should_retry_exception(e):
                break

            _sleep_before_retry(attempt)

    if last_exc is not None:
        raise last_exc

    raise RuntimeError(f"{op_name} failed without exception")


# -------------------------
# Core API call
# -------------------------

def telegram_api_call(bot_token: str, method: str, payload: Dict[str, Any]) -> Dict[str, Any]:
    if not bot_token:
        raise RuntimeError("bot_token missing")

    clean_method = str(method or "").strip()
    if not clean_method:
        raise RuntimeError("method missing")

    safe_payload = payload if isinstance(payload, dict) else {}

    url = f"{TELEGRAM_API_BASE}/bot{bot_token}/{clean_method}"
    data = json.dumps(safe_payload).encode("utf-8")

    req = urllib.request.Request(
        url,
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )

    def _do_request():
        with urllib.request.urlopen(req, timeout=_TELEGRAM_API_TIMEOUT_SECONDS) as resp:
            raw = resp.read().decode("utf-8")
            parsed = json.loads(raw)
            if not isinstance(parsed, dict):
                raise RuntimeError(f"Invalid Telegram response shape for method={clean_method}")
            return parsed

    return _call_with_retry(
        _do_request,
        op_name=f"telegram_api_call.{clean_method}",
        log_fields={"method": clean_method},
    )


# -------------------------
# Send helpers
# -------------------------

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
        log_event(
            "telegram_send_exception",
            chat_id=chat_id,
            error_type=type(e).__name__,
            error=str(e),
            error_body=_http_error_body(e),
        )
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
        log_event(
            "telegram_send_photo_exception",
            chat_id=chat_id,
            error_type=type(e).__name__,
            error=str(e),
            error_body=_http_error_body(e),
        )
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
        log_event(
            "telegram_send_document_exception",
            chat_id=chat_id,
            error_type=type(e).__name__,
            error=str(e),
            error_body=_http_error_body(e),
        )
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
        log_event(
            "telegram_ack_exception",
            error_type=type(e).__name__,
            error=str(e),
            error_body=_http_error_body(e),
        )


# -------------------------
# Keyboard helpers
# -------------------------

def reply_kb(button_rows: List[List[str]], resize: bool = True, one_time: bool = False) -> Dict[str, Any]:
    keyboard = [[{"text": txt} for txt in row] for row in button_rows]
    return {
        "keyboard": keyboard,
        "resize_keyboard": bool(resize),
        "one_time_keyboard": bool(one_time),
        "selective": False,
    }


# -------------------------
# Multipart helpers
# -------------------------

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


# -------------------------
# File helpers
# -------------------------

def telegram_get_file_path(bot_token: str, file_id: str) -> str:
    clean_file_id = str(file_id or "").strip()
    if not clean_file_id:
        raise RuntimeError("file_id missing")

    res = telegram_api_call(bot_token, "getFile", {"file_id": clean_file_id})
    if not res.get("ok"):
        raise RuntimeError(f"getFile failed: {res}")

    result = res.get("result") or {}
    file_path = str(result.get("file_path") or "").strip()
    if not file_path:
        raise RuntimeError("getFile succeeded but file_path missing")

    return file_path


def telegram_download_file_bytes(bot_token: str, file_path: str) -> bytes:
    clean_file_path = str(file_path or "").strip()
    if not clean_file_path:
        raise RuntimeError("file_path missing")

    url = f"{TELEGRAM_API_BASE}/file/bot{bot_token}/{clean_file_path}"

    def _download():
        with urllib.request.urlopen(url, timeout=_TELEGRAM_FILE_TIMEOUT_SECONDS) as resp:
            return resp.read()

    file_bytes = _call_with_retry(
        _download,
        op_name="telegram_download_file_bytes",
        log_fields={"file_path": clean_file_path},
    )

    if not isinstance(file_bytes, (bytes, bytearray)) or len(file_bytes) == 0:
        raise RuntimeError("Downloaded file is empty")

    return bytes(file_bytes)


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
    clean_method = str(method or "").strip()
    clean_file_field = str(file_field or "").strip()
    clean_filename = str(filename or "").strip() or "file"
    clean_content_type = str(content_type or "").strip() or "application/octet-stream"

    if not bot_token:
        log_event("telegram_send_file_bytes_missing_bot_token", chat_id=chat_id)
        return False
    if not clean_method:
        log_event("telegram_send_file_bytes_missing_method", chat_id=chat_id)
        return False
    if not clean_file_field:
        log_event("telegram_send_file_bytes_missing_file_field", chat_id=chat_id, method=clean_method)
        return False
    if not isinstance(file_bytes, (bytes, bytearray)) or len(file_bytes) == 0:
        log_event("telegram_send_file_bytes_missing_bytes", chat_id=chat_id, method=clean_method)
        return False

    url = f"{TELEGRAM_API_BASE}/bot{bot_token}/{clean_method}"
    fields = {"chat_id": str(chat_id)}
    if caption:
        fields["caption"] = caption

    body, ctype = _multipart_encode(fields, clean_file_field, clean_filename, clean_content_type, bytes(file_bytes))
    req = urllib.request.Request(url, data=body, headers={"Content-Type": ctype}, method="POST")

    def _send():
        with urllib.request.urlopen(req, timeout=_TELEGRAM_FILE_TIMEOUT_SECONDS) as resp:
            raw = resp.read().decode("utf-8")
            data = json.loads(raw)
            if not isinstance(data, dict):
                raise RuntimeError("Invalid Telegram multipart response shape")
            return data

    try:
        data = _call_with_retry(
            _send,
            op_name=f"telegram_send_file_bytes.{clean_method}",
            log_fields={"chat_id": chat_id, "method": clean_method},
        )
        ok = bool(data.get("ok", False))
        if not ok:
            log_event("telegram_send_file_bytes_failed", error=data.get("description") or data)
        return ok
    except Exception as e:
        log_event(
            "telegram_send_file_bytes_exception",
            error_type=type(e).__name__,
            error=str(e),
            error_body=_http_error_body(e),
        )
        return False


# -------------------------
# Alerts
# -------------------------

def telegram_send_alert(bot_token: str, chat_id: int, text: str) -> bool:
    try:
        alert_text = f"🚨 ALERTA SISTEMA\n\n{text}"
        return telegram_send_text(bot_token, chat_id, alert_text)
    except Exception as e:
        log_event(
            "telegram_alert_exception",
            error_type=type(e).__name__,
            error=str(e),
            error_body=_http_error_body(e),
        )
        return False
