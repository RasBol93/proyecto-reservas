# app/admin_callbacks_orders.py

from typing import Any, Dict, Optional
import time

from fastapi import HTTPException

from app.orders import (
    get_order_by_id,
    get_order_context_by_id,
    update_order_status,
)
from app.menu import get_menu_product_or_404
from app.telegram_api import telegram_send_text
from app.telegram_keyboard import kb
from app.utils import log_event
from app.webhook_helpers import (
    get_sess,
    get_client_bot_token,
    assert_admin_authorized,
    fmt_snapshot_lines,
    build_order_recap_text,
    parse_items_field,
    admin_fixed_kb,
)
from app.alerts import (
    alert_order_status_failed,
)
from app.admin_helpers import (
    _safe_str,
    _safe_client_chat_id_from_order,
    _extract_slot_hhmm,
)
from app.admin_manual_order import (
    _admin_order_reset,
    _admin_order_get_active_categories,
    _send_admin_order_home,
    _send_admin_order_category,
    _send_admin_order_product_qty,
    _admin_order_add_to_cart,
    _admin_order_inc_item,
    _admin_order_dec_item,
    _admin_order_remove_item,
    _send_admin_order_cart,
    _admin_order_time_choice_kb,
)
from app.admin_nav import admin_panel_kb
from app.admin_survey_runtime import (
    clear_admin_survey_runtime,
    send_admin_survey_runtime_question,
    finalize_admin_survey_runtime,
)
from app.admin_order_runtime import (
    finalize_admin_manual_order_from_tmp,
)
from app.survey import (
    get_survey_reward_text,
    get_runtime_survey_questions,
)


# -------------------------------------------------
# Soft lock en memoria para evitar doble confirmación
# dentro del mismo proceso / worker.
# No cambia contratos y reduce race conditions locales.
# -------------------------------------------------

_PAID_LOCKS: Dict[str, float] = {}
_PAID_LOCK_TTL_SECONDS = 30


def _cleanup_paid_locks() -> None:
    now = time.time()
    stale = [order_id for order_id, ts in _PAID_LOCKS.items() if (now - ts) > _PAID_LOCK_TTL_SECONDS]
    for order_id in stale:
        _PAID_LOCKS.pop(order_id, None)


def _acquire_paid_lock(order_id: str) -> bool:
    _cleanup_paid_locks()
    clean_order_id = str(order_id or "").strip()
    if not clean_order_id:
        return False
    if clean_order_id in _PAID_LOCKS:
        return False
    _PAID_LOCKS[clean_order_id] = time.time()
    return True


def _release_paid_lock(order_id: str) -> None:
    _PAID_LOCKS.pop(str(order_id or "").strip(), None)


def _safe_send_text(
    bot_token: str,
    chat_id: int,
    text: str,
    parse_mode: Optional[str] = None,
    reply_markup: Optional[Dict[str, Any]] = None,
) -> bool:
    try:
        return telegram_send_text(
            bot_token,
            chat_id,
            text,
            parse_mode=parse_mode,
            reply_markup=reply_markup,
        )
    except Exception:
        return False


def _build_paid_recap_from_order(order_id: str, order_after: Dict[str, Any]) -> Dict[str, Any]:
    customer_name = _safe_str(order_after.get("customer_name"))
    customer_contact = _safe_str(order_after.get("customer_contact"))
    requested_time = _safe_str(order_after.get("requested_time"))

    items_snapshot = parse_items_field(order_after.get("items_snapshot"))
    detail_lines, total_amount, total_qty = fmt_snapshot_lines(items_snapshot)

    recap = build_order_recap_text(
        order_id=order_id,
        customer_name=customer_name,
        customer_contact=customer_contact,
        requested_time=requested_time,
        detail_lines=detail_lines,
        total_qty=total_qty,
        total=total_amount,
    )

    return {
        "customer_name": customer_name,
        "customer_contact": customer_contact,
        "requested_time": requested_time,
        "detail_lines": detail_lines,
        "total_amount": total_amount,
        "total_qty": total_qty,
        "recap": recap,
    }


