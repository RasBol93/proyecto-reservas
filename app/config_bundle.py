from __future__ import annotations

import copy
import importlib
import json
import re
import time
from datetime import timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from fastapi import HTTPException

from app.admin_settings import (
    get_admin_setting_value,
    get_admin_settings_runtime_status,
    load_admin_settings,
)
from app.menu import get_menu_runtime_status, load_menu_index, resolve_effective_category_order
from app.pickup import _get_today_business_window, get_pickup_config
from app.promotions import get_promotions_runtime_status, load_promotions
from app.sheets import get_gspread_client, note_sheets_serving_source, open_spreadsheet_by_key
from app.tenants import get_tenant_or_404, get_tenants_runtime_status, load_tenants
from app.utils import log_event, normalize


CONFIG_BUNDLE_CACHE_TTL_SECONDS = 900
CONFIG_BUNDLE_SNAPSHOT_MAX_AGE_SECONDS = 86400
CONFIG_BUNDLE_SNAPSHOT_VERSION = 1
CONFIG_BUNDLE_SNAPSHOT_DIRNAME = ".config_bundle_snapshots"


_CONFIG_BUNDLE_CACHE: Dict[str, Tuple[float, Dict[str, Any]]] = {}
_CONFIG_BUNDLE_LAST_SERVE_SOURCE: Dict[str, str] = {}
_CONFIG_BUNDLE_LAST_SNAPSHOT_REJECTION_REASON: Dict[str, str] = {}

_OPTIONAL_CONTENT_MODULE_LOADED = False
_OPTIONAL_CONTENT_MODULE = None


def _norm_tenant_id(tenant_id: Any) -> str:
    return normalize(tenant_id).replace(" ", "")


def _safe_str(value: Any) -> str:
    return str(value or "").strip()


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except Exception:
        return float(default)


def _project_root() -> Path:
    return Path(__file__).resolve().parent.parent


def _snapshot_dir() -> Path:
    return _project_root() / CONFIG_BUNDLE_SNAPSHOT_DIRNAME


def _safe_path_key(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]", "_", str(value or "").strip()) or "unknown"


def _spreadsheet_id_from_orders_sh(orders_sh) -> str:
    try:
        sid = getattr(orders_sh, "id", None)
        if sid:
            return str(sid).strip()
    except Exception:
        pass
    return str(id(orders_sh))


def _cache_key(tenant_id: str, orders_sh) -> str:
    clean_tenant_id = _norm_tenant_id(tenant_id) or "unknown_tenant"
    spreadsheet_id = _spreadsheet_id_from_orders_sh(orders_sh) or "unknown_spreadsheet"
    return f"{clean_tenant_id}__{spreadsheet_id}"


def _snapshot_path(cache_key: str) -> Path:
    return _snapshot_dir() / f"{_safe_path_key(cache_key)}.json"


def _read_snapshot_payload(path: Path) -> Optional[Dict[str, Any]]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None

    if not isinstance(payload, dict):
        return None

    return payload


def _is_bundle_shape_valid(bundle: Any) -> bool:
    if not isinstance(bundle, dict):
        return False

    required_top_level = [
        "tenant_id",
        "version",
        "generated_at",
        "source",
        "tenant",
        "content",
        "admin_settings",
        "payment_info",
        "menu",
        "open_status",
    ]
    if not all(key in bundle for key in required_top_level):
        return False

    if not isinstance(bundle.get("tenant"), dict):
        return False
    if not isinstance(bundle.get("content"), list):
        return False
    if not isinstance(bundle.get("admin_settings"), dict):
        return False
    if not isinstance(bundle.get("payment_info"), dict):
        return False
    if not isinstance(bundle.get("menu"), list):
        return False
    if not isinstance(bundle.get("open_status"), dict):
        return False

    return True


def _load_config_bundle_snapshot(
    cache_key: str,
    *,
    tenant_id: str,
    spreadsheet_id: str,
) -> Optional[Tuple[float, Dict[str, Any]]]:
    path = _snapshot_path(cache_key)
    if not path.exists():
        return None

    payload = _read_snapshot_payload(path)
    if payload is None:
        return None

    if int(payload.get("version") or 0) != CONFIG_BUNDLE_SNAPSHOT_VERSION:
        return None

    if _safe_str(payload.get("tenant_id")) != _norm_tenant_id(tenant_id):
        return None

    if _safe_str(payload.get("spreadsheet_id")) != _safe_str(spreadsheet_id):
        return None

    try:
        generated_at_ts = float(payload.get("generated_at_ts") or 0)
    except Exception:
        return None

    if generated_at_ts <= 0:
        return None

    if (time.time() - generated_at_ts) > CONFIG_BUNDLE_SNAPSHOT_MAX_AGE_SECONDS:
        return None

    bundle = payload.get("bundle")
    if not _is_bundle_shape_valid(bundle):
        return None

    return generated_at_ts, bundle


