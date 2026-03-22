# app/alerts.py

import time
from collections import deque
from typing import Any, Deque, Dict, List, Optional

from app.config import get_alert_config
from app.telegram_api import telegram_send_alert
from app.utils import log_event


# =========================================================
# RUNTIME MEMORY
# =========================================================

MAX_RUNTIME_EVENTS = 200
MAX_RUNTIME_ERRORS = 200
MAX_RUNTIME_ALERTS = 100

_RUNTIME_EVENTS: Deque[Dict[str, Any]] = deque(maxlen=MAX_RUNTIME_EVENTS)
_RUNTIME_ERRORS: Deque[Dict[str, Any]] = deque(maxlen=MAX_RUNTIME_ERRORS)
_RUNTIME_ALERTS: Deque[Dict[str, Any]] = deque(maxlen=MAX_RUNTIME_ALERTS)

_LAST_ALERT_AT_BY_KEY: Dict[str, float] = {}

DEFAULT_ALERT_COOLDOWN_SECONDS = 300  # 5 min


def _now_ts() -> int:
    return int(time.time())


def _safe_text(v: Any, max_len: int = 500) -> str:
    s = str(v or "").strip()
    if len(s) > max_len:
        return s[:max_len] + "..."
    return s


def _push_event(bucket: Deque[Dict[str, Any]], item: Dict[str, Any]) -> None:
    try:
        bucket.append(item)
    except Exception:
        pass


# =========================================================
# PUBLIC RUNTIME LOGGING
# =========================================================

