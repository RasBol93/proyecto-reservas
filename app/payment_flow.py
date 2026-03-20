import time
from typing import Any, Dict

from app.menu import load_menu_index
from app.orders import get_order_by_id
from app.telegram_api import (
    telegram_send_text,
    telegram_get_file_path,
    telegram_download_file_bytes,
    telegram_send_file_bytes,
)
from app.telegram_keyboard import kb
from app.utils import log_event
from app.webhook_helpers import (
    get_admin_bot_token,
    get_admin_chat_id,
    get_client_bot_token,
    parse_items_field,
    fmt_cart_lines,
    fmt_snapshot_lines,
)


def forward_proof_to_admin(
    tenant: Dict[str, Any],
    tenant_id: str,
    proof_file_id: str,
    proof_type: str,
    proof_caption: str,
) -> bool:
    client_token = get_client_bot_token(tenant)
    admin_token = get_admin_bot_token(tenant)
    admin_chat_id = get_admin_chat_id(tenant)

    if not client_token or not admin_token or not admin_chat_id:
        log_event(
            "forward_proof_missing_config",
            tenant_id=tenant_id,
            has_client=bool(client_token),
            has_admin=bool(admin_token),
            has_admin_chat=bool(admin_chat_id),
        )
        return False

    try:
        file_path = telegram_get_file_path(client_token, proof_file_id)
        file_bytes = telegram_download_file_bytes(client_token, file_path)
        filename = file_path.split("/")[-1] if file_path else "proof"
        caption = proof_caption or ("Comprobante (foto)" if proof_type == "photo" else "Comprobante (archivo)")

        if proof_type == "photo":
            return telegram_send_file_bytes(
                bot_token=admin_token,
                method="sendPhoto",
                chat_id=admin_chat_id,
                file_field="photo",
                filename=filename or "proof.jpg",
                content_type="image/jpeg",
                file_bytes=file_bytes,
                caption=caption,
            )

        return telegram_send_file_bytes(
            bot_token=admin_token,
            method="sendDocument",
            chat_id=admin_chat_id,
            file_field="document",
            filename=filename or "proof.pdf",
            content_type="application/octet-stream",
            file_bytes=file_bytes,
            caption=caption,
        )

    except Exception as e:
        log_event("forward_proof_failed", tenant_id=tenant_id, error=str(e))
        return False


def notify_admin_payment_reported(
    tenant: Dict[str, Any],
    tenant_id: str,
    orders_sh,
    order_id: str,
    is_reminder: bool = False,
) -> bool:
    admin_token = get_admin_bot_token(tenant)
    admin_chat_id = get_admin_chat_id(tenant)

    if not admin_token or not admin_chat_id:
        log_event("admin_notify_failed", tenant_id=tenant_id, reason="missing_admin_token_or_chat")
        return False

    order = get_order_by_id(orders_sh, order_id)
    if not order:
        telegram_send_text(admin_token, admin_chat_id, f"⚠️ Pedido {order_id} no encontrado en Sheets.")
        return False

    items_snapshot = parse_items_field(order.get("items_snapshot"))
    if items_snapshot:
        lines_txt, snapshot_total, total_qty = fmt_snapshot_lines(items_snapshot)
        total = snapshot_total
    else:
        try:
            menu_idx = load_menu_index(orders_sh)
        except Exception as e:
            log_event("admin_menu_load_error", tenant_id=tenant_id, error=str(e))
            menu_idx = {}
        cart = parse_items_field(order.get("items"))
        lines_txt, _, total_qty = fmt_cart_lines(cart, menu_idx)
        try:
            total = float(order.get("total_amount") or 0)
        except Exception:
            total = 0.0

    proof_file_id = (order.get("payment_proof_file_id") or "").strip()
    proof_type = (order.get("payment_proof_type") or "").strip()
    proof_caption = (order.get("payment_proof_caption") or "").strip()

    confirm_btn = kb([[("✅ Confirmar pago", f"paid|{tenant_id}|{order_id}")]])

    title = "🔔 RECORDATORIO — PAGO REPORTADO" if is_reminder else "💳 PAGO REPORTADO"
    txt = (
        f"{title}\n\n"
        f"Tenant: {tenant_id}\n"
        f"ID: {order_id}\n"
        f"Cliente: {order.get('customer_name','')}\n"
        f"Contacto(chat_id): {order.get('customer_contact','')}\n"
        f"Hora recogida: {order.get('requested_time','pendiente')}\n"
        f"Cantidad total: {total_qty}\n"
        f"Total: {total:.2f} BOB\n\n"
        f"Detalle:\n{lines_txt}\n\n"
        "Presiona ✅ Confirmar pago cuando verifiques."
    )

    ok_txt = telegram_send_text(admin_token, admin_chat_id, txt, reply_markup=confirm_btn)

    ok_proof = False
    if proof_file_id and proof_type:
        ok_proof = forward_proof_to_admin(tenant, tenant_id, proof_file_id, proof_type, proof_caption)
    else:
        log_event("admin_missing_proof", tenant_id=tenant_id, order_id=order_id)

    log_event(
        "admin_notify_result",
        tenant_id=tenant_id,
        order_id=order_id,
        ok_txt=bool(ok_txt),
        ok_proof=bool(ok_proof),
        is_reminder=bool(is_reminder),
    )
    return bool(ok_txt)
