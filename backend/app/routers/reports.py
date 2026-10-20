"""Reports (#24; grows into the Reports API, #29)."""

from datetime import date, datetime
from decimal import Decimal

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.auth import require_api_key
from app.config import get_settings
from app.db import get_db
from app.routers.inventory import InventoryItem
from app.services import reports

router = APIRouter(prefix="/reports", tags=["reports"], dependencies=[Depends(require_api_key)])


class OrderStats(BaseModel):
    orders_today: int  # placed today (IST), cancelled included
    revenue_today: Decimal  # total_amount of today's orders that aren't cancelled (INR)
    refunded_today: Decimal
    orders_total: int
    revenue_total: Decimal
    refunded_total: Decimal
    cancelled_total: int
    other_currency_orders: int  # not added to revenue
    needs_attention: int


class StockStats(BaseModel):
    low_stock: int  # 0 < available ≤ threshold
    out_of_stock: int  # available ≤ 0
    skus: int  # active SKUs of products that aren't archived
    products: int


class RecentOrder(BaseModel):
    order_header_id: int
    order_number: str
    order_date: datetime
    customer_name: str | None
    total_amount: Decimal
    currency: str
    order_status: str
    attention_reason: str | None
    line_count: int


class Dashboard(BaseModel):
    generated_at: datetime
    today: date  # the IST day "today" refers to
    currency: str
    low_stock_threshold: int
    orders: OrderStats
    stock: StockStats
    returns_awaiting_qc: int
    recent_orders: list[RecentOrder]
    low_stock_items: list[InventoryItem]


@router.get("/dashboard", response_model=Dashboard)
def dashboard(db: Session = Depends(get_db)):
    """Everything the home page shows, in one call."""
    return reports.dashboard(db, get_settings().seller_id)
