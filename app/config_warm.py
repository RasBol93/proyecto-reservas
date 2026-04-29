import threading
import time
from typing import Any, Dict, Optional

from app.admin_settings import get_admin_settings_runtime_status, load_admin_settings
from app.config_bundle import build_config_bundle, get_config_bundle_runtime_status
from app.menu import (
    MENU_CACHE_STALE_WINDOW_SECONDS,
    MENU_CACHE_TTL_SECONDS,
    get_menu_runtime_status,
    load_menu_index,
)
from app.promotions import get_promotions_runtime_status, load_promotions
from app.sheets import get_gspread_client, open_spreadsheet_by_key
from app.tenants import get_tenants_runtime_status, load_tenants
from app.utils import log_event, normalize


AUTO_WARM_SUCCESS_COOLDOWN_SECONDS = 900
AUTO_WARM_FAILURE_COOLDOWN_SECONDS = 60

_AUTO_WARM_STATE_LOCK = threading.Lock()
_AUTO_WARM_TENANT_LOCKS: Dict[str, threading.Lock] = {}
_AUTO_WARM_STATE: Dict[str, Dict[str, Any]] = {}


def _norm_tenant_key(tenant_id: str) -> str:
    return normalize(tenant_id).replace(" ", "")


def _warm_component_result(*, warmed: bool, runtime_status: Optional[Dict[str, Any]] = None, error: str = "") -> Dict[str, Any]:
    status = dict(runtime_status or {})
    snapshot_valid = bool(status.get("snapshot_valid"))
    cache_present = bool(status.get("cache_present"))
    ready_for_serving = bool(status.get("ready_for_serving"))
    if not ready_for_serving:
        ready_for_serving = bool(snapshot_valid or cache_present)

    # Menu expone cache/runtime con shape distinta.
    if "memory_cache_fresh" in status or "memory_cache_age_seconds" in status:
        memory_cache_fresh = bool(status.get("memory_cache_fresh"))
        memory_cache_age_seconds = status.get("memory_cache_age_seconds")
        max_stale_age = MENU_CACHE_TTL_SECONDS + MENU_CACHE_STALE_WINDOW_SECONDS
        memory_cache_usable = memory_cache_fresh
        if not memory_cache_usable and isinstance(memory_cache_age_seconds, int):
            memory_cache_usable = memory_cache_age_seconds <= max_stale_age
        cache_present = bool(memory_cache_usable)
        ready_for_serving = bool(snapshot_valid or memory_cache_usable)

    return {
        "warmed": bool(warmed),
        "error": str(error or "").strip(),
        "last_served_from": str(status.get("last_served_from") or "").strip(),
        "snapshot_valid": snapshot_valid,
        "cache_present": cache_present,
        "ready_for_serving": ready_for_serving,
        "snapshot_age_seconds": status.get("snapshot_age_seconds"),
        "snapshot_path": str(status.get("snapshot_path") or "").strip(),
        "details": status,
    }


def _build_component_results(
    *,
    tenant_id: str,
    gc=None,
    tenant: Optional[Dict[str, Any]] = None,
    orders_sh=None,
) -> Dict[str, Dict[str, Any]]:
    return {
        "tenants": _warm_component_result(warmed=False, runtime_status=get_tenants_runtime_status()),
        "admin_settings": _warm_component_result(warmed=False, runtime_status=get_admin_settings_runtime_status(orders_sh)),
        "promotions": _warm_component_result(warmed=False, runtime_status=get_promotions_runtime_status(orders_sh)),
        "menu": _warm_component_result(warmed=False, runtime_status=get_menu_runtime_status(orders_sh)),
        "config_bundle": _warm_component_result(
            warmed=False,
            runtime_status=get_config_bundle_runtime_status(
                tenant_id=tenant_id,
                gc=gc,
                tenant=tenant,
                orders_sh=orders_sh,
            ),
        ),
    }


def _component_lists(component_results: Dict[str, Dict[str, Any]]) -> tuple[list[str], list[str]]:
    ready_components = [
        name for name, result in component_results.items()
        if bool(result.get("ready_for_serving"))
    ]
    failed_components = [
        name for name, result in component_results.items()
        if not bool(result.get("warmed"))
    ]
    return ready_components, failed_components


def _set_auto_warm_state(tenant_key: str, *, ready: bool) -> None:
    with _AUTO_WARM_STATE_LOCK:
        _AUTO_WARM_STATE[tenant_key] = {
            "last_finished_at": time.time(),
            "ready": bool(ready),
        }


def _get_auto_warm_lock(tenant_key: str) -> threading.Lock:
    with _AUTO_WARM_STATE_LOCK:
        lock = _AUTO_WARM_TENANT_LOCKS.get(tenant_key)
        if lock is None:
            lock = threading.Lock()
            _AUTO_WARM_TENANT_LOCKS[tenant_key] = lock
        return lock


