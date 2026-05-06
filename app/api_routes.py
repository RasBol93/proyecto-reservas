# app/api_routes.py — optimizado (cache de sheets + menor overhead)

from typing import List, Optional, Dict, Tuple
from urllib.parse import unquote, urlparse

from fastapi import APIRouter, HTTPException, Query, UploadFile, File, Request
from pydantic import BaseModel, Field

from app.config import (
    ENV_R2_PUBLIC_BASE_URL,
    MAX_ITEMS_PER_ORDER,
    MAX_NAME_LEN,
    RL_MENU_PER_MIN,
    RL_CREATE_PER_MIN,
    RL_MARKPAID_PER_MIN,
    env_required,
)
from app.rate_limit import rate_limiter
from app.sheets import get_gspread_client, open_spreadsheet_by_key
from app.tenants import get_tenant_or_404, load_tenants, tenants_cache_info
from app.menu import load_menu_index, group_menu_by_category, calc_total_amount
from app.pickup import generate_public_pickup_slots
from app.config_bundle import load_config_bundle
from app.content import upsert_content_entries

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
    get_order_by_id_strict,
    get_order_context_by_id,
    get_order_context_by_id_strict,
    OrdersReadTemporarilyUnavailable,
)
from app.payment_flow import notify_admin_payment_reported
from app.r2_storage import generate_payment_proof_presigned_upload
from app.webhook_helpers import parse_items_field, fmt_snapshot_lines

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


def _client_rate_limit_identity(request: Request) -> str:
    forwarded_for = str(request.headers.get("x-forwarded-for") or "").strip()
    if forwarded_for:
        first_ip = forwarded_for.split(",")[0].strip()
        if first_ip:
            return first_ip

    client = getattr(request, "client", None)
    client_host = str(getattr(client, "host", "") or "").strip()
    if client_host:
        return client_host

    return "unknown"


def _validate_webapp_payment_proof_url(proof_reference: str) -> str:
    clean_reference = str(proof_reference or "").strip()
    if not clean_reference:
        raise HTTPException(status_code=400, detail="proof_reference is required")

    base_url = env_required(ENV_R2_PUBLIC_BASE_URL).strip().rstrip("/")
    parsed_base = urlparse(base_url)
    parsed_ref = urlparse(clean_reference)

    if not parsed_ref.scheme or not parsed_ref.netloc:
        raise HTTPException(status_code=400, detail="proof_reference must be an absolute URL")

    base_scheme = str(parsed_base.scheme or "").strip().lower()
    ref_scheme = str(parsed_ref.scheme or "").strip().lower()
    if not base_scheme or ref_scheme != base_scheme:
        raise HTTPException(status_code=400, detail="proof_reference has invalid URL scheme")

    base_netloc = str(parsed_base.netloc or "").strip().lower()
    ref_netloc = str(parsed_ref.netloc or "").strip().lower()
    if not base_netloc or ref_netloc != base_netloc:
        raise HTTPException(status_code=400, detail="proof_reference host is not allowed")

    base_path = unquote(str(parsed_base.path or "").strip()).rstrip("/")
    ref_path = unquote(str(parsed_ref.path or "").strip())

    if base_path:
        if ref_path != base_path and not ref_path.startswith(base_path + "/"):
            raise HTTPException(status_code=400, detail="proof_reference path is outside the allowed storage base path")
        relative_path = ref_path[len(base_path):] or "/"
    else:
        relative_path = ref_path or "/"

    if not relative_path.startswith("/payment_proofs/"):
        raise HTTPException(status_code=400, detail="proof_reference must point to a payment proof object")

    return clean_reference


def _normalize_payment_proof_filename(filename: str) -> str:
    clean_filename = str(filename or "").strip()
    if not clean_filename:
        raise HTTPException(status_code=400, detail="filename is required")
    if len(clean_filename) > 180:
        raise HTTPException(status_code=400, detail="filename is too long")
    if "/" in clean_filename or "\\" in clean_filename:
        raise HTTPException(status_code=400, detail="filename must not contain path separators")
    return clean_filename


