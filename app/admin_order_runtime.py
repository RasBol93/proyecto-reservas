from typing import Any, Dict, Optional

from app.menu import load_menu_admin_index
from app.orders import append_order_row, gen_order_id, build_items_snapshot
from app.telegram_api import telegram_send_text
from app.telegram_keyboard import kb
from app.utils import log_event
from app.webhook_helpers import fmt_snapshot_lines, build_order_recap_text, admin_fixed_kb
from app.alerts import alert_order_failed
from app.admin_manual_order import _admin_order_reset


def _finalize_admin_manual_order_core(
    tenant_id: str,
    bot_token: str,
    chat_id: int,
    orders_sh,
    tmp: Dict[str, Any],
    tenant: Optional[Dict[str, Any]] = None,
    use_fixed_kb_on_error: bool = False,
) -> Dict[str, Any]:
    requested_time = str(tmp.get("admin_order_requested_time") or "").strip() or "ahora"
    cart = tmp.get("admin_order_cart") or []
    customer_name = str(tmp.get("admin_order_name") or "").strip()
    customer_contact = str(tmp.get("admin_order_contact") or "").strip()

    if not cart or not customer_name or not customer_contact:
        _admin_order_reset(tmp)
        telegram_send_text(
            bot_token,
            chat_id,
            "⚠️ Faltaban datos del pedido manual. Empecemos de nuevo.",
            reply_markup=admin_fixed_kb() if use_fixed_kb_on_error else None,
        )
        return {"ok": True}

    menu_idx = load_menu_admin_index(orders_sh, force=False)
    items_snapshot = build_items_snapshot(cart, menu_idx)
    lines_txt, total_amount, total_qty = fmt_snapshot_lines(items_snapshot)

    order_id = gen_order_id()

    result = append_order_row(
        orders_sh=orders_sh,
        tenant_id=tenant_id,
        order_id=order_id,
        customer_name=customer_name,
        customer_contact=customer_contact,
        customer_telegram_chat_id="",
        items=cart,
        items_snapshot=items_snapshot,
        currency="BOB",
        pricing_version="v1",
        notes="",
        delivery_type="pickup",
        requested_time=requested_time,
        status="PAID",
        source="admin_manual",
        total_amount=total_amount,
    )

    if not result.get("ok"):
        alert_order_failed(
            tenant_id=tenant_id,
            order_id=order_id,
            error=result.get("error") or "append_order_row failed",
        )
        telegram_send_text(
            bot_token,
            chat_id,
            "⚠️ Error guardando el pedido manual.",
            reply_markup=admin_fixed_kb() if use_fixed_kb_on_error else None,
        )
        return {"ok": True}

    _admin_order_reset(tmp)

    tmp["admin_order_last_id"] = order_id
    tmp["admin_order_waiting_proof"] = False
    tmp["admin_order_proof_received"] = False
    tmp["admin_order_last_phone"] = customer_contact
    tmp["admin_order_last_name"] = customer_name

    recap = build_order_recap_text(
        order_id=order_id,
        customer_name=customer_name,
        customer_contact=customer_contact,
        requested_time=requested_time,
        detail_lines=lines_txt,
        total_qty=total_qty,
        total=total_amount,
    )

    telegram_send_text(
        bot_token,
        chat_id,
        recap,
        parse_mode="Markdown",
    )
    telegram_send_text(
        bot_token,
        chat_id,
        "✅ *Pedido manual registrado como pagado.*\nAhora puedes fotografiar el comprobante.",
        parse_mode="Markdown",
        reply_markup=kb([
            [("📷 Fotografiar comprobante", f"admord|{tenant_id}|proof")],
            [("🧭 Panel admin", "admin_panel")],
        ]),
    )

    if tenant:
        try:
            owner_enabled = str(tenant.get("owner_enabled") or "").strip().lower() == "true"
            owner_chat = str(tenant.get("owner_chat_id") or "").strip()
            owner_token = str(tenant.get("owner_bot_token") or "").strip()

            if owner_enabled and owner_chat and owner_token:
                telegram_send_text(
                    owner_token,
                    int(owner_chat),
                    recap,
                    parse_mode="Markdown",
                )
        except Exception as e:
            log_event(
                "notify_owner_manual_order_failed",
                tenant_id=tenant_id,
                order_id=order_id,
                error=str(e),
            )

    return {"ok": True}


def finalize_admin_manual_order_from_tmp(
    tenant_id: str,
    bot_token: str,
    chat_id: int,
    orders_sh,
    tmp: Dict[str, Any],
    tenant: Dict[str, Any],
) -> Dict[str, Any]:
    return _finalize_admin_manual_order_core(
        tenant_id=tenant_id,
        bot_token=bot_token,
        chat_id=chat_id,
        orders_sh=orders_sh,
        tmp=tmp,
        tenant=tenant,
        use_fixed_kb_on_error=False,
    )


def finalize_admin_manual_order(
    tenant_id: str,
    bot_token: str,
    chat_id: int,
    orders_sh,
    tmp: Dict[str, Any],
) -> Dict[str, Any]:
    return _finalize_admin_manual_order_core(
        tenant_id=tenant_id,
        bot_token=bot_token,
        chat_id=chat_id,
        orders_sh=orders_sh,
        tmp=tmp,
        tenant=None,
        use_fixed_kb_on_error=True,
    )
