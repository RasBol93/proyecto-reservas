# app/validators.py

import re
from fastapi import HTTPException

from app.config import (
    MAX_CONTACT_LEN,
    MAX_REQUESTED_TIME_LEN,
    MAX_SOURCE_LEN,
    ALLOWED_DELIVERY_TYPES,
    ALLOWED_SOURCES,
)


def validate_tenant_id(tenant_id: str) -> None:
    if not tenant_id:
        raise HTTPException(status_code=400, detail="tenant_id is required")

    tenant_id = tenant_id.strip()

    if len(tenant_id) < 3:
        raise HTTPException(status_code=400, detail="tenant_id too short")

    if not re.match(r"^[a-zA-Z0-9_\-]+$", tenant_id):
        raise HTTPException(status_code=400, detail="tenant_id has invalid characters")


def validate_order_id(order_id: str) -> None:
    if not order_id:
        raise HTTPException(status_code=400, detail="order_id is required")

    order_id = order_id.strip().lower()

    # gen_order_id() = token_hex(4) => 8 caracteres hex
    if len(order_id) != 8:
        raise HTTPException(status_code=400, detail="order_id must be 8 hex chars")

    if not re.fullmatch(r"[a-f0-9]{8}", order_id):
        raise HTTPException(status_code=400, detail="order_id invalid format")


def validate_contact(contact: str) -> None:
    if not contact:
        raise HTTPException(status_code=400, detail="customer_contact is required")

    contact = contact.strip()

    if len(contact) < 3:
        raise HTTPException(status_code=400, detail="customer_contact too short")

    if len(contact) > MAX_CONTACT_LEN:
        raise HTTPException(status_code=422, detail="customer_contact too long")


def validate_delivery_type(delivery_type: str) -> str:
    # En tu proyecto: SOLO pickup
    if not delivery_type:
        return "pickup"

    delivery_type = delivery_type.strip().lower()

    if delivery_type not in ALLOWED_DELIVERY_TYPES:
        raise HTTPException(
            status_code=422,
            detail=f"delivery_type must be one of {sorted(ALLOWED_DELIVERY_TYPES)}",
        )

    return delivery_type


def validate_requested_time(requested_time: str) -> str:
    if not requested_time:
        return "ahora"

    requested_time = requested_time.strip()
    if requested_time.lower() == "ahora":
        return "ahora"

    if len(requested_time) > MAX_REQUESTED_TIME_LEN:
        raise HTTPException(status_code=422, detail="requested_time too long")

    if not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", requested_time):
        raise HTTPException(status_code=400, detail="requested_time must be 'ahora' or HH:MM")

    return requested_time


def validate_source(source: str) -> str:
    if not source:
        return "api"

    source = source.strip().lower()

    if len(source) > MAX_SOURCE_LEN:
        raise HTTPException(status_code=422, detail="source too long")

    if source not in ALLOWED_SOURCES:
        raise HTTPException(
            status_code=422,
            detail=f"source must be one of {sorted(ALLOWED_SOURCES)}",
        )

    return source
