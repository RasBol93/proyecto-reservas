# app/alerts.py

import time
from typing import Any, Dict, Optional

from app.config import get_alert_config
from app.telegram_api import telegram_send_alert
from app.utils import log_event


# =========================================================
# CACHE / RATE LIMIT ANTI-SPAM
# =========================================================

_ALERT_LAST_SENT_AT: Dict[str, float] = {}

# cooldown por evento-clave
ALERT_COOLDOWN_SECONDS = 60

# housekeeping simple para evitar crecimiento infinito del cache
_ALERT_CACHE_CLEANUP_EVERY = 200
_ALERT_CACHE_STALE_AFTER_SECONDS = max(ALERT_COOLDOWN_SECONDS * 10, 3600)
_alert_cache_ops_count = 0


def _now_ts() -> float:
    return time.time()


def _safe_str(v: Any) -> str:
    if v is None:
        return ""
    return str(v).strip()


def _compact_error(error: Any, max_len: int = 500) -> str:
    s = _safe_str(error)
    if len(s) <= max_len:
        return s
    return s[:max_len] + "..."


def _build_alert_key(event: str, tenant_id: str = "", order_id: str = "", extra_key: str = "") -> str:
    return "|".join([
        _safe_str(event),
        _safe_str(tenant_id),
        _safe_str(order_id),
        _safe_str(extra_key),
    ])


def _cleanup_alert_cache_if_needed() -> None:
    global _alert_cache_ops_count

    _alert_cache_ops_count += 1
    if _alert_cache_ops_count % _ALERT_CACHE_CLEANUP_EVERY != 0:
        return

    now = _now_ts()
    stale_before = now - _ALERT_CACHE_STALE_AFTER_SECONDS

    stale_keys = [
        key for key, ts in _ALERT_LAST_SENT_AT.items()
        if ts < stale_before
    ]
    for key in stale_keys:
        _ALERT_LAST_SENT_AT.pop(key, None)


def _should_send_alert(key: str, cooldown_seconds: int = ALERT_COOLDOWN_SECONDS) -> bool:
    _cleanup_alert_cache_if_needed()

    now = _now_ts()
    last = _ALERT_LAST_SENT_AT.get(key)

    if last is not None and (now - last) < cooldown_seconds:
        return False

    _ALERT_LAST_SENT_AT[key] = now
    return True


def alerts_cache_info() -> Dict[str, Any]:
    return {
        "tracked_keys": len(_ALERT_LAST_SENT_AT),
        "cooldown_seconds": ALERT_COOLDOWN_SECONDS,
        "stale_after_seconds": _ALERT_CACHE_STALE_AFTER_SECONDS,
        "cleanup_every_ops": _ALERT_CACHE_CLEANUP_EVERY,
        "keys": list(_ALERT_LAST_SENT_AT.keys())[:100],
    }


def reset_alerts_cache() -> Dict[str, Any]:
    global _alert_cache_ops_count
    _ALERT_LAST_SENT_AT.clear()
    _alert_cache_ops_count = 0
    return {"ok": True, "cleared": True}


# =========================================================
# FORMATTERS
# =========================================================

def _format_extra_lines(**kwargs: Any) -> str:
    lines = []
    for k, v in kwargs.items():
        if v is None:
            continue
        s = _safe_str(v)
        if not s:
            continue
        lines.append(f"{k}: {s}")
    return "\n".join(lines)


def _build_alert_text(title: str, message: str = "", **kwargs: Any) -> str:
    parts = [f"🚨 {_safe_str(title)}".strip()]

    msg = _safe_str(message)
    if msg:
        parts.append(msg)

    extra = _format_extra_lines(**kwargs)
    if extra:
        parts.append(extra)

    return "\n\n".join(parts).strip()


# =========================================================
# ENVÍO CENTRAL
# =========================================================

def send_alert(
    event: str,
    title: str,
    message: str = "",
    tenant_id: str = "",
    order_id: str = "",
    extra_key: str = "",
    cooldown_seconds: int = ALERT_COOLDOWN_SECONDS,
    **kwargs: Any,
) -> Dict[str, Any]:
    """
    Punto central para enviar alertas por Telegram.
    No lanza excepción.
    """

    safe_event = _safe_str(event)
    safe_tenant_id = _safe_str(tenant_id)
    safe_order_id = _safe_str(order_id)
    safe_extra_key = _safe_str(extra_key)

    cfg = get_alert_config()
    if not cfg.get("enabled"):
        log_event(
            "alert_skipped_not_enabled",
            event_name=safe_event,
            tenant_id=safe_tenant_id,
            order_id=safe_order_id,
        )
        return {"ok": False, "reason": "alerts_not_enabled"}

    bot_token = cfg.get("bot_token")
    chat_id = cfg.get("chat_id")

    if not bot_token or not chat_id:
        log_event(
            "alert_skipped_missing_config",
            event_name=safe_event,
            tenant_id=safe_tenant_id,
            order_id=safe_order_id,
        )
        return {"ok": False, "reason": "missing_alert_config"}

    key = _build_alert_key(
        event=safe_event,
        tenant_id=safe_tenant_id,
        order_id=safe_order_id,
        extra_key=safe_extra_key,
    )

    if not _should_send_alert(key, cooldown_seconds=cooldown_seconds):
        log_event(
            "alert_suppressed_cooldown",
            event_name=safe_event,
            tenant_id=safe_tenant_id,
            order_id=safe_order_id,
            alert_key=key,
        )
        return {"ok": False, "reason": "cooldown"}

    try:
        text = _build_alert_text(
            title=title,
            message=message,
            event=safe_event,
            tenant_id=safe_tenant_id,
            order_id=safe_order_id,
            **kwargs,
        )

        sent = telegram_send_alert(
            bot_token=bot_token,
            chat_id=chat_id,
            text=text,
        )

        if sent:
            log_event(
                "alert_sent",
                event_name=safe_event,
                tenant_id=safe_tenant_id,
                order_id=safe_order_id,
                alert_key=key,
            )
            return {"ok": True, "sent": True}

        log_event(
            "alert_send_failed",
            event_name=safe_event,
            tenant_id=safe_tenant_id,
            order_id=safe_order_id,
            alert_key=key,
        )
        return {"ok": False, "reason": "telegram_send_failed"}

    except Exception as e:
        log_event(
            "alert_send_exception",
            event_name=safe_event,
            tenant_id=safe_tenant_id,
            order_id=safe_order_id,
            error_type=type(e).__name__,
            error=_compact_error(e),
        )
        return {"ok": False, "reason": "exception", "error": str(e)}


