"""Catalog endpoints: Shopify import (#13), list (#43), detail (#44), create (#45),
publish to Shopify (#46) and enable / disable on Shopify (#47)."""

from dataclasses import asdict
from datetime import datetime
from decimal import Decimal
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field, HttpUrl, field_validator
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.auth import require_api_key
from app.config import get_settings
from app.db import SessionLocal, get_db
from app.models import Article, ChannelListing, ChannelListingItem
from app.services import catalog, order_ingestion, product_import, shopify_catalog
from app.services.shopify import ShopifyConfigError, ShopifyError

router = APIRouter(prefix="/products", tags=["products"], dependencies=[Depends(require_api_key)])

ArticleStatus = Literal["draft", "active", "archived"]
ChannelStatusFilter = Literal["live", "inactive", "unlisted"]
SaleStatus = Literal["live", "inactive"]


# --- Response models ----------------------------------------------------------------------


class ChannelSummary(BaseModel):
    listing_id: int
    channel_account_id: int
    account_name: str
    channel_code: str
    status: str


class ProductSummary(BaseModel):
    article_id: int
    article_code: str
    name: str
    brand: str | None
    category: str | None
    status: str
    image_url: str | None
    item_count: int
    low_stock_items: int
    total_stock: int
    price_min: Decimal | None
    price_max: Decimal | None
    channels: list[ChannelSummary]
    created_at: datetime


class ProductList(BaseModel):
    total: int
    products: list[ProductSummary]


class ProductItem(BaseModel):
    item_id: int
    article_id: int
    article_name: str
    article_status: str
    sku: str
    barcode: str | None
    variant_values: dict[str, str]
    mrp: Decimal | None
    stock_qty: int
    status: str


class ItemDetail(BaseModel):
    item_id: int
    sku: str
    barcode: str | None
    variant_values: dict[str, str]
    mrp: Decimal | None
    weight: Decimal | None
    stock_qty: int
    stock_status: str
    stock_baseline_at: datetime | None
    status: str
    image_url: str | None


class ListingItemDetail(BaseModel):
    listing_item_id: int
    item_id: int
    channel_sku: str | None
    channel_item_id: str | None
    selling_price: Decimal | None
    status: str
    last_sync_error: str | None


class ListingDetail(BaseModel):
    listing_id: int
    channel_account_id: int
    account_name: str
    channel_code: str
    channel_name: str
    split_key: dict[str, str]
    title: str | None
    description: str | None
    channel_parent_id: str | None
    status: str
    items: list[ListingItemDetail]


class ProductDetail(BaseModel):
    article_id: int
    article_code: str
    name: str
    description: str | None
    brand: str | None
    category: str | None
    variation_axes: list[str]
    attributes: dict[str, str]
    images: list[dict]
    image_url: str | None
    status: str
    created_at: datetime
    updated_at: datetime
    items: list[ItemDetail]
    listings: list[ListingDetail]


class ProductImportResult(BaseModel):
    products: int
    articles_created: int
    articles_updated: int
    items_created: int
    items_updated: int
    products_deactivated: int
    variants_deactivated: int
    untracked_stock: int
    order_lines_linked: int
    complete: bool
    warnings: list[str]
    errors: list[str]


# --- Request models -----------------------------------------------------------------------


def stripped(value: str | None) -> str | None:
    return value.strip() or None if isinstance(value, str) else value


class NewImage(BaseModel):
    url: HttpUrl
    filter: dict[str, str] = {}

    @field_validator("url")
    @classmethod
    def https_only(cls, url: HttpUrl) -> HttpUrl:
        if url.scheme != "https":
            raise ValueError("Image URLs must start with https://")
        return url


class NewVariant(BaseModel):
    sku: str = Field(min_length=1, max_length=100)
    variant_values: dict[str, str] = {}
    barcode: str | None = Field(None, max_length=50)
    mrp: Decimal | None = Field(None, ge=0, max_digits=12, decimal_places=2)
    weight: Decimal | None = Field(None, ge=0, max_digits=10, decimal_places=3)
    stock_qty: int = Field(0, ge=0, le=1_000_000)

    _strip = field_validator("sku", "barcode", mode="before")(stripped)


class NewProduct(BaseModel):
    article_code: str = Field(min_length=1, max_length=100)
    name: str = Field(min_length=1, max_length=255)
    description: str | None = Field(None, max_length=20_000)
    brand: str | None = Field(None, max_length=100)
    category: str | None = Field(None, max_length=255)
    variation_axes: list[str] = Field([], max_length=10)
    attributes: dict[str, str] = {}
    images: list[NewImage] = Field([], max_length=20)
    items: list[NewVariant] = Field(min_length=1)

    _strip = field_validator(
        "article_code", "name", "description", "brand", "category", mode="before"
    )(stripped)

    @field_validator("variation_axes", mode="before")
    @classmethod
    def strip_axes(cls, axes: list) -> list:
        return [axis.strip() if isinstance(axis, str) else axis for axis in axes]