def _notify_client_order_paid(
    tenant: Dict[str, Any],
    tenant_id: str,
    order_id: str,
    order_after: Dict[str, Any],
) -> None:
    client_token = get_client_bot_token(tenant)
    client_chat = _safe_client_chat_id_from_order(order_after)

    if not client_token or not client_chat:
        log_event(
            "notify_client_paid_skipped",
            tenant_id=tenant_id,
            order_id=order_id,
            reason="missing_client_token_or_chat_id",
        )
        return

    try:
        final_slot_for_msg = _safe_str(_extract_slot_hhmm(order_after.get("requested_time")))

        if final_slot_for_msg:
            msg_client = (
                "🟢 *PAGO CONFIRMADO*\n\n"
                f"📦 Pedido: *{order_id}*\n"
                f"⏰ Hora de recojo: *{final_slot_for_msg}*\n\n"
                "Gracias por tu compra 🙌"
            )
        else:
            msg_client = (
                "🟢 *PAGO CONFIRMADO*\n\n"
                f"📦 Pedido: *{order_id}*\n\n"
                "Gracias por tu compra 🙌"
            )

        _safe_send_text(
            client_token,
            int(client_chat),
            msg_client,
            parse_mode="Markdown",
        )
    except Exception as e:
        log_event(
            "notify_client_paid_failed",
            tenant_id=tenant_id,
            order_id=order_id,
            client_chat=client_chat,
            error=str(e),
        )


def _notify_owner_order_paid(
    tenant: Dict[str, Any],
    tenant_id: str,
    order_id: str,
    order_after: Dict[str, Any],
) -> None:
    try:
        owner_enabled = str(tenant.get("owner_enabled") or "").strip().lower() == "true"
        owner_chat = str(tenant.get("owner_chat_id") or "").strip()
        owner_token = str(tenant.get("owner_bot_token") or "").strip()

        log_event(
            "DEBUG_OWNER_NOTIFY_ENTER",
            tenant_id=tenant_id,
            order_id=order_id,
            owner_enabled=owner_enabled,
            owner_chat_present=bool(owner_chat),
            owner_token_present=bool(owner_token),
            owner_chat_id=owner_chat,
        )

        if not (owner_enabled and owner_chat and owner_token):
            log_event(
                "DEBUG_OWNER_NOTIFY_SKIPPED_CONFIG",
                tenant_id=tenant_id,
                order_id=order_id,
                owner_enabled=owner_enabled,
                owner_chat_present=bool(owner_chat),
                owner_token_present=bool(owner_token),
            )
            return

        recap_data = _build_paid_recap_from_order(order_id, order_after)
        owner_msg = (
            "🟢 *VENTA CONFIRMADA*\n\n"
            f"{recap_data['recap']}\n\n"
            "💰 Pago validado correctamente."
        )

        sent = _safe_send_text(
            owner_token,
            int(owner_chat),
            owner_msg,
            parse_mode="Markdown",
        )

        log_event(
            "DEBUG_OWNER_NOTIFY_SEND_RESULT",
            tenant_id=tenant_id,
            order_id=order_id,
            owner_chat_id=owner_chat,
            sent=bool(sent),
        )
    except Exception as e:
        log_event(
            "notify_owner_paid_validated_failed",
            tenant_id=tenant_id,
            order_id=order_id,
            error=str(e),
        )


def _send_admin_paid_confirmation(
    bot_token: str,
    chat_id: int,
    order_id: str,
    order_after: Optional[Dict[str, Any]],
    already_paid: bool = False,
) -> None:
    if order_after:
        recap_data = _build_paid_recap_from_order(order_id, order_after)

        if already_paid:
            header = "🟡 *PEDIDO YA CONFIRMADO*"
            footer = "ℹ️ Este pedido ya había sido validado anteriormente."
        else:
            header = "🟢 *PAGO CONFIRMADO*"
            footer = "✔️ Estado actualizado correctamente."

        admin_msg = (
            f"{header}\n\n"
            f"📦 *Detalle del pedido*\n\n"
            f"{recap_data['recap']}\n\n"
            f"{footer}"
        )

        _safe_send_text(
            bot_token,
            chat_id,
            admin_msg,
            parse_mode="Markdown",
        )
    else:
        if already_paid:
            _safe_send_text(
                bot_token,
                chat_id,
                f"🟡 Pedido *{order_id}* ya estaba confirmado.",
                parse_mode="Markdown",
            )
        else:
            _safe_send_text(
                bot_token,
                chat_id,
                f"🟢 Pedido *{order_id}* confirmado correctamente.",
                parse_mode="Markdown",
            )


