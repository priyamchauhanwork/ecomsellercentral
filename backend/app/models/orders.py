"""Order management & import tables (see docs/order-erd.md).

Orders belong to a seller (ecs_sellers) and, when they come from a marketplace, to the
seller's account on it (ecs_channel_accounts, in app/models/catalog.py). Columns whose
parent tables don't exist yet (users, customers, warehouses) are plain BIGINTs.
"""

from datetime import date, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, declared_attr, mapped_column, relationship

from app.models.base import Base, TimestampMixin, bigint_pk, in_check, money

# --- Allowed values -------------------------------------------------------------------

CHANNELS = ("csv", "website", "shopify", "amazon", "flipkart", "woocommerce")
IMPORT_INGESTION_METHODS = ("csv_upload", "scheduled_sync", "manual_sync", "webhook")
ORDER_INGESTION_METHODS = (*IMPORT_INGESTION_METHODS, "direct")
CHANGE_SOURCES = (*IMPORT_INGESTION_METHODS, "admin_ui", "customer", "system")
IMPORT_STATUSES = (
    "pending",
    "validating",
    "processing",
    "completed",
    "partially_completed",
    "failed",
    "cancelled",
)
IMPORT_ROW_STATUSES = ("pending", "valid", "invalid", "processed", "failed")
IMPORT_ROW_ACTIONS = (
    "created",
    "updated",
    "unchanged",
    "skipped_duplicate",
    "skipped_stale",
    "failed",
)
LINE_STATUSES = (
    "pending",
    "allocated",
    "packed",
    "shipped",
    "delivered",
    "cancelled",
    "return_requested",
    "returned",
)
ORDER_HISTORY_ACTIONS = (
    "created",
    "status_change",
    "address_update",
    "payment_update",
    "cancelled",
    "synced_update",
    "acknowledged",
    "rto",
    "attention_cleared",
)
# Why an order needs the team's attention (docs/inventory-erd.md, decisions).
ATTENTION_REASONS = ("cancelled_after_acknowledgement", "edited_after_acknowledgement")
LINE_HISTORY_ACTIONS = (
    "created",
    "status_change",
    "qty_change",
    "shipped",
    "cancelled",
    "returned",
    "refunded",
)


# --- Ingestion ------------------------------------------------------------------------


class OrderImport(TimestampMixin, Base):
    """One import run: a CSV upload, a scheduled or manual sync, or a webhook delivery."""

    __tablename__ = "ecs_order_import"
    __table_args__ = (
        # Duplicate protection for webhooks: one run per marketplace event.
        UniqueConstraint("channel_account_id", "external_event_id"),
        Index(None, "seller_id", "created_at"),
        Index(None, "channel_account_id", "status"),
        Index(None, "file_hash"),
        CheckConstraint(in_check("channel", CHANNELS), name="channel"),
        CheckConstraint(
            in_check("ingestion_method", IMPORT_INGESTION_METHODS), name="ingestion_method"
        ),
        CheckConstraint(in_check("status", IMPORT_STATUSES), name="status"),
    )

    order_import_id: Mapped[int] = bigint_pk()
    seller_id: Mapped[int] = mapped_column(ForeignKey("ecs_sellers.seller_id"))
    channel_account_id: Mapped[int | None] = mapped_column(  # null for CSV uploads
        ForeignKey("ecs_channel_accounts.channel_account_id")
    )
    channel: Mapped[str] = mapped_column(String(30))
    ingestion_method: Mapped[str] = mapped_column(String(20))
    triggered_by_user_id: Mapped[int | None] = mapped_column(BigInteger)
    external_event_id: Mapped[str | None] = mapped_column(String(150))
    event_topic: Mapped[str | None] = mapped_column(String(50))
    file_name: Mapped[str | None] = mapped_column(String(255))
    file_path: Mapped[str | None] = mapped_column(String(500))
    file_hash: Mapped[str | None] = mapped_column(String(64))
    sync_from: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    sync_to: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    cursor_start: Mapped[str | None] = mapped_column(String(500))
    cursor_end: Mapped[str | None] = mapped_column(String(500))
    status: Mapped[str] = mapped_column(String(25), server_default="pending")
    total_records: Mapped[int] = mapped_column(Integer, server_default="0")
    processed_records: Mapped[int] = mapped_column(Integer, server_default="0")
    created_count: Mapped[int] = mapped_column(Integer, server_default="0")
    updated_count: Mapped[int] = mapped_column(Integer, server_default="0")
    unchanged_count: Mapped[int] = mapped_column(Integer, server_default="0")
    skipped_count: Mapped[int] = mapped_column(Integer, server_default="0")
    failed_count: Mapped[int] = mapped_column(Integer, server_default="0")
    error_summary: Mapped[str | None] = mapped_column(Text)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    rows: Mapped[list["OrderImportRow"]] = relationship(back_populates="order_import")