def _persist_config_bundle_snapshot(
    cache_key: str,
    bundle: Dict[str, Any],
    *,
    tenant_id: str,
    spreadsheet_id: str,
    ts: Optional[float] = None,
) -> None:
    snapshot_ts = float(ts if ts is not None else time.time())
    payload = {
        "version": CONFIG_BUNDLE_SNAPSHOT_VERSION,
        "tenant_id": _norm_tenant_id(tenant_id),
        "spreadsheet_id": _safe_str(spreadsheet_id),
        "generated_at_ts": snapshot_ts,
        "bundle": bundle,
    }

    snapshot_dir = _snapshot_dir()
    snapshot_dir.mkdir(parents=True, exist_ok=True)

    path = _snapshot_path(cache_key)
    temp_path = path.with_suffix(path.suffix + ".tmp")
    temp_path.write_text(
        json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
        encoding="utf-8",
    )
    temp_path.replace(path)


def _set_last_bundle_serve_source(cache_key: str, source: str) -> None:
    _CONFIG_BUNDLE_LAST_SERVE_SOURCE[cache_key] = _safe_str(source)


def _set_last_bundle_snapshot_rejection_reason(cache_key: str, reason: str) -> None:
    _CONFIG_BUNDLE_LAST_SNAPSHOT_REJECTION_REASON[cache_key] = _safe_str(reason)


def _clone_bundle(bundle: Dict[str, Any]) -> Dict[str, Any]:
    return copy.deepcopy(bundle)


def _bundle_for_serving_source(bundle: Dict[str, Any], source: str) -> Dict[str, Any]:
    served = _clone_bundle(bundle)
    served["serving_source"] = _safe_str(source) or "unknown"
    return served


def _note_bundle_serving_source(source: str) -> None:
    clean_source = _safe_str(source)
    if not clean_source:
        return
    note_sheets_serving_source(f"config_bundle:{clean_source}")


def _inspect_config_bundle_snapshot(cache_key: str, *, tenant_id: str, spreadsheet_id: str) -> Dict[str, Any]:
    path = _snapshot_path(cache_key)
    payload = _read_snapshot_payload(path) if path.exists() else None
    snapshot_valid = _load_config_bundle_snapshot(
        cache_key,
        tenant_id=tenant_id,
        spreadsheet_id=spreadsheet_id,
    ) is not None

    age_seconds: Optional[int] = None
    snapshot_tenant_id = ""
    snapshot_spreadsheet_id = ""
    generated_at_ts: Optional[float] = None
    rejection_reason = ""

    if payload is None:
        rejection_reason = "invalid_shape" if path.exists() else "missing"
    else:
        snapshot_tenant_id = _safe_str(payload.get("tenant_id"))
        snapshot_spreadsheet_id = _safe_str(payload.get("spreadsheet_id"))
        try:
            generated_at_ts = float(payload.get("generated_at_ts") or 0)
        except Exception:
            generated_at_ts = None

        if generated_at_ts and generated_at_ts > 0:
            try:
                age_seconds = max(0, int(time.time() - generated_at_ts))
            except Exception:
                age_seconds = None

        if not snapshot_valid:
            if int(payload.get("version") or 0) != CONFIG_BUNDLE_SNAPSHOT_VERSION:
                rejection_reason = "invalid_version"
            elif snapshot_tenant_id != _norm_tenant_id(tenant_id) or snapshot_spreadsheet_id != _safe_str(spreadsheet_id):
                rejection_reason = "id_mismatch"
            elif generated_at_ts is None or generated_at_ts <= 0:
                rejection_reason = "invalid_shape"
            elif age_seconds is not None and age_seconds > CONFIG_BUNDLE_SNAPSHOT_MAX_AGE_SECONDS:
                rejection_reason = "too_old"
            elif not _is_bundle_shape_valid(payload.get("bundle")):
                rejection_reason = "invalid_shape"

    return {
        "snapshot_path": str(path),
        "snapshot_exists": path.exists(),
        "snapshot_valid": bool(snapshot_valid),
        "snapshot_age_seconds": age_seconds,
        "tenant_id": _norm_tenant_id(tenant_id),
        "spreadsheet_id": _safe_str(spreadsheet_id),
        "snapshot_tenant_id": snapshot_tenant_id,
        "snapshot_spreadsheet_id": snapshot_spreadsheet_id,
        "snapshot_rejection_reason": rejection_reason,
    }


def _cache_get(cache_key: str) -> Optional[Dict[str, Any]]:
    cached = _CONFIG_BUNDLE_CACHE.get(cache_key)
    if not cached:
        return None

    ts, bundle = cached
    if (time.time() - ts) <= CONFIG_BUNDLE_CACHE_TTL_SECONDS:
        return bundle

    return None