class PublishRequest(BaseModel):
    # Selling price per item id; items without one use their MRP.
    prices: dict[int, Decimal] = {}
    enable: bool = False

    @field_validator("prices")
    @classmethod
    def positive(cls, prices: dict[int, Decimal]) -> dict[int, Decimal]:
        if any(price <= 0 for price in prices.values()):
            raise ValueError("Prices must be above 0")
        return {item_id: price.quantize(Decimal("0.01")) for item_id, price in prices.items()}


class PublishResponse(BaseModel):
    listing_id: int
    shopify_product_id: str
    variants: int
    enabled: bool
    warnings: list[str]
    product: ProductDetail


class StatusChange(BaseModel):
    status: SaleStatus


class BulkStatusChange(BaseModel):
    article_ids: list[int] = Field(min_length=1, max_length=100)
    status: SaleStatus


class BulkResult(BaseModel):
    article_id: int
    ok: bool
    message: str


# --- Helpers ------------------------------------------------------------------------------


def seller_id() -> int:
    return get_settings().seller_id


def detail_or_404(db: Session, article_id: int) -> ProductDetail:
    detail = catalog.product_detail(db, seller_id(), article_id)
    if detail is None:
        raise HTTPException(404, "Product not found")
    return ProductDetail(**detail)


def article_or_404(db: Session, article_id: int, *, for_update: bool = False) -> Article:
    article = db.get(Article, article_id, with_for_update=for_update)
    if article is None or article.seller_id != seller_id():
        raise HTTPException(404, "Product not found")
    return article


def shopify_client():
    try:
        return product_import.shopify_client_factory()
    except ShopifyConfigError as exc:
        raise HTTPException(503, f"Shopify is not configured: {exc}") from exc


def record_sync_error(listing_item_ids: list[int], message: str) -> None:
    """Keep Shopify's error on the listing items (own transaction: the change itself was
    rolled back), so the product page shows it until a retry succeeds."""
    with SessionLocal.begin() as session:
        for listing_item_id in listing_item_ids:
            session.get(ChannelListingItem, listing_item_id).last_sync_error = message[:2000]


# --- Endpoints ----------------------------------------------------------------------------


@router.get("", response_model=ProductList)
def list_products(
    status: ArticleStatus | None = None,
    q: str | None = Query(None, max_length=100),
    channel_status: ChannelStatusFilter | None = None,
    low_stock: bool = False,
    sort: Literal["newest", "name", "stock"] = "newest",
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
):
    """Products (articles), with variant count, stock, price range and channel status."""
    total, products = catalog.list_products(
        db,
        seller_id(),
        status=status,
        q=q,
        channel_status=channel_status,
        low_stock=low_stock,
        sort=sort,
        limit=limit,
        offset=offset,
    )
    return ProductList(total=total, products=products)


@router.get("/items", response_model=list[ProductItem])
def list_items(db: Session = Depends(get_db)):
    """Every item (variant) with its article, sorted by article name, then SKU."""
    return catalog.list_items(db, seller_id())


@router.post("/import", response_model=ProductImportResult)
def import_products():
    """Import or refresh every Shopify product into the catalog. Runs to completion and
    returns a summary (Phase 0 catalogues are small). Stock is copied only for items
    created now; existing items keep their stock."""
    try:
        with SessionLocal.begin() as session:
            account = order_ingestion.ensure_shopify_account(session)
            session.expunge(account)
        with shopify_client() as shopify:
            scopes = shopify.access_scopes()
    except ShopifyError as exc:
        raise HTTPException(502, f"Shopify rejected the request: {exc}") from exc
    except ShopifyConfigError as exc:
        raise HTTPException(503, f"Shopify is not configured: {exc}") from exc
    if "read_products" not in scopes:
        raise HTTPException(503, "The Shopify app lacks the read_products scope.")
    try:
        summary = product_import.import_products(account)
    except product_import.ImportInProgress as exc:
        raise HTTPException(409, "A product import is already running for this store.") from exc
    return ProductImportResult(**asdict(summary))


@router.post("", response_model=ProductDetail, status_code=201)
def create_product(product: NewProduct, db: Session = Depends(get_db)):
    """Create a draft product with its variants. Every problem is reported at once, with
    the field it belongs to; nothing is saved unless the whole product is valid."""
    try:
        article = catalog.create_product(
            db,
            seller_id(),
            article_code=product.article_code,
            name=product.name,
            description=product.description,
            brand=product.brand,
            category=product.category,
            variation_axes=product.variation_axes,
            attributes=product.attributes,
            images=[{"url": str(image.url), "filter": image.filter} for image in product.images],
            items=[catalog.NewItem(**item.model_dump()) for item in product.items],
        )
        db.commit()
    except catalog.ProductInvalid as exc:
        db.rollback()
        raise HTTPException(422, exc.errors) from exc
    db.expire_all()  # answer with the stored values (e.g. weight 180 → 180.000), as GET does
    return detail_or_404(db, article.article_id)


