"""Import the Shopify catalogue into the catalog tables (#13, docs/catalog-erd.md).

Shopify product → ecs_articles + one ecs_channel_listings row on the store's account;
variant → ecs_items + one ecs_channel_listing_items row. Re-imports match by Shopify id
(listing.channel_parent_id, listing item.channel_item_id), never by name or SKU, so
renames in Shopify update rather than duplicate. Stock is copied only when an item is
first created (its stock baseline); later imports never overwrite it.
"""

import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import Any

from sqlalchemy import func, select, text
from sqlalchemy.orm import Session, sessionmaker

from app.db import SessionLocal
from app.models import (
    Article,
    ChannelAccount,
    ChannelListing,
    ChannelListingItem,
    Item,
    OrderHeader,
    OrderImportRow,
    OrderLine,
)
from app.services.order_mapping import clip
from app.services.shopify import ShopifyClient, get_shopify_client

logger = logging.getLogger(__name__)

# Shopify gives products without real options a single "Title" option, "Default Title".
DEFAULT_OPTION = ("Title", "Default Title")
GRAMS_PER_UNIT = {
    "g": Decimal(1),
    "kg": Decimal(1000),
    "lb": Decimal("453.592"),
    "oz": Decimal("28.3495"),
}
ARTICLE_STATUS = {"active": "active", "draft": "draft", "archived": "archived"}

shopify_client_factory: Callable[[], ShopifyClient] = get_shopify_client  # swapped in tests


class ImportInProgress(Exception):
    pass


class ProductError(ValueError):
    """A product that can't be imported as it is; the rest of the import continues."""


@dataclass
class ImportSummary:
    products: int = 0
    articles_created: int = 0
    articles_updated: int = 0
    items_created: int = 0
    items_updated: int = 0
    products_deactivated: int = 0
    variants_deactivated: int = 0
    untracked_stock: int = 0
    order_lines_linked: int = 0
    complete: bool = False
    warnings: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


# --- Mapping ------------------------------------------------------------------------------


def to_decimal(value: Any) -> Decimal | None:
    if value in (None, ""):
        return None
    try:
        return Decimal(str(value)).quantize(Decimal("0.01"))
    except InvalidOperation:
        return None


def variant_axes(product: dict[str, Any]) -> list[str]:
    options = product.get("options") or []
    names = [str(option.get("name") or "").strip() for option in options]
    if (
        len(options) == 1
        and names[0] == DEFAULT_OPTION[0]
        and options[0].get("values")
        in (
            [DEFAULT_OPTION[1]],
            None,
        )
    ):
        return []
    return names


def variant_values(variant: dict[str, Any], axes: list[str]) -> dict[str, str]:
    return {axis: str(variant.get(f"option{i}") or "") for i, axis in enumerate(axes, 1)}


def variant_weight_grams(variant: dict[str, Any]) -> Decimal | None:
    if variant.get("grams"):
        return Decimal(str(variant["grams"]))
    weight, unit = variant.get("weight"), (variant.get("weight_unit") or "").lower()
    if weight and unit in GRAMS_PER_UNIT:
        return (Decimal(str(weight)) * GRAMS_PER_UNIT[unit]).quantize(Decimal("0.001"))
    return None


def product_images(product: dict[str, Any], values_by_variant: dict[int, dict[str, str]]):
    """Image URLs; an image tied to variants gets the axis values those variants share."""
    images = []
    for image in product.get("images") or []:
        if not image.get("src"):
            continue
        tied = [
            values_by_variant[v] for v in image.get("variant_ids") or [] if v in values_by_variant
        ]
        shared = dict(set.intersection(*(set(v.items()) for v in tied))) if tied else {}
        images.append({"url": image["src"], "filter": shared})
    return images


def image_for(images: list[dict[str, Any]] | None, values: dict[str, str]) -> str | None:
    """The image to show for a variant: the most specific image whose filter matches its
    option values (e.g. the black watch for the black variant), else the article's first
    image. None when the article has no images."""
    images = [image for image in images or [] if image.get("url")]
    matching = [
        image
        for image in images
        if all(values.get(axis) == value for axis, value in (image.get("filter") or {}).items())
    ]
    if matching:
        return max(matching, key=lambda image: len(image.get("filter") or {}))["url"]
    return images[0]["url"] if images else None