def _cache_set(cache_key: str, bundle: Dict[str, Any], *, ts: Optional[float] = None) -> None:
    _CONFIG_BUNDLE_CACHE[cache_key] = (float(ts if ts is not None else time.time()), _clone_bundle(bundle))


def _resolve_bundle_context(
    *,
    tenant_id: str,
    gc=None,
    tenant: Optional[Dict[str, Any]] = None,
    orders_sh=None,
    force: bool = False,
) -> Tuple[Any, Dict[str, Any], Any, str, str]:
    clean_tenant_id = _safe_str(tenant_id)
    norm_tenant_id = _norm_tenant_id(clean_tenant_id)
    if not norm_tenant_id:
        raise HTTPException(status_code=400, detail="tenant_id is required")

    gc = gc or get_gspread_client()
    resolved_tenant = dict(tenant or {}) if isinstance(tenant, dict) else None

    if force and resolved_tenant is None:
        load_tenants(gc=gc, force=True)

    if resolved_tenant is None:
        resolved_tenant = get_tenant_or_404(clean_tenant_id, gc=gc)

    resolved_tenant_id = _safe_str(resolved_tenant.get("tenant_id")) or norm_tenant_id
    orders_sheet_id = _safe_str(resolved_tenant.get("orders_sheet_id"))
    if not orders_sheet_id:
        raise RuntimeError("orders_sheet_id missing for tenant")

    resolved_orders_sh = orders_sh if orders_sh is not None else open_spreadsheet_by_key(gc, orders_sheet_id)
    spreadsheet_id = _spreadsheet_id_from_orders_sh(resolved_orders_sh)

    return gc, resolved_tenant, resolved_orders_sh, resolved_tenant_id, spreadsheet_id


def _get_optional_content_module():
    global _OPTIONAL_CONTENT_MODULE_LOADED, _OPTIONAL_CONTENT_MODULE

    if _OPTIONAL_CONTENT_MODULE_LOADED:
        return _OPTIONAL_CONTENT_MODULE

    _OPTIONAL_CONTENT_MODULE_LOADED = True
    try:
        _OPTIONAL_CONTENT_MODULE = importlib.import_module("app.content")
    except Exception as e:
        _OPTIONAL_CONTENT_MODULE = None
        try:
            log_event(
                "config_bundle_optional_content_unavailable",
                error_type=type(e).__name__,
                error=str(e),
            )
        except Exception:
            pass

    return _OPTIONAL_CONTENT_MODULE


def _get_content_value(content_map: Dict[str, Any], key: str, default: str = "") -> str:
    module = _get_optional_content_module()
    getter = getattr(module, "get_content_value", None) if module is not None else None
    if callable(getter):
        try:
            return _safe_str(getter(content_map, key, default))
        except Exception:
            pass

    normalized_key = normalize(key)
    if isinstance(content_map, dict):
        return _safe_str(content_map.get(normalized_key, default))
    return _safe_str(default)


def _safe_load_content_map(orders_sh, *, force: bool = False) -> Dict[str, str]:
    module = _get_optional_content_module()
    loader = getattr(module, "load_content_map", None) if module is not None else None
    if callable(loader):
        try:
            content_map = loader(orders_sh, force=force)
            if isinstance(content_map, dict):
                return {normalize(k): _safe_str(v) for k, v in content_map.items()}
        except Exception as e:
            try:
                log_event(
                    "config_bundle_content_fallback",
                    error_type=type(e).__name__,
                    error=str(e),
                )
            except Exception:
                pass
    return {}


def _drive_file_id_from_url(url: str) -> Optional[str]:
    if not url:
        return None
    match = re.search(r"/file/d/([a-zA-Z0-9_-]+)", url)
    if match:
        return match.group(1)
    match = re.search(r"[?&]id=([a-zA-Z0-9_-]+)", url)
    if match:
        return match.group(1)
    return None


def _normalize_public_qr_url(url: str) -> str:
    clean_url = _safe_str(url)
    if not clean_url:
        return ""
    file_id = _drive_file_id_from_url(clean_url)
    if file_id:
        return f"https://drive.google.com/uc?export=download&id={file_id}"
    return clean_url


