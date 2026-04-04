# app/admin_survey_runtime.py

from typing import Any, Dict

from app.telegram_keyboard import kb
from app.survey import append_response, generate_coupon_if_applicable


def survey_runtime_stars_kb(prefix: str = "stars") -> Dict[str, Any]:
    return kb([
        [("⭐", f"{prefix}|1"), ("⭐⭐", f"{prefix}|2"), ("⭐⭐⭐", f"{prefix}|3")],
        [("⭐⭐⭐⭐", f"{prefix}|4"), ("⭐⭐⭐⭐⭐", f"{prefix}|5")],
    ])


def clear_admin_survey_runtime(tmp: Dict[str, Any]) -> None:
    keys = list(tmp.keys())
    for k in keys:
        if str(k).startswith("admin_survey_"):
            tmp.pop(k, None)


def finalize_admin_survey_runtime(
    tenant: Dict[str, Any],
    tenant_id: str,
    tmp: Dict[str, Any],
    orders_sh,
) -> Dict[str, Any]:
    """
    Finaliza encuesta embebida en admin.
    NO cambia comportamiento, solo centraliza.
    """

    try:
        responses = tmp.get("admin_survey_responses") or {}
        contact = tmp.get("admin_survey_contact") or ""

        if not responses:
            return {
                "text": "⚠️ No hay respuestas de encuesta.",
            }

        # guardar respuesta
        append_response(
            orders_sh=orders_sh,
            tenant_id=tenant_id,
            contact=contact,
            responses=responses,
        )

        # cupón (si aplica)
        coupon = generate_coupon_if_applicable(
            orders_sh=orders_sh,
            tenant_id=tenant_id,
            contact=contact,
        )

        clear_admin_survey_runtime(tmp)

        txt = "✅ Encuesta registrada correctamente."

        if coupon:
            txt += f"\n\n🎁 Cupón generado: {coupon}"

        return {"text": txt}

    except Exception as e:
        return {
            "text": "⚠️ Error registrando encuesta.",
        }