def _normalize_status(value: Any) -> str:
    return _safe_str(value).strip().upper()


def _is_paid_transition_allowed(current_status: str) -> bool:
    """
    Reglas actuales:
    - PENDING_PAYMENT -> PAID : permitido
    - PAID -> PAID : idempotente, no reescribe
    - cualquier otro estado: bloqueado
    """
    return current_status == "PENDING_PAYMENT"


def handle_admin_orders_callback(
    tenant: Dict[str, Any],
    tenant_id: str,
    bot_token: str,
    chat_id: int,
    data: str,
    orders_sh,
    tenant_tz: str,
    get_effective_admin_role,
) -> Optional[Dict[str, Any]]:
    if data == "admin_order":
        assert_admin_authorized(tenant, chat_id, tenant_id)

        user_role = get_effective_admin_role(tenant, chat_id)
        if user_role == "owner":
            _safe_send_text(
                bot_token,
                chat_id,
                "🚫 *Acceso restringido*\n\nEsta opción no está disponible para el propietario.",
                parse_mode="Markdown",
            )
            return {"ok": True}

        sess = get_sess(tenant_id, chat_id)
        tmp = sess.setdefault("tmp", {})
        _admin_order_reset(tmp)
        tmp["admin_order_cart"] = []
        return {"ok": _send_admin_order_home(bot_token, chat_id, tenant_id, orders_sh, sess)}

    if data.startswith("paid|"):
        parts = data.split("|")
        if len(parts) != 3:
            return {"ok": True}

        cb_tenant_id = parts[1].strip()
        order_id = parts[2].strip()

        if cb_tenant_id != tenant_id:
            raise HTTPException(status_code=400, detail="Tenant mismatch in callback")

        assert_admin_authorized(tenant, chat_id, tenant_id)

        if not order_id:
            _safe_send_text(
                bot_token,
                chat_id,
                "⚠️ *Pago no procesado*\n\nNo llegó el código de pedido.",
                parse_mode="Markdown",
            )
            return {"ok": True}

        if not _acquire_paid_lock(order_id):
            _safe_send_text(
                bot_token,
                chat_id,
                "⏳ *Procesando pedido*\n\nEste pedido ya se está procesando. Intenta en unos segundos.",
                parse_mode="Markdown",
            )
            return {"ok": True}

        try:
            order_ctx = get_order_context_by_id(orders_sh, order_id)
            if not order_ctx:
                _safe_send_text(
                    bot_token,
                    chat_id,
                    f"⚠️ *Pedido no encontrado*\n\nNo encontré el pedido *{order_id}* en Sheets.",
                    parse_mode="Markdown",
                )
                return {"ok": True}

            order_before = dict(order_ctx.get("order") or {})
            status_before = _normalize_status(order_before.get("status"))
            already_paid = status_before == "PAID"

            if already_paid:
                _send_admin_paid_confirmation(
                    bot_token=bot_token,
                    chat_id=chat_id,
                    order_id=order_id,
                    order_after=order_before,
                    already_paid=True,
                )
                log_event(
                    "admin_paid_idempotent_hit",
                    tenant_id=tenant_id,
                    order_id=order_id,
                    chat_id=chat_id,
                )
                return {"ok": True}

            if not _is_paid_transition_allowed(status_before):
                _safe_send_text(
                    bot_token,
                    chat_id,
                    (
                        "⚠️ *Pago no permitido*\n\n"
                        "No se puede confirmar este pago porque el pedido está en el estado:\n"
                        f"*{status_before or 'SIN ESTADO'}*."
                    ),
                    parse_mode="Markdown",
                )
                log_event(
                    "admin_paid_invalid_transition",
                    tenant_id=tenant_id,
                    order_id=order_id,
                    chat_id=chat_id,
                    current_status=status_before,
                )
                return {"ok": True}

            res = update_order_status(orders_sh, order_id, "PAID", order_ctx=order_ctx)
            if not res.get("ok"):
                alert_order_status_failed(
                    tenant_id=tenant_id,
                    order_id=order_id,
                    new_status="PAID",
                    error=res.get("error") or "update_order_status failed",
                )
                _safe_send_text(
                    bot_token,
                    chat_id,
                    "⚠️ *Pago no procesado*\n\nOcurrió un error actualizando el estado.",
                    parse_mode="Markdown",
                )
                return {"ok": True}

            if not res.get("found"):
                _safe_send_text(
                    bot_token,
                    chat_id,
                    f"⚠️ *Pedido no encontrado*\n\nNo encontré el pedido *{order_id}* en Sheets.",
                    parse_mode="Markdown",
                )
                return {"ok": True}

            order_after = dict(order_before or {})
            order_after["status"] = "PAID"
            if not _safe_str(order_after.get("payment_confirmed_at")):
                order_after["payment_confirmed_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
            status_after = "PAID"

            if status_after != "PAID":
                log_event(
                    "admin_paid_postcheck_failed",
                    tenant_id=tenant_id,
                    order_id=order_id,
                    chat_id=chat_id,
                    status_after=status_after,
                )
                _safe_send_text(
                    bot_token,
                    chat_id,
                    (
                        "⚠️ *Verificación incompleta*\n\n"
                        "El sistema no pudo verificar correctamente el estado final del pedido."
                    ),
                    parse_mode="Markdown",
                )
                return {"ok": True}

            _send_admin_paid_confirmation(
                bot_token=bot_token,
                chat_id=chat_id,
                order_id=order_id,
                order_after=order_after,
                already_paid=False,
            )

            if order_after:
                _notify_client_order_paid(
                    tenant=tenant,
                    tenant_id=tenant_id,
                    order_id=order_id,
                    order_after=order_after,
                )
                _notify_owner_order_paid(
                    tenant=tenant,
                    tenant_id=tenant_id,
                    order_id=order_id,
                    order_after=order_after,
                )

            return {"ok": True}

        finally:
            _release_paid_lock(order_id)

    if not data.startswith("admord|"):
        return None

    assert_admin_authorized(tenant, chat_id, tenant_id)

    user_role = get_effective_admin_role(tenant, chat_id)
    if user_role == "owner":
        _safe_send_text(
            bot_token,
            chat_id,
            "🚫 *Acceso restringido*\n\nEsta opción no está disponible para el propietario.",
            parse_mode="Markdown",
        )
        return {"ok": True}

    parts = data.split("|")
    if len(parts) < 3:
        return {"ok": True}

    cb_tenant_id = parts[1].strip()
    if cb_tenant_id != tenant_id:
        raise HTTPException(status_code=400, detail="Tenant mismatch in admin order callback")

    action = parts[2].strip()
    sess = get_sess(tenant_id, chat_id)
    tmp = sess.setdefault("tmp", {})

    if action == "noop":
        return {"ok": True}

    if action == "start":
        _admin_order_reset(tmp)
        tmp["admin_order_cart"] = []
        return {"ok": _send_admin_order_home(bot_token, chat_id, tenant_id, orders_sh, sess)}

    if action == "panel":
        _admin_order_reset(tmp)
        user_role = get_effective_admin_role(tenant, chat_id)
        _safe_send_text(
            bot_token,
            chat_id,
            "🧭 *PANEL ADMIN*\n\nGestiona tu negocio desde aquí.",
            reply_markup=admin_panel_kb(user_role=user_role),
            parse_mode="Markdown",
        )
        return {"ok": True}

    if action == "home":
        return {"ok": _send_admin_order_home(bot_token, chat_id, tenant_id, orders_sh, sess)}

    if action == "cat" and len(parts) == 4:
        try:
            idx = int(parts[3].strip())
        except Exception:
            idx = -1

        _, cats, cat_names = _admin_order_get_active_categories(orders_sh)
        if idx < 0 or idx >= len(cat_names):
            return {"ok": _send_admin_order_home(bot_token, chat_id, tenant_id, orders_sh, sess)}

        category = cat_names[idx]
        return {"ok": _send_admin_order_category(bot_token, chat_id, tenant_id, orders_sh, sess, category)}

    if action == "catback":
        current_category = str(tmp.get("admin_order_current_category") or "").strip()
        if not current_category:
            return {"ok": _send_admin_order_home(bot_token, chat_id, tenant_id, orders_sh, sess)}
        return {"ok": _send_admin_order_category(bot_token, chat_id, tenant_id, orders_sh, sess, current_category)}

    if action == "prd" and len(parts) == 4:
        sku = parts[3].strip()
        return {"ok": _send_admin_order_product_qty(bot_token, chat_id, tenant_id, orders_sh, sku)}

    if action == "qty" and len(parts) == 5:
        sku = parts[3].strip()
        try:
            qty = int(parts[4].strip())
        except Exception:
            qty = 1
        qty = max(1, qty)

        item = get_menu_product_or_404(orders_sh, sku)
        _admin_order_add_to_cart(tmp, sku, qty)

        _safe_send_text(
            bot_token,
            chat_id,
            f"✅ *Producto agregado*\n\n{qty} x {item.get('name', '')}",
            parse_mode="Markdown",
        )
        return {"ok": _send_admin_order_cart(bot_token, chat_id, tenant_id, orders_sh, sess)}

    if action == "inc" and len(parts) == 4:
        sku = parts[3].strip()
        _admin_order_inc_item(tmp, sku)
        return {"ok": _send_admin_order_cart(bot_token, chat_id, tenant_id, orders_sh, sess)}

    if action == "dec" and len(parts) == 4:
        sku = parts[3].strip()
        _admin_order_dec_item(tmp, sku)
        return {"ok": _send_admin_order_cart(bot_token, chat_id, tenant_id, orders_sh, sess)}

    if action == "rem" and len(parts) == 4:
        sku = parts[3].strip()
        _admin_order_remove_item(tmp, sku)
        return {"ok": _send_admin_order_cart(bot_token, chat_id, tenant_id, orders_sh, sess)}

    if action == "cart":
        return {"ok": _send_admin_order_cart(bot_token, chat_id, tenant_id, orders_sh, sess)}

    if action == "clear":
        tmp["admin_order_cart"] = []
        _safe_send_text(
            bot_token,
            chat_id,
            "🧹 *Carrito vaciado correctamente.*",
            parse_mode="Markdown",
        )
        return {"ok": _send_admin_order_home(bot_token, chat_id, tenant_id, orders_sh, sess)}

    if action == "confirm":
        cart = tmp.get("admin_order_cart") or []
        if not cart:
            _safe_send_text(
                bot_token,
                chat_id,
                "⚠️ *Carrito vacío*\n\nAgrega productos antes de continuar.",
                parse_mode="Markdown",
            )
            return {"ok": _send_admin_order_home(bot_token, chat_id, tenant_id, orders_sh, sess)}

        tmp["admin_order_step"] = "awaiting_name"
        _safe_send_text(
            bot_token,
            chat_id,
            "👤 *Paso 1 de 3*\n\nEscribe el nombre del cliente o toca el botón inferior.",
            reply_markup=kb([
                [("⏭ Sin nombre", f"admord|{tenant_id}|noname")],
                [("❌ Cancelar", f"admord|{tenant_id}|panel")],
            ]),
            parse_mode="Markdown",
        )
        return {"ok": True}

    if action == "noname":
        tmp["admin_order_name"] = "SIN_NOMBRE"
        tmp["admin_order_step"] = "awaiting_phone"
        _safe_send_text(
            bot_token,
            chat_id,
            "📱 *Paso 2 de 3*\n\nEscribe el celular del cliente o toca el botón inferior.",
            reply_markup=kb([
                [("⏭ Sin celular", f"admord|{tenant_id}|nophone")],
                [("❌ Cancelar", f"admord|{tenant_id}|panel")],
            ]),
            parse_mode="Markdown",
        )
        return {"ok": True}

    if action == "nophone":
        tmp["admin_order_contact"] = "SIN_CONTACTO"
        tmp["admin_order_step"] = "awaiting_time_choice"
        _safe_send_text(
            bot_token,
            chat_id,
            "⏰ *Paso 3 de 3*\n\nElige la hora del pedido:",
            reply_markup=_admin_order_time_choice_kb(tenant_id),
            parse_mode="Markdown",
        )
        return {"ok": True}

    if action == "timenow":
        tmp["admin_order_requested_time"] = "ahora"
        return finalize_admin_manual_order_from_tmp(
            tenant_id=tenant_id,
            bot_token=bot_token,
            chat_id=chat_id,
            orders_sh=orders_sh,
            tmp=tmp,
            tenant=tenant,
        )

    if action == "timelater":
        tmp["admin_order_step"] = "awaiting_time_manual"
        _safe_send_text(
            bot_token,
            chat_id,
            "⏰ *Paso final*\n\nEscribe la hora solicitada.\nEjemplos: 19:30, 20h",
            parse_mode="Markdown",
        )
        return {"ok": True}

    if action == "proof":
        last_order_id = str(tmp.get("admin_order_last_id") or "").strip()
        if not last_order_id:
            _safe_send_text(
                bot_token,
                chat_id,
                "⚠️ *Pedido no encontrado*\n\nNo encontré el pedido recién creado.",
                parse_mode="Markdown",
            )
            return {"ok": True}

        tmp["admin_order_waiting_proof"] = True
        tmp["admin_order_proof_received"] = False

        _safe_send_text(
            bot_token,
            chat_id,
            f"📷 *Comprobante requerido*\n\nEnvía la foto del comprobante para el pedido *{last_order_id}*.",
            parse_mode="Markdown",
        )
        return {"ok": True}

    if action == "proof_ok":
        if not bool(tmp.get("admin_order_proof_received")):
            _safe_send_text(
                bot_token,
                chat_id,
                "⚠️ *Comprobante pendiente*\n\nAún no recibí la foto del comprobante.",
                parse_mode="Markdown",
            )
            return {"ok": True}

        last_order_id = str(tmp.get("admin_order_last_id") or "").strip()
        log_event(
            "DEBUG_PROOF_OK_ENTER",
            tenant_id=tenant_id,
            chat_id=chat_id,
            order_id=last_order_id,
            proof_received=bool(tmp.get("admin_order_proof_received")),
        )

        if last_order_id:
            snapshot = tmp.get("admin_order_last_snapshot")

            if snapshot and snapshot.get("order_id") == last_order_id:
                log_event(
                    "DEBUG_PROOF_OK_USING_SNAPSHOT",
                    tenant_id=tenant_id,
                    chat_id=chat_id,
                    order_id=last_order_id,
                )

                order_after = {
                    "customer_name": snapshot.get("customer_name") or "",
                    "customer_contact": snapshot.get("customer_contact") or "",
                    "requested_time": snapshot.get("requested_time") or "",
                    "items_snapshot": snapshot.get("items_snapshot") or [],
                }

                _notify_owner_order_paid(
                    tenant=tenant,
                    tenant_id=tenant_id,
                    order_id=last_order_id,
                    order_after=order_after,
                )
            else:
                order_after = get_order_by_id(orders_sh, last_order_id)

                log_event(
                    "DEBUG_PROOF_OK_FALLBACK_SHEETS",
                    tenant_id=tenant_id,
                    chat_id=chat_id,
                    order_id=last_order_id,
                    order_found=bool(order_after),
                )

                if order_after:
                    _notify_owner_order_paid(
                        tenant=tenant,
                        tenant_id=tenant_id,
                        order_id=last_order_id,
                        order_after=order_after,
                    )
                else:
                    log_event(
                        "DEBUG_PROOF_OK_NO_DATA",
                        tenant_id=tenant_id,
                        chat_id=chat_id,
                        order_id=last_order_id,
                    )

        _safe_send_text(
            bot_token,
            chat_id,
            "✅ *Comprobante validado*\n\nAhora puedes abrir la encuesta.",
            reply_markup=kb([
                [("📝 Encuesta", f"admord|{tenant_id}|survey")],
                [("🧭 Panel admin", "admin_panel")],
            ]),
            parse_mode="Markdown",
        )
        return {"ok": True}

    if action == "survey":
        questions = get_runtime_survey_questions(orders_sh)
        if not questions:
            _safe_send_text(
                bot_token,
                chat_id,
                "⚠️ *Encuesta no disponible*\n\nNo hay preguntas activas configuradas.",
                parse_mode="Markdown",
            )
            return {"ok": True}

        customer_phone = str(tmp.get("admin_order_last_phone") or "").strip()
        customer_name = str(tmp.get("admin_order_last_name") or "").strip()

        if not customer_phone:
            _safe_send_text(
                bot_token,
                chat_id,
                "⚠️ *Datos incompletos*\n\nNo encontré el número del cliente del pedido.",
                parse_mode="Markdown",
            )
            return {"ok": True}

        reward_text = get_survey_reward_text(orders_sh)
        tmp["admin_survey_runtime"] = True
        tmp["admin_survey_step"] = "q_0"
        tmp["admin_survey_answers"] = []
        tmp["admin_survey_phone"] = customer_phone
        tmp["admin_survey_name"] = customer_name

        intro = "📝 *ENCUESTA DEL CLIENTE*\n\nIniciaremos la encuesta con los datos del pedido ya registrado."
        if reward_text:
            intro += f"\n\n🎁 Recompensa configurada:\n{reward_text}"

        _safe_send_text(
            bot_token,
            chat_id,
            intro,
            parse_mode="Markdown",
        )

        first_q = questions[0]
        send_admin_survey_runtime_question(
            bot_token=bot_token,
            chat_id=chat_id,
            tenant_id=tenant_id,
            question=first_q,
            q_idx=0,
        )
        return {"ok": True}

    if action == "sstar" and len(parts) == 5:
        try:
            q_idx = int(parts[3].strip())
            stars_value = int(parts[4].strip())
        except Exception:
            _safe_send_text(
                bot_token,
                chat_id,
                "⚠️ *Respuesta inválida*\n\nNo pude leer esa calificación.",
                reply_markup=admin_fixed_kb(),
                parse_mode="Markdown",
            )
            return {"ok": True}

        if stars_value < 1 or stars_value > 5:
            _safe_send_text(
                bot_token,
                chat_id,
                "⚠️ *Calificación inválida*\n\nDebe estar entre 1 y 5.",
                reply_markup=admin_fixed_kb(),
                parse_mode="Markdown",
            )
            return {"ok": True}

        if not bool(tmp.get("admin_survey_runtime")):
            _safe_send_text(
                bot_token,
                chat_id,
                "⚠️ *Encuesta inactiva*\n\nNo hay una encuesta activa en este momento.",
                reply_markup=admin_fixed_kb(),
                parse_mode="Markdown",
            )
            return {"ok": True}

        questions = get_runtime_survey_questions(orders_sh)
        if not questions or q_idx < 0 or q_idx >= len(questions):
            clear_admin_survey_runtime(tmp)
            _safe_send_text(
                bot_token,
                chat_id,
                "⚠️ *Error de flujo*\n\nSe perdió el estado de la encuesta.",
                reply_markup=admin_fixed_kb(),
                parse_mode="Markdown",
            )
            return {"ok": True}

        current_q = questions[q_idx]
        qtype = str(current_q.get("type") or "").strip().lower()
        if qtype != "stars":
            _safe_send_text(
                bot_token,
                chat_id,
                "⚠️ *Tipo de pregunta inválido*\n\nEsta pregunta no es de estrellas.",
                reply_markup=admin_fixed_kb(),
                parse_mode="Markdown",
            )
            return {"ok": True}

        answers = tmp.setdefault("admin_survey_answers", [])
        answers.append({
            "question_id": str(current_q.get("question_id") or ""),
            "question_order": int(current_q.get("order", 0) or 0),
            "question_text": str(current_q.get("question_text") or ""),
            "answer_type": qtype,
            "answer_value": str(stars_value),
        })

        next_idx = q_idx + 1
        if next_idx < len(questions):
            next_q = questions[next_idx]
            tmp["admin_survey_step"] = f"q_{next_idx}"
            send_admin_survey_runtime_question(
                bot_token=bot_token,
                chat_id=chat_id,
                tenant_id=tenant_id,
                question=next_q,
                q_idx=next_idx,
            )
            return {"ok": True}

        return finalize_admin_survey_runtime(
            tenant_id=tenant_id,
            bot_token=bot_token,
            chat_id=chat_id,
            orders_sh=orders_sh,
            tenant_tz=tenant_tz,
            tmp=tmp,
        )

    return {"ok": True}
