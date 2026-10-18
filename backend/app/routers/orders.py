"""Order endpoints: Shopify import (#9), list, detail and manual status change (#10).

The /orders/imports routes are declared before /orders/{order_id} so they match first.
"""

from datetime import date, datetime, time, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session, selectinload

from app.auth import require_api_key
from app.db import get_db
from app.models import OrderHeader, OrderImport, OrderImportRow, OrderLine
from app.services import order_ingestion
from app.services.order_status import OrderStatus, StatusChangeNotAllowed, change_order_status
from app.services.shopify import ShopifyConfigError, ShopifyError

# Customer data: every route needs the API key.
router = APIRouter(prefix="/orders", tags=["orders"], dependencies=[Depends(require_api_key)])

IST = ZoneInfo("Asia/Kolkata")  # order date filters use Indian day boundaries

READ_ALL_ORDERS_WARNING = (
    "The Shopify app lacks the read_all_orders scope, so only orders from the last "
    "60 days are imported. Add the scope in the Shopify Dev Dashboard to import older orders."
)


class ImportRun(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    order_import_id: int
    channel: str
    ingestion_method: str
    status: str
    total_records: int
    processed_records: int
    created_count: int
    updated_count: int
    unchanged_count: int
    skipped_count: int
    failed_count: int
    error_summary: str | None
    started_at: datetime | None
    completed_at: datetime | None
    created_at: datetime


class ImportStarted(ImportRun):
    warnings: list[str]


class ImportRowOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    order_import_row_id: int
    record_number: int
    external_order_id: str | None
    status: str
    action_taken: str | None
    error_messages: list[str] | None
    order_header_id: int | None


@router.post("/import", status_code=202, response_model=ImportStarted)
def import_orders(background_tasks: BackgroundTasks, db: Session = Depends(get_db)):
    """Start importing every Shopify order. Runs in the background: poll
    GET /orders/imports/{order_import_id} for progress."""
    try:
        with order_ingestion.shopify_client_factory() as shopify:
            scopes = shopify.access_scopes()
    except ShopifyConfigError as exc:
        raise HTTPException(503, f"Shopify is not configured: {exc}") from exc
    except ShopifyError as exc:
        raise HTTPException(502, f"Shopify rejected the request: {exc}") from exc
    if "read_orders" not in scopes:
        raise HTTPException(503, "The Shopify app lacks the read_orders scope.")
    warnings = [] if "read_all_orders" in scopes else [READ_ALL_ORDERS_WARNING]

    try:
        connection = order_ingestion.ensure_shopify_connection(db)
        run = order_ingestion.start_import(db, connection.channel_connection_id, "manual_sync")
        db.commit()
    except order_ingestion.ImportAlreadyRunning as exc:
        db.rollback()
        raise HTTPException(
            409,
            {
                "message": "An order import is already running for this store.",
                "order_import_id": exc.order_import_id,
            },
        ) from exc

    background_tasks.add_task(order_ingestion.run_shopify_order_import, run.order_import_id)
    return ImportStarted(**ImportRun.model_validate(run).model_dump(), warnings=warnings)


def get_run(db: Session, order_import_id: int) -> OrderImport:
    run = db.get(OrderImport, order_import_id)
    if run is None:
        raise HTTPException(404, "Import run not found")
    return run


@router.get("/imports/{order_import_id}", response_model=ImportRun)
def get_import(order_import_id: int, db: Session = Depends(get_db)):
    return get_run(db, order_import_id)


@router.get("/imports/{order_import_id}/rows", response_model=list[ImportRowOut])
def get_import_rows(
    order_import_id: int,
    status: str | None = Query(None, description="e.g. failed"),
    limit: int = Query(100, ge=1, le=1000),
    db: Session = Depends(get_db),
):
    get_run(db, order_import_id)
    query = select(OrderImportRow).where(OrderImportRow.order_import_id == order_import_id)
    if status:
        query = query.where(OrderImportRow.status == status)
    return db.scalars(query.order_by(OrderImportRow.record_number).limit(limit)).all()


# --- Orders (#10) -----------------------------------------------------------------------


class OrderSummary(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    order_header_id: int
    order_number: str
    channel: str
    order_date: datetime
    customer_name: str | None
    customer_email: str | None
    customer_phone: str | None
    order_status: str
    payment_status: str
    fulfillment_status: str
    currency: str
    total_amount: Decimal
    line_count: int = 0  # not a column; set from a count query or the loaded lines


class OrderList(BaseModel):
    total: int  # all matching orders, even beyond `limit`
    orders: list[OrderSummary]


class OrderLineOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    order_line_id: int
    line_number: int
    external_line_id: str | None
    product_id: int | None
    variant_id: int | None
    sku: str | None
    product_name: str
    variant_name: str | None
    hsn_code: str | None
    ordered_qty: int
    shipped_qty: int
    cancelled_qty: int
    returned_qty: int
    unit_price: Decimal
    line_discount_amount: Decimal
    tax_rate: Decimal
    cgst_amount: Decimal
    sgst_amount: Decimal
    igst_amount: Decimal
    line_total_amount: Decimal
    line_refunded_amount: Decimal
    courier_name: str | None
    tracking_number: str | None
    shipped_at: datetime | None
    delivered_at: datetime | None
    line_status: str
    version_no: int


class OrderHistoryOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    order_history_id: int
    version_no: int
    action_type: str
    old_status: str | None
    new_status: str | None
    changed_fields: dict | None
    change_source: str
    order_import_id: int | None
    remarks: str | None
    changed_at: datetime


class OrderDetail(OrderSummary):
    external_order_id: str | None
    ingestion_method: str
    last_ingestion_method: str | None
    last_synced_at: datetime | None
    ship_name: str | None
    ship_phone: str | None
    ship_address_line1: str | None
    ship_address_line2: str | None
    ship_landmark: str | None
    ship_city: str | None
    ship_state: str | None
    ship_state_code: str | None
    ship_pincode: str | None
    ship_country: str | None
    bill_same_as_ship: bool
    bill_name: str | None
    bill_phone: str | None
    bill_address_line1: str | None
    bill_address_line2: str | None
    bill_city: str | None
    bill_state: str | None
    bill_state_code: str | None
    bill_pincode: str | None
    bill_country: str | None
    bill_gstin: str | None
    subtotal_amount: Decimal
    discount_amount: Decimal
    coupon_code: str | None
    shipping_amount: Decimal
    tax_amount: Decimal
    paid_amount: Decimal
    refunded_amount: Decimal
    payment_gateway: str | None
    paid_at: datetime | None
    customer_notes: str | None
    cancel_reason: str | None
    cancelled_at: datetime | None
    version_no: int
    updated_at: datetime
    lines: list[OrderLineOut]
    history: list[OrderHistoryOut]


class StatusChange(BaseModel):
    status: OrderStatus
    remarks: str | None = Field(None, max_length=500)


@router.get("", response_model=OrderList)
def list_orders(
    status: OrderStatus | None = None,
    from_date: date | None = Query(None, alias="from", description="YYYY-MM-DD, IST"),
    to_date: date | None = Query(None, alias="to", description="YYYY-MM-DD, IST, inclusive"),
    q: str | None = Query(None, max_length=100, description="Order number, customer or phone"),
    limit: int = Query(500, ge=1, le=1000),
    db: Session = Depends(get_db),
):
    """Orders newest first, filtered by status, order date (IST days) and a search term."""
    if from_date and to_date and from_date > to_date:
        raise HTTPException(422, "'from' must be on or before 'to'.")

    filters = [OrderHeader.is_deleted.is_(False)]
    if status:
        filters.append(OrderHeader.order_status == status)
    if from_date:
        filters.append(OrderHeader.order_date >= datetime.combine(from_date, time.min, IST))
    if to_date:
        next_day = datetime.combine(to_date + timedelta(days=1), time.min, IST)
        filters.append(OrderHeader.order_date < next_day)
    if q and q.strip():
        # Escape LIKE wildcards so "%" or "_" in the search are matched literally.
        term = q.strip().replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        pattern = f"%{term}%"
        filters.append(
            or_(
                *(
                    column.ilike(pattern, escape="\\")
                    for column in (
                        OrderHeader.order_number,
                        OrderHeader.customer_name,
                        OrderHeader.customer_email,
                        OrderHeader.customer_phone,
                    )
                )
            )
        )

    line_count = (
        select(func.count(OrderLine.order_line_id))
        .where(OrderLine.order_header_id == OrderHeader.order_header_id)
        .scalar_subquery()
    )
    rows = db.execute(
        select(OrderHeader, line_count.label("line_count"))
        .where(*filters)
        .order_by(OrderHeader.order_date.desc(), OrderHeader.order_header_id.desc())
        .limit(limit)
    ).all()
    total = db.scalar(select(func.count()).select_from(OrderHeader).where(*filters))
    return OrderList(
        total=total,
        orders=[
            OrderSummary.model_validate(header).model_copy(update={"line_count": count})
            for header, count in rows
        ],
    )


def load_order(db: Session, order_id: int, *, for_update: bool = False) -> OrderHeader:
    query = (
        select(OrderHeader)
        .where(OrderHeader.order_header_id == order_id, OrderHeader.is_deleted.is_(False))
        .options(selectinload(OrderHeader.lines), selectinload(OrderHeader.history))
    )
    if for_update:
        query = query.with_for_update(of=OrderHeader)
    header = db.scalars(query).one_or_none()
    if header is None:
        raise HTTPException(404, "Order not found")
    return header


def order_detail(header: OrderHeader) -> OrderDetail:
    return OrderDetail.model_validate(header).model_copy(update={"line_count": len(header.lines)})


@router.get("/{order_id}", response_model=OrderDetail)
def get_order(order_id: int, db: Session = Depends(get_db)):
    """One order with its lines (by line number) and history (oldest first)."""
    return order_detail(load_order(db, order_id))


@router.post("/{order_id}/status", response_model=OrderDetail)
def update_order_status(order_id: int, change: StatusChange, db: Session = Depends(get_db)):
    """Manually set an order's status (e.g. "Mark shipped"). Applied to every line still
    open, then the order status is recalculated; header and line history are written.

    Allowed: pending → shipped / delivered / cancelled, shipped → delivered.
    Setting the current status again changes nothing.
    """
    header = load_order(db, order_id, for_update=True)
    try:
        changed = change_order_status(db, header, change.status, change.remarks)
    except StatusChangeNotAllowed as exc:
        db.rollback()
        raise HTTPException(409, str(exc)) from exc
    if changed:
        db.commit()
        db.expire_all()
        header = load_order(db, order_id)
    return order_detail(header)
