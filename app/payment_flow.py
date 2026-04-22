# app/payment_flow.py — optimizado (menos lecturas, más simple)
# hardened incremental: misma estructura, mismos contratos, más robustez

from typing import Any, Dict, Tuple
import mimetypes
from urllib.parse import urlparse, unquote
from urllib.request import Request, urlopen

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
from app.alerts import (
    alert_payment_failed,
    alert_tenant_error,
    alert_telegram_error,
    alert_system_error,
)


_EXTERNAL_PROOF_TIMEOUT_SECONDS = 10


def _guess_filename_from_url(url: str, content_type: str) -> str:
    try:
        parsed = urlparse(str(url or "").strip())
        name = unquote((parsed.path or "").split("/")[-1]).strip()
    except Exception:
        name = ""

    if name:
        return name

    ext = mimetypes.guess_extension((content_type or "").split(";")[0].strip()) or ""
    return f"proof{ext}"


def _looks_like_image_content(filename: str, content_type: str) -> bool:
    clean_content_type = (content_type or "").split(";")[0].strip().lower()
    if clean_content_type.startswith("image/"):
        return True

    guessed_type, _ = mimetypes.guess_type(filename or "")
    return bool((guessed_type or "").startswith("image/"))


def _download_external_proof_bytes(url: str) -> Tuple[bytes, str, str]:
    clean_url = str(url or "").strip()
    if not clean_url:
        raise RuntimeError("external proof url missing")

    req = Request(
        clean_url,
        headers={
            "User-Agent": "proyecto-reservas/1.0",
            "Accept": "*/*",
        },
    )

    with urlopen(req, timeout=_EXTERNAL_PROOF_TIMEOUT_SECONDS) as resp:
        file_bytes = resp.read()
        content_type = str(resp.headers.get("Content-Type") or "application/octet-stream").strip()

    if not isinstance(file_bytes, (bytes, bytearray)) or len(file_bytes) == 0:
        raise RuntimeError("downloaded external proof is empty")

    filename = _guess_filename_from_url(clean_url, content_type)
    return bytes(file_bytes), filename, content_type


def _build_proof_fallback_text(proof_file_id: str, proof_type: str, proof_caption: str) -> str:
    clean_file_id = str(proof_file_id or "").strip()
    clean_proof_type = str(proof_type or "").strip()
    clean_caption = str(proof_caption or "").strip()

    if not clean_file_id:
        return ""

    if clean_proof_type == "external_url":
        if clean_caption and clean_caption != clean_file_id:
            return f"Comprobante: {clean_caption}\nReferencia: {clean_file_id}"
        return f"Comprobante: {clean_file_id}"

    if clean_caption and clean_caption != clean_file_id:
        return f"Comprobante: {clean_caption}"

    return "Comprobante reportado, pero no se pudo reenviar la media."


def forward_proof_to_admin(
    tenant: Dict[str, Any],
    tenant_id: str,
    proof_file_id: str,
    proof_type: str,
    proof_caption: str,
) -> bool:
    admin_token = get_admin_bot_token(tenant)
    admin_chat_id = get_admin_chat_id(tenant)

    if not admin_token or not admin_chat_id:
        log_event("forward_proof_missing_config", tenant_id=tenant_id)
        alert_tenant_error(
            tenant_id=tenant_id,
            error="Missing admin token or admin_chat_id",
        )
        return False

    clean_file_id = str(proof_file_id or "").strip()
    clean_proof_type = str(proof_type or "").strip()
    clean_caption = str(proof_caption or "").strip()

    if not clean_file_id:
        log_event("forward_proof_missing_file_id", tenant_id=tenant_id)
        alert_payment_failed(tenant_id=tenant_id, error="Missing proof_file_id")
        return False

    if clean_proof_type not in ("photo", "document", "external_url"):
        log_event("forward_proof_invalid_type", tenant_id=tenant_id, proof_type=clean_proof_type)
        alert_payment_failed(tenant_id=tenant_id, error=f"Invalid proof_type: {clean_proof_type}")
        return False

    try:
        content_type = "application/octet-stream"
        if clean_proof_type in ("photo", "document"):
            client_token = get_client_bot_token(tenant)
            if not client_token:
                log_event("forward_proof_missing_client_token", tenant_id=tenant_id)
                alert_tenant_error(
                    tenant_id=tenant_id,
                    error="Missing client token for Telegram proof forwarding",
                )
                return False

            file_path = telegram_get_file_path(client_token, clean_file_id)
            if not file_path:
                log_event("forward_proof_file_path_missing", tenant_id=tenant_id, proof_type=clean_proof_type)
                alert_telegram_error(
                    error="telegram_get_file_path returned empty path",
                    method="getFile",
                    chat_id=admin_chat_id,
                )
                return False

            file_bytes = telegram_download_file_bytes(client_token, file_path)
            if not file_bytes:
                log_event("forward_proof_file_bytes_missing", tenant_id=tenant_id, proof_type=clean_proof_type)
                alert_telegram_error(
                    error="telegram_download_file_bytes returned empty bytes",
                    method="downloadFile",
                    chat_id=admin_chat_id,
                )
                return False

            filename = file_path.split("/")[-1] if file_path else "proof"
            send_as_photo = clean_proof_type == "photo"
        else:
            file_bytes, filename, content_type = _download_external_proof_bytes(clean_file_id)
            send_as_photo = _looks_like_image_content(filename, content_type)

        caption = clean_caption or (
            "Comprobante (foto)" if send_as_photo else "Comprobante (archivo)"
        )

        method = "sendPhoto" if send_as_photo else "sendDocument"
        field = "photo" if send_as_photo else "document"

        ok = telegram_send_file_bytes(
            bot_token=admin_token,
            method=method,
            chat_id=admin_chat_id,
            file_field=field,
            filename=filename,
            content_type=content_type,
            file_bytes=file_bytes,
            caption=caption,
        )

        if not ok:
            alert_payment_failed(tenant_id=tenant_id, error="Error enviando comprobante")
            alert_telegram_error(
                error="telegram_send_file_bytes returned False",
                method=method,
                chat_id=admin_chat_id,
            )

        return bool(ok)

    except Exception as e:
        log_event("forward_proof_failed", tenant_id=tenant_id, error=str(e))
        alert_payment_failed(tenant_id=tenant_id, error=str(e))
        alert_telegram_error(
            error=str(e),
            method="forward_proof_to_admin",
            chat_id=admin_chat_id,
        )
        return False