def _build_tenant_payload(tenant: Dict[str, Any], content_map: Dict[str, str]) -> Dict[str, Any]:
    restaurant_name = (
        _get_content_value(content_map, "restaurant_name")
        or _safe_str(tenant.get("restaurant_name"))
        or _safe_str(tenant.get("name"))
        or _safe_str(tenant.get("tenant_id"))
    )

    payload: Dict[str, Any] = {
        "tenant_id": _safe_str(tenant.get("tenant_id")),
        "restaurant_name": restaurant_name,
        "timezone": _safe_str(tenant.get("timezone")) or "America/La_Paz",
        "currency": _safe_str(tenant.get("currency")) or "BOB",
    }

    logo_url = _get_content_value(content_map, "logo_url") or _safe_str(tenant.get("logo_url"))
    cover_image_url = _get_content_value(content_map, "cover_image_url") or _safe_str(tenant.get("cover_image_url"))
    if logo_url:
        payload["logo_url"] = logo_url
    if cover_image_url:
        payload["cover_image_url"] = cover_image_url

    branding: Dict[str, str] = {}
    primary_color = _safe_str(tenant.get("primary_color"))
    accent_color = _safe_str(tenant.get("accent_color"))
    surface_color = _safe_str(tenant.get("surface_color"))
    if primary_color:
        branding["primaryColor"] = primary_color
    if accent_color:
        branding["accentColor"] = accent_color
    if surface_color:
        branding["surfaceColor"] = surface_color
    if branding:
        payload["branding"] = branding

    if _safe_str(tenant.get("name")):
        payload["name"] = _safe_str(tenant.get("name"))

    return payload


def _content_block(key: str, value: str, active: bool) -> Dict[str, Any]:
    return {
        "key": key,
        "value": _safe_str(value),
        "active": bool(active),
    }


def _build_content_payload(tenant: Dict[str, Any], content_map: Dict[str, str]) -> List[Dict[str, Any]]:
    restaurant_name = (
        _get_content_value(content_map, "restaurant_name")
        or _safe_str(tenant.get("name"))
        or _safe_str(tenant.get("tenant_id"))
    )

    welcome_text = _get_content_value(content_map, "welcome_text")
    location_text = _get_content_value(content_map, "location_text")
    location_link = _get_content_value(content_map, "location_link")
    faq_text = _get_content_value(content_map, "faq_text")
    survey_text = _get_content_value(content_map, "survey_text")

    if not welcome_text:
        welcome_text = f"Bienvenido a {restaurant_name}."

    fallback_blocks = [
        ("restaurant_name", restaurant_name, True),
        ("welcome_text", welcome_text, True),
        ("location_text", location_text, bool(location_text)),
        ("location_link", location_link, bool(location_link)),
        ("faq_text", faq_text, bool(faq_text)),
        ("survey_text", survey_text, bool(survey_text)),
    ]

    return [_content_block(key, value, active) for key, value, active in fallback_blocks]


def _map_weekday_code_to_frontend(day_code: str) -> str:
    mapping = {
        "lun": "monday",
        "mar": "tuesday",
        "mie": "wednesday",
        "jue": "thursday",
        "vie": "friday",
        "sab": "saturday",
        "dom": "sunday",
    }
    clean_code = _safe_str(day_code).lower()
    return mapping.get(clean_code, clean_code)


def _map_today_mode_to_frontend(today_mode: str) -> str:
    clean_mode = _safe_str(today_mode).lower()
    if clean_mode == "closed_today":
        return "closed"
    if clean_mode == "closed_now":
        return "temporary_closed"
    if clean_mode == "open_now":
        return "special_hours"
    return "regular"


def _build_admin_settings_payload(settings_map: Dict[str, Dict[str, Any]]) -> Dict[str, Any]:
    pickup_interval_minutes = 15
    try:
        pickup_interval_minutes = max(
            1,
            int(get_admin_setting_value(settings_map, "pickup_interval_minutes", "15") or 15),
        )
    except Exception:
        pickup_interval_minutes = 15

    weekly_open_days_raw = settings_map.get("weekly_open_days", {})
    weekly_open_days_value = _safe_str(weekly_open_days_raw.get("value"))
    weekly_open_days = [
        _map_weekday_code_to_frontend(part)
        for part in [p.strip() for p in weekly_open_days_value.split(",") if p.strip()]
    ]

    weekly_slot_mode_value = _safe_str(get_admin_setting_value(settings_map, "weekly_slot_mode", "1"))
    weekly_slot_mode = "split" if weekly_slot_mode_value == "2" else "single"

    return {
        "weekly_open_days": weekly_open_days,
        "weekly_slot_mode": weekly_slot_mode,
        "weekly_slot1_open": _safe_str(get_admin_setting_value(settings_map, "weekly_slot1_open", "11:00")) or None,
        "weekly_slot1_close": _safe_str(get_admin_setting_value(settings_map, "weekly_slot1_close", "23:00")) or None,
        "weekly_slot2_open": _safe_str(get_admin_setting_value(settings_map, "weekly_slot2_open", "")) or None,
        "weekly_slot2_close": _safe_str(get_admin_setting_value(settings_map, "weekly_slot2_close", "")) or None,
        "today_mode": _map_today_mode_to_frontend(get_admin_setting_value(settings_map, "today_mode", "habitual")),
        "today_date": _safe_str(get_admin_setting_value(settings_map, "today_date", "")) or None,
        "today_closed_message": _safe_str(get_admin_setting_value(settings_map, "today_closed_message", "")) or None,
        "temp_closed_message": _safe_str(get_admin_setting_value(settings_map, "today_temporal_close_message", "")) or None,
        "prep_time_min": pickup_interval_minutes,
        "interval_horarios_recog_minutos": pickup_interval_minutes,
        "pickup_interval_minutes": pickup_interval_minutes,
    }


