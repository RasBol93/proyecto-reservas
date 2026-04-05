# app/admin_messages.py — admin por texto "panel", pedido manual mejorado, QR de pagos,
# comprobante manual y encuesta runtime

from typing import Any, Dict, Optional

from app.admin_messages_menu import handle_admin_menu_message
from app.admin_messages_surveys import handle_admin_surveys_message
from app.admin_messages_orders import handle_admin_orders_message

from app.telegram_api import telegram_send_text
from app.telegram_keyboard import kb
from app.utils import normalize, log_event
from app.stats import build_periods
from app.webhook_helpers import (
    get_sess,
    assert_admin_authorized,
    admin_periods_inline_kb,
    admin_fixed_kb,
)
from app.admin_hours import send_admin_hours_menu
from app.admin_menu import (
    send_admin_menu_home,
)
from app.alerts import (
    alert_system_error,
)
from app.admin_consumers import _send_consumers_menu
from app.admin_manual_order import (
    _admin_order_reset,
    _send_admin_order_home,
)
from app.admin_nav import (
    admin_panel_kb,
)
from app.tenants import update_tenant_payment_qr
from app.image_storage import upload_product_photo_for_tenant


def _is_owner_bot(tenant: Dict[str, Any]) -> bool:
    return bool(tenant.get("_is_owner_bot"))


def _safe_send_text(
    bot_token: str,
    chat_id: int,
    text: str,
    reply_markup: Optional[Dict[str, Any]] = None,
    parse_mode: Optional[str] = None,
) -> bool:
    try:
        return telegram_send_text(
            bot_token,
            chat_id,
            text,
            reply_markup=reply_markup,
            parse_mode=parse_mode,
        )
    except Exception:
        return False


def _handle_admin_payment_qr_upload(
    tenant: Dict[str, Any],
    tenant_id: str,
    bot_token: str,
    chat_id: int,
    msg: Dict[str, Any],
    tmp: Dict[str, Any],
) -> Dict[str, Any]:
    if not msg.get("photo"):
        _safe_send_text(
            bot_token,
            chat_id,
            "📷 Estoy esperando una imagen del QR.",
            reply_markup=admin_fixed_kb(),
        )
        return {"ok": True}

    file_id = ""
    try:
        file_id = str(msg["photo"][-1].get("file_id") or "").strip()
    except Exception:
        file_id = ""

    if not file_id:
        _safe_send_text(
            bot_token,
            chat_id,
            "⚠️ No pude leer la imagen del QR. Intenta nuevamente.",
            reply_markup=admin_fixed_kb(),
        )
        return {"ok": True}

    try:
        from app.telegram_api import telegram_get_file_path, telegram_download_file_bytes

        file_path = telegram_get_file_path(bot_token, file_id)
        file_bytes = telegram_download_file_bytes(bot_token, file_path)

        content_type = "image/jpeg"
        low_path = str(file_path or "").lower()
        if low_path.endswith(".png"):
            content_type = "image/png"
        elif low_path.endswith(".webp"):
            content_type = "image/webp"

        qr_url = upload_product_photo_for_tenant(
            tenant=tenant,
            tenant_id=tenant_id,
            sku="payment_qr",
            file_bytes=file_bytes,
            mime_type=content_type,
        )

    except Exception as e:
        log_event(
            "admin_payment_qr_upload_failed",
            tenant_id=tenant_id,
            error_type=type(e).__name__,
            error=str(e),
        )
        _safe_send_text(
            bot_token,
            chat_id,
            "⚠️ Error procesando el QR. Intenta nuevamente.",
            reply_markup=admin_fixed_kb(),
        )
        return {"ok": True}

    ok = update_tenant_payment_qr(
        tenant_id=tenant_id,
        qr_url=qr_url,
    )

    tmp.pop("admin_payment_mode", None)

    if ok:
        _safe_send_text(
            bot_token,
            chat_id,
            "✅ QR actualizado correctamente.",
            reply_markup=admin_fixed_kb(),
        )
    else:
        _safe_send_text(
            bot_token,
            chat_id,
            "⚠️ Error guardando el QR.",
            reply_markup=admin_fixed_kb(),
        )

    return {"ok": True}


