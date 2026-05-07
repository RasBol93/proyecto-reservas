from typing import Any, Dict, Optional
from urllib.parse import urlparse

from app.telegram_api import (
    telegram_send_text,
    telegram_get_file_path,
    telegram_download_file_bytes,
)
from app.content import upsert_content_entries
from app.image_storage import upload_business_logo_for_tenant
from app.utils import log_event
from app.webhook_helpers import assert_admin_authorized
from app.admin_callbacks_business import (
    BUSINESS_FIELD_LABELS,
    clear_admin_business_state,
    send_admin_business_home,
    send_admin_business_logo_menu,
)


MAX_BUSINESS_LOGO_BYTES = 5 * 1024 * 1024
ALLOWED_LOGO_CONTENT_TYPES = {
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".webp": "image/webp",
}


def _safe_send_text(
    bot_token: str,
    chat_id: int,
    text: str,
    reply_markup=None,
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


def _normalize_admin_business_value(raw_text: str) -> str:
    clean_text = str(raw_text or "").strip()
    if clean_text == "-":
        return ""
    return clean_text


def _validate_optional_public_url(value: str, *, field_name: str) -> str:
    clean_value = str(value or "").strip()
    if not clean_value:
        return ""

    parsed = urlparse(clean_value)
    scheme = str(parsed.scheme or "").strip().lower()
    netloc = str(parsed.netloc or "").strip().lower()
    if scheme not in {"http", "https"} or not netloc:
        raise ValueError(f"{field_name} debe ser una URL completa http/https o '-' para limpiar.")

    return clean_value


def _build_update_entry(field_name: str, field_value: str) -> Dict[str, Any]:
    if field_name == "restaurant_name":
        return {
            "key": field_name,
            "value": field_value,
            "active": True,
        }

    return {
        "key": field_name,
        "value": field_value,
        "active": bool(field_value),
    }


def _infer_logo_content_type(file_path: str) -> str:
    low_path = str(file_path or "").strip().lower()
    for ext, content_type in ALLOWED_LOGO_CONTENT_TYPES.items():
        if low_path.endswith(ext):
            return content_type
    raise ValueError("Formato de imagen no permitido. Usa una foto JPG, PNG o WEBP.")


def _handle_admin_business_logo_upload(
    tenant: Dict[str, Any],
    tenant_id: str,
    bot_token: str,
    chat_id: int,
    msg: Dict[str, Any],
    orders_sh,
    tmp: Dict[str, Any],
) -> Dict[str, Any]:
    if not msg.get("photo"):
        _safe_send_text(
            bot_token,
            chat_id,
            "📷 Estoy esperando una foto del logo. Envíala como foto de Telegram.",
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
            "No pude leer la foto del logo. Intenta enviarla nuevamente.",
        )
        return {"ok": True}

    try:
        file_path = telegram_get_file_path(bot_token, file_id)
        content_type = _infer_logo_content_type(file_path)
        file_bytes = telegram_download_file_bytes(bot_token, file_path)

        if len(file_bytes) > MAX_BUSINESS_LOGO_BYTES:
            raise ValueError("La foto es demasiado pesada. El máximo permitido es 5 MB.")

        logo_url = upload_business_logo_for_tenant(
            tenant=tenant,
            tenant_id=tenant_id,
            file_bytes=file_bytes,
            mime_type=content_type,
        )

        upsert_content_entries(
            orders_sh,
            [_build_update_entry("logo_url", logo_url)],
        )

    except ValueError as e:
        _safe_send_text(
            bot_token,
            chat_id,
            str(e),
        )
        return {"ok": True}
    except Exception as e:
        log_event(
            "admin_business_logo_upload_failed",
            tenant_id=tenant_id,
            error_type=type(e).__name__,
            error=str(e),
        )
        _safe_send_text(
            bot_token,
            chat_id,
            "No pude procesar o subir el logo. Intenta nuevamente.",
        )
        return {"ok": True}

    clear_admin_business_state(tmp)

    _safe_send_text(
        bot_token,
        chat_id,
        "✅ Logo actualizado correctamente.",
    )
    return {"ok": send_admin_business_home(bot_token, chat_id, tenant_id)}


def handle_admin_business_message(
    tenant: Dict[str, Any],
    tenant_id: str,
    bot_token: str,
    chat_id: int,
    msg: Dict[str, Any],
    orders_sh,
    tmp: Dict[str, Any],
) -> Optional[Dict[str, Any]]:
    mode = str(tmp.get("admbiz_mode") or "").strip()
    if not mode:
        return None

    assert_admin_authorized(tenant, chat_id, tenant_id)

    if mode == "awaiting_logo_photo":
        return _handle_admin_business_logo_upload(
            tenant=tenant,
            tenant_id=tenant_id,
            bot_token=bot_token,
            chat_id=chat_id,
            msg=msg,
            orders_sh=orders_sh,
            tmp=tmp,
        )

    if mode != "awaiting_text":
        return None

    field_name = str(tmp.get("admbiz_field") or "").strip()
    if field_name not in BUSINESS_FIELD_LABELS:
        clear_admin_business_state(tmp)
        return {"ok": send_admin_business_home(bot_token, chat_id, tenant_id)}

    raw_text = msg.get("text")
    if raw_text is None:
        _safe_send_text(
            bot_token,
            chat_id,
            "Estoy esperando un texto. Envía el valor nuevo o `-` para limpiar.",
            parse_mode="Markdown",
        )
        return {"ok": True}

    field_value = _normalize_admin_business_value(raw_text)

    if field_name == "restaurant_name" and not field_value:
        _safe_send_text(
            bot_token,
            chat_id,
            "El nombre del negocio no puede quedar vacío. Envía un valor válido.",
        )
        return {"ok": True}

    try:
        if field_name == "location_link":
            field_value = _validate_optional_public_url(field_value, field_name="location_link")
        elif field_name == "logo_url":
            field_value = _validate_optional_public_url(field_value, field_name="logo_url")
    except ValueError as e:
        _safe_send_text(
            bot_token,
            chat_id,
            str(e),
        )
        return {"ok": True}

    upsert_content_entries(
        orders_sh,
        [_build_update_entry(field_name, field_value)],
    )

    clear_admin_business_state(tmp)

    label = BUSINESS_FIELD_LABELS.get(field_name, field_name)
    _safe_send_text(
        bot_token,
        chat_id,
        f"✅ Información actualizada.\nCampo: {label}",
    )

    if field_name == "logo_url":
        return {"ok": send_admin_business_logo_menu(bot_token, chat_id, tenant_id)}

    return {"ok": send_admin_business_home(bot_token, chat_id, tenant_id)}