def validate(product: dict[str, Any], axes: list[str]) -> None:
    """The ERD's validation rules 1 and 2, before anything is written."""
    if not product.get("id"):
        raise ProductError("product has no id")
    if len(set(axes)) != len(axes) or any(not axis for axis in axes):
        raise ProductError(f"option names are blank or repeated: {axes}")
    seen = set()
    for variant in product.get("variants") or []:
        if not variant.get("id"):
            raise ProductError("a variant has no id")
        key = tuple(variant_values(variant, axes).items())
        if key in seen:
            raise ProductError(f"two variants share the option values {dict(key)}")
        seen.add(key)


# --- Import -------------------------------------------------------------------------------


def import_products(
    account: ChannelAccount,
    *,
    client_factory: Callable[[], ShopifyClient] | None = None,
    session_factory: sessionmaker[Session] = SessionLocal,
) -> ImportSummary:
    """Import every Shopify product for this account. One product per transaction, so one
    bad product doesn't stop the rest; products missing from Shopify are deactivated only
    when every page was fetched. Raises ImportInProgress if another import is running."""
    summary = ImportSummary()
    seen_products: set[str] = set()
    with session_factory() as lock_session:
        # Session-level advisory lock: one product import per account at a time.
        locked = lock_session.scalar(
            text("SELECT pg_try_advisory_lock(:key)"),
            {"key": 13_000_000 + account.channel_account_id},
        )
        if not locked:
            raise ImportInProgress
        try:
            with (client_factory or shopify_client_factory)() as shopify:
                for product in shopify.paginate("/products.json", "products", {}):
                    summary.products += 1
                    seen_products.add(str(product.get("id")))
                    try:
                        with session_factory.begin() as session:
                            import_product(session, account, product, summary)
                    except ProductError as exc:
                        summary.errors.append(
                            f"Product {product.get('id')} ({product.get('title')}): {exc}"
                        )
                    except Exception as exc:
                        logger.exception("Importing Shopify product %s failed", product.get("id"))
                        summary.errors.append(
                            f"Product {product.get('id')}: {type(exc).__name__}: {exc}"
                        )
            summary.complete = True
        except Exception as exc:
            logger.exception("Shopify product import stopped")
            summary.errors.append(f"Import stopped after {summary.products} products: {exc}")
        finally:
            lock_session.execute(
                text("SELECT pg_advisory_unlock(:key)"),
                {"key": 13_000_000 + account.channel_account_id},
            )

    if summary.complete:
        with session_factory.begin() as session:
            deactivate_missing(session, account, seen_products, summary)
    with session_factory.begin() as session:
        summary.order_lines_linked = link_order_lines(session, account)
    return summary


def import_product(
    session: Session, account: ChannelAccount, product: dict[str, Any], summary: ImportSummary
) -> None:
    axes = variant_axes(product)
    validate(product, axes)
    variants = product.get("variants") or []
    values_by_variant = {v["id"]: variant_values(v, axes) for v in variants}
    now = datetime.now(UTC)
    shopify_status = product.get("status") or "active"
    published = shopify_status == "active" and bool(product.get("published_at"))
    listing_status = "live" if published else ("draft" if shopify_status == "draft" else "inactive")

    listing = session.scalars(
        select(ChannelListing).where(
            ChannelListing.channel_account_id == account.channel_account_id,
            ChannelListing.channel_parent_id == str(product["id"]),
        )
    ).first()
    article_values = {
        "name": clip(product.get("title"), 255) or f"Shopify product {product['id']}",
        "description": product.get("body_html") or None,
        "brand": clip(product.get("vendor"), 100),
        "category": clip(product.get("product_type"), 255),
        "variation_axes": axes,
        "images": product_images(product, values_by_variant),
        "status": ARTICLE_STATUS.get(shopify_status, "active"),
    }
    if listing is None:
        article = Article(
            seller_id=account.seller_id,
            article_code=article_code(session, account, product),
            **article_values,
        )
        session.add(article)
        session.flush()
        listing = ChannelListing(
            article_id=article.article_id,
            channel_account_id=account.channel_account_id,
            split_key={},
            channel_parent_id=str(product["id"]),
        )
        session.add(listing)
        summary.articles_created += 1
    else:
        article = session.get(Article, listing.article_id, with_for_update=True)
        if article.variation_axes != axes and article.items:
            summary.warnings.append(
                f"{article.name}: options changed from {article.variation_axes} to {axes}"
            )
        for key, value in article_values.items():
            setattr(article, key, value)
        summary.articles_updated += 1
    listing.status = listing_status
    session.flush()

    seen_variants = set()
    for variant in variants:
        seen_variants.add(str(variant["id"]))
        import_variant(
            session,
            account,
            article,
            listing,
            variant,
            values_by_variant[variant["id"]],
            now,
            summary,
        )

    # Variants removed from the product: their listing items stop being live.
    for listing_item in session.scalars(
        select(ChannelListingItem).where(ChannelListingItem.listing_id == listing.listing_id)
    ):
        if listing_item.channel_item_id not in seen_variants and listing_item.status != "inactive":
            listing_item.status = "inactive"
            summary.variants_deactivated += 1


