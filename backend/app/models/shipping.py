"""Courier aggregator tables: accounts, warehouses, shipments, tracking, documents,
webhook inbox and API-call log (docs/shipping-erd.md, #58).

Aggregator-agnostic: Shiprocket is the first aggregator; another one means new rows in
ecs_courier_aggregators / ecs_aggregator_status_mappings, not new tables. Adapted from
*Courier Aggregator Integration — Database Schema* (23 Oct 2026): ecs_ names, BIGINT
keys, money as NUMERIC(12,2) rupees.
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
    ForeignKeyConstraint,
    Index,
    Integer,
    LargeBinary,
    Numeric,
    PrimaryKeyConstraint,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin, bigint_pk, in_check

AUTH_TYPES = ("credentials_token", "api_key", "oauth2")
ACCOUNT_STATUSES = ("active", "auth_failed", "disconnected")
SYNC_STATUSES = ("pending", "synced", "failed")
COURIER_MODES = ("surface", "air", "other")
PAYMENT_MODES = ("prepaid", "cod")
# Canonical shipment statuses (docs/shipping-erd.md, "Shipment status model").
SHIPMENT_STATUSES = (
    "draft",
    "creation_failed",
    "created",
    "awb_assigned",
    "pickup_scheduled",
    "picked_up",
    "in_transit",
    "out_for_delivery",
    "delivered",
    "undelivered",
    "rto_initiated",
    "rto_delivered",
    "cancelled",
    "lost",
    "damaged",
)
TERMINAL_STATUSES = (
    "creation_failed",
    "delivered",
    "rto_delivered",
    "cancelled",
    "lost",
    "damaged",
)
AWB_REQUIRED_STATUSES = (
    "awb_assigned",
    "pickup_scheduled",
    "picked_up",
    "in_transit",
    "out_for_delivery",
    "delivered",
)
# Shipments the tracking poller looks at.
ACTIVE_STATUSES = (
    "awb_assigned",
    "pickup_scheduled",
    "picked_up",
    "in_transit",
    "out_for_delivery",
    "undelivered",
    "rto_initiated",
)
DOC_TYPES = ("label", "manifest", "invoice", "packing_slip")
DOC_SOURCES = ("aggregator", "oms")
EVENT_SOURCES = ("webhook", "poll", "api_response", "manual")
WEBHOOK_STATUSES = ("pending", "processing", "processed", "ignored", "failed")
API_OPERATIONS = (
    "auth",
    "create_order",
    "assign_awb",
    "generate_pickup",
    "generate_manifest",
    "generate_label",
    "generate_invoice",
    "cancel",
    "track",
    "create_pickup_location",
    "other",
)


class CourierAggregator(Base):
    """Static catalog of supported aggregators; seeded by migration."""

    __tablename__ = "ecs_courier_aggregators"
    __table_args__ = (CheckConstraint(in_check("auth_type", AUTH_TYPES), name="auth_type"),)

    aggregator_id: Mapped[int] = bigint_pk()
    code: Mapped[str] = mapped_column(String(30), unique=True)  # 'shiprocket'
    name: Mapped[str] = mapped_column(String(100))
    auth_type: Mapped[str] = mapped_column(String(20))
    api_base_url: Mapped[str] = mapped_column(Text)
    token_ttl_hours: Mapped[int | None] = mapped_column(Integer)
    is_active: Mapped[bool] = mapped_column(Boolean, server_default=text("true"))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("now()")
    )


class AggregatorAccount(TimestampMixin, Base):
    """A seller's authenticated connection to an aggregator: credentials (encrypted by
    the app), current token, and how its webhooks are recognised."""

    __tablename__ = "ecs_aggregator_accounts"
    __table_args__ = (
        UniqueConstraint("seller_id", "aggregator_id", "label"),
        # Target of the composite FKs that keep a shipment's seller consistent.
        UniqueConstraint("aggregator_account_id", "seller_id"),
        Index(
            "uq_ecs_aggregator_accounts_default",
            "seller_id",
            unique=True,
            postgresql_where=text("is_default"),
        ),
        CheckConstraint(in_check("status", ACCOUNT_STATUSES), name="status"),
    )

    aggregator_account_id: Mapped[int] = bigint_pk()
    seller_id: Mapped[int] = mapped_column(ForeignKey("ecs_sellers.seller_id"))
    aggregator_id: Mapped[int] = mapped_column(ForeignKey("ecs_courier_aggregators.aggregator_id"))
    label: Mapped[str] = mapped_column(String(50), server_default="default")
    # Shiprocket: the API user's email (not the panel login).
    api_username: Mapped[str | None] = mapped_column(String(255))
    api_secret_encrypted: Mapped[bytes | None] = mapped_column(LargeBinary)
    access_token_encrypted: Mapped[bytes | None] = mapped_column(LargeBinary)
    token_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # Inbound webhooks: the token Shiprocket sends as x-api-key (hash only), and the
    # opaque key in the webhook URL that identifies this account.
    webhook_token_hash: Mapped[str | None] = mapped_column(String(128))
    webhook_path_key: Mapped[str | None] = mapped_column(String(64), unique=True)
    status: Mapped[str] = mapped_column(String(20), server_default="active")
    is_default: Mapped[bool] = mapped_column(Boolean, server_default=text("false"))
    last_auth_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error: Mapped[str | None] = mapped_column(Text)


class Warehouse(TimestampMixin, Base):
    """A seller's pickup location (aggregator-neutral). Stock stays a single figure per
    SKU (#49): this is where couriers collect from, not where stock is counted."""

    __tablename__ = "ecs_warehouses"
    __table_args__ = (
        UniqueConstraint("seller_id", "name"),
        UniqueConstraint("warehouse_id", "seller_id"),
        Index(
            "uq_ecs_warehouses_default",
            "seller_id",
            unique=True,
            postgresql_where=text("is_default AND is_active"),
        ),
        CheckConstraint("pincode ~ '^[1-9][0-9]{5}$'", name="pincode_format"),
    )

    warehouse_id: Mapped[int] = bigint_pk()
    seller_id: Mapped[int] = mapped_column(ForeignKey("ecs_sellers.seller_id"))
    name: Mapped[str] = mapped_column(String(100))
    contact_name: Mapped[str] = mapped_column(String(150))
    phone: Mapped[str] = mapped_column(String(20))
    email: Mapped[str | None] = mapped_column(String(255))
    address_line1: Mapped[str] = mapped_column(String(255))
    address_line2: Mapped[str | None] = mapped_column(String(255))
    city: Mapped[str] = mapped_column(String(100))
    state: Mapped[str] = mapped_column(String(100))
    pincode: Mapped[str] = mapped_column(String(6))
    country_code: Mapped[str] = mapped_column(String(2), server_default="IN")
    gstin: Mapped[str | None] = mapped_column(String(15))
    is_default: Mapped[bool] = mapped_column(Boolean, server_default=text("false"))
    is_active: Mapped[bool] = mapped_column(Boolean, server_default=text("true"))


class WarehouseAggregatorLink(TimestampMixin, Base):
    """How a warehouse is registered on an aggregator account (Shiprocket identifies
    pickup locations by a nickname sent with every order)."""

    __tablename__ = "ecs_warehouse_aggregator_links"
    __table_args__ = (
        UniqueConstraint("warehouse_id", "aggregator_account_id"),
        UniqueConstraint("aggregator_account_id", "external_location_code"),
        CheckConstraint(in_check("sync_status", SYNC_STATUSES), name="sync_status"),
    )

    link_id: Mapped[int] = bigint_pk()
    warehouse_id: Mapped[int] = mapped_column(ForeignKey("ecs_warehouses.warehouse_id"))
    aggregator_account_id: Mapped[int] = mapped_column(
        ForeignKey("ecs_aggregator_accounts.aggregator_account_id")
    )
    external_location_code: Mapped[str] = mapped_column(String(36))  # Shiprocket nickname
    external_location_id: Mapped[str | None] = mapped_column(String(50))
    sync_status: Mapped[str] = mapped_column(String(20), server_default="pending")
    last_synced_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error: Mapped[str | None] = mapped_column(Text)


class Courier(Base):
    """A carrier available through an aggregator; upserted from API responses."""

    __tablename__ = "ecs_couriers"
    __table_args__ = (
        UniqueConstraint("aggregator_id", "external_courier_id"),
        CheckConstraint(f"mode IS NULL OR {in_check('mode', COURIER_MODES)}", name="mode"),
    )

    courier_id: Mapped[int] = bigint_pk()
    aggregator_id: Mapped[int] = mapped_column(ForeignKey("ecs_courier_aggregators.aggregator_id"))
    external_courier_id: Mapped[str] = mapped_column(String(50))  # courier_company_id
    name: Mapped[str] = mapped_column(String(150))
    mode: Mapped[str | None] = mapped_column(String(10))
    is_active: Mapped[bool] = mapped_column(Boolean, server_default=text("true"))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("now()")
    )


