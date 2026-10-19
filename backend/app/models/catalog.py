"""Product catalog tables (docs/catalog-erd.md).

A seller's products (articles and their items) are stored once; each channel account
gets its own listings saying what is live there and under what name.
"""

from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, TimestampMixin, bigint_pk, in_check

SELLER_STATUSES = ("active", "suspended")
ARTICLE_STATUSES = ("draft", "active", "archived")
ITEM_STATUSES = ("active", "inactive")
LISTING_STATUSES = ("draft", "live", "inactive", "rejected")
LISTING_ITEM_STATUSES = ("live", "inactive")
CONNECTION_STATUSES = ("active", "auth_expired", "disconnected", "error")

EMPTY_LIST = text("'[]'::jsonb")
EMPTY_OBJECT = text("'{}'::jsonb")


class Seller(TimestampMixin, Base):
    """A tenant of the app."""

    __tablename__ = "ecs_sellers"
    __table_args__ = (
        CheckConstraint(in_check("status", SELLER_STATUSES), name="status"),
        CheckConstraint("return_eta_days > 0", name="return_eta_days_positive"),
    )

    seller_id: Mapped[int] = bigint_pk()
    name: Mapped[str] = mapped_column(String(255))
    status: Mapped[str] = mapped_column(String(20), server_default="active")
    # Days after a return is approved that its units are expected back (inventory ERD).
    return_eta_days: Mapped[int] = mapped_column(Integer, server_default="10")


class Channel(TimestampMixin, Base):
    """A marketplace and its default rule for splitting an article into listings."""

    __tablename__ = "ecs_channels"

    channel_id: Mapped[int] = bigint_pk()
    code: Mapped[str] = mapped_column(String(50), unique=True)  # AMAZON, FLIPKART, SHOPIFY…
    name: Mapped[str] = mapped_column(String(100))
    # Variation axes that become separate listings on this channel, e.g. [] or ["Colour"].
    default_split_axes: Mapped[list[str]] = mapped_column(JSONB, server_default=EMPTY_LIST)


class ChannelAccount(TimestampMixin, Base):
    """A seller's account on one channel: used by catalog listings, and by order import,
    sync and webhooks (the order ERD's "channel connection", merged into this table)."""

    __tablename__ = "ecs_channel_accounts"
    __table_args__ = (
        UniqueConstraint("seller_id", "account_name"),
        # One account per marketplace account (e.g. per Shopify store domain).
        UniqueConstraint("seller_id", "channel_id", "external_seller_id"),
        Index(None, "channel_id"),
        CheckConstraint(
            in_check("connection_status", CONNECTION_STATUSES), name="connection_status"
        ),
        CheckConstraint("sync_interval_minutes > 0", name="sync_interval_positive"),
    )

    channel_account_id: Mapped[int] = bigint_pk()
    seller_id: Mapped[int] = mapped_column(ForeignKey("ecs_sellers.seller_id"))
    channel_id: Mapped[int] = mapped_column(ForeignKey("ecs_channels.channel_id"))
    account_name: Mapped[str] = mapped_column(String(100))
    # Seller id on the marketplace; for Shopify, the store domain.
    external_seller_id: Mapped[str | None] = mapped_column(String(100))
    is_active: Mapped[bool] = mapped_column(Boolean, server_default=text("true"))

    # Order import, sync and webhooks (from the order ERD).
    marketplace_region: Mapped[str | None] = mapped_column(String(30))
    # Pointer to the credentials (Phase 0: env var names); secrets are never stored here.
    credential_ref: Mapped[str | None] = mapped_column(String(255))
    connection_status: Mapped[str] = mapped_column(String(20), server_default="active")
    sync_enabled: Mapped[bool] = mapped_column(Boolean, server_default=text("true"))
    sync_interval_minutes: Mapped[int] = mapped_column(Integer, server_default="15")
    sync_cursor: Mapped[str | None] = mapped_column(String(500))
    last_synced_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_sync_status: Mapped[str | None] = mapped_column(String(20))
    webhook_enabled: Mapped[bool] = mapped_column(Boolean, server_default=text("false"))
    webhook_secret_ref: Mapped[str | None] = mapped_column(String(255))
    webhook_topics: Mapped[list[str] | None] = mapped_column(JSONB)
    created_by: Mapped[int | None] = mapped_column(BigInteger)  # FK once a users table exists


class Article(TimestampMixin, Base):
    """The master product: its variation axes and shared attributes."""

    __tablename__ = "ecs_articles"
    __table_args__ = (
        UniqueConstraint("seller_id", "article_code"),
        # Target of ecs_items' composite foreign key (an item's seller is its article's).
        UniqueConstraint("article_id", "seller_id"),
        CheckConstraint(in_check("status", ARTICLE_STATUSES), name="status"),
    )

    article_id: Mapped[int] = bigint_pk()
    seller_id: Mapped[int] = mapped_column(ForeignKey("ecs_sellers.seller_id"))
    article_code: Mapped[str] = mapped_column(String(100))
    name: Mapped[str] = mapped_column(String(255))  # master name
    description: Mapped[str | None] = mapped_column(Text)
    brand: Mapped[str | None] = mapped_column(String(100))  # plain text in the MVP
    category: Mapped[str | None] = mapped_column(String(255))  # plain text in the MVP
    # Ordered list, e.g. ["Colour", "Size"]; [] for a product without variants.
    variation_axes: Mapped[list[str]] = mapped_column(JSONB, server_default=EMPTY_LIST)
    attributes: Mapped[dict[str, Any]] = mapped_column(JSONB, server_default=EMPTY_OBJECT)
    # [{"url": "...", "filter": {"Colour": "Black"}}]
    images: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, server_default=EMPTY_LIST)
    status: Mapped[str] = mapped_column(String(20), server_default="draft")

    items: Mapped[list["Item"]] = relationship(back_populates="article", order_by="Item.item_id")