@router.get("/{article_id}", response_model=ProductDetail)
def get_product(article_id: int, db: Session = Depends(get_db)):
    """One product: its article, every variant, and its listings on each channel."""
    return detail_or_404(db, article_id)


@router.post("/{article_id}/publish", response_model=PublishResponse)
def publish_product(article_id: int, request: PublishRequest, db: Session = Depends(get_db)):
    """Create the product on the seller's Shopify store (draft unless `enable`)."""
    article = article_or_404(db, article_id, for_update=True)
    unknown = set(request.prices) - {item.item_id for item in article.items}
    if unknown:
        raise HTTPException(422, f"Not variants of this product: {sorted(unknown)}")
    account = order_ingestion.ensure_shopify_account(db)
    try:
        with shopify_client() as shopify:
            result = shopify_catalog.publish_article(
                db, shopify, account, article, request.prices, request.enable
            )
        db.commit()
    except shopify_catalog.CatalogConflict as exc:
        db.rollback()
        raise HTTPException(409, str(exc)) from exc
    except shopify_catalog.CatalogInvalid as exc:
        db.rollback()
        raise HTTPException(422, str(exc)) from exc
    except ShopifyError as exc:
        db.rollback()
        raise HTTPException(502, str(exc)) from exc
    return PublishResponse(**asdict(result), product=detail_or_404(db, article_id))


def change_listing(db: Session, article_id: int, listing_id: int, status: str) -> None:
    listing = db.get(ChannelListing, listing_id, with_for_update=True)
    if listing is None or listing.article_id != article_or_404(db, article_id).article_id:
        raise HTTPException(404, "Listing not found")
    if listing.status == status:
        return
    if not listing.channel_parent_id:
        raise HTTPException(409, "This listing isn't on the channel yet; publish it first.")
    item_ids = [li.listing_item_id for li in listing.items]
    try:
        with shopify_client() as shopify:
            shopify_catalog.set_listing_status(db, shopify, listing, status)
        for listing_item in listing.items:
            listing_item.last_sync_error = None
        db.commit()
    except ShopifyError as exc:
        db.rollback()
        record_sync_error(item_ids, str(exc))
        raise HTTPException(502, str(exc)) from exc


@router.post("/{article_id}/listings/{listing_id}/status", response_model=ProductDetail)
def set_listing_status(
    article_id: int, listing_id: int, change: StatusChange, db: Session = Depends(get_db)
):
    """Enable (on sale) or disable a product on its channel. Shopify first."""
    change_listing(db, article_id, listing_id, change.status)
    return detail_or_404(db, article_id)


@router.post("/{article_id}/listing-items/{listing_item_id}/status", response_model=ProductDetail)
def set_listing_item_status(
    article_id: int, listing_item_id: int, change: StatusChange, db: Session = Depends(get_db)
):
    """Enable or disable one SKU on its channel (on Shopify: sold out while disabled)."""
    listing_item = db.get(ChannelListingItem, listing_item_id, with_for_update=True)
    article = article_or_404(db, article_id)
    if (
        listing_item is None
        or db.get(ChannelListing, listing_item.listing_id).article_id != article.article_id
    ):
        raise HTTPException(404, "Variant listing not found")
    if listing_item.status != change.status:
        try:
            with shopify_client() as shopify:
                shopify_catalog.set_listing_item_status(db, shopify, listing_item, change.status)
            listing_item.last_sync_error = None
            db.commit()
        except ShopifyError as exc:
            db.rollback()
            record_sync_error([listing_item_id], str(exc))
            raise HTTPException(502, str(exc)) from exc
    return detail_or_404(db, article_id)


@router.post("/status", response_model=list[BulkResult])
def set_status_in_bulk(change: BulkStatusChange, db: Session = Depends(get_db)):
    """Enable or disable several products on their channel listings, one at a time; each
    gets its own result, so one failure doesn't stop the rest."""
    results = []
    for article_id in dict.fromkeys(change.article_ids):
        listings = db.scalars(
            select(ChannelListing).where(
                ChannelListing.article_id == article_id,
                ChannelListing.channel_parent_id.is_not(None),
            )
        ).all()
        if not listings:
            results.append(
                BulkResult(article_id=article_id, ok=False, message="Not on any channel")
            )
            continue
        try:
            for listing in listings:
                change_listing(db, article_id, listing.listing_id, change.status)
            results.append(BulkResult(article_id=article_id, ok=True, message="Done"))
        except HTTPException as exc:
            results.append(BulkResult(article_id=article_id, ok=False, message=str(exc.detail)))
    return results