def _build_payment_info_payload(tenant: Dict[str, Any], content_map: Dict[str, str]) -> Dict[str, Any]:
    qr_image_url = (
        _normalize_public_qr_url(_safe_str(tenant.get("payment_qr_url")))
        or _normalize_public_qr_url(_safe_str(tenant.get("payment_qr_link")))
    )

    instructions = _get_content_value(content_map, "payment_instructions")
    if not instructions:
        if qr_image_url:
            instructions = "Escanea el QR o realiza la transferencia y sube tu comprobante antes de tocar 'Ya pague'."
        else:
            instructions = "Realiza el pago y sube tu comprobante antes de tocar 'Ya pague'."

    payload: Dict[str, Any] = {
        "instructions": instructions,
    }

    if qr_image_url:
        payload["qr_image_url"] = qr_image_url

    reference_label = _get_content_value(content_map, "payment_reference_label")
    reference_value = _get_content_value(content_map, "payment_reference_value")
    if reference_label:
        payload["reference_label"] = reference_label
    if reference_value:
        payload["reference_value"] = reference_value

    return payload


def _build_menu_payload(
    menu_idx: Dict[str, Dict[str, Any]],
    orders_sh=None,
    *,
    content_map: Optional[Dict[str, str]] = None,
) -> List[Dict[str, Any]]:
    items: List[Dict[str, Any]] = []
    category_names_in_order: List[str] = []
    seen_categories = set()

    for item in menu_idx.values():
        category_name = _safe_str(item.get("category")) or "Otros"
        category_key = normalize(category_name)
        if category_key and category_key not in seen_categories:
            seen_categories.add(category_key)
            category_names_in_order.append(category_name)

        row: Dict[str, Any] = {
            "sku": _safe_str(item.get("sku")),
            "name": _safe_str(item.get("name")),
            "price": _safe_float(item.get("price"), 0.0),
            "active": True,
            "category": category_name,
        }

        photo_url = _safe_str(item.get("photo_url"))
        if photo_url:
            row["photo_url"] = photo_url

        description = _safe_str(item.get("description")) or _safe_str(item.get("promo_description"))
        if description:
            row["description"] = description

        items.append(row)

    ordered_category_names = resolve_effective_category_order(
        category_names_in_order,
        orders_sh=orders_sh,
        content_map=content_map,
    )
    order_index = {normalize(cat_name): idx for idx, cat_name in enumerate(ordered_category_names)}

    items.sort(
        key=lambda x: (
            order_index.get(normalize(x.get("category", "")), len(order_index)),
            normalize(x.get("category", "")),
            normalize(x.get("name", "")),
        )
    )
    return items


def _build_pickup_base_response(ctx: Dict[str, Any], interval: int) -> Dict[str, Any]:
    return {
        "open_time": ctx.get("open_time"),
        "close_time": ctx.get("close_time"),
        "last_order_time": ctx.get("last_order_time"),
        "pickup_interval_minutes": interval,
    }


def _build_public_pickup_payload_from_settings(
    orders_sh,
    *,
    tenant_tz: str,
    settings_map: Dict[str, Dict[str, Any]],
) -> Dict[str, Any]:
    cfg = get_pickup_config(orders_sh, settings_map=settings_map)
    ctx = _get_today_business_window(orders_sh, tenant_tz, settings_map=settings_map)

    try:
        interval = max(1, int(cfg.get("pickup_interval_minutes") or 15))
    except Exception:
        interval = 15

    base_response = _build_pickup_base_response(ctx, interval)
    if not ctx.get("accepts_orders_now"):
        return {
            "ok": False,
            "message": _safe_str(ctx.get("public_message")) or "No estamos recibiendo pedidos en este momento.",
            "slots": [],
            **base_response,
        }

    current = ctx["now"] + timedelta(minutes=interval)
    if ctx.get("last_dt") and current > ctx["last_dt"]:
        return {
            "ok": False,
            "message": "Ya no estamos aceptando pedidos hoy.",
            "slots": [],
            **base_response,
        }

    slots = []
    while True:
        if ctx.get("last_dt") and current > ctx["last_dt"]:
            break

        hhmm = current.strftime("%H:%M")
        slots.append({
            "label": hhmm,
            "hhmm": hhmm,
        })
        current += timedelta(minutes=interval)

    return {
        "ok": bool(slots),
        "message": "Elige una hora de recojo:" if slots else "No hay horarios disponibles.",
        "slots": slots,
        **base_response,
    }