def article_code(session: Session, account: ChannelAccount, product: dict[str, Any]) -> str:
    """The handle, unless another article of the seller already uses it."""
    fallback = f"shopify-{product['id']}"
    code = clip(product.get("handle"), 100) or fallback
    taken = session.scalar(
        select(Article.article_id).where(
            Article.seller_id == account.seller_id, Article.article_code == code
        )
    )
    return fallback if taken else code


def import_variant(
    session: Session,
    account: ChannelAccount,
    article: Article,
    listing: ChannelListing,
    variant: dict[str, Any],
    values: dict[str, str],
    now: datetime,
    summary: ImportSummary,
) -> None:
    variant_id = str(variant["id"])
    price = to_decimal(variant.get("price"))
    compare_at = to_decimal(variant.get("compare_at_price"))
    item_values = {
        "barcode": clip(variant.get("barcode"), 50),
        "variant_values": values,
        "mrp": compare_at if compare_at and price and compare_at > price else price,
        "weight": variant_weight_grams(variant),
        "status": "active",
    }
    listing_item = session.scalars(
        select(ChannelListingItem).where(
            ChannelListingItem.channel_account_id == account.channel_account_id,
            ChannelListingItem.channel_item_id == variant_id,
        )
    ).first()

    if listing_item is None:
        item = Item(
            article_id=article.article_id,
            seller_id=account.seller_id,
            sku=item_sku(session, account, variant, summary),
            stock_qty=int(variant.get("inventory_quantity") or 0),
            stock_baseline_at=now,  # stock copied from Shopify now: the baseline for #21
            **item_values,
        )
        session.add(item)
        session.flush()
        listing_item = ChannelListingItem(
            listing_id=listing.listing_id,
            item_id=item.item_id,
            channel_account_id=account.channel_account_id,
        )
        session.add(listing_item)
        summary.items_created += 1
        if not variant.get("inventory_management"):
            summary.untracked_stock += 1
    else:
        item = session.get(Item, listing_item.item_id, with_for_update=True)
        if item.article_id != article.article_id:  # variant moved to another product
            item.article_id = article.article_id
            listing_item.listing_id = listing.listing_id
        for key, value in item_values.items():  # never stock_qty: the app owns it now
            setattr(item, key, value)
        wanted_sku = clip(variant.get("sku"), 100)
        if wanted_sku and wanted_sku != item.sku:
            item.sku = item_sku(session, account, variant, summary, current_item_id=item.item_id)
        summary.items_updated += 1

    listing_item.channel_item_id = variant_id
    listing_item.channel_sku = clip(variant.get("sku"), 100)
    listing_item.selling_price = price
    # The listing item says whether this SKU is enabled (the listing says whether the
    # product is). A SKU disabled from the app (#47) looks sold out on Shopify (tracked,
    # no overselling, 0 available); it stays disabled until someone restocks it there.
    if not (listing_item.status == "inactive" and variant_sold_out(variant)):
        listing_item.status = "live"
    session.flush()


def variant_sold_out(variant: dict[str, Any]) -> bool:
    return (
        bool(variant.get("inventory_management"))
        and variant.get("inventory_policy") == "deny"
        and int(variant.get("inventory_quantity") or 0) <= 0
    )