class OrderImportRow(Base):
    """One order record received in a run, with the raw payload. Written once, then
    processed, so it has created_at but no updated_at."""

    __tablename__ = "ecs_order_import_row"
    __table_args__ = (
        Index(None, "order_import_id", "status"),
        Index(None, "external_order_id"),
        Index(None, "order_header_id"),
        CheckConstraint(in_check("status", IMPORT_ROW_STATUSES), name="status"),
        CheckConstraint(in_check("action_taken", IMPORT_ROW_ACTIONS), name="action_taken"),
    )

    order_import_row_id: Mapped[int] = bigint_pk()
    order_import_id: Mapped[int] = mapped_column(ForeignKey("ecs_order_import.order_import_id"))
    record_number: Mapped[int] = mapped_column(Integer)
    external_order_id: Mapped[str | None] = mapped_column(String(100))
    external_line_id: Mapped[str | None] = mapped_column(String(100))
    external_updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    raw_payload: Mapped[dict[str, Any] | None] = mapped_column(JSONB)  # purged after ~90 days
    payload_hash: Mapped[str | None] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(20), server_default="pending")
    action_taken: Mapped[str | None] = mapped_column(String(20))
    error_messages: Mapped[list[Any] | None] = mapped_column(JSONB)
    order_header_id: Mapped[int | None] = mapped_column(
        ForeignKey("ecs_order_header.order_header_id")
    )
    processed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    order_import: Mapped[OrderImport] = relationship(back_populates="rows")


# --- Orders ---------------------------------------------------------------------------