def _normalize_pickup_slot_option(slot: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    value = _safe_str(slot.get("value")) or _safe_str(slot.get("hhmm"))
    if not value:
        return None

    return {
        "value": value,
        "label": _safe_str(slot.get("label")) or value,
        "hhmm": _safe_str(slot.get("hhmm")) or value,
        "is_asap": bool(slot.get("is_asap")),
    }


def _build_open_status_payload(pickup_payload: Dict[str, Any]) -> Dict[str, Any]:
    options = []
    for slot in pickup_payload.get("slots") or []:
        if not isinstance(slot, dict):
            continue
        option = _normalize_pickup_slot_option(slot)
        if option:
            options.append(option)

    hours_label = "Horarios sujetos a disponibilidad"
    open_time = _safe_str(pickup_payload.get("open_time"))
    close_time = _safe_str(pickup_payload.get("close_time"))
    if open_time and close_time:
        hours_label = f"{open_time} - {close_time}"

    message = _safe_str(pickup_payload.get("message"))
    if not message:
        message = (
            "Elige una hora de recojo disponible."
            if options
            else "No hay horarios de pickup disponibles."
        )

    return {
        "can_place_order": bool(options),
        "is_open_now": bool(options),
        "closed_now": not bool(options),
        "closed_today": not bool(options),
        "message": message,
        "today_hours_label": hours_label,
        "pickup_slots": [str(option["value"]) for option in options],
        "pickup_slot_options": options,
    }


def build_config_bundle(
    tenant_id: str,
    gc=None,
    tenant: Optional[Dict[str, Any]] = None,
    orders_sh=None,
    force: bool = False,
) -> Dict[str, Any]:
    gc, resolved_tenant, resolved_orders_sh, resolved_tenant_id, spreadsheet_id = _resolve_bundle_context(
        tenant_id=tenant_id,
        gc=gc,
        tenant=tenant,
        orders_sh=orders_sh,
        force=force,
    )

    cache_key = _cache_key(resolved_tenant_id, resolved_orders_sh)
    _set_last_bundle_snapshot_rejection_reason(cache_key, "")

    settings_map = load_admin_settings(resolved_orders_sh, force=force)
    load_promotions(resolved_orders_sh, force=force)
    menu_idx = load_menu_index(resolved_orders_sh, force=force)
    content_map = _safe_load_content_map(resolved_orders_sh, force=force)
    pickup_payload = _build_public_pickup_payload_from_settings(
        resolved_orders_sh,
        settings_map=settings_map,
        tenant_tz=_safe_str(resolved_tenant.get("timezone")) or "America/La_Paz",
    )

    generated_at_ts = time.time()
    generated_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(generated_at_ts))

    bundle = {
        "tenant_id": resolved_tenant_id,
        "version": CONFIG_BUNDLE_SNAPSHOT_VERSION,
        "generated_at": generated_at,
        "source": "bundle",
        "serving_source": "sheets",
        "tenant": _build_tenant_payload(resolved_tenant, content_map),
        "content": _build_content_payload(resolved_tenant, content_map),
        "admin_settings": _build_admin_settings_payload(settings_map),
        "payment_info": _build_payment_info_payload(resolved_tenant, content_map),
        "menu": _build_menu_payload(menu_idx, resolved_orders_sh, content_map=content_map),
        "open_status": _build_open_status_payload(pickup_payload),
        "metadata": {
            "tenant_id": resolved_tenant_id,
            "spreadsheet_id": spreadsheet_id,
            "generated_at_ts": generated_at_ts,
        },
    }

    _cache_set(cache_key, bundle, ts=generated_at_ts)
    _set_last_bundle_serve_source(cache_key, "sheets")
    _note_bundle_serving_source("sheets")

    try:
        _persist_config_bundle_snapshot(
            cache_key,
            bundle,
            tenant_id=resolved_tenant_id,
            spreadsheet_id=spreadsheet_id,
            ts=generated_at_ts,
        )
    except Exception as e:
        try:
            log_event(
                "config_bundle_snapshot_write_failed",
                tenant_id=resolved_tenant_id,
                cache_key=cache_key,
                error_type=type(e).__name__,
                error=str(e),
            )
        except Exception:
            pass

    try:
        log_event(
            "config_bundle_built",
            tenant_id=resolved_tenant_id,
            cache_key=cache_key,
            spreadsheet_id=spreadsheet_id,
            force=bool(force),
            menu_items=len(bundle.get("menu") or []),
            content_blocks=len(bundle.get("content") or []),
        )
    except Exception:
        pass

    return _bundle_for_serving_source(bundle, "sheets")


