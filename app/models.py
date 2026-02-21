from typing import List, Optional
from pydantic import BaseModel, Field


class AdminTokenIn(BaseModel):
    token: str


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
