"""Catalog reads and product creation (#43, #44, #45; docs/catalog-erd.md)."""

from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import and_, exists, func, or_, select
from sqlalchemy.orm import Session

from app.models import (
    Article,
    Channel,
    ChannelAccount,
    ChannelListing,
    ChannelListingItem,
    Item,
)
from app.services.product_import import image_for
from app.services.stock import open_item_stock

LOW_STOCK = 5  # an item at or below this many units is low on stock
MAX_VARIANTS = 100  # Shopify's classic variant limit, so a product can be published


def stock_status(stock_qty: int) -> str:
    if stock_qty <= 0:
        return "out"
    return "low" if stock_qty <= LOW_STOCK else "ok"


def like_pattern(term: str) -> str:
    """A case-insensitive substring pattern with % and _ matched literally."""
    escaped = term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}%"


# --- List (#43) ---------------------------------------------------------------------------


def list_products(
    session: Session,
    seller_id: int,
    *,
    status: str | None = None,
    q: str | None = None,
    channel_status: str | None = None,
    low_stock: bool = False,
    sort: str = "newest",
    limit: int = 50,
    offset: int = 0,
) -> tuple[int, list[dict[str, Any]]]:
    """Articles with their item, stock, price and channel summaries."""
    filters = [Article.seller_id == seller_id]
    if status:
        filters.append(Article.status == status)
    if q and q.strip():
        pattern = like_pattern(q.strip())
        item_match = exists().where(
            Item.article_id == Article.article_id,
            or_(Item.sku.ilike(pattern, escape="\\"), Item.barcode.ilike(pattern, escape="\\")),
        )
        filters.append(
            or_(
                Article.name.ilike(pattern, escape="\\"),
                Article.article_code.ilike(pattern, escape="\\"),
                Article.brand.ilike(pattern, escape="\\"),
                item_match,
            )
        )
    listed = exists().where(ChannelListing.article_id == Article.article_id)
    live = exists().where(
        ChannelListing.article_id == Article.article_id, ChannelListing.status == "live"
    )
    if channel_status == "live":
        filters.append(live)
    elif channel_status == "inactive":
        filters.append(and_(listed, ~live))
    elif channel_status == "unlisted":
        filters.append(~listed)
    if low_stock:
        filters.append(
            exists().where(
                Item.article_id == Article.article_id,
                Item.status == "active",
                Item.stock_qty <= LOW_STOCK,
            )
        )

    total = session.scalar(select(func.count()).select_from(Article).where(*filters))
    stock_total = (
        select(func.coalesce(func.sum(Item.stock_qty), 0))
        .where(Item.article_id == Article.article_id, Item.status == "active")
        .scalar_subquery()
    )
    order = {
        "name": (func.lower(Article.name), Article.article_id),
        "stock": (stock_total, Article.article_id),
    }.get(sort, (Article.created_at.desc(), Article.article_id.desc()))
    articles = session.scalars(
        select(Article).where(*filters).order_by(*order).limit(limit).offset(offset)
    ).all()
    return total, summaries(session, articles)


def summaries(session: Session, articles: list[Article]) -> list[dict[str, Any]]:
    ids = [article.article_id for article in articles]
    if not ids:
        return []
    item_stats = {
        row.article_id: row
        for row in session.execute(
            select(
                Item.article_id,
                func.count().label("items"),
                func.coalesce(func.sum(Item.stock_qty).filter(Item.status == "active"), 0).label(
                    "stock"
                ),
                func.min(Item.mrp).label("mrp_min"),
                func.max(Item.mrp).label("mrp_max"),
                func.count()
                .filter(Item.status == "active", Item.stock_qty <= LOW_STOCK)
                .label("low"),
            )
            .where(Item.article_id.in_(ids))
            .group_by(Item.article_id)
        )
    }
    prices = {
        row.article_id: row
        for row in session.execute(
            select(
                Item.article_id,
                func.min(ChannelListingItem.selling_price).label("low"),
                func.max(ChannelListingItem.selling_price).label("high"),
            )
            .join(ChannelListingItem, ChannelListingItem.item_id == Item.item_id)
            .where(Item.article_id.in_(ids))
            .group_by(Item.article_id)
        )
    }
    channels: dict[int, list[dict[str, Any]]] = {}
    for row in session.execute(
        select(
            ChannelListing.article_id,
            ChannelListing.listing_id,
            ChannelListing.status,
            ChannelAccount.channel_account_id,
            ChannelAccount.account_name,
            Channel.code,
        )
        .join(
            ChannelAccount, ChannelAccount.channel_account_id == ChannelListing.channel_account_id
        )
        .join(Channel, Channel.channel_id == ChannelAccount.channel_id)
        .where(ChannelListing.article_id.in_(ids))
        .order_by(ChannelListing.listing_id)
    ):
        channels.setdefault(row.article_id, []).append(
            {
                "listing_id": row.listing_id,
                "channel_account_id": row.channel_account_id,
                "account_name": row.account_name,
                "channel_code": row.code,
                "status": row.status,
            }
        )

    result = []
    for article in articles:
        stats = item_stats.get(article.article_id)
        price = prices.get(article.article_id)
        low, high = (
            (price.low, price.high)
            if price and price.low is not None
            else ((stats.mrp_min, stats.mrp_max) if stats else (None, None))
        )
        result.append(
            {
                "article_id": article.article_id,
                "article_code": article.article_code,
                "name": article.name,
                "brand": article.brand,
                "category": article.category,
                "status": article.status,
                "image_url": image_for(article.images, {}),
                "item_count": stats.items if stats else 0,
                "low_stock_items": stats.low if stats else 0,
                "total_stock": stats.stock if stats else 0,
                "price_min": low,
                "price_max": high,
                "channels": channels.get(article.article_id, []),
                "created_at": article.created_at,
            }
        )
    return result


