"""Inventory tables: supply & demand, returns and the stock history (docs/inventory-erd.md).

available = ONHAND − (OPEN + ALLOCATED + SHIPPED); ecs_items.stock_qty caches it.
"""

import uuid
from datetime import date, datetime

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, TimestampMixin, bigint_pk, in_check
from app.models.orders import CHANNELS

SUPPLY_TYPES = ("ONHAND", "RETURN_PENDING", "DAMAGED")
DEMAND_TYPES = ("OPEN", "ALLOCATED", "SHIPPED")
SIDES = ("SUPPLY", "DEMAND")

RETURN_TYPES = ("customer_return", "rto")
RETURN_STATUSES = ("requested", "open", "received", "qc_done", "closed", "declined", "cancelled")
RETURN_LINE_STATUSES = ("pending", "received", "qc_done", "cancelled")

MANUAL_STOCK_REASONS = ("restock", "damage", "correction", "return", "other")
STOCK_REASONS = (
    *MANUAL_STOCK_REASONS,
    # #21, before the supply & demand model; kept for history
    "order_created",
    "order_cancelled",
    "order_returned",
    "order_edited",
    # supply & demand
    "order_allocated",
    "order_shipped",
    "order_delivered",
    "order_rto",
    "return_opened",
    "return_accepted",
    "return_rejected",
    "return_cancelled",
    "return_closed",
    "damaged_repaired",
    "damaged_written_off",
    "opening_stock",
    "migration",
)


class InventorySupply(TimestampMixin, Base):
    """What the seller has (ONHAND), will get back (RETURN_PENDING) or can't sell (DAMAGED).
    One ONHAND and one DAMAGED row per item; one RETURN_PENDING row per return line."""

    __tablename__ = "ecs_inventory_supply"
    __table_args__ = (
        UniqueConstraint(
            "item_id", "supply_type", "return_line_id", postgresql_nulls_not_distinct=True
        ),
        ForeignKeyConstraint(
            ["item_id", "seller_id"],
            ["ecs_items.item_id", "ecs_items.seller_id"],
            name="fk_ecs_inventory_supply_item_seller",
        ),
        Index(None, "return_line_id"),
        CheckConstraint(in_check("supply_type", SUPPLY_TYPES), name="supply_type"),
        CheckConstraint(
            "(supply_type = 'RETURN_PENDING') = (return_line_id IS NOT NULL)",
            name="return_line_only_pending",
        ),
        CheckConstraint("eta IS NULL OR supply_type = 'RETURN_PENDING'", name="eta_only_pending"),
        # ONHAND may go below 0 only when a delivery is recorded for stock we never had.
        CheckConstraint("supply_type = 'ONHAND' OR quantity >= 0", name="quantity_non_negative"),
    )

    supply_id: Mapped[int] = bigint_pk()
    seller_id: Mapped[int] = mapped_column(ForeignKey("ecs_sellers.seller_id"))
    item_id: Mapped[int] = mapped_column(BigInteger)  # FK via fk_ecs_inventory_supply_item_seller
    supply_type: Mapped[str] = mapped_column(String(20))
    quantity: Mapped[int] = mapped_column(Integer, server_default="0")
    eta: Mapped[date | None] = mapped_column(Date)
    return_line_id: Mapped[int | None] = mapped_column(
        ForeignKey("ecs_return_lines.return_line_id")
    )


class InventoryDemand(TimestampMixin, Base):
    """What an order line claims, by state. Deleted when it reaches 0."""

    __tablename__ = "ecs_inventory_demand"
    __table_args__ = (
        UniqueConstraint("order_line_id", "demand_type"),
        ForeignKeyConstraint(
            ["item_id", "seller_id"],
            ["ecs_items.item_id", "ecs_items.seller_id"],
            name="fk_ecs_inventory_demand_item_seller",
        ),
        ForeignKeyConstraint(
            ["order_line_id", "order_header_id"],
            ["ecs_order_line.order_line_id", "ecs_order_line.order_header_id"],
            name="fk_ecs_inventory_demand_line_header",
        ),
        Index(None, "item_id", "demand_type"),
        Index(None, "order_header_id"),
        CheckConstraint(in_check("demand_type", DEMAND_TYPES), name="demand_type"),
        CheckConstraint("quantity > 0", name="quantity_positive"),
    )

    demand_id: Mapped[int] = bigint_pk()
    seller_id: Mapped[int] = mapped_column(ForeignKey("ecs_sellers.seller_id"))
    item_id: Mapped[int] = mapped_column(BigInteger)
    order_header_id: Mapped[int] = mapped_column(BigInteger)
    order_line_id: Mapped[int] = mapped_column(BigInteger)
    demand_type: Mapped[str] = mapped_column(String(20))
    quantity: Mapped[int] = mapped_column(Integer)