class Item(TimestampMixin, Base):
    """A sellable variant (SKU) with its axis values."""

    __tablename__ = "ecs_items"
    __table_args__ = (
        UniqueConstraint("seller_id", "sku"),
        # Target of the inventory tables' composite foreign keys.
        UniqueConstraint("item_id", "seller_id"),
        # seller_id is copied from the article (for the SKU constraint); this keeps it equal.
        ForeignKeyConstraint(
            ["article_id", "seller_id"],
            ["ecs_articles.article_id", "ecs_articles.seller_id"],
            name="fk_ecs_items_article_seller",
        ),
        Index(None, "article_id"),
        CheckConstraint(in_check("status", ITEM_STATUSES), name="status"),
        CheckConstraint("mrp IS NULL OR mrp >= 0", name="mrp_non_negative"),
        CheckConstraint("weight IS NULL OR weight >= 0", name="weight_non_negative"),
    )

    item_id: Mapped[int] = bigint_pk()
    article_id: Mapped[int] = mapped_column(BigInteger)  # FK via fk_ecs_items_article_seller
    seller_id: Mapped[int] = mapped_column(ForeignKey("ecs_sellers.seller_id"))
    sku: Mapped[str] = mapped_column(String(100))
    barcode: Mapped[str | None] = mapped_column(String(50))  # EAN / UPC / GTIN
    variant_values: Mapped[dict[str, str]] = mapped_column(JSONB, server_default=EMPTY_OBJECT)
    mrp: Mapped[Decimal | None] = mapped_column(Numeric(12, 2))
    # Single stock figure in the MVP. May go negative when an order oversells (#21).
    stock_qty: Mapped[int] = mapped_column(Integer, server_default="0")
    # When stock_qty was last copied from a channel (#13); orders placed before it are
    # already reflected in it, so #21 doesn't deduct them. Extension of the catalog ERD.
    stock_baseline_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    weight: Mapped[Decimal | None] = mapped_column(Numeric(10, 3))  # grams
    status: Mapped[str] = mapped_column(String(20), server_default="active")

    article: Mapped[Article] = relationship(back_populates="items")


class ChannelListing(TimestampMixin, Base):
    """An article's listing on one channel account, for one combination of split values."""

    __tablename__ = "ecs_channel_listings"
    __table_args__ = (
        UniqueConstraint("article_id", "channel_account_id", "split_key"),
        # Target of ecs_channel_listing_items' composite foreign key.
        UniqueConstraint("listing_id", "channel_account_id"),
        # Finding a listing by the channel's product id (re-imports).
        Index(None, "channel_account_id", "channel_parent_id"),
        CheckConstraint(in_check("status", LISTING_STATUSES), name="status"),
    )

    listing_id: Mapped[int] = bigint_pk()
    article_id: Mapped[int] = mapped_column(ForeignKey("ecs_articles.article_id"))
    channel_account_id: Mapped[int] = mapped_column(
        ForeignKey("ecs_channel_accounts.channel_account_id")
    )
    # {} or {"Colour": "Black"}: the split-axis values this listing covers.
    split_key: Mapped[dict[str, str]] = mapped_column(JSONB, server_default=EMPTY_OBJECT)
    title: Mapped[str | None] = mapped_column(String(500))  # NULL = ecs_articles.name
    description: Mapped[str | None] = mapped_column(Text)  # NULL = ecs_articles.description
    channel_parent_id: Mapped[str | None] = mapped_column(String(100))
    status: Mapped[str] = mapped_column(String(20), server_default="draft")

    items: Mapped[list["ChannelListingItem"]] = relationship(back_populates="listing")


class ChannelListingItem(TimestampMixin, Base):
    """An item inside a listing: its channel ids, price and status on that account."""

    __tablename__ = "ecs_channel_listing_items"
    __table_args__ = (
        # An item may sit in only one listing per account.
        UniqueConstraint("item_id", "channel_account_id"),
        # channel_account_id is copied from the listing; this keeps it equal.
        ForeignKeyConstraint(
            ["listing_id", "channel_account_id"],
            ["ecs_channel_listings.listing_id", "ecs_channel_listings.channel_account_id"],
            name="fk_ecs_channel_listing_items_listing_account",
        ),
        Index(None, "listing_id"),
        # Finding a listing item by the channel's variant id (re-imports, order linking).
        Index(None, "channel_account_id", "channel_item_id"),
        CheckConstraint(in_check("status", LISTING_ITEM_STATUSES), name="status"),
        CheckConstraint(
            "selling_price IS NULL OR selling_price >= 0", name="selling_price_non_negative"
        ),
    )

    listing_item_id: Mapped[int] = bigint_pk()
    listing_id: Mapped[int] = mapped_column(BigInteger)  # FK via ..._listing_account
    item_id: Mapped[int] = mapped_column(ForeignKey("ecs_items.item_id"))
    channel_account_id: Mapped[int] = mapped_column(
        ForeignKey("ecs_channel_accounts.channel_account_id")
    )
    channel_sku: Mapped[str | None] = mapped_column(String(100))
    channel_item_id: Mapped[str | None] = mapped_column(String(100))  # ASIN / FSN / …
    selling_price: Mapped[Decimal | None] = mapped_column(Numeric(12, 2))
    status: Mapped[str] = mapped_column(String(20), server_default="inactive")
    last_sync_error: Mapped[str | None] = mapped_column(Text)  # last error from the channel

    listing: Mapped[ChannelListing] = relationship(back_populates="items")