def list_items(session: Session, seller_id: int) -> list[dict[str, Any]]:
    """Every item with its article (the item-level list from #13)."""
    rows = session.execute(
        select(Item, Article.name, Article.status)
        .join(Article, Item.article_id == Article.article_id)
        .where(Item.seller_id == seller_id)
        .order_by(Article.name, Item.sku)
    ).all()
    return [
        {
            "item_id": item.item_id,
            "article_id": item.article_id,
            "article_name": name,
            "article_status": article_status,
            "sku": item.sku,
            "barcode": item.barcode,
            "variant_values": item.variant_values,
            "mrp": item.mrp,
            "stock_qty": item.stock_qty,
            "status": item.status,
        }
        for item, name, article_status in rows
    ]


# --- Detail (#44) -------------------------------------------------------------------------


def product_detail(session: Session, seller_id: int, article_id: int) -> dict[str, Any] | None:
    article = session.get(Article, article_id)
    if article is None or article.seller_id != seller_id:
        return None
    items = sorted(article.items, key=lambda item: item.item_id)
    listings = session.execute(
        select(ChannelListing, ChannelAccount.account_name, Channel.code, Channel.name)
        .join(
            ChannelAccount, ChannelAccount.channel_account_id == ChannelListing.channel_account_id
        )
        .join(Channel, Channel.channel_id == ChannelAccount.channel_id)
        .where(ChannelListing.article_id == article_id)
        .order_by(ChannelListing.listing_id)
    ).all()
    return {
        "article_id": article.article_id,
        "article_code": article.article_code,
        "name": article.name,
        "description": article.description,
        "brand": article.brand,
        "category": article.category,
        "variation_axes": article.variation_axes,
        "attributes": article.attributes,
        "images": article.images,
        "image_url": image_for(article.images, {}),
        "status": article.status,
        "created_at": article.created_at,
        "updated_at": article.updated_at,
        "items": [
            {
                "item_id": item.item_id,
                "sku": item.sku,
                "barcode": item.barcode,
                "variant_values": item.variant_values,
                "mrp": item.mrp,
                "weight": item.weight,
                "stock_qty": item.stock_qty,
                "stock_status": stock_status(item.stock_qty),
                "stock_baseline_at": item.stock_baseline_at,
                "status": item.status,
                "image_url": image_for(article.images, item.variant_values),
            }
            for item in items
        ],
        "listings": [
            {
                "listing_id": listing.listing_id,
                "channel_account_id": listing.channel_account_id,
                "account_name": account_name,
                "channel_code": code,
                "channel_name": channel_name,
                "split_key": listing.split_key,
                "title": listing.title,
                "description": listing.description,
                "channel_parent_id": listing.channel_parent_id,
                "status": listing.status,
                "items": [
                    {
                        "listing_item_id": li.listing_item_id,
                        "item_id": li.item_id,
                        "channel_sku": li.channel_sku,
                        "channel_item_id": li.channel_item_id,
                        "selling_price": li.selling_price,
                        "status": li.status,
                        "last_sync_error": li.last_sync_error,
                    }
                    for li in sorted(listing.items, key=lambda li: li.item_id)
                ],
            }
            for listing, account_name, code, channel_name in listings
        ],
    }


# --- Create (#45) -------------------------------------------------------------------------