def _validate_payment_proof_presign_input(filename: str, content_type: str) -> Tuple[str, str]:
    clean_filename = _normalize_payment_proof_filename(filename)
    clean_content_type = str(content_type or "").strip().lower()

    if not clean_content_type:
        raise HTTPException(status_code=400, detail="content_type is required")

    allowed_extensions_by_type = {
        "image/jpeg": {".jpg", ".jpeg"},
        "image/png": {".png"},
        "image/webp": {".webp"},
        "application/pdf": {".pdf"},
    }

    allowed_extensions = {ext for exts in allowed_extensions_by_type.values() for ext in exts}

    if clean_content_type not in allowed_extensions_by_type:
        raise HTTPException(status_code=400, detail="content_type is not allowed")

    dot_idx = clean_filename.rfind(".")
    ext = clean_filename[dot_idx:].lower() if dot_idx > 0 else ""
    if ext not in allowed_extensions:
        raise HTTPException(status_code=400, detail="file extension is not allowed")

    if ext not in allowed_extensions_by_type[clean_content_type]:
        raise HTTPException(status_code=400, detail="file extension does not match content_type")

    return clean_filename, clean_content_type


def _validate_optional_public_url(value: str, *, field_name: str) -> str:
    clean_value = str(value or "").strip()
    if not clean_value:
        return ""

    parsed = urlparse(clean_value)
    scheme = str(parsed.scheme or "").strip().lower()
    netloc = str(parsed.netloc or "").strip().lower()
    if scheme not in {"http", "https"} or not netloc:
        raise HTTPException(status_code=422, detail=f"{field_name} must be an absolute http/https URL")

    return clean_value


def _get_order_for_tenant_or_404(orders_sh, tenant_id: str, order_id: str):
    order = get_order_by_id(orders_sh, order_id)
    if not order:
        raise HTTPException(status_code=404, detail="Order not found")

    order_tenant_id = str(order.get("tenant_id") or "").strip()
    if order_tenant_id != tenant_id:
        raise HTTPException(status_code=404, detail="Order not found")

    return order


def _get_order_context_for_tenant_or_404(orders_sh, tenant_id: str, order_id: str):
    order_ctx = get_order_context_by_id(orders_sh, order_id)
    if not order_ctx:
        raise HTTPException(status_code=404, detail="Order not found")

    order = dict(order_ctx.get("order") or {})
    order_tenant_id = str(order.get("tenant_id") or "").strip()
    if order_tenant_id != tenant_id:
        raise HTTPException(status_code=404, detail="Order not found")

    return order_ctx


def _get_order_for_tenant_or_404_strict(orders_sh, tenant_id: str, order_id: str):
    try:
        order = get_order_by_id_strict(orders_sh, order_id)
    except OrdersReadTemporarilyUnavailable:
        raise HTTPException(status_code=503, detail="Order status temporarily unavailable")

    if not order:
        raise HTTPException(status_code=404, detail="Order not found")

    order_tenant_id = str(order.get("tenant_id") or "").strip()
    if order_tenant_id != tenant_id:
        raise HTTPException(status_code=404, detail="Order not found")

    return order


def _get_order_context_for_tenant_or_404_strict(orders_sh, tenant_id: str, order_id: str):
    try:
        order_ctx = get_order_context_by_id_strict(orders_sh, order_id)
    except OrdersReadTemporarilyUnavailable:
        raise HTTPException(status_code=503, detail="Order status temporarily unavailable")

    if not order_ctx:
        raise HTTPException(status_code=404, detail="Order not found")

    order = dict(order_ctx.get("order") or {})
    order_tenant_id = str(order.get("tenant_id") or "").strip()
    if order_tenant_id != tenant_id:
        raise HTTPException(status_code=404, detail="Order not found")

    return order_ctx


def _safe_float(value, default: float = 0.0) -> float:
    try:
        return float(value)
    except Exception:
        return float(default)


def _safe_int(value, default: int = 0) -> int:
    try:
        return int(value)
    except Exception:
        return int(default)


def _serialize_order_snapshot(order: Dict[str, str]):
    items_snapshot = parse_items_field(order.get("items_snapshot"))
    if items_snapshot:
        detail_lines, total_amount, total_qty = fmt_snapshot_lines(items_snapshot)
        normalized_items = []
        for item in items_snapshot:
            qty = _safe_int(item.get("qty") or 1, 1)
            unit_price = _safe_float(item.get("unit_price") or 0)
            line_total = _safe_float(item.get("line_total") or 0)
            normalized_items.append({
                "sku": str(item.get("sku") or "").strip(),
                "name": str(item.get("name") or item.get("sku") or "").strip(),
                "qty": qty,
                "quantity": qty,
                "unit_price": unit_price,
                "price": unit_price,
                "line_total": line_total,
                "subtotal": line_total,
            })
        return normalized_items, detail_lines, total_amount, total_qty

    items = parse_items_field(order.get("items"))
    normalized_items = []
    total_qty = 0
    for item in items:
        qty = max(1, _safe_int(item.get("qty") or 1, 1))
        sku = str(item.get("sku") or "").strip()
        total_qty += qty
        normalized_items.append({
            "sku": sku,
            "name": sku,
            "qty": qty,
            "quantity": qty,
            "unit_price": 0.0,
            "price": 0.0,
            "line_total": 0.0,
            "subtotal": 0.0,
        })

    detail_lines = "\n".join(
        [f"{it['qty']} x {it['name']}" for it in normalized_items]
    ) or "(vacío)"

    return normalized_items, detail_lines, _safe_float(order.get("total_amount") or 0), total_qty