class Shipment(TimestampMixin, Base):
    """One physical package / AWB. An order can have several (split shipments, or a new
    shipment after a cancellation); one aggregator order per shipment."""

    __tablename__ = "ecs_shipments"
    __table_args__ = (
        # The order, account and warehouse all belong to the shipment's seller.
        ForeignKeyConstraint(
            ["order_header_id", "seller_id"],
            ["ecs_order_header.order_header_id", "ecs_order_header.seller_id"],
            name="fk_ecs_shipments_order_seller",
        ),
        ForeignKeyConstraint(
            ["aggregator_account_id", "seller_id"],
            ["ecs_aggregator_accounts.aggregator_account_id", "ecs_aggregator_accounts.seller_id"],
            name="fk_ecs_shipments_account_seller",
        ),
        ForeignKeyConstraint(
            ["warehouse_id", "seller_id"],
            ["ecs_warehouses.warehouse_id", "ecs_warehouses.seller_id"],
            name="fk_ecs_shipments_warehouse_seller",
        ),
        Index(
            "uq_ecs_shipments_awb",
            "aggregator_account_id",
            "awb_number",
            unique=True,
            postgresql_where=text("awb_number IS NOT NULL"),
        ),
        Index(
            "uq_ecs_shipments_external_shipment",
            "aggregator_account_id",
            "external_shipment_id",
            unique=True,
            postgresql_where=text("external_shipment_id IS NOT NULL"),
        ),
        Index(None, "order_header_id"),
        Index(None, "seller_id", "status", text("created_at DESC")),
        Index(None, "awb_number"),
        Index(
            "ix_ecs_shipments_active_stale",
            "status_updated_at",
            postgresql_where=text(in_check("status", ACTIVE_STATUSES)),
        ),
        CheckConstraint(in_check("status", SHIPMENT_STATUSES), name="status"),
        CheckConstraint(in_check("payment_mode", PAYMENT_MODES), name="payment_mode"),
        CheckConstraint("payment_mode = 'cod' OR cod_amount = 0", name="cod_only_for_cod"),
        CheckConstraint(
            f"NOT {in_check('status', AWB_REQUIRED_STATUSES)} OR awb_number IS NOT NULL",
            name="awb_required",
        ),
        CheckConstraint(
            "cod_amount >= 0 AND declared_value >= 0 AND dead_weight_g > 0"
            " AND length_cm > 0 AND breadth_cm > 0 AND height_cm > 0"
            " AND (charged_weight_g IS NULL OR charged_weight_g > 0)"
            " AND (shipping_charge IS NULL OR shipping_charge >= 0)",
            name="package_values",
        ),
        CheckConstraint("version >= 1", name="version_positive"),
    )

    shipment_id: Mapped[int] = bigint_pk()
    seller_id: Mapped[int] = mapped_column(ForeignKey("ecs_sellers.seller_id"))
    order_header_id: Mapped[int] = mapped_column(BigInteger)
    aggregator_account_id: Mapped[int] = mapped_column(BigInteger)
    warehouse_id: Mapped[int] = mapped_column(BigInteger)
    courier_id: Mapped[int | None] = mapped_column(ForeignKey("ecs_couriers.courier_id"))
    # Prevents duplicate bookings on retries and double clicks.
    idempotency_key: Mapped[str] = mapped_column(String(100), unique=True)

    # Aggregator identifiers: Shiprocket returns both an order id and a shipment id.
    external_order_id: Mapped[str | None] = mapped_column(String(50))
    external_shipment_id: Mapped[str | None] = mapped_column(String(50))
    awb_number: Mapped[str | None] = mapped_column(String(50))
    courier_name_snapshot: Mapped[str | None] = mapped_column(String(150))

    status: Mapped[str] = mapped_column(String(20), server_default="draft")
    external_status_code: Mapped[str | None] = mapped_column(String(20))
    external_status_label: Mapped[str | None] = mapped_column(String(100))
    status_updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # Courier time of the latest applied event: older events never overwrite newer ones.
    last_event_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    # Package and charges (money in rupees, like the rest of the database)
    payment_mode: Mapped[str] = mapped_column(String(10))
    cod_amount: Mapped[Decimal] = mapped_column(Numeric(12, 2), server_default="0")
    declared_value: Mapped[Decimal] = mapped_column(Numeric(12, 2))
    dead_weight_g: Mapped[int] = mapped_column(Integer)
    length_cm: Mapped[Decimal] = mapped_column(Numeric(6, 1))
    breadth_cm: Mapped[Decimal] = mapped_column(Numeric(6, 1))
    height_cm: Mapped[Decimal] = mapped_column(Numeric(6, 1))
    charged_weight_g: Mapped[int | None] = mapped_column(Integer)
    shipping_charge: Mapped[Decimal | None] = mapped_column(Numeric(12, 2))

    # {name, phone, email, line1, line2, city, state, pincode, country} at booking time
    ship_to: Mapped[dict[str, Any]] = mapped_column(JSONB)

    tracking_url: Mapped[str | None] = mapped_column(Text)
    pickup_scheduled_for: Mapped[date | None] = mapped_column(Date)
    pickup_token: Mapped[str | None] = mapped_column(String(100))
    expected_delivery_date: Mapped[date | None] = mapped_column(Date)
    picked_up_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    delivered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    cancelled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    cancelled_by: Mapped[int | None] = mapped_column(BigInteger)  # FK once users exist
    cancel_reason: Mapped[str | None] = mapped_column(Text)

    created_by: Mapped[int | None] = mapped_column(BigInteger)  # FK once users exist
    version: Mapped[int] = mapped_column(Integer, server_default="1")  # optimistic lock


