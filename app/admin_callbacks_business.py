from typing import Any, Dict, Optional

from fastapi import HTTPException

from app.telegram_api import telegram_send_text
from app.telegram_keyboard import kb
from app.webhook_helpers import get_sess, assert_admin_authorized
from app.admin_nav import admin_panel_kb


BUSINESS_FIELD_LABELS = {
    "restaurant_name": "Nombre",
    "welcome_text": "Descripción",
    "location_text": "Dirección",
    "location_link": "Link ubicación",
    "faq_text": "FAQ",
    "logo_url": "Logo",
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


def clear_admin_business_state(tmp: Dict[str, Any]) -> None:
    tmp.pop("admbiz_mode", None)
    tmp.pop("admbiz_field", None)


def admin_business_home_kb(tenant_id: str):
    return kb([
        [("Nombre", f"admbiz|{tenant_id}|edit|restaurant_name")],
        [("Descripción", f"admbiz|{tenant_id}|edit|welcome_text")],
        [("Dirección", f"admbiz|{tenant_id}|edit|location_text")],
        [("Link ubicación", f"admbiz|{tenant_id}|edit|location_link")],
        [("FAQ", f"admbiz|{tenant_id}|edit|faq_text")],
        [("Logo", f"admbiz|{tenant_id}|logo")],
        [("⬅️ Volver", f"admbiz|{tenant_id}|panel")],
    ])


def admin_business_logo_kb(tenant_id: str):
    return kb([
        [("🔗 Pegar URL", f"admbiz|{tenant_id}|edit|logo_url")],
        [("📷 Subir foto", f"admbiz|{tenant_id}|logo_upload")],
        [("⬅️ Volver", f"admbiz|{tenant_id}|home")],
    ])


def send_admin_business_home(bot_token: str, chat_id: int, tenant_id: str) -> bool:
    return _safe_send_text(
        bot_token,
        chat_id,
        (
            "🏪 *INFO GENERAL DEL NEGOCIO*\n\n"
            "Desde aquí puedes actualizar la información general visible en la app.\n\n"
            "Elige el campo que quieres editar:"
        ),
        reply_markup=admin_business_home_kb(tenant_id),
        parse_mode="Markdown",
    )


def send_admin_business_logo_menu(bot_token: str, chat_id: int, tenant_id: str) -> bool:
    return _safe_send_text(
        bot_token,
        chat_id,
        (
            "🖼 *LOGO DEL NEGOCIO*\n\n"
            "Puedes pegar una URL pública o subir una foto del logo."
        ),
        reply_markup=admin_business_logo_kb(tenant_id),
        parse_mode="Markdown",
    )


def _field_prompt(field_name: str) -> str:
    label = BUSINESS_FIELD_LABELS.get(field_name, field_name)

    if field_name == "restaurant_name":
        return (
            "🏪 *Editar nombre del negocio*\n\n"
            "Envía el nuevo nombre del negocio."
        )

    if field_name == "location_link":
        return (
            "🔗 *Editar link de ubicación*\n\n"
            "Envía una URL completa `http/https`.\n"
            "Envía `-` para limpiar el campo."
        )

    if field_name == "logo_url":
        return (
            "🖼 *Editar logo del negocio*\n\n"
            "Envía la URL pública completa del logo (`http/https`).\n"
            "Envía `-` para limpiar el campo."
        )

    return (
        f"✏️ *Editar {label.lower()}*\n\n"
        "Envía el nuevo valor.\n"
        "Envía `-` para limpiar el campo."
    )


def handle_admin_business_callback(
    tenant: Dict[str, Any],
    tenant_id: str,
    bot_token: str,
    chat_id: int,
    data: str,
    get_effective_admin_role,
) -> Optional[Dict[str, Any]]:
    if data != "admin_business" and not data.startswith("admbiz|"):
        return None

    assert_admin_authorized(tenant, chat_id, tenant_id)

    sess = get_sess(tenant_id, chat_id)
    tmp = sess.setdefault("tmp", {})

    if data == "admin_business":
        clear_admin_business_state(tmp)
        return {"ok": send_admin_business_home(bot_token, chat_id, tenant_id)}

    parts = data.split("|")
    if len(parts) < 3:
        return {"ok": True}

    cb_tenant_id = parts[1].strip()
    if cb_tenant_id != tenant_id:
        raise HTTPException(status_code=400, detail="Tenant mismatch in admin business callback")

    action = parts[2].strip()

    if action == "home":
        clear_admin_business_state(tmp)
        return {"ok": send_admin_business_home(bot_token, chat_id, tenant_id)}

    if action == "logo":
        clear_admin_business_state(tmp)
        return {"ok": send_admin_business_logo_menu(bot_token, chat_id, tenant_id)}

    if action == "panel":
        clear_admin_business_state(tmp)
        user_role = get_effective_admin_role(tenant, chat_id)
        return {
            "ok": _safe_send_text(
                bot_token,
                chat_id,
                "🧭 PANEL ADMIN\n\nElige una opción:",
                reply_markup=admin_panel_kb(user_role=user_role, tenant=tenant),
            )
        }

    if action == "logo_upload":
        clear_admin_business_state(tmp)
        tmp["admbiz_mode"] = "awaiting_logo_photo"
        return {
            "ok": _safe_send_text(
                bot_token,
                chat_id,
                (
                    "📷 *Subir logo del negocio*\n\n"
                    "Envíame una foto del logo.\n"
                    "Formatos permitidos: JPG, PNG o WEBP.\n"
                    "Tamaño máximo: 5 MB."
                ),
                reply_markup=admin_business_logo_kb(tenant_id),
                parse_mode="Markdown",
            )
        }

    if action == "edit" and len(parts) == 4:
        field_name = parts[3].strip()
        if field_name not in BUSINESS_FIELD_LABELS:
            return {"ok": True}

        clear_admin_business_state(tmp)
        tmp["admbiz_mode"] = "awaiting_text"
        tmp["admbiz_field"] = field_name

        return {
            "ok": _safe_send_text(
                bot_token,
                chat_id,
                _field_prompt(field_name),
                reply_markup=admin_business_home_kb(tenant_id),
                parse_mode="Markdown",
            )
        }

    return {"ok": True}