@dataclass
class NewItem:
    sku: str
    variant_values: dict[str, str]
    barcode: str | None = None
    mrp: Decimal | None = None
    weight: Decimal | None = None
    stock_qty: int = 0


class ProductInvalid(ValueError):
    """Field errors in FastAPI's format: [{"loc": [...], "msg": ...}]."""

    def __init__(self, errors: list[dict[str, Any]]) -> None:
        super().__init__("; ".join(error["msg"] for error in errors))
        self.errors = errors


def error(msg: str, *loc: Any) -> dict[str, Any]:
    return {"loc": ["body", *loc], "msg": msg, "type": "value_error"}


def validate_new_product(
    session: Session,
    seller_id: int,
    article_code: str,
    axes: list[str],
    items: list[NewItem],
    images: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """The catalog ERD's validation rules plus per-seller uniqueness. Empty when valid."""
    errors = []
    if any(not axis for axis in axes):
        errors.append(error("Option names can't be blank.", "variation_axes"))
    if len({axis.lower() for axis in axes}) != len(axes):
        errors.append(error("Option names must be different.", "variation_axes"))
    if not items:
        errors.append(error("Add at least one variant.", "items"))
    if len(items) > MAX_VARIANTS:
        errors.append(error(f"At most {MAX_VARIANTS} variants.", "items"))
    if axes == [] and len(items) > 1:
        errors.append(error("A product without options has exactly one variant.", "items"))

    seen_values: dict[tuple, int] = {}
    seen_skus: dict[str, int] = {}
    for index, item in enumerate(items):
        if set(item.variant_values) != set(axes):  # ERD rule 1
            errors.append(
                error(
                    f"Needs a value for exactly these options: {', '.join(axes) or 'none'}.",
                    "items",
                    index,
                    "variant_values",
                )
            )
        elif any(not value.strip() for value in item.variant_values.values()):
            errors.append(error("Option values can't be blank.", "items", index, "variant_values"))
        key = tuple((axis, item.variant_values.get(axis, "").strip().lower()) for axis in axes)
        if key in seen_values:  # ERD rule 2
            errors.append(
                error(
                    f"Same options as variant {seen_values[key] + 1}.",
                    "items",
                    index,
                    "variant_values",
                )
            )
        seen_values.setdefault(key, index)
        sku = item.sku.lower()
        if sku in seen_skus:
            errors.append(
                error(f"Same SKU as variant {seen_skus[sku] + 1}.", "items", index, "sku")
            )
        seen_skus.setdefault(sku, index)

    for index, image in enumerate(images):
        for axis, value in (image.get("filter") or {}).items():
            if axis not in axes or not any(i.variant_values.get(axis) == value for i in items):
                errors.append(error(f"No variant has {axis}: {value}.", "images", index, "filter"))

    if session.scalar(
        select(Article.article_id).where(
            Article.seller_id == seller_id, func.lower(Article.article_code) == article_code.lower()
        )
    ):
        errors.append(error("Another product already uses this code.", "article_code"))
    taken = set(
        session.scalars(
            select(func.lower(Item.sku)).where(
                Item.seller_id == seller_id, func.lower(Item.sku).in_(list(seen_skus))
            )
        )
    )
    for index, item in enumerate(items):
        if item.sku.lower() in taken:
            errors.append(error("Another product already uses this SKU.", "items", index, "sku"))
    return errors


def create_product(
    session: Session,
    seller_id: int,
    *,
    article_code: str,
    name: str,
    description: str | None,
    brand: str | None,
    category: str | None,
    variation_axes: list[str],
    attributes: dict[str, str],
    images: list[dict[str, Any]],
    items: list[NewItem],
) -> Article:
    """Create a draft article with its items in the caller's transaction, or raise
    ProductInvalid with every problem found."""
    errors = validate_new_product(session, seller_id, article_code, variation_axes, items, images)
    if errors:
        raise ProductInvalid(errors)
    now = datetime.now(UTC)
    article = Article(
        seller_id=seller_id,
        article_code=article_code,
        name=name,
        description=description,
        brand=brand,
        category=category,
        variation_axes=variation_axes,
        attributes=attributes,
        images=images,
        status="draft",
    )
    article.items = [
        Item(
            seller_id=seller_id,
            sku=item.sku,
            barcode=item.barcode,
            variant_values={axis: item.variant_values[axis].strip() for axis in variation_axes},
            mrp=item.mrp,
            weight=item.weight,
            stock_qty=item.stock_qty,
            stock_baseline_at=now,  # the opening stock is this app's own figure
        )
        for item in items
    ]
    session.add(article)
    session.flush()
    open_item_stock(session, article.items)
    return article