class OrderHeader(TimestampMixin, Base):
    """The complete order: provenance, customer, addresses, totals, payment, status."""

    __tablename__ = "ecs_order_header"
    __table_args__ = (
        # Duplicate protection for orders. NULL external_order_id (website orders) never
        # collides, because Postgres treats NULLs as distinct.
        UniqueConstraint("seller_id", "channel", "external_order_id"),
        Index(None, "seller_id", "order_status", "order_date"),
        Index(None, "customer_phone"),
        CheckConstraint(in_check("channel", CHANNELS), name="channel"),
        CheckConstraint(
            in_check("ingestion_method", ORDER_INGESTION_METHODS), name="ingestion_method"
        ),
        CheckConstraint(
            in_check("last_ingestion_method", ORDER_INGESTION_METHODS),
            name="last_ingestion_method",
        ),
        CheckConstraint("version_no >= 1", name="version_no_positive"),
        CheckConstraint(
            f"attention_reason IS NULL OR {in_check('attention_reason', ATTENTION_REASONS)}",
            name="attention_reason",
        ),
    )

    order_header_id: Mapped[int] = bigint_pk()
    order_number: Mapped[str] = mapped_column(String(30), unique=True)
    seller_id: Mapped[int] = mapped_column(ForeignKey("ecs_sellers.seller_id"))

    # Provenance
    channel: Mapped[str] = mapped_column(String(30))
    channel_account_id: Mapped[int | None] = mapped_column(  # null for CSV / website orders
        ForeignKey("ecs_channel_accounts.channel_account_id")
    )
    external_order_id: Mapped[str | None] = mapped_column(String(100))
    ingestion_method: Mapped[str] = mapped_column(String(20))
    created_by_import_id: Mapped[int | None] = mapped_column(
        ForeignKey("ecs_order_import.order_import_id")
    )
    last_import_id: Mapped[int | None] = mapped_column(
        ForeignKey("ecs_order_import.order_import_id")
    )
    last_ingestion_method: Mapped[str | None] = mapped_column(String(20))
    external_updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_synced_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    order_date: Mapped[datetime] = mapped_column(DateTime(timezone=True))

    # Customer snapshot
    customer_id: Mapped[int | None] = mapped_column(BigInteger)
    customer_name: Mapped[str | None] = mapped_column(String(150))
    customer_email: Mapped[str | None] = mapped_column(String(255))
    customer_phone: Mapped[str | None] = mapped_column(String(20))

    # Shipping address snapshot
    ship_name: Mapped[str | None] = mapped_column(String(150))
    ship_phone: Mapped[str | None] = mapped_column(String(20))
    ship_address_line1: Mapped[str | None] = mapped_column(String(255))
    ship_address_line2: Mapped[str | None] = mapped_column(String(255))
    ship_landmark: Mapped[str | None] = mapped_column(String(150))
    ship_city: Mapped[str | None] = mapped_column(String(100))
    ship_state: Mapped[str | None] = mapped_column(String(100))
    ship_state_code: Mapped[str | None] = mapped_column(String(10))
    ship_pincode: Mapped[str | None] = mapped_column(String(10))
    ship_country: Mapped[str | None] = mapped_column(String(60))

    # Billing address snapshot
    bill_name: Mapped[str | None] = mapped_column(String(150))
    bill_phone: Mapped[str | None] = mapped_column(String(20))
    bill_address_line1: Mapped[str | None] = mapped_column(String(255))
    bill_address_line2: Mapped[str | None] = mapped_column(String(255))
    bill_landmark: Mapped[str | None] = mapped_column(String(150))
    bill_city: Mapped[str | None] = mapped_column(String(100))
    bill_state: Mapped[str | None] = mapped_column(String(100))
    bill_state_code: Mapped[str | None] = mapped_column(String(10))
    bill_pincode: Mapped[str | None] = mapped_column(String(10))
    bill_country: Mapped[str | None] = mapped_column(String(60))
    bill_gstin: Mapped[str | None] = mapped_column(String(15))
    bill_same_as_ship: Mapped[bool] = mapped_column(Boolean, server_default=text("true"))

    # Three independent statuses
    order_status: Mapped[str] = mapped_column(String(20), server_default="pending")
    payment_status: Mapped[str] = mapped_column(String(20), server_default="pending")
    fulfillment_status: Mapped[str] = mapped_column(String(20), server_default="unfulfilled")

    # Totals (equal to the sum of the lines; recalculated whenever a line changes)
    currency: Mapped[str] = mapped_column(String(3), server_default="INR")
    subtotal_amount: Mapped[Decimal] = money()
    discount_amount: Mapped[Decimal] = money()
    coupon_code: Mapped[str | None] = mapped_column(String(50))
    shipping_amount: Mapped[Decimal] = money()
    tax_amount: Mapped[Decimal] = money()
    total_amount: Mapped[Decimal] = money()
    paid_amount: Mapped[Decimal] = money()
    refunded_amount: Mapped[Decimal] = money()

    # Order-level payment
    payment_method: Mapped[str | None] = mapped_column(String(50))
    payment_gateway: Mapped[str | None] = mapped_column(String(50))
    payment_reference: Mapped[str | None] = mapped_column(String(100))
    paid_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    customer_notes: Mapped[str | None] = mapped_column(Text)
    internal_notes: Mapped[str | None] = mapped_column(Text)
    cancel_reason: Mapped[str | None] = mapped_column(Text)
    cancelled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # Set when Shopify cancels or cuts an order the team already acknowledged.
    attention_reason: Mapped[str | None] = mapped_column(String(40))
    attention_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    version_no: Mapped[int] = mapped_column(Integer, server_default="1")
    is_deleted: Mapped[bool] = mapped_column(Boolean, server_default=text("false"))
    created_by: Mapped[int | None] = mapped_column(BigInteger)  # null when an import made it
    updated_by: Mapped[int | None] = mapped_column(BigInteger)

    lines: Mapped[list["OrderLine"]] = relationship(
        back_populates="order_header", order_by="OrderLine.line_number"
    )
    history: Mapped[list["OrderHistory"]] = relationship(
        back_populates="order_header", order_by="OrderHistory.order_history_id"
    )