def warm_tenant_config(
    *,
    tenant_id: str,
    gc=None,
    force: bool = True,
    tenant: Optional[Dict[str, Any]] = None,
    orders_sh=None,
    trigger: str = "manual",
) -> Dict[str, Any]:
    clean_tenant_id = str(tenant_id or "").strip()
    tenant_key = _norm_tenant_key(clean_tenant_id)
    gc = gc or get_gspread_client()

    tenants_result: Dict[str, Any]
    admin_settings_result: Dict[str, Any]
    promotions_result: Dict[str, Any]
    menu_result: Dict[str, Any]
    config_bundle_result: Dict[str, Any]

    loaded_tenants: Dict[str, Dict[str, Any]] = {}
    try:
        loaded_tenants = load_tenants(gc=gc, force=force)
        tenants_result = _warm_component_result(
            warmed=True,
            runtime_status=get_tenants_runtime_status(),
        )
    except Exception as e:
        tenants_result = _warm_component_result(
            warmed=False,
            runtime_status=get_tenants_runtime_status(),
            error=str(e),
        )

    resolved_tenant = tenant if isinstance(tenant, dict) else None
    tenant_error = ""

    try:
        if resolved_tenant is None:
            resolved_tenant = loaded_tenants.get(tenant_key)
            if resolved_tenant is None:
                loaded_tenants = load_tenants(gc=gc, force=False)
                resolved_tenant = loaded_tenants.get(tenant_key)
            if resolved_tenant is None:
                raise RuntimeError(f"Tenant not found or inactive: {clean_tenant_id}")

        if orders_sh is None:
            orders_sheet_id = str(resolved_tenant.get("orders_sheet_id") or "").strip()
            if not orders_sheet_id:
                raise RuntimeError("orders_sheet_id missing for tenant")
            orders_sh = open_spreadsheet_by_key(gc, orders_sheet_id)
    except Exception as e:
        tenant_error = str(e)

    if orders_sh is None:
        admin_settings_result = _warm_component_result(warmed=False, error=tenant_error)
        promotions_result = _warm_component_result(warmed=False, error=tenant_error)
        menu_result = _warm_component_result(warmed=False, error=tenant_error)
        config_bundle_result = _warm_component_result(warmed=False, error=tenant_error)
    else:
        try:
            load_admin_settings(orders_sh, force=force)
            admin_settings_result = _warm_component_result(
                warmed=True,
                runtime_status=get_admin_settings_runtime_status(orders_sh),
            )
        except Exception as e:
            admin_settings_result = _warm_component_result(
                warmed=False,
                runtime_status=get_admin_settings_runtime_status(orders_sh),
                error=str(e),
            )

        try:
            load_promotions(orders_sh, force=force)
            promotions_result = _warm_component_result(
                warmed=True,
                runtime_status=get_promotions_runtime_status(orders_sh),
            )
        except Exception as e:
            promotions_result = _warm_component_result(
                warmed=False,
                runtime_status=get_promotions_runtime_status(orders_sh),
                error=str(e),
            )

        try:
            load_menu_index(orders_sh, force=force)
            menu_result = _warm_component_result(
                warmed=True,
                runtime_status=get_menu_runtime_status(orders_sh),
            )
        except Exception as e:
            menu_result = _warm_component_result(
                warmed=False,
                runtime_status=get_menu_runtime_status(orders_sh),
                error=str(e),
            )

        try:
            build_config_bundle(
                tenant_id=clean_tenant_id,
                gc=gc,
                tenant=resolved_tenant,
                orders_sh=orders_sh,
                force=force,
            )
            config_bundle_result = _warm_component_result(
                warmed=True,
                runtime_status=get_config_bundle_runtime_status(
                    tenant_id=clean_tenant_id,
                    gc=gc,
                    tenant=resolved_tenant,
                    orders_sh=orders_sh,
                ),
            )
        except Exception as e:
            config_bundle_result = _warm_component_result(
                warmed=False,
                runtime_status=get_config_bundle_runtime_status(
                    tenant_id=clean_tenant_id,
                    gc=gc,
                    tenant=resolved_tenant,
                    orders_sh=orders_sh,
                ),
                error=str(e),
            )

    component_results = {
        "tenants": tenants_result,
        "admin_settings": admin_settings_result,
        "promotions": promotions_result,
        "menu": menu_result,
        "config_bundle": config_bundle_result,
    }

    ok = all(bool(result.get("warmed")) for result in component_results.values())
    ready_components, failed_components = _component_lists(component_results)
    all_components_ready = len(ready_components) == len(component_results)

    _set_auto_warm_state(tenant_key, ready=all_components_ready)

    try:
        log_event(
            "config_warm_completed",
            trigger=str(trigger or "").strip() or "manual",
            tenant_id=clean_tenant_id,
            resolved_tenant_id=str((resolved_tenant or {}).get("tenant_id") or clean_tenant_id),
            all_components_warmed=ok,
            all_components_ready=all_components_ready,
            ready_components=ready_components,
            failed_components=failed_components,
        )
    except Exception:
        pass

    return {
        "ok": ok,
        "requested_tenant_id": clean_tenant_id,
        "resolved_tenant_id": str((resolved_tenant or {}).get("tenant_id") or clean_tenant_id),
        "all_components_warmed": ok,
        "all_components_ready": all_components_ready,
        "ready_components": ready_components,
        "failed_components": failed_components,
        **component_results,
    }