def load_config_bundle(
    tenant_id: str,
    gc=None,
    tenant: Optional[Dict[str, Any]] = None,
    orders_sh=None,
    force: bool = False,
) -> Dict[str, Any]:
    gc, resolved_tenant, resolved_orders_sh, resolved_tenant_id, spreadsheet_id = _resolve_bundle_context(
        tenant_id=tenant_id,
        gc=gc,
        tenant=tenant,
        orders_sh=orders_sh,
        force=False,
    )
    cache_key = _cache_key(resolved_tenant_id, resolved_orders_sh)
    _set_last_bundle_snapshot_rejection_reason(cache_key, "")

    if force:
        invalidate_config_bundle(tenant_id=resolved_tenant_id, orders_sh=resolved_orders_sh)
        return build_config_bundle(
            tenant_id=resolved_tenant_id,
            gc=gc,
            tenant=resolved_tenant,
            orders_sh=resolved_orders_sh,
            force=True,
        )

    cached = _cache_get(cache_key)
    if cached is not None:
        _set_last_bundle_serve_source(cache_key, "memory")
        _note_bundle_serving_source("memory")
        try:
            log_event(
                "config_bundle_served_from_memory",
                tenant_id=resolved_tenant_id,
                cache_key=cache_key,
            )
        except Exception:
            pass
        return _bundle_for_serving_source(cached, "memory")

    snapshot_info = _inspect_config_bundle_snapshot(
        cache_key,
        tenant_id=resolved_tenant_id,
        spreadsheet_id=spreadsheet_id,
    )
    rejection_reason = _safe_str(snapshot_info.get("snapshot_rejection_reason"))
    if rejection_reason:
        _set_last_bundle_snapshot_rejection_reason(cache_key, rejection_reason)
        try:
            log_event(
                "config_bundle_snapshot_rejected",
                tenant_id=resolved_tenant_id,
                cache_key=cache_key,
                reason=rejection_reason,
                snapshot_path=snapshot_info.get("snapshot_path"),
                snapshot_exists=bool(snapshot_info.get("snapshot_exists")),
                snapshot_age_seconds=snapshot_info.get("snapshot_age_seconds"),
                spreadsheet_id=spreadsheet_id,
            )
        except Exception:
            pass

    snapshot_cached = _load_config_bundle_snapshot(
        cache_key,
        tenant_id=resolved_tenant_id,
        spreadsheet_id=spreadsheet_id,
    )
    if snapshot_cached is not None:
        snapshot_ts, snapshot_bundle = snapshot_cached
        _cache_set(cache_key, snapshot_bundle, ts=snapshot_ts)
        _set_last_bundle_serve_source(cache_key, "snapshot")
        _note_bundle_serving_source("snapshot")
        try:
            log_event(
                "config_bundle_served_from_snapshot",
                tenant_id=resolved_tenant_id,
                cache_key=cache_key,
                snapshot_age_seconds=max(0, int(time.time() - snapshot_ts)),
                snapshot_max_age_seconds=CONFIG_BUNDLE_SNAPSHOT_MAX_AGE_SECONDS,
            )
        except Exception:
            pass
        return _bundle_for_serving_source(snapshot_bundle, "snapshot")

    return build_config_bundle(
        tenant_id=resolved_tenant_id,
        gc=gc,
        tenant=resolved_tenant,
        orders_sh=resolved_orders_sh,
        force=True,
    )


def _delete_snapshot_file(path: Path) -> None:
    try:
        path.unlink(missing_ok=True)
    except Exception:
        pass