class ShipmentItem(Base):
    """Which order lines, and how many units, are in a package (split shipments)."""

    __tablename__ = "ecs_shipment_items"
    __table_args__ = (
        PrimaryKeyConstraint("shipment_id", "order_line_id"),
        Index(None, "order_line_id"),
        CheckConstraint("quantity > 0", name="quantity_positive"),
    )

    shipment_id: Mapped[int] = mapped_column(
        ForeignKey("ecs_shipments.shipment_id", ondelete="CASCADE")
    )
    order_line_id: Mapped[int] = mapped_column(ForeignKey("ecs_order_line.order_line_id"))
    quantity: Mapped[int] = mapped_column(Integer)


class ShipmentDocument(Base):
    """Label, manifest, invoice or packing slip of a shipment; one current per type."""

    __tablename__ = "ecs_shipment_documents"
    __table_args__ = (
        Index(
            "uq_ecs_shipment_documents_current",
            "shipment_id",
            "doc_type",
            unique=True,
            postgresql_where=text("is_current"),
        ),
        CheckConstraint(in_check("doc_type", DOC_TYPES), name="doc_type"),
        CheckConstraint(in_check("source", DOC_SOURCES), name="source"),
        CheckConstraint("external_url IS NOT NULL OR storage_key IS NOT NULL", name="retrievable"),
    )

    document_id: Mapped[int] = bigint_pk()
    shipment_id: Mapped[int] = mapped_column(
        ForeignKey("ecs_shipments.shipment_id", ondelete="CASCADE")
    )
    doc_type: Mapped[str] = mapped_column(String(20))
    source: Mapped[str] = mapped_column(String(20))
    external_url: Mapped[str | None] = mapped_column(Text)  # aggregator-hosted, may expire
    storage_key: Mapped[str | None] = mapped_column(Text)  # our copy, once storage exists
    file_size_bytes: Mapped[int | None] = mapped_column(Integer)
    is_current: Mapped[bool] = mapped_column(Boolean, server_default=text("true"))
    generated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("now()")
    )
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class AggregatorStatusMapping(Base):
    """An aggregator's raw status code → canonical shipment status."""

    __tablename__ = "ecs_aggregator_status_mappings"
    __table_args__ = (
        PrimaryKeyConstraint("aggregator_id", "external_status_code"),
        CheckConstraint(in_check("canonical_status", SHIPMENT_STATUSES), name="canonical_status"),
    )

    aggregator_id: Mapped[int] = mapped_column(ForeignKey("ecs_courier_aggregators.aggregator_id"))
    external_status_code: Mapped[str] = mapped_column(String(20))
    external_status_label: Mapped[str | None] = mapped_column(String(100))
    canonical_status: Mapped[str] = mapped_column(String(20))
    is_terminal: Mapped[bool] = mapped_column(Boolean, server_default=text("false"))


