# app/api_routes.py — optimizado (cache de sheets + menor overhead)

from typing import List, Optional, Dict

from fastapi import APIRouter, HTTPException, Query, UploadFile, File
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
from app.pickup import generate_public_pickup_slots

try:
    from app.orders import append_order_row
except Exception:
    try:
        from app.orders import append_order as append_order_row
    except Exception as e:
        raise ImportError("Error importing order writer") from e

from app.orders import (
    update_order_status,
    update_order_payment_proof,
    gen_order_id,
    build_items_snapshot,
    get_order_by_id,
)
from app.payment_flow import notify_admin_payment_reported
from app.r2_storage import upload_payment_proof_fileobj

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


# -------------------------
# CACHE DE SPREADSHEETS
# -------------------------

_SHEET_CACHE: Dict[str, object] = {}


def _get_orders_sheet(gc, sheet_id):
    if sheet_id in _SHEET_CACHE:
        return _SHEET_CACHE[sheet_id]

    sh = open_spreadsheet_by_key(gc, sheet_id)
    _SHEET_CACHE[sheet_id] = sh
    return sh


def _serialize_pickup_slots(slots):
    serialized = []
    for slot in slots or []:
        hhmm = str(slot.get("hhmm") or "").strip()
        label = str(slot.get("label") or hhmm).strip()

        if not hhmm:
            continue

        serialized.append({
            "value": hhmm,
            "label": label,
        })

    return serialized


def _get_order_for_tenant_or_404(orders_sh, tenant_id: str, order_id: str):
    order = get_order_by_id(orders_sh, order_id)
    if not order:
        raise HTTPException(status_code=404, detail="Order not found")

    order_tenant_id = str(order.get("tenant_id") or "").strip()
    if order_tenant_id != tenant_id:
        raise HTTPException(status_code=404, detail="Order not found")

    return order


# =========================
# Models
# =========================

class AdminTokenIn(BaseModel):
    token: str


class OrderItem(BaseModel):
    sku: str
    qty: int


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


class OrderReportPaidIn(BaseModel):
    tenant_id: str
    order_id: str


class OrderReportPaidOut(BaseModel):
    ok: bool
    order_id: str
    notified_admin: bool
    already_paid: Optional[bool] = None


class OrderPaymentProofIn(BaseModel):
    tenant_id: str
    order_id: str
    proof_type: str
    proof_reference: str
    proof_caption: Optional[str] = ""


class OrderPaymentProofOut(BaseModel):
    ok: bool
    order_id: str
    proof_type: str
    notified_admin: bool
    verification_status: str


class PaymentProofUploadOut(BaseModel):
    success: bool
    url: str


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
def get_menu(tenant_id: str = Query(...)):
    validate_tenant_id(tenant_id)
    rate_limiter.hit(f"menu:{tenant_id}", RL_MENU_PER_MIN)

    gc = get_gspread_client()
    tenant = get_tenant_or_404(tenant_id, gc=gc)

    if not tenant.get("orders_enabled", False):
        raise HTTPException(status_code=400, detail="Orders not enabled")

    orders_sh = _get_orders_sheet(gc, tenant["orders_sheet_id"])

    menu_idx = load_menu_index(orders_sh)
    categories = group_menu_by_category(menu_idx)

    return {"ok": True, "tenant_id": tenant_id, "categories": categories}


@router.get("/pickup/slots")
def get_pickup_slots(tenant_id: str = Query(...)):
    validate_tenant_id(tenant_id)
    rate_limiter.hit(f"pickup_slots:{tenant_id}", RL_MENU_PER_MIN)

    gc = get_gspread_client()
    tenant = get_tenant_or_404(tenant_id, gc=gc)

    if not tenant.get("orders_enabled", False):
        raise HTTPException(status_code=400, detail="Orders not enabled")

    orders_sh = _get_orders_sheet(gc, tenant["orders_sheet_id"])
    tenant_tz = str(tenant.get("timezone") or "America/La_Paz").strip() or "America/La_Paz"

    pickup_data = generate_public_pickup_slots(orders_sh=orders_sh, tenant_tz=tenant_tz)

    return {
        "ok": bool(pickup_data.get("ok")),
        "tenant_id": tenant.get("tenant_id") or tenant_id,
        "message": str(pickup_data.get("message") or ""),
        "slots": _serialize_pickup_slots(pickup_data.get("slots") or []),
        "pickup_interval_minutes": int(pickup_data.get("pickup_interval_minutes") or 0),
        "open_time": str(pickup_data.get("open_time") or ""),
        "close_time": str(pickup_data.get("close_time") or ""),
        "last_order_time": str(pickup_data.get("last_order_time") or ""),
    }


