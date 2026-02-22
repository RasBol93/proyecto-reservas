# app/validators.py

import re
from fastapi import HTTPException


# =========================
# Tenant
# =========================

def validate_tenant_id(tenant_id: str) -> None:
    if not tenant_id:
        raise HTTPException(status_code=400, detail="tenant_id is required")

    tenant_id = tenant_id.strip()

    if len(tenant_id) < 3:
        raise HTTPException(status_code=400, detail="tenant_id too short")

    if not re.match(r"^[a-zA-Z0-9_\-]+$", tenant_id):
        raise HTTPException(status_code=400, detail="tenant_id has invalid characters")


# =========================
# Order ID
# =========================

def validate_order_id(order_id: str) -> None:
    if not order_id:
        raise HTTPException(status_code=400, detail="order_id is required")

    order_id = order_id.strip()

    if len(order_id) < 4:
        raise HTTPException(status_code=400, detail="order_id too short")

    if not re.match(r"^[a-f0-9]+$", order_id.lower()):
        raise HTTPException(status_code=400, detail="order_id invalid format")


# =========================
# Contact
# =========================

def validate_contact(contact: str) -> None:
    if not contact:
        raise HTTPException(status_code=400, detail="customer_contact is required")

    contact = contact.strip()

    if len(contact) < 5:
        raise HTTPException(status_code=400, detail="customer_contact too short")


# =========================
# Delivery Type
# =========================

def validate_delivery_type(delivery_type: str) -> str:
    allowed = {"pickup", "delivery"}

    if not delivery_type:
        return "pickup"

    delivery_type = delivery_type.strip().lower()

    if delivery_type not in allowed:
        raise HTTPException(status_code=422, detail=f"delivery_type must be one of {allowed}")

    return delivery_type


# =========================
# Requested Time
# =========================

def validate_requested_time(requested_time: str) -> str:
    if not requested_time:
        return "ahora"

    requested_time = requested_time.strip()

    if len(requested_time) > 100:
        raise HTTPException(status_code=422, detail="requested_time too long")

    return requested_time


# =========================
# Source
# =========================

def validate_source(source: str) -> str:
    allowed = {"api", "telegram", "web", "manychat"}

    if not source:
        return "api"

    source = source.strip().lower()

    if source not in allowed:
        raise HTTPException(status_code=422, detail=f"source must be one of {allowed}")

    return source