class AggregatorWebhookEvent(Base):
    """Inbox for inbound aggregator webhooks: stored first, answered 200, processed by a
    worker. Shopify webhooks keep using ecs_order_import."""

    __tablename__ = "ecs_aggregator_webhook_events"
    __table_args__ = (
        Index(
            "ix_ecs_aggregator_webhook_events_queue",
            text("next_attempt_at NULLS FIRST"),
            "webhook_event_id",
            postgresql_where=text("processing_status IN ('pending', 'failed')"),
        ),
        Index(None, "awb_number"),
        CheckConstraint(in_check("processing_status", WEBHOOK_STATUSES), name="processing_status"),
        CheckConstraint("attempts >= 0", name="attempts_non_negative"),
    )

    webhook_event_id: Mapped[int] = bigint_pk()
    aggregator_id: Mapped[int] = mapped_column(ForeignKey("ecs_courier_aggregators.aggregator_id"))
    aggregator_account_id: Mapped[int | None] = mapped_column(
        ForeignKey("ecs_aggregator_accounts.aggregator_account_id")
    )
    shipment_id: Mapped[int | None] = mapped_column(ForeignKey("ecs_shipments.shipment_id"))
    awb_number: Mapped[str | None] = mapped_column(String(50))
    event_type: Mapped[str] = mapped_column(String(50), server_default="tracking_update")
    headers: Mapped[dict[str, Any] | None] = mapped_column(JSONB)  # x-api-key redacted
    payload: Mapped[Any] = mapped_column(JSONB)
    auth_valid: Mapped[bool] = mapped_column(Boolean)
    dedupe_key: Mapped[str] = mapped_column(String(64), unique=True)  # hash of the raw body
    processing_status: Mapped[str] = mapped_column(String(20), server_default="pending")
    attempts: Mapped[int] = mapped_column(Integer, server_default="0")
    next_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error: Mapped[str | None] = mapped_column(Text)
    received_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("now()")
    )
    processed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class ShipmentTrackingEvent(Base):
    """Append-only history of every scan / status event of a shipment."""

    __tablename__ = "ecs_shipment_tracking_events"
    __table_args__ = (
        UniqueConstraint("shipment_id", "dedupe_key"),
        Index(None, "shipment_id", text("event_at DESC")),
        CheckConstraint(in_check("canonical_status", SHIPMENT_STATUSES), name="canonical_status"),
        CheckConstraint(in_check("source", EVENT_SOURCES), name="source"),
    )

    tracking_event_id: Mapped[int] = bigint_pk()
    shipment_id: Mapped[int] = mapped_column(ForeignKey("ecs_shipments.shipment_id"))
    canonical_status: Mapped[str] = mapped_column(String(20))
    external_status_code: Mapped[str | None] = mapped_column(String(20))
    external_status_label: Mapped[str | None] = mapped_column(String(100))
    activity: Mapped[str | None] = mapped_column(Text)
    location: Mapped[str | None] = mapped_column(String(255))
    event_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    received_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("now()")
    )
    source: Mapped[str] = mapped_column(String(20))
    webhook_event_id: Mapped[int | None] = mapped_column(
        ForeignKey("ecs_aggregator_webhook_events.webhook_event_id")
    )
    dedupe_key: Mapped[str] = mapped_column(String(64))  # hash(awb, code, event_at, location)