def invalidate_config_bundle(tenant_id: Optional[str] = None, orders_sh=None) -> None:
    if tenant_id is None and orders_sh is None:
        _CONFIG_BUNDLE_CACHE.clear()
        _CONFIG_BUNDLE_LAST_SERVE_SOURCE.clear()
        _CONFIG_BUNDLE_LAST_SNAPSHOT_REJECTION_REASON.clear()
        try:
            snapshot_dir = _snapshot_dir()
            if snapshot_dir.exists():
                for path in snapshot_dir.glob("*.json"):
                    _delete_snapshot_file(path)
        except Exception:
            pass
        return

    target_tenant_id = _norm_tenant_id(tenant_id) if tenant_id is not None else ""
    target_spreadsheet_id = _spreadsheet_id_from_orders_sh(orders_sh) if orders_sh is not None else ""

    cache_keys_to_delete: List[str] = []
    for cache_key in list(_CONFIG_BUNDLE_CACHE.keys()):
        tenant_part, _, spreadsheet_part = cache_key.partition("__")
        if target_tenant_id and target_spreadsheet_id:
            if tenant_part == target_tenant_id and spreadsheet_part == target_spreadsheet_id:
                cache_keys_to_delete.append(cache_key)
        elif target_tenant_id:
            if tenant_part == target_tenant_id:
                cache_keys_to_delete.append(cache_key)
        elif target_spreadsheet_id:
            if spreadsheet_part == target_spreadsheet_id:
                cache_keys_to_delete.append(cache_key)

    for cache_key in cache_keys_to_delete:
        _CONFIG_BUNDLE_CACHE.pop(cache_key, None)
        _CONFIG_BUNDLE_LAST_SERVE_SOURCE.pop(cache_key, None)
        _CONFIG_BUNDLE_LAST_SNAPSHOT_REJECTION_REASON.pop(cache_key, None)
        _delete_snapshot_file(_snapshot_path(cache_key))

    if not cache_keys_to_delete:
        snapshot_dir = _snapshot_dir()
        if snapshot_dir.exists():
            if target_tenant_id and target_spreadsheet_id:
                _delete_snapshot_file(_snapshot_path(f"{target_tenant_id}__{target_spreadsheet_id}"))
            elif target_tenant_id:
                safe_prefix = _safe_path_key(f"{target_tenant_id}__")
                for path in snapshot_dir.glob(f"{safe_prefix}*.json"):
                    _delete_snapshot_file(path)
            elif target_spreadsheet_id:
                safe_suffix = _safe_path_key(target_spreadsheet_id)
                for path in snapshot_dir.glob(f"*__{safe_suffix}.json"):
                    _delete_snapshot_file(path)


def get_config_bundle_runtime_status(
    tenant_id: str,
    gc=None,
    tenant: Optional[Dict[str, Any]] = None,
    orders_sh=None,
) -> Dict[str, Any]:
    try:
        _, resolved_tenant, resolved_orders_sh, resolved_tenant_id, spreadsheet_id = _resolve_bundle_context(
            tenant_id=tenant_id,
            gc=gc,
            tenant=tenant,
            orders_sh=orders_sh,
            force=False,
        )
        cache_key = _cache_key(resolved_tenant_id, resolved_orders_sh)
    except Exception as e:
        clean_tenant_id = _norm_tenant_id(tenant_id)
        return {
            "tenant_id": clean_tenant_id,
            "snapshot_path": "",
            "snapshot_exists": False,
            "snapshot_valid": False,
            "snapshot_age_seconds": None,
            "spreadsheet_id": "",
            "cache_present": False,
            "cache_age_seconds": None,
            "last_served_from": "",
            "last_snapshot_rejection_reason": "",
            "snapshot_rejection_reason": "",
            "ready_for_serving": False,
            "ttl_seconds": CONFIG_BUNDLE_CACHE_TTL_SECONDS,
            "snapshot_max_age_seconds": CONFIG_BUNDLE_SNAPSHOT_MAX_AGE_SECONDS,
            "resolve_error": str(e),
        }

    snapshot_info = _inspect_config_bundle_snapshot(
        cache_key,
        tenant_id=resolved_tenant_id,
        spreadsheet_id=spreadsheet_id,
    )

    cache_present = cache_key in _CONFIG_BUNDLE_CACHE
    cache_age_seconds: Optional[int] = None
    if cache_present:
        try:
            cache_age_seconds = max(0, int(time.time() - _CONFIG_BUNDLE_CACHE[cache_key][0]))
        except Exception:
            cache_age_seconds = None

    ready_for_serving = bool(cache_present or snapshot_info.get("snapshot_valid"))

    return {
        **snapshot_info,
        "tenant_id": resolved_tenant_id,
        "resolved_tenant_name": _safe_str(resolved_tenant.get("name")),
        "cache_present": cache_present,
        "cache_age_seconds": cache_age_seconds,
        "last_served_from": _safe_str(_CONFIG_BUNDLE_LAST_SERVE_SOURCE.get(cache_key)),
        "last_snapshot_rejection_reason": _safe_str(_CONFIG_BUNDLE_LAST_SNAPSHOT_REJECTION_REASON.get(cache_key)),
        "ready_for_serving": ready_for_serving,
        "ttl_seconds": CONFIG_BUNDLE_CACHE_TTL_SECONDS,
        "snapshot_max_age_seconds": CONFIG_BUNDLE_SNAPSHOT_MAX_AGE_SECONDS,
        "component_runtime": {
            "tenants": get_tenants_runtime_status(),
            "admin_settings": get_admin_settings_runtime_status(resolved_orders_sh),
            "promotions": get_promotions_runtime_status(resolved_orders_sh),
            "menu": get_menu_runtime_status(resolved_orders_sh),
        },
    }