# =========================================================
# ALERTAS DE ALTO NIVEL
# =========================================================

def alert_order_failed(
    tenant_id: str,
    order_id: str = "",
    chat_id: Optional[int] = None,
    error: str = "",
) -> Dict[str, Any]:
    return send_alert(
        event="order_failed",
        title="Error creando pedido",
        message="Falló la creación o guardado de un pedido.",
        tenant_id=tenant_id,
        order_id=order_id,
        extra_key=str(chat_id or ""),
        chat_id=chat_id,
        error=_compact_error(error),
    )


def alert_order_status_failed(
    tenant_id: str,
    order_id: str,
    new_status: str,
    error: str = "",
) -> Dict[str, Any]:
    return send_alert(
        event="order_status_failed",
        title="Error actualizando estado de pedido",
        message="Falló el cambio de estado en ORDERS.",
        tenant_id=tenant_id,
        order_id=order_id,
        new_status=new_status,
        error=_compact_error(error),
    )


def alert_payment_proof_failed(
    tenant_id: str,
    order_id: str = "",
    chat_id: Optional[int] = None,
    error: str = "",
) -> Dict[str, Any]:
    return send_alert(
        event="payment_proof_failed",
        title="Error guardando comprobante",
        message="Falló la actualización del comprobante de pago.",
        tenant_id=tenant_id,
        order_id=order_id,
        extra_key=str(chat_id or ""),
        chat_id=chat_id,
        error=_compact_error(error),
    )


def alert_payment_failed(
    tenant_id: str,
    order_id: str = "",
    error: str = "",
) -> Dict[str, Any]:
    return send_alert(
        event="payment_failed",
        title="Error procesando pago",
        message="Hubo un problema en el flujo de pago o confirmación.",
        tenant_id=tenant_id,
        order_id=order_id,
        error=_compact_error(error),
    )


def alert_sheet_error(
    tenant_id: str,
    error: str = "",
    extra_key: str = "",
) -> Dict[str, Any]:
    return send_alert(
        event="sheet_error",
        title="Error Google Sheets",
        message="Falló una operación contra Google Sheets.",
        tenant_id=tenant_id,
        extra_key=extra_key,
        error=_compact_error(error),
    )


def alert_menu_error(
    tenant_id: str,
    error: str = "",
    sku: str = "",
) -> Dict[str, Any]:
    return send_alert(
        event="menu_error",
        title="Error de menú",
        message="Hubo un problema leyendo o actualizando el menú.",
        tenant_id=tenant_id,
        extra_key=sku,
        sku=sku,
        error=_compact_error(error),
    )


def alert_photo_upload_failed(
    tenant_id: str,
    sku: str = "",
    error: str = "",
) -> Dict[str, Any]:
    return send_alert(
        event="photo_upload_failed",
        title="Error subiendo foto de producto",
        message="Falló la subida o vinculación de una foto de producto.",
        tenant_id=tenant_id,
        extra_key=sku,
        sku=sku,
        error=_compact_error(error),
    )


def alert_tenant_error(
    tenant_id: str = "",
    error: str = "",
) -> Dict[str, Any]:
    return send_alert(
        event="tenant_error",
        title="Error de tenant/configuración",
        message="Hubo un problema resolviendo tenant o configuración.",
        tenant_id=tenant_id,
        error=_compact_error(error),
    )


def alert_telegram_error(
    error: str = "",
    method: str = "",
    chat_id: Any = "",
) -> Dict[str, Any]:
    return send_alert(
        event="telegram_error",
        title="Error Telegram API",
        message="Falló una operación contra Telegram.",
        extra_key=method,
        method=method,
        chat_id=chat_id,
        error=_compact_error(error),
    )


def alert_webhook_error(
    tenant_id: str = "",
    error: str = "",
    mode: str = "",
) -> Dict[str, Any]:
    return send_alert(
        event="webhook_error",
        title="Error en webhook",
        message="El webhook capturó un error no controlado.",
        tenant_id=tenant_id,
        extra_key=mode,
        mode=mode,
        error=_compact_error(error),
    )


def alert_system_error(
    error: str = "",
    module: str = "",
) -> Dict[str, Any]:
    return send_alert(
        event="system_error",
        title="Error crítico del sistema",
        message="Se detectó un error general del sistema.",
        extra_key=module,
        module=module,
        error=_compact_error(error),
    )


def send_test_alert(message: str = "Prueba manual de alertas") -> Dict[str, Any]:
    return send_alert(
        event="test_alert",
        title="Prueba de alerta",
        message=message,
        cooldown_seconds=1,
    )