def handle_admin_message_impl(
    tenant: Dict[str, Any],
    tenant_id: str,
    bot_token: str,
    chat_id: int,
    msg: Dict[str, Any],
    orders_sh,
    tenant_tz: str,
) -> Dict[str, Any]:
    try:
        is_owner = _is_owner_bot(tenant)

        text = (msg.get("text") or "").strip()
        txt_norm = normalize(text)
        sess = get_sess(tenant_id, chat_id)
        tmp = sess.setdefault("tmp", {})

        if txt_norm in ("panel", "⚙️panel", "⚙️ panel"):
            assert_admin_authorized(tenant, chat_id, tenant_id)
            _safe_send_text(
                bot_token,
                chat_id,
                "🧭 PANEL ADMIN\n\nElige una opción:",
                reply_markup=admin_panel_kb("owner" if is_owner else "admin"),
            )
            return {"ok": True}

        admin_payment_mode = str(tmp.get("admin_payment_mode") or "").strip()

        if admin_payment_mode == "awaiting_qr":
            assert_admin_authorized(tenant, chat_id, tenant_id)
            return _handle_admin_payment_qr_upload(
                tenant=tenant,
                tenant_id=tenant_id,
                bot_token=bot_token,
                chat_id=chat_id,
                msg=msg,
                tmp=tmp,
            )

        surveys_result = handle_admin_surveys_message(
            tenant=tenant,
            tenant_id=tenant_id,
            bot_token=bot_token,
            chat_id=chat_id,
            text=text,
            orders_sh=orders_sh,
            tenant_tz=tenant_tz,
            tmp=tmp,
        )
        if surveys_result is not None:
            return surveys_result

        orders_result = handle_admin_orders_message(
            tenant=tenant,
            tenant_id=tenant_id,
            bot_token=bot_token,
            chat_id=chat_id,
            msg=msg,
            orders_sh=orders_sh,
            tmp=tmp,
            is_owner=is_owner,
        )
        if orders_result is not None:
            return orders_result

        menu_result = handle_admin_menu_message(
            tenant=tenant,
            tenant_id=tenant_id,
            bot_token=bot_token,
            chat_id=chat_id,
            msg=msg,
            orders_sh=orders_sh,
            sess=sess,
        )
        if menu_result is not None:
            return menu_result

        if txt_norm in ("estadisticas", "/stats", "stats"):
            assert_admin_authorized(tenant, chat_id, tenant_id)
            periods = build_periods(tenant_tz)
            _safe_send_text(
                bot_token,
                chat_id,
                "📊 Elige el período:",
                reply_markup=admin_periods_inline_kb(tenant_id, periods),
            )
            _safe_send_text(
                bot_token,
                chat_id,
                "Usa el botón inferior ⚙️ Panel cuando quieras volver.",
                reply_markup=admin_fixed_kb(),
            )
            return {"ok": True}

        if (
            txt_norm in ("crear pedido", "crear pedido manual", "pedido manual", "nuevo pedido")
            or "crear pedido" in txt_norm
        ):
            assert_admin_authorized(tenant, chat_id, tenant_id)

            if is_owner:
                _safe_send_text(
                    bot_token,
                    chat_id,
                    "🚫 Como propietario no puedes crear pedidos.",
                )
                return {"ok": True}

            _admin_order_reset(tmp)
            tmp["admin_order_cart"] = []
            return {"ok": _send_admin_order_home(bot_token, chat_id, tenant_id, orders_sh, sess)}

        if txt_norm in (
            "base de consumidores",
            "consumidores",
            "clientes",
            "base consumidores",
        ):
            assert_admin_authorized(tenant, chat_id, tenant_id)
            return {"ok": _send_consumers_menu(bot_token, chat_id, tenant_id)}

        if txt_norm in (
            "config dias y horarios",
            "dias y horarios",
            "configuracion dias y horarios",
            "configuracion de dias y horarios",
        ):
            assert_admin_authorized(tenant, chat_id, tenant_id)
            return {"ok": send_admin_hours_menu(bot_token, chat_id, tenant_id, orders_sh, tenant_tz)}

        if txt_norm in (
            "config menu y precios",
            "menu y precios",
            "configuracion menu y precios",
            "configuracion de menu y precios",
        ):
            assert_admin_authorized(tenant, chat_id, tenant_id)
            return {"ok": send_admin_menu_home(bot_token, chat_id, tenant_id, orders_sh, sess)}

        if txt_norm in (
            "encuestas",
            "encuesta",
            "config encuestas",
            "configuracion encuestas",
            "configuracion de encuestas",
        ):
            assert_admin_authorized(tenant, chat_id, tenant_id)
            _safe_send_text(
                bot_token,
                chat_id,
                "📝 ENCUESTAS\n\n¿Qué deseas hacer?",
                reply_markup=kb([
                    [("⚙️ Configuración", f"admsurv|{tenant_id}|config")],
                    [("❓ Gestionar preguntas", f"admsurv|{tenant_id}|questions")],
                    [("📊 Ver resultados", f"admsurv|{tenant_id}|analytics")],
                    [("🧭 Panel admin", "admin_panel")],
                ]),
            )
            return {"ok": True}

        if txt_norm in ("start", "/start", "hola"):
            if is_owner:
                _safe_send_text(
                    bot_token,
                    chat_id,
                    "Bot propietario listo ✅\n\nUsa el botón inferior ⚙️ Panel.",
                    reply_markup=admin_fixed_kb(),
                )
            else:
                _safe_send_text(
                    bot_token,
                    chat_id,
                    "Admin bot listo ✅\n\nUsa el botón inferior ⚙️ Panel.",
                    reply_markup=admin_fixed_kb(),
                )
            return {"ok": True}

        if is_owner:
            _safe_send_text(
                bot_token,
                chat_id,
                "OK propietario ✅\n\nUsa el botón inferior ⚙️ Panel.",
                reply_markup=admin_fixed_kb(),
            )
        else:
            _safe_send_text(
                bot_token,
                chat_id,
                "OK admin ✅\n\nUsa el botón inferior ⚙️ Panel.",
                reply_markup=admin_fixed_kb(),
            )

        return {"ok": True}

    except Exception as e:
        log_event(
            "admin_message_error",
            tenant_id=tenant_id,
            chat_id=chat_id,
            error_type=type(e).__name__,
            error=str(e),
        )
        alert_system_error(error=str(e), module="admin_message")
        _safe_send_text(
            bot_token,
            chat_id,
            "⚠️ Ocurrió un error en el panel admin.",
            reply_markup=admin_fixed_kb(),
        )
        return {"ok": True}