class AggregatorApiCall(Base):
    """Every outbound call to an aggregator (credentials and tokens redacted)."""

    __tablename__ = "ecs_aggregator_api_calls"
    __table_args__ = (
        Index(None, "shipment_id", text("created_at DESC")),
        Index(
            "ix_ecs_aggregator_api_calls_failures",
            text("created_at DESC"),
            postgresql_where=text("NOT success"),
        ),
        CheckConstraint(in_check("operation", API_OPERATIONS), name="operation"),
    )

    api_call_id: Mapped[int] = bigint_pk()
    aggregator_account_id: Mapped[int] = mapped_column(
        ForeignKey("ecs_aggregator_accounts.aggregator_account_id")
    )
    shipment_id: Mapped[int | None] = mapped_column(ForeignKey("ecs_shipments.shipment_id"))
    operation: Mapped[str] = mapped_column(String(30))
    http_method: Mapped[str] = mapped_column(String(10))
    endpoint: Mapped[str] = mapped_column(String(255))
    request_body: Mapped[Any | None] = mapped_column(JSONB)
    response_body: Mapped[Any | None] = mapped_column(JSONB)
    http_status: Mapped[int | None] = mapped_column(Integer)  # null on network failure
    success: Mapped[bool] = mapped_column(Boolean)
    error_code: Mapped[str | None] = mapped_column(String(50))
    error_message: Mapped[str | None] = mapped_column(Text)
    duration_ms: Mapped[int | None] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("now()")
    )