class OrderLine(TimestampMixin, Base):
    """One item in an order, with its pricing, GST, payment share and shipping."""

    __tablename__ = "ecs_order_line"
    __table_args__ = (
        UniqueConstraint("order_header_id", "line_number"),
        # Target of ecs_inventory_demand's composite foreign key.
        UniqueConstraint("order_line_id", "order_header_id"),
        Index(None, "tracking_number"),
        Index(None, "sku"),
        Index(None, "variant_id"),
        CheckConstraint(in_check("line_status", LINE_STATUSES), name="line_status"),
        CheckConstraint(
            "ordered_qty >= 0 AND shipped_qty >= 0 AND cancelled_qty >= 0 AND returned_qty >= 0",
            name="quantities_non_negative",
        ),
        CheckConstraint(
            "delivered_qty >= 0 AND delivered_qty <= shipped_qty", name="delivered_within_shipped"
        ),
    )

    order_line_id: Mapped[int] = bigint_pk()
    order_header_id: Mapped[int] = mapped_column(ForeignKey("ecs_order_header.order_header_id"))
    line_number: Mapped[int] = mapped_column(Integer)
    external_line_id: Mapped[str | None] = mapped_column(String(100))

    # Catalog links (FKs once the catalog tables exist) and product snapshot
    product_id: Mapped[int | None] = mapped_column(ForeignKey("ecs_articles.article_id"))
    variant_id: Mapped[int | None] = mapped_column(ForeignKey("ecs_items.item_id"))
    sku: Mapped[str | None] = mapped_column(String(100))
    product_name: Mapped[str] = mapped_column(String(255))
    variant_name: Mapped[str | None] = mapped_column(String(255))
    hsn_code: Mapped[str | None] = mapped_column(String(10))

    ordered_qty: Mapped[int] = mapped_column(Integer)
    shipped_qty: Mapped[int] = mapped_column(Integer, server_default="0")
    cancelled_qty: Mapped[int] = mapped_column(Integer, server_default="0")
    returned_qty: Mapped[int] = mapped_column(Integer, server_default="0")
    delivered_qty: Mapped[int] = mapped_column(Integer, server_default="0")

    # Pricing and GST
    unit_price: Mapped[Decimal] = money()
    line_discount_amount: Mapped[Decimal] = money()
    tax_rate: Mapped[Decimal] = mapped_column(Numeric(5, 2), server_default="0")
    cgst_amount: Mapped[Decimal] = money()
    sgst_amount: Mapped[Decimal] = money()
    igst_amount: Mapped[Decimal] = money()
    line_total_amount: Mapped[Decimal] = money()

    # This line's share of payment and refunds
    line_paid_amount: Mapped[Decimal] = money()
    line_refunded_amount: Mapped[Decimal] = money()
    refund_reference: Mapped[str | None] = mapped_column(String(100))
    refunded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    # Shipping for this line
    warehouse_id: Mapped[int | None] = mapped_column(BigInteger)
    courier_name: Mapped[str | None] = mapped_column(String(100))
    tracking_number: Mapped[str | None] = mapped_column(String(100))
    shipping_charge: Mapped[Decimal] = money()
    shipped_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    expected_delivery_date: Mapped[date | None] = mapped_column(Date)
    delivered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    line_status: Mapped[str] = mapped_column(String(20), server_default="pending")
    version_no: Mapped[int] = mapped_column(Integer, server_default="1")
    created_by: Mapped[int | None] = mapped_column(BigInteger)
    updated_by: Mapped[int | None] = mapped_column(BigInteger)

    order_header: Mapped[OrderHeader] = relationship(back_populates="lines")
    history: Mapped[list["OrderLineHistory"]] = relationship(
        back_populates="order_line", order_by="OrderLineHistory.order_line_history_id"
    )


# --- Audit ----------------------------------------------------------------------------


class HistoryColumns:
    """Columns shared by both history tables. Rows are only inserted, never updated."""

    version_no: Mapped[int] = mapped_column(Integer)
    action_type: Mapped[str] = mapped_column(String(30))
    old_status: Mapped[str | None] = mapped_column(String(20))
    new_status: Mapped[str | None] = mapped_column(String(20))
    changed_fields: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    change_source: Mapped[str] = mapped_column(String(20))
    changed_by: Mapped[int | None] = mapped_column(BigInteger)
    remarks: Mapped[str | None] = mapped_column(Text)
    changed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    @declared_attr
    def order_import_id(cls) -> Mapped[int | None]:
        return mapped_column(ForeignKey("ecs_order_import.order_import_id"))

    @declared_attr
    def order_import_row_id(cls) -> Mapped[int | None]:
        return mapped_column(ForeignKey("ecs_order_import_row.order_import_row_id"))


class OrderHistory(HistoryColumns, Base):
    __tablename__ = "ecs_order_history"
    __table_args__ = (
        Index(None, "order_header_id", "changed_at"),
        CheckConstraint(in_check("action_type", ORDER_HISTORY_ACTIONS), name="action_type"),
        CheckConstraint(in_check("change_source", CHANGE_SOURCES), name="change_source"),
    )

    order_history_id: Mapped[int] = bigint_pk()
    order_header_id: Mapped[int] = mapped_column(ForeignKey("ecs_order_header.order_header_id"))

    order_header: Mapped[OrderHeader] = relationship(back_populates="history")


class OrderLineHistory(HistoryColumns, Base):
    __tablename__ = "ecs_order_line_history"
    __table_args__ = (
        Index(None, "order_line_id", "changed_at"),
        Index(None, "order_header_id"),
        CheckConstraint(in_check("action_type", LINE_HISTORY_ACTIONS), name="action_type"),
        CheckConstraint(in_check("change_source", CHANGE_SOURCES), name="change_source"),
    )

    order_line_history_id: Mapped[int] = bigint_pk()
    order_line_id: Mapped[int] = mapped_column(ForeignKey("ecs_order_line.order_line_id"))
    # Copied so an order's line changes need no join.
    order_header_id: Mapped[int] = mapped_column(ForeignKey("ecs_order_header.order_header_id"))

    order_line: Mapped[OrderLine] = relationship(back_populates="history")
