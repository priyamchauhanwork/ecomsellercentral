"""Inventory endpoints: list, item, adjust and stock history (#19), Shopify stock sync
and drift (#20), supply & demand figures and damaged units (#51). Static paths are
declared before /inventory/{item_id}."""

from datetime import date, datetime
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field, StrictInt, field_validator
from sqlalchemy.orm import Session

from app.auth import require_api_key
from app.config import get_settings
from app.db import get_db
from app.models.inventory import MANUAL_STOCK_REASONS
from app.services import inventory, product_import, stock, stock_sync
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
    stock_qty: int  # = available (cached)
    stock_status: Literal["ok", "low", "out"]  # judged on available
    onhand: int
    open: int
    allocated: int
    shipped: int
    committed: int  # open + allocated + shipped
    return_pending: int
    next_eta: date | None  # earliest expected return
    damaged: int
    available: int  # onhand − committed; pushed to Shopify (≥ 0)
    future_available: int  # available + return_pending
    on_shelf: int  # onhand − shipped
    status: str
    shopify: ShopifyState | None  # null when the item isn't on Shopify


class InventorySummary(BaseModel):
    items: int
    low_stock: int
    out_of_stock: int
    returning: int
    damaged: int


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


class DamagedChange(BaseModel):
    action: Literal["repair", "write_off"]
    quantity: Annotated[StrictInt, Field(gt=0, le=MAX_CHANGE)]
    note: str | None = Field(None, max_length=500)

    @field_validator("note", mode="before")
    @classmethod
    def blank_note(cls, value):
        return (value.strip() or None) if isinstance(value, str) else value


class HistoryEntry(BaseModel):
    inventory_log_id: int
    side: Literal["SUPPLY", "DEMAND"] | None  # null: before the supply & demand model
    inv_type: str | None
    qty_change: int
    stock_after: int  # that type's quantity after the change
    reason: str
    note: str | None
    transaction_id: str | None
    order_header_id: int | None
    order_number: str | None
    return_id: int | None
    return_number: str | None
    created_at: datetime


class AdjustResult(BaseModel):
    item: InventoryItem
    entries: list[HistoryEntry]  # the rows this change wrote
    synced: bool | None  # null: nothing to push (not on Shopify, or disabled there)
    sync_error: str | None
    warning: str | None = None  # e.g. the SKU is now oversold


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
    """Add or remove on-hand stock by hand; `damage` moves units from the shelf to
    damaged. A removal may not exceed what is on the shelf (on hand − shipped). Saved
    with its log rows, then `available` is pushed to Shopify (synced: false if that
    failed; the change is kept)."""
    return apply_change(
        db,
        adjustment.item_id,
        lambda: inventory.adjust_stock(
            db,
            seller_id(),
            adjustment.item_id,
            adjustment.quantity_change,
            adjustment.reason,
            adjustment.note,
        ),
        "quantity_change",
    )


def apply_change(db: Session, item_id: int, make_change, field: str) -> AdjustResult:
    try:
        change = make_change()
        changed = change.finish()
    except inventory.ItemNotFound as exc:
        db.rollback()
        raise HTTPException(404, "Item not found") from exc
    except stock.StockInvalid as exc:
        db.rollback()
        raise HTTPException(
            422, [{"loc": ["body", field], "msg": str(exc), "type": "value_error"}]
        ) from exc
    transaction_id = str(change.transaction_id)
    db.commit()
    outcome = (
        stock_sync.sync_items({item_id})[item_id]
        if changed
        else stock_sync.SyncOutcome(item_id, None)
    )
    db.expire_all()
    item = item_or_404(db, item_id)
    history = inventory.stock_history(db, seller_id(), item_id)
    return AdjustResult(
        item=item,
        entries=[e for e in history if e["transaction_id"] == transaction_id],
        synced=outcome.synced,
        sync_error=outcome.error,
        warning=(
            f"Now oversold: {item.committed} committed to orders but {item.onhand} on hand."
            if item.available < 0
            else None
        ),
    )


@router.post("/{item_id}/damaged", response_model=AdjustResult)
def damaged(item_id: int, change: DamagedChange, db: Session = Depends(get_db)):
    """Repair damaged units (back to on hand) or write them off."""
    return apply_change(
        db,
        item_id,
        lambda: inventory.change_damaged(
            db, seller_id(), item_id, change.action, change.quantity, change.note
        ),
        "quantity",
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
