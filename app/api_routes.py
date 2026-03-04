# app/api_routes.py

from typing import List, Optional

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field

from app.config import (
    MAX_ITEMS_PER_ORDER,
    MAX_NAME_LEN,
    RL_MENU_PER_MIN,
    RL_CREATE_PER_MIN,
    RL_MARKPAID_PER_MIN,
)
from app.rate_limit import rate_limiter
from app.sheets import get_gspread_client, open_spreadsheet_by_key
from app.tenants import get_tenant_or_404, load_tenants, tenants_cache_info
from app.menu import load_menu_index, group_menu_by_category, calc_total_amount

# =========================
# Orders imports (BLINDADO)
# =========================
# Render a veces queda corriendo una versión vieja de app/orders.py.
# Esto evita que el deploy muera solo por un rename de función.
try:
    from app.orders import append_order_row  # versión nueva
except Exception:
    try:
        from app.orders import append_order as append_order_row  # versión vieja (si existía)
    except Exception as e:
        raise ImportError(
            "No se pudo importar append_order_row ni append_order desde app.orders. "
            "Revisa que el archivo app/orders.py en el deploy sea el correcto."
        ) from e

from app.orders import update_order_status, gen_order_id

from app.validators import (
    validate_tenant_id,
    validate_order_id,
    validate_contact,
    validate_delivery_type,
    validate_requested_time,
    validate_source,
)
from app.admin_auth import require_admin_token

router = APIRouter()


# =========================
# Models
# =========================

class AdminTokenIn(BaseModel):
    token: str = Field(..., min_length=1)


class OrderItem(BaseModel):
    sku: str = Field(..., min_length=1)
    qty: int = Field(..., ge=1)


class OrderCreateIn(BaseModel):
    tenant_id: str
    customer_name: str
    customer_contact: str
    items: List[OrderItem]
    delivery_type: Optional[str] = "pickup"
    requested_time: Optional[str] = "ahora"
    source: Optional[str] = "api"


class OrderCreateOut(BaseModel):
    ok: bool
    order_id: str
    total_amount: float
    currency: str = "BOB"


class MarkPaidIn(BaseModel):
    tenant_id: str
    order_id: str
    admin_chat_id: str


class MarkPaidOut(BaseModel):
    ok: bool
    order_id: str
    status: str
    old_status: Optional[str] = None
    already_paid: Optional[bool] = None


# =========================
# Routes
# =========================

@router.post("/admin/reload_tenants")
def admin_reload_tenants(payload: AdminTokenIn):
    require_admin_token(payload.token)

    gc = get_gspread_client()
    load_tenants(gc=gc, force=True)
    return {"ok": True, **tenants_cache_info()}


@router.get("/menu")
def get_menu(tenant_id: str = Query(..., description="tenant_id, ej: resto_demo")):
    validate_tenant_id(tenant_id)
    rate_limiter.hit(f"menu:{tenant_id}", RL_MENU_PER_MIN)

    gc = get_gspread_client()
    tenant = get_tenant_or_404(tenant_id, gc=gc)

    if not tenant.get("orders_enabled", False):
        raise HTTPException(status_code=400, detail=f"Orders not enabled for tenant: {tenant_id}")

    orders_sh = open_spreadsheet_by_key(gc, tenant["orders_sheet_id"])
    menu_idx = load_menu_index(orders_sh)
    categories = group_menu_by_category(menu_idx)

    return {"ok": True, "tenant_id": tenant_id, "categories": categories}


@router.post("/orders/create", response_model=OrderCreateOut)
def create_order(payload: OrderCreateIn):
    validate_tenant_id(payload.tenant_id)
    rate_limiter.hit(f"create:{payload.tenant_id}", RL_CREATE_PER_MIN)

    gc = get_gspread_client()
    tenant = get_tenant_or_404(payload.tenant_id, gc=gc)

    if not tenant.get("orders_enabled", False):
        raise HTTPException(status_code=400, detail=f"Orders not enabled for tenant: {payload.tenant_id}")

    name = (payload.customer_name or "").strip()
    if not name or len(name) > MAX_NAME_LEN:
        raise HTTPException(status_code=422, detail="customer_name missing or too long")

    contact = (payload.customer_contact or "").strip()
    validate_contact(contact)

    if not payload.items or len(payload.items) > MAX_ITEMS_PER_ORDER:
        raise HTTPException(status_code=422, detail=f"items must be 1..{MAX_ITEMS_PER_ORDER}")

    delivery_type = validate_delivery_type(payload.delivery_type or "pickup")
    requested_time = validate_requested_time(payload.requested_time or "ahora")
    source = validate_source(payload.source or "api")

    orders_sh = open_spreadsheet_by_key(gc, tenant["orders_sheet_id"])
    menu_idx = load_menu_index(orders_sh)

    items_list = [{"sku": it.sku.strip(), "qty": int(it.qty)} for it in payload.items]
    total_amount = calc_total_amount(items_list, menu_idx)

    order_id = gen_order_id()

    append_order_row(
        orders_sh=orders_sh,
        tenant_id=payload.tenant_id,
        order_id=order_id,
        customer_name=name,
        customer_contact=contact,
        items=items_list,
        delivery_type=delivery_type,
        requested_time=requested_time,
        status="PENDING_PAYMENT",
        source=source,
        total_amount=total_amount,
    )

    return OrderCreateOut(ok=True, order_id=order_id, total_amount=total_amount, currency="BOB")


@router.post("/orders/mark_paid", response_model=MarkPaidOut)
def mark_paid(payload: MarkPaidIn):
    validate_tenant_id(payload.tenant_id)
    validate_order_id(payload.order_id)
    rate_limiter.hit(f"mark_paid:{payload.tenant_id}", RL_MARKPAID_PER_MIN)

    gc = get_gspread_client()
    tenant = get_tenant_or_404(payload.tenant_id, gc=gc)

    expected_admin_chat_id = str(tenant.get("admin_chat_id", "")).strip()
    if not expected_admin_chat_id:
        raise HTTPException(status_code=500, detail="admin_chat_id is not set for this tenant")

    if str(payload.admin_chat_id).strip() != expected_admin_chat_id:
        raise HTTPException(status_code=403, detail="Invalid admin_chat_id for this tenant")

    orders_sh = open_spreadsheet_by_key(gc, tenant["orders_sheet_id"])

    result = update_order_status(orders_sh, payload.order_id, "PAID")
    if not result.get("found"):
        raise HTTPException(status_code=404, detail=f"Order not found: {payload.order_id}")

    old_status = str(result.get("old_status", "") or "")
    already_paid = (old_status.strip().upper() == "PAID")

    return MarkPaidOut(
        ok=True,
        order_id=payload.order_id,
        status="PAID",
        old_status=old_status,
        already_paid=already_paid,
    )