def _derive_web_order_ui_status(order: Dict[str, str]) -> str:
    status = str(order.get("status") or "").strip().upper()
    has_proof = bool(str(order.get("payment_proof_file_id") or "").strip())

    if status == "PAID":
        return "paid"
    if status == "PENDING_PAYMENT" and has_proof:
        return "pending_payment_review"
    return "pending_payment"


# =========================
# Models
# =========================

class AdminTokenIn(BaseModel):
    token: str


class AdminBusinessInfoIn(BaseModel):
    token: str
    tenant_id: str
    restaurant_name: Optional[str] = None
    welcome_text: Optional[str] = None
    location_text: Optional[str] = None
    location_link: Optional[str] = None
    faq_text: Optional[str] = None


class AdminBusinessInfoOut(BaseModel):
    ok: bool
    tenant_id: str
    updated_keys: List[str]
    values: Dict[str, str]


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
    token: str
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


class OrderStatusItemOut(BaseModel):
    sku: str
    name: str
    qty: int
    quantity: int
    unit_price: float
    price: float
    line_total: float
    subtotal: float


class OrderStatusOut(BaseModel):
    ok: bool
    tenant_id: str
    order_id: str
    status: str
    ui_status: str
    verification_status: str
    is_paid: bool
    payment_confirmed_at: str
    requested_time: str
    currency: str
    total_amount: float
    total_qty: int
    detail_lines: str
    items: List[OrderStatusItemOut]


class PaymentProofUploadOut(BaseModel):
    success: bool
    url: str
    object_key: str


class PaymentProofPresignIn(BaseModel):
    filename: str
    content_type: Optional[str] = ""


class PaymentProofPresignOut(BaseModel):
    success: bool
    upload_url: str
    file_url: str
    object_key: str


# =========================
# Routes
# =========================

@router.post("/admin/reload_tenants")
def admin_reload_tenants(payload: AdminTokenIn):
    require_admin_token(payload.token)
    gc = get_gspread_client()
    load_tenants(gc=gc, force=True)
    return {"ok": True, **tenants_cache_info()}


@router.post("/admin/content/business_info", response_model=AdminBusinessInfoOut)
def admin_update_business_info(payload: AdminBusinessInfoIn):
    require_admin_token(payload.token)
    validate_tenant_id(payload.tenant_id)

    fields_set = getattr(payload, "model_fields_set", None)
    if fields_set is None:
        fields_set = getattr(payload, "__fields_set__", set())
    provided_fields = set(fields_set or set())

    updates = []

    if "restaurant_name" in provided_fields:
        restaurant_name = str(payload.restaurant_name or "").strip()
        if not restaurant_name:
            raise HTTPException(status_code=422, detail="restaurant_name cannot be empty")
        updates.append({
            "key": "restaurant_name",
            "value": restaurant_name,
            "active": True,
        })

    if "welcome_text" in provided_fields:
        welcome_text = str(payload.welcome_text or "").strip()
        updates.append({
            "key": "welcome_text",
            "value": welcome_text,
            "active": bool(welcome_text),
        })

    if "location_text" in provided_fields:
        location_text = str(payload.location_text or "").strip()
        updates.append({
            "key": "location_text",
            "value": location_text,
            "active": bool(location_text),
        })

    if "location_link" in provided_fields:
        location_link = _validate_optional_public_url(
            payload.location_link or "",
            field_name="location_link",
        )
        updates.append({
            "key": "location_link",
            "value": location_link,
            "active": bool(location_link),
        })

    if "faq_text" in provided_fields:
        faq_text = str(payload.faq_text or "").strip()
        updates.append({
            "key": "faq_text",
            "value": faq_text,
            "active": bool(faq_text),
        })

    if not updates:
        raise HTTPException(status_code=400, detail="No business info fields provided")

    gc = get_gspread_client()
    tenant = get_tenant_or_404(payload.tenant_id, gc=gc)
    orders_sh = _get_orders_sheet(gc, tenant["orders_sheet_id"])
    resolved_tenant_id = str(tenant.get("tenant_id") or payload.tenant_id).strip()

    applied = upsert_content_entries(orders_sh, updates)
    return AdminBusinessInfoOut(
        ok=True,
        tenant_id=resolved_tenant_id,
        updated_keys=sorted(list(applied.keys())),
        values=applied,
    )


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