def notify_admin_payment_reported(
    tenant: Dict[str, Any],
    tenant_id: str,
    orders_sh,
    order_id: str,
    is_reminder: bool = False,
) -> bool:

    try:
        admin_token = get_admin_bot_token(tenant)
        admin_chat_id = get_admin_chat_id(tenant)

        if not admin_token or not admin_chat_id:
            alert_tenant_error(tenant_id=tenant_id, error="Missing admin config")
            return False

        clean_order_id = str(order_id or "").strip()
        if not clean_order_id:
            alert_payment_failed(tenant_id=tenant_id, error="Missing order_id")
            return False

        order = get_order_by_id(orders_sh, clean_order_id)
        if not order:
            telegram_send_text(admin_token, admin_chat_id, f"⚠️ Pedido {clean_order_id} no encontrado.")
            return False

        # -------------------------
        # PRIORIDAD: snapshot
        # -------------------------
        items_snapshot = parse_items_field(order.get("items_snapshot"))

        if items_snapshot:
            lines_txt, total, total_qty = fmt_snapshot_lines(items_snapshot)
        else:
            # fallback SOLO si no hay snapshot
            cart = parse_items_field(order.get("items"))
            try:
                lines_txt, _, total_qty = fmt_cart_lines(cart, {})
            except Exception:
                lines_txt = "(vacío)"
                total_qty = 0

            try:
                total = float(order.get("total_amount") or 0)
            except Exception:
                total = 0.0

        proof_file_id = str(order.get("payment_proof_file_id") or "").strip()
        proof_type = str(order.get("payment_proof_type") or "").strip()
        proof_caption = str(order.get("payment_proof_caption") or "").strip()
        has_forwardable_proof = bool(proof_file_id and proof_type in ("photo", "document", "external_url"))

        confirm_btn = kb([[("✅ Confirmar pago", f"paid|{tenant_id}|{clean_order_id}")]])

        title = "🔔 RECORDATORIO — NUEVO PEDIDO" if is_reminder else "🆕 NUEVO PEDIDO"

        txt = (
            f"{title}\n\n"
            f"Código de pedido: {clean_order_id}\n\n"
            f"Cliente: {order.get('customer_name', '')}\n"
            f"Teléfono: {order.get('customer_contact', '')}\n\n"
            f"Hora de recojo: {order.get('requested_time', 'pendiente')}\n\n"
            f"Detalle:\n{lines_txt}\n\n"
            f"Total: Bs {total:.2f}\n\n"
            "Presiona ✅ Confirmar pago cuando verifiques."
        )

        ok_txt = telegram_send_text(admin_token, admin_chat_id, txt, reply_markup=confirm_btn)
        if not ok_txt:
            alert_telegram_error(
                error="telegram_send_text returned False",
                method="sendMessage",
                chat_id=admin_chat_id,
            )

        ok_proof = False
        if has_forwardable_proof:
            ok_proof = forward_proof_to_admin(
                tenant, tenant_id, proof_file_id, proof_type, proof_caption
            )
            if not ok_proof:
                fallback_txt = _build_proof_fallback_text(
                    proof_file_id,
                    proof_type,
                    proof_caption,
                )
                if fallback_txt:
                    fallback_ok = telegram_send_text(admin_token, admin_chat_id, fallback_txt)
                    if not fallback_ok:
                        alert_telegram_error(
                            error="telegram_send_text returned False",
                            method="sendMessage",
                            chat_id=admin_chat_id,
                        )

        try:
            log_event(
                "notify_admin_payment_reported_result",
                tenant_id=tenant_id,
                order_id=clean_order_id,
                is_reminder=bool(is_reminder),
                ok_txt=bool(ok_txt),
                ok_proof=bool(ok_proof),
                has_proof=bool(proof_file_id and proof_type),
                total_qty=total_qty,
                total=total,
            )
        except Exception:
            pass

        return bool(ok_txt)

    except Exception as e:
        log_event("notify_admin_payment_reported_error", tenant_id=tenant_id, order_id=order_id, error=str(e))
        alert_payment_failed(tenant_id=tenant_id, error=str(e))
        alert_system_error(error=str(e), module="payment_flow.notify_admin_payment_reported")
        return False