def maybe_auto_warm_tenant_config(
    *,
    tenant_id: str,
    gc=None,
    tenant: Optional[Dict[str, Any]] = None,
    orders_sh=None,
    trigger: str,
) -> Dict[str, Any]:
    clean_tenant_id = str(tenant_id or "").strip()
    tenant_key = _norm_tenant_key(clean_tenant_id)
    if not tenant_key:
        return {"action": "skipped_invalid"}

    now_ts = time.time()
    if orders_sh is not None:
        component_results = _build_component_results(
            tenant_id=clean_tenant_id,
            gc=gc,
            tenant=tenant,
            orders_sh=orders_sh,
        )
        ready_components, _ = _component_lists(component_results)
        if len(ready_components) == len(component_results):
            _set_auto_warm_state(tenant_key, ready=True)
            try:
                log_event(
                    "auto_warm_skipped_ready",
                    trigger=str(trigger or "").strip(),
                    tenant_id=clean_tenant_id,
                    ready_components=ready_components,
                )
            except Exception:
                pass
            return {"action": "skipped_ready", "ready_components": ready_components}

    with _AUTO_WARM_STATE_LOCK:
        state = dict(_AUTO_WARM_STATE.get(tenant_key) or {})

    last_finished_at = float(state.get("last_finished_at") or 0)
    last_ready = bool(state.get("ready"))
    if orders_sh is None and last_ready and last_finished_at > 0 and (now_ts - last_finished_at) <= AUTO_WARM_SUCCESS_COOLDOWN_SECONDS:
        try:
            log_event(
                "auto_warm_skipped_ready",
                trigger=str(trigger or "").strip(),
                tenant_id=clean_tenant_id,
                cooldown_seconds=AUTO_WARM_SUCCESS_COOLDOWN_SECONDS,
                age_seconds=max(0, int(now_ts - last_finished_at)),
            )
        except Exception:
            pass
        return {"action": "skipped_ready"}

    if (not last_ready) and last_finished_at > 0 and (now_ts - last_finished_at) <= AUTO_WARM_FAILURE_COOLDOWN_SECONDS:
        try:
            log_event(
                "auto_warm_skipped_cooldown",
                trigger=str(trigger or "").strip(),
                tenant_id=clean_tenant_id,
                cooldown_seconds=AUTO_WARM_FAILURE_COOLDOWN_SECONDS,
                age_seconds=max(0, int(now_ts - last_finished_at)),
                reason="recent_failure",
            )
        except Exception:
            pass
        return {"action": "skipped_cooldown"}

    lock = _get_auto_warm_lock(tenant_key)
    if not lock.acquire(blocking=False):
        try:
            log_event(
                "auto_warm_skipped_cooldown",
                trigger=str(trigger or "").strip(),
                tenant_id=clean_tenant_id,
                cooldown_seconds=AUTO_WARM_FAILURE_COOLDOWN_SECONDS,
                reason="lock_busy",
            )
        except Exception:
            pass
        return {"action": "skipped_cooldown"}

    try:
        try:
            log_event(
                "auto_warm_started",
                trigger=str(trigger or "").strip(),
                tenant_id=clean_tenant_id,
            )
        except Exception:
            pass

        result = warm_tenant_config(
            tenant_id=clean_tenant_id,
            gc=gc,
            force=True,
            tenant=tenant,
            orders_sh=orders_sh,
            trigger=f"auto:{trigger}",
        )

        try:
            log_event(
                "auto_warm_completed",
                trigger=str(trigger or "").strip(),
                tenant_id=clean_tenant_id,
                all_components_warmed=bool(result.get("all_components_warmed")),
                all_components_ready=bool(result.get("all_components_ready")),
                ready_components=result.get("ready_components") or [],
                failed_components=result.get("failed_components") or [],
            )
        except Exception:
            pass
        return {"action": "completed", "result": result}
    except Exception as e:
        _set_auto_warm_state(tenant_key, ready=False)
        try:
            log_event(
                "auto_warm_completed",
                trigger=str(trigger or "").strip(),
                tenant_id=clean_tenant_id,
                all_components_warmed=False,
                all_components_ready=False,
                ready_components=[],
                failed_components=["tenants", "admin_settings", "promotions", "menu", "config_bundle"],
                error=str(e),
            )
        except Exception:
            pass
        return {"action": "failed", "error": str(e)}
    finally:
        lock.release()