@router.get("/webapp/bootstrap")
def get_webapp_bootstrap(tenant_id: str = Query(...)):
    validate_tenant_id(tenant_id)
    rate_limiter.hit(f"webapp_bootstrap:{tenant_id}", RL_MENU_PER_MIN)

    gc = get_gspread_client()
    tenant = get_tenant_or_404(tenant_id, gc=gc)

    if not tenant.get("orders_enabled", False):
        raise HTTPException(status_code=400, detail="Orders not enabled")

    orders_sh = _get_orders_sheet(gc, tenant["orders_sheet_id"])
    resolved_tenant_id = str(tenant.get("tenant_id") or tenant_id).strip()
    bundle = load_config_bundle(
        tenant_id=resolved_tenant_id,
        gc=gc,
        tenant=tenant,
        orders_sh=orders_sh,
        force=False,
    )

    return {
        "ok": True,
        "tenant_id": resolved_tenant_id,
        "bootstrap": bundle,
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
    require_admin_token(payload.token)
    validate_tenant_id(payload.tenant_id)
    validate_order_id(payload.order_id)
    rate_limiter.hit(f"mark_paid:{payload.tenant_id}", RL_MARKPAID_PER_MIN)

    gc = get_gspread_client()
    tenant = get_tenant_or_404(payload.tenant_id, gc=gc)

    if str(payload.admin_chat_id).strip() != str(tenant.get("admin_chat_id", "")).strip():
        raise HTTPException(status_code=403, detail="Invalid admin_chat_id")

    orders_sh = _get_orders_sheet(gc, tenant["orders_sheet_id"])
    resolved_tenant_id = str(tenant.get("tenant_id") or payload.tenant_id).strip()
    _get_order_for_tenant_or_404(orders_sh, resolved_tenant_id, payload.order_id)

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
    try:
        raise HTTPException(
            status_code=410,
            detail="Legacy payment proof upload is disabled. Use /upload/payment-proof/presign instead.",
        )
    finally:
        if file is not None:
            await file.close()


@router.post("/upload/payment-proof/presign", response_model=PaymentProofPresignOut)
def presign_payment_proof_upload(payload: PaymentProofPresignIn, request: Request):
    client_key = _client_rate_limit_identity(request)
    rate_limiter.hit(
        f"payment_proof_presign:{client_key}",
        RL_MARKPAID_PER_MIN,
    )

    filename, content_type = _validate_payment_proof_presign_input(
        filename=payload.filename,
        content_type=payload.content_type,
    )

    try:
        presigned = generate_payment_proof_presigned_upload(
            filename=filename,
            content_type=content_type,
        )
        return PaymentProofPresignOut(
            success=True,
            upload_url=presigned["upload_url"],
            file_url=presigned["file_url"],
            object_key=presigned["object_key"],
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Could not presign payment proof upload: {e}")


@router.post("/orders/payment_proof", response_model=OrderPaymentProofOut)
def set_order_payment_proof(payload: OrderPaymentProofIn):
    validate_tenant_id(payload.tenant_id)
    validate_order_id(payload.order_id)
    rate_limiter.hit(
        f"payment_proof:{payload.tenant_id}:{payload.order_id}",
        RL_MARKPAID_PER_MIN,
    )

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
    order_ctx = _get_order_context_for_tenant_or_404(orders_sh, resolved_tenant_id, payload.order_id)
    order = dict(order_ctx.get("order") or {})

    order_source = str(order.get("source") or "").strip().lower()
    if order_source not in {"webapp", "api"}:
        raise HTTPException(status_code=400, detail="Order source not supported for this endpoint")

    if clean_proof_type != "external_url":
        raise HTTPException(status_code=400, detail="proof_type must be 'external_url' for webapp/api orders")

    clean_proof_reference = _validate_webapp_payment_proof_url(clean_proof_reference)

    if str(order.get("status") or "").strip().upper() == "PAID":
        raise HTTPException(status_code=409, detail="Order is already paid")

    result = update_order_payment_proof(
        orders_sh=orders_sh,
        order_id=payload.order_id,
        proof_file_id=clean_proof_reference,
        proof_type=clean_proof_type,
        proof_caption=str(payload.proof_caption or "").strip(),
        order_ctx=order_ctx,
    )

    if not result.get("ok"):
        raise HTTPException(
            status_code=502,
            detail=f"Could not persist payment proof: {str(result.get('error') or 'sheet write failed')}",
        )

    if result.get("already_same"):
        return OrderPaymentProofOut(
            ok=True,
            order_id=payload.order_id,
            proof_type=clean_proof_type,
            notified_admin=False,
            verification_status="pending_verification",
        )

    order_with_proof = dict(order)
    order_with_proof["payment_proof_file_id"] = clean_proof_reference
    order_with_proof["payment_proof_type"] = clean_proof_type
    order_with_proof["payment_proof_caption"] = str(payload.proof_caption or "").strip()

    notified_admin = notify_admin_payment_reported(
        tenant=tenant,
        tenant_id=resolved_tenant_id,
        orders_sh=orders_sh,
        order_id=payload.order_id,
        is_reminder=False,
        order=order_with_proof,
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


@router.get("/orders/status", response_model=OrderStatusOut)
def get_order_status(
    tenant_id: str = Query(...),
    order_id: str = Query(...),
):
    validate_tenant_id(tenant_id)
    validate_order_id(order_id)
    rate_limiter.hit(f"order_status:{tenant_id}:{order_id}", RL_MENU_PER_MIN)

    gc = get_gspread_client()
    tenant = get_tenant_or_404(tenant_id, gc=gc)

    if not tenant.get("orders_enabled", False):
        raise HTTPException(status_code=400, detail="Orders not enabled")

    orders_sh = _get_orders_sheet(gc, tenant["orders_sheet_id"])
    resolved_tenant_id = str(tenant.get("tenant_id") or tenant_id).strip()
    order_ctx = _get_order_context_for_tenant_or_404_strict(orders_sh, resolved_tenant_id, order_id)
    order = dict(order_ctx.get("order") or {})
    order_source = str(order.get("source") or "").strip().lower()
    if order_source not in {"webapp", "api"}:
        raise HTTPException(status_code=404, detail="Order not found")

    items, detail_lines, total_amount, total_qty = _serialize_order_snapshot(order)
    raw_status = str(order.get("status") or "").strip().upper()
    ui_status = _derive_web_order_ui_status(order)
    is_paid = raw_status == "PAID"
    verification_status = "confirmed" if is_paid else "pending_verification"

    return OrderStatusOut(
        ok=True,
        tenant_id=resolved_tenant_id,
        order_id=order_id,
        status=raw_status,
        ui_status=ui_status,
        verification_status=verification_status,
        is_paid=is_paid,
        payment_confirmed_at=str(order.get("payment_confirmed_at") or "").strip(),
        requested_time=str(order.get("requested_time") or "").strip(),
        currency=str(order.get("currency") or "BOB").strip() or "BOB",
        total_amount=total_amount,
        total_qty=total_qty,
        detail_lines=detail_lines,
        items=items,
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

    has_payment_proof = bool(str(order.get("payment_proof_file_id") or "").strip())
    if has_payment_proof:
        return OrderReportPaidOut(
            ok=True,
            order_id=payload.order_id,
            notified_admin=False,
            already_paid=False,
        )

    freshest_order = order
    try:
        fresh_order_ctx = _get_order_context_for_tenant_or_404_strict(
            orders_sh,
            resolved_tenant_id,
            payload.order_id,
        )
        fresh_order = dict(fresh_order_ctx.get("order") or {})
        fresh_order_source = str(fresh_order.get("source") or "").strip().lower()
        if fresh_order_source not in {"webapp", "api"}:
            raise HTTPException(status_code=400, detail="Order source not supported for this endpoint")

        fresh_already_paid = str(fresh_order.get("status") or "").strip().upper() == "PAID"
        if fresh_already_paid:
            return OrderReportPaidOut(
                ok=True,
                order_id=payload.order_id,
                notified_admin=False,
                already_paid=True,
            )

        fresh_has_payment_proof = bool(str(fresh_order.get("payment_proof_file_id") or "").strip())
        if fresh_has_payment_proof:
            return OrderReportPaidOut(
                ok=True,
                order_id=payload.order_id,
                notified_admin=False,
                already_paid=False,
            )

        freshest_order = fresh_order
    except HTTPException as e:
        if int(getattr(e, "status_code", 0) or 0) != 503:
            raise

    notified_admin = notify_admin_payment_reported(
        tenant=tenant,
        tenant_id=resolved_tenant_id,
        orders_sh=orders_sh,
        order_id=payload.order_id,
        is_reminder=False,
        order=freshest_order,
    )

    if not notified_admin:
        raise HTTPException(status_code=502, detail="Could not notify admin about reported payment")

    return OrderReportPaidOut(
        ok=True,
        order_id=payload.order_id,
        notified_admin=True,
        already_paid=False,
    )
