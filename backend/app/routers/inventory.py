"""Inventory endpoints: list, item, adjust and stock history (#19), Shopify stock sync
and drift (#20). Static paths are declared before /inventory/{item_id}."""

from datetime import datetime
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field, StrictInt, field_validator
from sqlalchemy.orm import Session

from app.auth import require_api_key
from app.config import get_settings
from app.db import get_db
from app.models.catalog import MANUAL_STOCK_REASONS
from app.services import inventory, product_import, stock_sync
from app.services.shopify import ShopifyConfigError, ShopifyError

router = APIRouter(prefix="/inventory", tags=["inventory"], dependencies=[Depends(require_api_key)])

ManualReason = Literal[MANUAL_STOCK_REASONS]  # type: ignore[valid-type]
MAX_CHANGE = 1_000_000


class ShopifyState(BaseModel):
    listing_item_id: int
    status: str
    last_sync_error: str | None


class InventoryItem(BaseModel):
    item_id: int
    article_id: int
    article_name: str
    sku: str
    barcode: str | None
    variant_values: dict[str, str]
    image_url: str | None
    stock_qty: int
    stock_status: Literal["ok", "low", "out"]
    status: str
    shopify: ShopifyState | None  # null when the item isn't on Shopify


class InventorySummary(BaseModel):
    items: int
    low_stock: int
    out_of_stock: int


class InventoryList(BaseModel):
    summary: InventorySummary
    total: int  # items matching the filters
    items: list[InventoryItem]


class Adjustment(BaseModel):
    item_id: StrictInt
    quantity_change: Annotated[StrictInt, Field(ge=-MAX_CHANGE, le=MAX_CHANGE)]
    reason: ManualReason
    note: str | None = Field(None, max_length=500)

    @field_validator("quantity_change")
    @classmethod
    def non_zero(cls, value: int) -> int:
        if value == 0:
            raise ValueError("Quantity change can't be 0")
        return value

    @field_validator("note", mode="before")
    @classmethod
    def blank_note(cls, value):
        return (value.strip() or None) if isinstance(value, str) else value


class HistoryEntry(BaseModel):
    inventory_log_id: int
    qty_change: int
    stock_after: int
    reason: str
    note: str | None
    order_header_id: int | None
    order_number: str | None
    created_at: datetime


class AdjustResult(BaseModel):
    item: InventoryItem
    entry: HistoryEntry
    synced: bool | None  # null: nothing to push (not on Shopify, or disabled there)
    sync_error: str | None


class SyncResult(BaseModel):
    item: InventoryItem
    synced: bool | None
    sync_error: str | None


class PendingFailure(BaseModel):
    item_id: int
    error: str | None


class PendingResult(BaseModel):
    retried: int
    synced: int
    failed: list[PendingFailure]


class DriftItem(BaseModel):
    item_id: int
    article_id: int
    article_name: str
    sku: str
    listing_item_id: int
    stock_qty: int
    expected_on_shopify: int
    shopify_qty: int | None
    state: Literal["ok", "drift", "untracked", "missing"]


class DriftReport(BaseModel):
    checked: int
    mismatched: int
    accuracy_pct: float | None
    items: list[DriftItem]


def seller_id() -> int:
    return get_settings().seller_id


def item_or_404(db: Session, item_id: int) -> InventoryItem:
    try:
        return InventoryItem(**inventory.inventory_item(db, seller_id(), item_id))
    except inventory.ItemNotFound as exc:
        raise HTTPException(404, "Item not found") from exc


@router.get("", response_model=InventoryList)
def list_inventory(
    q: str | None = Query(None, max_length=100),
    low: bool = False,
    limit: int = Query(200, ge=1, le=500),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
):
    """Active items of products that aren't archived, by product name then SKU. `q`
    matches product name, SKU or barcode; `low` keeps items at or below 5 (incl. out)."""
    return inventory.list_inventory(db, seller_id(), q=q, low=low, limit=limit, offset=offset)


@router.post("/adjust", response_model=AdjustResult)
def adjust(adjustment: Adjustment, db: Session = Depends(get_db)):
    """Add or remove stock by hand. Stock and its log row are saved together; then the
    new figure is pushed to Shopify. A failed push keeps the change (synced: false)."""
    try:
        entry = inventory.adjust_stock(
            db,
            seller_id(),
            adjustment.item_id,
            adjustment.quantity_change,
            adjustment.reason,
            adjustment.note,
        )
    except inventory.ItemNotFound as exc:
        db.rollback()
        raise HTTPException(404, "Item not found") from exc
    except inventory.AdjustmentInvalid as exc:
        db.rollback()
        raise HTTPException(
            422, [{"loc": ["body", "quantity_change"], "msg": str(exc), "type": "value_error"}]
        ) from exc
    db.commit()
    entry_id = entry.inventory_log_id
    outcome = stock_sync.sync_items({adjustment.item_id})[adjustment.item_id]
    db.expire_all()
    history = inventory.stock_history(db, seller_id(), adjustment.item_id)
    return AdjustResult(
        item=item_or_404(db, adjustment.item_id),
        entry=next(e for e in history if e["inventory_log_id"] == entry_id),
        synced=outcome.synced,
        sync_error=outcome.error,
    )


@router.post("/sync-pending", response_model=PendingResult)
def sync_pending():
    """Retry the Shopify push for every enabled SKU whose last sync failed (the polling
    job calls this)."""
    return stock_sync.sync_pending()


@router.get("/drift", response_model=DriftReport)
def drift(db: Session = Depends(get_db)):
    """The app's stock against Shopify's live available quantity, per Shopify SKU."""
    try:
        with product_import.shopify_client_factory() as shopify:
            return stock_sync.drift(db, shopify, seller_id())
    except ShopifyConfigError as exc:
        raise HTTPException(503, f"Shopify is not configured: {exc}") from exc
    except ShopifyError as exc:
        raise HTTPException(502, str(exc)) from exc


@router.get("/{item_id}", response_model=InventoryItem)
def get_item(item_id: int, db: Session = Depends(get_db)):
    return item_or_404(db, item_id)


@router.get("/{item_id}/history", response_model=list[HistoryEntry])
def history(item_id: int, db: Session = Depends(get_db)):
    """The latest 100 stock changes, newest first, naming the order for order changes."""
    try:
        return inventory.stock_history(db, seller_id(), item_id)
    except inventory.ItemNotFound as exc:
        raise HTTPException(404, "Item not found") from exc


@router.post("/{item_id}/sync", response_model=SyncResult)
def sync_item(item_id: int, db: Session = Depends(get_db)):
    """Set Shopify's available quantity to this item's stock now (fixes drift)."""
    item_or_404(db, item_id)
    db.close()
    outcome = stock_sync.sync_items({item_id})[item_id]
    return SyncResult(
        item=item_or_404(db, item_id), synced=outcome.synced, sync_error=outcome.error
    )