class Return(TimestampMixin, Base):
    """A return on any channel, or an RTO (shipped, undelivered, coming back)."""

    __tablename__ = "ecs_returns"
    __table_args__ = (
        UniqueConstraint("channel_account_id", "external_return_id"),
        Index(None, "order_header_id"),
        Index(None, "seller_id", "status"),
        CheckConstraint(in_check("channel", CHANNELS), name="channel"),
        CheckConstraint(in_check("return_type", RETURN_TYPES), name="return_type"),
        CheckConstraint(in_check("status", RETURN_STATUSES), name="status"),
        CheckConstraint("version_no >= 1", name="version_no_positive"),
    )

    return_id: Mapped[int] = bigint_pk()
    seller_id: Mapped[int] = mapped_column(ForeignKey("ecs_sellers.seller_id"))
    order_header_id: Mapped[int] = mapped_column(ForeignKey("ecs_order_header.order_header_id"))
    channel: Mapped[str] = mapped_column(String(30))
    channel_account_id: Mapped[int | None] = mapped_column(
        ForeignKey("ecs_channel_accounts.channel_account_id")
    )
    external_return_id: Mapped[str | None] = mapped_column(String(100))
    return_number: Mapped[str] = mapped_column(String(50))
    return_type: Mapped[str] = mapped_column(String(20))
    status: Mapped[str] = mapped_column(String(20))
    external_status: Mapped[str | None] = mapped_column(String(20))  # the channel's own status
    expected_at: Mapped[date | None] = mapped_column(Date)
    received_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    qc_completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    qc_by: Mapped[int | None] = mapped_column(BigInteger)  # FK once a users table exists
    note: Mapped[str | None] = mapped_column(Text)
    created_by_import_id: Mapped[int | None] = mapped_column(
        ForeignKey("ecs_order_import.order_import_id")
    )
    last_import_id: Mapped[int | None] = mapped_column(
        ForeignKey("ecs_order_import.order_import_id")
    )
    external_created_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    external_updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    version_no: Mapped[int] = mapped_column(Integer, server_default="1")

    lines: Mapped[list["ReturnLine"]] = relationship(
        back_populates="return_", order_by="ReturnLine.return_line_id"
    )


class ReturnLine(Base):
    """One order line on a return, with what came back and the QC outcome."""

    __tablename__ = "ecs_return_lines"
    __table_args__ = (
        UniqueConstraint("return_id", "order_line_id"),
        Index(None, "order_line_id"),
        Index(None, "item_id"),
        CheckConstraint(in_check("status", RETURN_LINE_STATUSES), name="status"),
        CheckConstraint("return_qty > 0", name="return_qty_positive"),
        CheckConstraint(
            "received_qty >= 0 AND accepted_qty >= 0 AND rejected_qty >= 0 AND missing_qty >= 0"
            " AND opening_accepted_qty >= 0 AND opening_rejected_qty >= 0",
            name="quantities_non_negative",
        ),
        CheckConstraint(
            "accepted_qty + rejected_qty <= received_qty AND received_qty <= return_qty"
            " AND accepted_qty + rejected_qty + missing_qty <= return_qty",
            name="quantities_consistent",
        ),
        CheckConstraint(
            "opening_accepted_qty <= accepted_qty AND opening_rejected_qty <= rejected_qty",
            name="opening_within_outcome",
        ),
    )

    return_line_id: Mapped[int] = bigint_pk()
    return_id: Mapped[int] = mapped_column(ForeignKey("ecs_returns.return_id"))
    order_line_id: Mapped[int] = mapped_column(ForeignKey("ecs_order_line.order_line_id"))
    item_id: Mapped[int | None] = mapped_column(ForeignKey("ecs_items.item_id"))  # null: unlinked
    external_return_line_id: Mapped[str | None] = mapped_column(String(100))
    # Shopify reverse fulfilment order line item id, used to report the QC outcome.
    external_disposal_ref: Mapped[str | None] = mapped_column(String(100))
    return_qty: Mapped[int] = mapped_column(Integer)
    received_qty: Mapped[int] = mapped_column(Integer, server_default="0")
    accepted_qty: Mapped[int] = mapped_column(Integer, server_default="0")
    rejected_qty: Mapped[int] = mapped_column(Integer, server_default="0")
    missing_qty: Mapped[int] = mapped_column(Integer, server_default="0")
    # Outcomes already reflected in the item's starting stock: they never move supply.
    opening_accepted_qty: Mapped[int] = mapped_column(Integer, server_default="0")
    opening_rejected_qty: Mapped[int] = mapped_column(Integer, server_default="0")
    return_reason: Mapped[str | None] = mapped_column(String(50))
    customer_note: Mapped[str | None] = mapped_column(Text)
    qc_note: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(20), server_default="pending")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("now()")
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("now()"), onupdate=text("now()")
    )

    return_: Mapped[Return] = relationship(back_populates="lines")


class InventoryLog(Base):
    """Append-only stock history: one row per change to a supply or demand row (rows
    from before the supply & demand model have no side and track stock_qty)."""

    __tablename__ = "ecs_inventory_log"
    __table_args__ = (
        Index(None, "item_id", "created_at"),
        Index(None, "order_line_id"),
        Index(None, "return_line_id"),
        Index(None, "transaction_id"),
        CheckConstraint(in_check("reason", STOCK_REASONS), name="reason"),
        CheckConstraint("qty_change <> 0", name="qty_change_non_zero"),
        CheckConstraint(f"side IS NULL OR {in_check('side', SIDES)}", name="side"),
        CheckConstraint(
            f"inv_type IS NULL OR {in_check('inv_type', (*SUPPLY_TYPES, *DEMAND_TYPES))}",
            name="inv_type",
        ),
    )

    inventory_log_id: Mapped[int] = bigint_pk()
    item_id: Mapped[int] = mapped_column(ForeignKey("ecs_items.item_id"))
    side: Mapped[str | None] = mapped_column(String(10))
    inv_type: Mapped[str | None] = mapped_column(String(20))
    qty_change: Mapped[int] = mapped_column(Integer)
    stock_after: Mapped[int] = mapped_column(Integer)  # that type's quantity after the change
    reason: Mapped[str] = mapped_column(String(20))
    note: Mapped[str | None] = mapped_column(Text)
    order_line_id: Mapped[int | None] = mapped_column(ForeignKey("ecs_order_line.order_line_id"))
    return_line_id: Mapped[int | None] = mapped_column(
        ForeignKey("ecs_return_lines.return_line_id")
    )
    transaction_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    created_by: Mapped[int | None] = mapped_column(BigInteger)  # FK once a users table exists
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("now()")
    )