def item_sku(
    session: Session,
    account: ChannelAccount,
    variant: dict[str, Any],
    summary: ImportSummary,
    current_item_id: int | None = None,
) -> str:
    """The variant's SKU; `shopify-<variant id>` when it is blank or already used by
    another of the seller's items (SKUs are unique per seller)."""
    fallback = f"shopify-{variant['id']}"
    sku = clip(variant.get("sku"), 100)
    if not sku:
        return fallback
    owner = session.scalar(
        select(Item.item_id).where(Item.seller_id == account.seller_id, Item.sku == sku)
    )
    if owner is not None and owner != current_item_id:
        summary.warnings.append(
            f"SKU {sku} is already used by another item; "
            f"variant {variant['id']} imported as {fallback}"
        )
        return fallback
    return sku


def deactivate_missing(
    session: Session, account: ChannelAccount, seen_products: set[str], summary: ImportSummary
) -> None:
    """Products no longer on Shopify: listing and its items inactive, article archived."""
    for listing in session.scalars(
        select(ChannelListing).where(
            ChannelListing.channel_account_id == account.channel_account_id,
            ChannelListing.channel_parent_id.is_not(None),
            ChannelListing.status != "inactive",
        )
    ):
        if listing.channel_parent_id in seen_products:
            continue
        listing.status = "inactive"
        for listing_item in listing.items:
            listing_item.status = "inactive"
        session.get(Article, listing.article_id).status = "archived"
        summary.products_deactivated += 1


# --- Linking orders to the catalog --------------------------------------------------------


def link_order_lines(session: Session, account: ChannelAccount) -> int:
    """Fill ecs_order_line.variant_id / product_id for this account's order lines that
    aren't linked yet: by the Shopify variant id in the order's latest payload, else by
    SKU. Returns how many lines were linked."""
    unlinked = session.scalars(
        select(OrderLine)
        .join(OrderHeader)
        .where(
            OrderHeader.channel_account_id == account.channel_account_id,
            OrderLine.variant_id.is_(None),
        )
    ).all()
    if not unlinked:
        return 0
    payloads = latest_payloads(session, {line.order_header_id for line in unlinked})
    linked = 0
    for line in unlinked:
        linked += link_line(
            session,
            account.channel_account_id,
            account.seller_id,
            line,
            payloads.get(line.order_header_id),
        )
    return linked


def link_line(
    session: Session,
    channel_account_id: int,
    seller_id: int,
    line: OrderLine,
    payload: dict[str, Any] | None,
) -> int:
    """Link one order line to its catalog item, if found. Returns 1 if linked, else 0.
    Also used by the order processing step (#9) for lines it creates."""
    variant_id = shopify_variant_id(payload, line.external_line_id)
    match = catalog_match(session, channel_account_id, seller_id, variant_id, line.sku)
    if not match:
        return 0
    line.variant_id, line.product_id = match
    return 1


def latest_payloads(session: Session, header_ids: set[int]) -> dict[int, dict[str, Any]]:
    newest = (
        select(
            OrderImportRow.order_header_id,
            func.max(OrderImportRow.order_import_row_id).label("row_id"),
        )
        .where(
            OrderImportRow.order_header_id.in_(header_ids), OrderImportRow.raw_payload.is_not(None)
        )
        .group_by(OrderImportRow.order_header_id)
        .subquery()
    )
    rows = session.execute(
        select(OrderImportRow.order_header_id, OrderImportRow.raw_payload).join(
            newest, OrderImportRow.order_import_row_id == newest.c.row_id
        )
    )
    return {header_id: payload for header_id, payload in rows}


def shopify_variant_id(payload: dict[str, Any] | None, external_line_id: str | None) -> str | None:
    for line_item in (payload or {}).get("line_items") or []:
        if str(line_item.get("id")) == str(external_line_id) and line_item.get("variant_id"):
            return str(line_item["variant_id"])
    return None


def catalog_match(
    session: Session,
    channel_account_id: int,
    seller_id: int,
    variant_id: str | None,
    sku: str | None,
) -> tuple[int, int] | None:
    """(item_id, article_id) for a Shopify variant id on this account, else for the SKU."""
    if variant_id:
        found = session.execute(
            select(Item.item_id, Item.article_id)
            .join(ChannelListingItem, ChannelListingItem.item_id == Item.item_id)
            .where(
                ChannelListingItem.channel_account_id == channel_account_id,
                ChannelListingItem.channel_item_id == variant_id,
            )
        ).first()
        if found:
            return tuple(found)
    if sku:
        found = session.execute(
            select(Item.item_id, Item.article_id).where(
                Item.seller_id == seller_id, Item.sku == sku
            )
        ).first()
        if found:
            return tuple(found)
    return None
