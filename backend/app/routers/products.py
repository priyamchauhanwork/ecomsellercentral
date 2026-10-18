"""Catalog endpoints (#13): Shopify product import and the item list."""

from dataclasses import asdict
from decimal import Decimal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.auth import require_api_key
from app.config import get_settings
from app.db import SessionLocal, get_db
from app.models import Article, Item
from app.services import order_ingestion, product_import
from app.services.shopify import ShopifyConfigError, ShopifyError

router = APIRouter(prefix="/products", tags=["products"], dependencies=[Depends(require_api_key)])


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


@router.post("/import", response_model=ProductImportResult)
def import_products():
    """Import or refresh every Shopify product into the catalog. Runs to completion and
    returns a summary (Phase 0 catalogues are small). Stock is copied only for items
    created now; existing items keep their stock."""
    try:
        with SessionLocal.begin() as session:
            account = order_ingestion.ensure_shopify_account(session)
            session.expunge(account)
        with product_import.shopify_client_factory() as shopify:
            scopes = shopify.access_scopes()
    except ShopifyConfigError as exc:
        raise HTTPException(503, f"Shopify is not configured: {exc}") from exc
    except ShopifyError as exc:
        raise HTTPException(502, f"Shopify rejected the request: {exc}") from exc
    if "read_products" not in scopes:
        raise HTTPException(503, "The Shopify app lacks the read_products scope.")
    try:
        summary = product_import.import_products(account)
    except product_import.ImportInProgress as exc:
        raise HTTPException(409, "A product import is already running for this store.") from exc
    return ProductImportResult(**asdict(summary))


@router.get("", response_model=list[ProductItem])
def list_products(db: Session = Depends(get_db)):
    """Every item with its article, sorted by article name, then SKU."""
    rows = db.execute(
        select(Item, Article.name, Article.status)
        .join(Article, Item.article_id == Article.article_id)
        .where(Item.seller_id == get_settings().seller_id)
        .order_by(Article.name, Item.sku)
    ).all()
    return [
        ProductItem(
            item_id=item.item_id,
            article_id=item.article_id,
            article_name=name,
            article_status=article_status,
            sku=item.sku,
            barcode=item.barcode,
            variant_values=item.variant_values,
            mrp=item.mrp,
            stock_qty=item.stock_qty,
            status=item.status,
        )
        for item, name, article_status in rows
    ]