@router.post("/orders/create", response_model=OrderCreateOut)
def create_order(payload: OrderCreateIn):
    validate_tenant_id(payload.tenant_id)
    rate_limiter.hit(f"create:{payload.tenant_id}", RL_CREATE_PER_MIN)

    gc = get_gspread_client()
    tenant = get_tenant_or_404(payload.tenant_id, gc=gc)

    if not tenant.get("orders_enabled", False):
        raise HTTPException(status_code=400, detail="Orders not enabled")

    name = (payload.customer_name or "").strip()
    if not name or len(name) > MAX_NAME_LEN:
        raise HTTPException(status_code=422, detail="Invalid name")

    contact = (payload.customer_contact or "").strip()
    validate_contact(contact)

    if not payload.items or len(payload.items) > MAX_ITEMS_PER_ORDER:
        raise HTTPException(status_code=422, detail="Invalid items")

    delivery_type = validate_delivery_type(payload.delivery_type or "pickup")
    requested_time = validate_requested_time(payload.requested_time or "ahora")

    raw_source = str(payload.source or "").strip().lower()
    if not raw_source or raw_source == "api":
        raw_source = "webapp"
    source = validate_source(raw_source)

    orders_sh = _get_orders_sheet(gc, tenant["orders_sheet_id"])

    menu_idx = load_menu_index(orders_sh)

    items_list = [{"sku": it.sku.strip(), "qty": int(it.qty)} for it in payload.items]
    total_amount = calc_total_amount(items_list, menu_idx)
    items_snapshot = build_items_snapshot(items_list, menu_idx)

    order_id = gen_order_id()
    resolved_tenant_id = str(tenant.get("tenant_id") or payload.tenant_id).strip()

    result = append_order_row(
        orders_sh=orders_sh,
        tenant_id=resolved_tenant_id,
        order_id=order_id,
        customer_name=name,
        customer_contact=contact,
        items=items_list,
        items_snapshot=items_snapshot,
        delivery_type=delivery_type,
        requested_time=requested_time,
        status="PENDING_PAYMENT",
        source=source,
        total_amount=total_amount,
    )

    if not result.get("ok"):
        raise HTTPException(
            status_code=502,
            detail=f"Could not persist order: {str(result.get('error') or 'sheet write failed')}",
        )

    return OrderCreateOut(ok=True, order_id=order_id, total_amount=total_amount)


@router.post("/orders/mark_paid", response_model=MarkPaidOut)
def mark_paid(payload: MarkPaidIn):
    validate_tenant_id(payload.tenant_id)
    validate_order_id(payload.order_id)
    rate_limiter.hit(f"mark_paid:{payload.tenant_id}", RL_MARKPAID_PER_MIN)

    gc = get_gspread_client()
    tenant = get_tenant_or_404(payload.tenant_id, gc=gc)

    if str(payload.admin_chat_id).strip() != str(tenant.get("admin_chat_id", "")).strip():
        raise HTTPException(status_code=403, detail="Invalid admin_chat_id")

    orders_sh = _get_orders_sheet(gc, tenant["orders_sheet_id"])

    result = update_order_status(orders_sh, payload.order_id, "PAID")

    if not result.get("found"):
        raise HTTPException(status_code=404, detail="Order not found")

    return MarkPaidOut(
        ok=True,
        order_id=payload.order_id,
        status="PAID",
    )


@router.post("/upload/payment-proof", response_model=PaymentProofUploadOut)
async def upload_payment_proof(file: UploadFile = File(...)):
    if file is None:
        raise HTTPException(status_code=400, detail="file is required")

    try:
        uploaded = upload_payment_proof_fileobj(
            fileobj=file.file,
            filename=str(file.filename or "").strip(),
            content_type=str(file.content_type or "").strip(),
        )
        return PaymentProofUploadOut(success=True, url=uploaded["url"])
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Could not upload payment proof: {e}")
    finally:
        await file.close()