def record_runtime_event(
    event_type: str,
    severity: str = "info",
    tenant_id: str = "",
    module: str = "",
    action: str = "",
    order_id: str = "",
    chat_id: str = "",
    details: str = "",
    extra: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    item = {
        "ts": _now_ts(),
        "event_type": _safe_text(event_type, 120),
        "severity": _safe_text(severity, 20).lower(),
        "tenant_id": _safe_text(tenant_id, 120),
        "module": _safe_text(module, 120),
        "action": _safe_text(action, 120),
        "order_id": _safe_text(order_id, 120),
        "chat_id": _safe_text(chat_id, 120),
        "details": _safe_text(details, 500),
        "extra": extra or {},
    }

    _push_event(_RUNTIME_EVENTS, item)

    if item["severity"] in ("error", "critical"):
        _push_event(_RUNTIME_ERRORS, item)

    try:
        log_event(
            "runtime_event",
            severity=item["severity"],
            tenant_id=item["tenant_id"],
            module=item["module"],
            action=item["action"],
            order_id=item["order_id"],
            chat_id=item["chat_id"],
            details=item["details"],
        )
    except Exception:
        pass

    return item


def get_runtime_snapshot(limit: int = 20) -> Dict[str, Any]:
    limit = max(1, min(int(limit or 20), 100))

    events = list(_RUNTIME_EVENTS)[-limit:]
    errors = list(_RUNTIME_ERRORS)[-limit:]
    alerts = list(_RUNTIME_ALERTS)[-limit:]

    now = _now_ts()
    errors_last_15m = [x for x in _RUNTIME_ERRORS if (now - int(x.get("ts") or 0)) <= 900]
    alerts_last_15m = [x for x in _RUNTIME_ALERTS if (now - int(x.get("ts") or 0)) <= 900]

    return {
        "ok": True,
        "now_ts": now,
        "totals": {
            "events_buffered": len(_RUNTIME_EVENTS),
            "errors_buffered": len(_RUNTIME_ERRORS),
            "alerts_buffered": len(_RUNTIME_ALERTS),
            "errors_last_15m": len(errors_last_15m),
            "alerts_last_15m": len(alerts_last_15m),
        },
        "recent_events": events,
        "recent_errors": errors,
        "recent_alerts": alerts,
    }


# =========================================================
# ALERTING
# =========================================================

def _build_alert_key(
    code: str,
    tenant_id: str = "",
    module: str = "",
    order_id: str = "",
) -> str:
    return "|".join([
        _safe_text(code, 120),
        _safe_text(tenant_id, 120),
        _safe_text(module, 120),
        _safe_text(order_id, 120),
    ])


def _should_send_alert(alert_key: str, cooldown_seconds: int) -> bool:
    now = time.time()
    last = float(_LAST_ALERT_AT_BY_KEY.get(alert_key) or 0)
    if last and (now - last) < max(1, cooldown_seconds):
        return False
    _LAST_ALERT_AT_BY_KEY[alert_key] = now
    return True


def send_system_alert(
    code: str,
    message: str,
    tenant_id: str = "",
    module: str = "",
    order_id: str = "",
    chat_id: str = "",
    severity: str = "critical",
    cooldown_seconds: int = DEFAULT_ALERT_COOLDOWN_SECONDS,
    extra: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    cfg = get_alert_config()

    runtime_item = record_runtime_event(
        event_type=code,
        severity=severity,
        tenant_id=tenant_id,
        module=module,
        action="alert_triggered",
        order_id=order_id,
        chat_id=chat_id,
        details=message,
        extra=extra or {},
    )

    alert_key = _build_alert_key(code=code, tenant_id=tenant_id, module=module, order_id=order_id)

    if not cfg.get("enabled"):
        result = {
            "ok": False,
            "sent": False,
            "reason": "alerts_not_configured",
            "runtime_event": runtime_item,
        }
        _push_event(_RUNTIME_ALERTS, {
            "ts": _now_ts(),
            "code": code,
            "tenant_id": tenant_id,
            "module": module,
            "order_id": order_id,
            "chat_id": chat_id,
            "message": _safe_text(message, 500),
            "sent": False,
            "reason": "alerts_not_configured",
        })
        return result

    if not _should_send_alert(alert_key, cooldown_seconds):
        result = {
            "ok": True,
            "sent": False,
            "reason": "cooldown_active",
            "runtime_event": runtime_item,
        }
        _push_event(_RUNTIME_ALERTS, {
            "ts": _now_ts(),
            "code": code,
            "tenant_id": tenant_id,
            "module": module,
            "order_id": order_id,
            "chat_id": chat_id,
            "message": _safe_text(message, 500),
            "sent": False,
            "reason": "cooldown_active",
        })
        return result

    bot_token = cfg.get("bot_token") or ""
    alert_chat_id = cfg.get("chat_id")

    text_lines: List[str] = [
        f"Código: {code}",
        f"Módulo: {module or '-'}",
        f"Tenant: {tenant_id or '-'}",
        f"Order: {order_id or '-'}",
        f"Chat: {chat_id or '-'}",
        "",
        _safe_text(message, 1500),
    ]
    text = "\n".join(text_lines)

    sent = False
    try:
        sent = telegram_send_alert(bot_token=bot_token, chat_id=int(alert_chat_id), text=text)
    except Exception as e:
        try:
            log_event("system_alert_send_exception", code=code, module=module, tenant_id=tenant_id, error=str(e))
        except Exception:
            pass
        sent = False

    alert_item = {
        "ts": _now_ts(),
        "code": code,
        "tenant_id": tenant_id,
        "module": module,
        "order_id": order_id,
        "chat_id": chat_id,
        "message": _safe_text(message, 500),
        "sent": bool(sent),
        "reason": "sent" if sent else "send_failed",
    }
    _push_event(_RUNTIME_ALERTS, alert_item)

    try:
        log_event(
            "system_alert_sent" if sent else "system_alert_failed",
            code=code,
            module=module,
            tenant_id=tenant_id,
            order_id=order_id,
            chat_id=chat_id,
        )
    except Exception:
        pass

    return {
        "ok": bool(sent),
        "sent": bool(sent),
        "reason": "sent" if sent else "send_failed",
        "runtime_event": runtime_item,
    }