@router.post("/orders/payment_proof", response_model=OrderPaymentProofOut)
def set_order_payment_proof(payload: OrderPaymentProofIn):
    validate_tenant_id(payload.tenant_id)
    validate_order_id(payload.order_id)

    clean_proof_type = str(payload.proof_type or "").strip().lower()
    if clean_proof_type not in {"photo", "document", "external_url"}:
        raise HTTPException(status_code=400, detail="proof_type must be one of ['document', 'external_url', 'photo']")

    clean_proof_reference = str(payload.proof_reference or "").strip()
    if not clean_proof_reference:
        raise HTTPException(status_code=400, detail="proof_reference is required")

    gc = get_gspread_client()
    tenant = get_tenant_or_404(payload.tenant_id, gc=gc)

    if not tenant.get("orders_enabled", False):
        raise HTTPException(status_code=400, detail="Orders not enabled")

    orders_sh = _get_orders_sheet(gc, tenant["orders_sheet_id"])
    resolved_tenant_id = str(tenant.get("tenant_id") or payload.tenant_id).strip()
    order = _get_order_for_tenant_or_404(orders_sh, resolved_tenant_id, payload.order_id)

    order_source = str(order.get("source") or "").strip().lower()
    if order_source not in {"webapp", "api"}:
        raise HTTPException(status_code=400, detail="Order source not supported for this endpoint")

    if str(order.get("status") or "").strip().upper() == "PAID":
        raise HTTPException(status_code=409, detail="Order is already paid")

    result = update_order_payment_proof(
        orders_sh=orders_sh,
        order_id=payload.order_id,
        proof_file_id=clean_proof_reference,
        proof_type=clean_proof_type,
        proof_caption=str(payload.proof_caption or "").strip(),
    )

    if not result.get("ok"):
        raise HTTPException(
            status_code=502,
            detail=f"Could not persist payment proof: {str(result.get('error') or 'sheet write failed')}",
        )

    notified_admin = notify_admin_payment_reported(
        tenant=tenant,
        tenant_id=resolved_tenant_id,
        orders_sh=orders_sh,
        order_id=payload.order_id,
        is_reminder=False,
    )

    if not notified_admin:
        raise HTTPException(status_code=502, detail="Payment proof saved but could not notify admin")

    return OrderPaymentProofOut(
        ok=True,
        order_id=payload.order_id,
        proof_type=clean_proof_type,
        notified_admin=True,
        verification_status="pending_verification",
    )


@router.post("/orders/report_paid", response_model=OrderReportPaidOut)
def report_order_paid(payload: OrderReportPaidIn):
    validate_tenant_id(payload.tenant_id)
    validate_order_id(payload.order_id)
    rate_limiter.hit(f"report_paid:{payload.tenant_id}", RL_MARKPAID_PER_MIN)

    gc = get_gspread_client()
    tenant = get_tenant_or_404(payload.tenant_id, gc=gc)

    if not tenant.get("orders_enabled", False):
        raise HTTPException(status_code=400, detail="Orders not enabled")

    orders_sh = _get_orders_sheet(gc, tenant["orders_sheet_id"])
    resolved_tenant_id = str(tenant.get("tenant_id") or payload.tenant_id).strip()
    order = _get_order_for_tenant_or_404(orders_sh, resolved_tenant_id, payload.order_id)

    order_source = str(order.get("source") or "").strip().lower()
    if order_source not in {"webapp", "api"}:
        raise HTTPException(status_code=400, detail="Order source not supported for this endpoint")

    already_paid = str(order.get("status") or "").strip().upper() == "PAID"
    if already_paid:
        return OrderReportPaidOut(
            ok=True,
            order_id=payload.order_id,
            notified_admin=False,
            already_paid=True,
        )

    notified_admin = notify_admin_payment_reported(
        tenant=tenant,
        tenant_id=resolved_tenant_id,
        orders_sh=orders_sh,
        order_id=payload.order_id,
        is_reminder=False,
    )

    if not notified_admin:
        raise HTTPException(status_code=502, detail="Could not notify admin about reported payment")

    return OrderReportPaidOut(
        ok=True,
        order_id=payload.order_id,
        notified_admin=True,
        already_paid=False,
    )
