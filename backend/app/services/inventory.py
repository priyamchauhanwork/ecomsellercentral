"""Stock: the inventory list, manual adjustments and stock history (#19), and keeping
stock in step with orders (#21). Stock is ecs_items.stock_qty; every change to it is
logged in ecs_inventory_log, in the same transaction.
"""

import logging
from datetime import datetime
from typing import Any

from sqlalchemy import case, func, or_, select
from sqlalchemy.orm import Session

from app.models import (
    Article,
    Channel,
    ChannelAccount,
    ChannelListingItem,
    InventoryLog,
    Item,
    OrderHeader,
    OrderLine,
)
from app.services.catalog import LOW_STOCK, like_pattern, stock_status
from app.services.product_import import image_for

logger = logging.getLogger(__name__)

SHOPIFY = "SHOPIFY"  # ecs_channels.code
HISTORY_LIMIT = 100


class ItemNotFound(LookupError):
    pass


class AdjustmentInvalid(ValueError):
    pass


# --- List and detail (#19) ----------------------------------------------------------------


def inventory_rows(
    session: Session, filters: list, limit: int | None = None, offset: int = 0
) -> list[dict[str, Any]]:
    """Items with their article and Shopify listing item, as the inventory API returns them."""
    shopify_listing = (
        select(ChannelListingItem)
        .join(
            ChannelAccount,
            ChannelAccount.channel_account_id == ChannelListingItem.channel_account_id,
        )
        .join(Channel, Channel.channel_id == ChannelAccount.channel_id)
        .where(Channel.code == SHOPIFY)
        .subquery()
    )
    rows = session.execute(
        select(
            Item,
            Article.name,
            Article.images,
            shopify_listing.c.listing_item_id,
            shopify_listing.c.status,
            shopify_listing.c.last_sync_error,
        )
        .join(Article, Article.article_id == Item.article_id)
        .outerjoin(shopify_listing, shopify_listing.c.item_id == Item.item_id)
        .where(*filters)
        .order_by(Article.name, Item.sku)
        .limit(limit)
        .offset(offset)
    ).all()
    return [
        {
            "item_id": item.item_id,
            "article_id": item.article_id,
            "article_name": name,
            "sku": item.sku,
            "barcode": item.barcode,
            "variant_values": item.variant_values,
            "image_url": image_for(images, item.variant_values),
            "stock_qty": item.stock_qty,
            "stock_status": stock_status(item.stock_qty),
            "status": item.status,
            "shopify": None
            if listing_item_id is None
            else {
                "listing_item_id": listing_item_id,
                "status": listing_status,
                "last_sync_error": sync_error,
            },
        }
        for item, name, images, listing_item_id, listing_status, sync_error in rows
    ]


def listed_filters(seller_id: int) -> list:
    """What the inventory lists: active items of products that aren't archived."""
    return [Item.seller_id == seller_id, Item.status == "active", Article.status != "archived"]


def list_inventory(
    session: Session,
    seller_id: int,
    *,
    q: str | None = None,
    low: bool = False,
    limit: int = 200,
    offset: int = 0,
) -> dict[str, Any]:
    """The inventory list, with summary counts over every listed item (not just the
    filtered ones), so the summary bar doesn't change while searching."""
    filters = listed_filters(seller_id)
    total, low_count, out_count = session.execute(
        select(
            func.count(),
            func.count().filter(Item.stock_qty > 0, Item.stock_qty <= LOW_STOCK),
            func.count().filter(Item.stock_qty <= 0),
        )
        .select_from(Item)
        .join(Article, Article.article_id == Item.article_id)
        .where(*filters)
    ).one()

    if q and q.strip():
        pattern = like_pattern(q.strip())
        filters.append(
            or_(
                Article.name.ilike(pattern, escape="\\"),
                Item.sku.ilike(pattern, escape="\\"),
                Item.barcode.ilike(pattern, escape="\\"),
            )
        )
    if low:
        filters.append(Item.stock_qty <= LOW_STOCK)
    matching = session.scalar(
        select(func.count())
        .select_from(Item)
        .join(Article, Article.article_id == Item.article_id)
        .where(*filters)
    )
    items = inventory_rows(session, filters, limit, offset)
    return {
        "summary": {"items": total, "low_stock": low_count, "out_of_stock": out_count},
        "total": matching,
        "items": items,
    }


def inventory_item(session: Session, seller_id: int, item_id: int) -> dict[str, Any]:
    rows = inventory_rows(session, [Item.seller_id == seller_id, Item.item_id == item_id])
    if not rows:
        raise ItemNotFound(item_id)
    return rows[0]


# --- Adjust and history (#19) -------------------------------------------------------------


def adjust_stock(
    session: Session,
    seller_id: int,
    item_id: int,
    quantity_change: int,
    reason: str,
    note: str | None,
) -> InventoryLog:
    """Change an item's stock by hand and log it, in the caller's transaction, with the
    item row locked (FOR NO KEY UPDATE) so concurrent changes can't be lost. A removal may
    not take stock below 0; adding is always allowed, even while stock is negative (oversold)."""
    item = session.get(Item, item_id, with_for_update={"key_share": True})
    if item is None or item.seller_id != seller_id:
        raise ItemNotFound(item_id)
    new_stock = item.stock_qty + quantity_change
    if quantity_change < 0 and new_stock < 0:
        raise AdjustmentInvalid(
            f"Only {max(item.stock_qty, 0)} in stock, so {-quantity_change} can't be removed."
        )
    return log_change(session, item, quantity_change, reason, note)


def log_change(
    session: Session,
    item: Item,
    qty_change: int,
    reason: str,
    note: str | None,
    order_line_id: int | None = None,
) -> InventoryLog:
    item.stock_qty += qty_change
    entry = InventoryLog(
        item_id=item.item_id,
        qty_change=qty_change,
        stock_after=item.stock_qty,
        reason=reason,
        note=note,
        order_line_id=order_line_id,
    )
    session.add(entry)
    session.flush()
    return entry


def stock_history(session: Session, seller_id: int, item_id: int) -> list[dict[str, Any]]:
    """The item's latest stock changes, newest first, with the order for order-caused ones."""
    item = session.get(Item, item_id)
    if item is None or item.seller_id != seller_id:
        raise ItemNotFound(item_id)
    rows = session.execute(
        select(InventoryLog, OrderHeader.order_header_id, OrderHeader.order_number)
        .outerjoin(OrderLine, OrderLine.order_line_id == InventoryLog.order_line_id)
        .outerjoin(OrderHeader, OrderHeader.order_header_id == OrderLine.order_header_id)
        .where(InventoryLog.item_id == item_id)
        .order_by(InventoryLog.created_at.desc(), InventoryLog.inventory_log_id.desc())
        .limit(HISTORY_LIMIT)
    ).all()
    return [
        {
            "inventory_log_id": entry.inventory_log_id,
            "qty_change": entry.qty_change,
            "stock_after": entry.stock_after,
            "reason": entry.reason,
            "note": entry.note,
            "order_header_id": order_header_id,
            "order_number": order_number,
            "created_at": entry.created_at,
        }
        for entry, order_header_id, order_number in rows
    ]


# --- Orders (#21) -------------------------------------------------------------------------


def counts_for_stock(order_date: datetime, item: Item) -> bool:
    """Only orders placed after the item's starting stock was taken change it: that
    figure (copied from Shopify by #13) already reflects every older order."""
    return item.stock_baseline_at is None or order_date > item.stock_baseline_at


def reconcile_order_stock(session: Session, header: OrderHeader, *, new_order: bool) -> set[int]:
    """Bring stock in line with an order, in the caller's transaction (#21).

    Each counted line should hold back ordered − cancelled − returned units. That is
    compared with what is already logged for the line, and only the difference is
    logged, so replays, re-imports and repeated updates never deduct twice. Returns the
    ids of the items whose stock changed.
    """
    lines = [line for line in header.lines if line.variant_id]
    if not lines:
        return set()
    items = {
        item.item_id: item
        for item in session.scalars(
            select(Item)
            .where(Item.item_id.in_({line.variant_id for line in lines}))
            .order_by(Item.item_id)  # a fixed lock order, so concurrent orders can't deadlock
            # FOR NO KEY UPDATE: stock changes still wait for each other, but not for the
            # key-share locks that inserting order lines (FK to the item) takes.
            .with_for_update(key_share=True)
        )
    }
    logged = {
        (line_id, item_id): (total, returned, entries)
        for line_id, item_id, total, returned, entries in session.execute(
            select(
                InventoryLog.order_line_id,
                InventoryLog.item_id,
                func.sum(InventoryLog.qty_change),
                func.sum(case((InventoryLog.reason == "order_returned", InventoryLog.qty_change))),
                func.count(),
            )
            .where(InventoryLog.order_line_id.in_([line.order_line_id for line in lines]))
            .group_by(InventoryLog.order_line_id, InventoryLog.item_id)
        )
    }

    changed: set[int] = set()
    note = f"Order {header.order_number}"
    for line in lines:
        item = items[line.variant_id]
        if not counts_for_stock(header.order_date, item):
            logger.info(
                "Order %s line %s: placed before %s's starting stock; not counted",
                header.order_number,
                line.line_number,
                item.sku,
            )
            continue
        total, returned, entries = logged.get((line.order_line_id, item.item_id), (0, None, 0))
        returned = returned or 0  # SUM of no returns is NULL
        changes = []
        returned_change = line.returned_qty - returned
        if returned_change:
            changes.append((returned_change, "order_returned"))
        should_hold = max(line.ordered_qty - line.cancelled_qty - line.returned_qty, 0)
        held = -(total + returned_change)
        if should_hold > held:
            reason = "order_created" if new_order or not entries else "order_edited"
            changes.append((held - should_hold, reason))
        elif should_hold < held:
            changes.append((held - should_hold, "order_cancelled"))
        for qty_change, reason in changes:
            log_change(session, item, qty_change, reason, note, line.order_line_id)
            changed.add(item.item_id)
    return changed


def line_stock(session: Session, header: OrderHeader) -> dict[int, dict[str, Any]]:
    """Each line's effect on stock, for the order page: the net change logged for it,
    or why it doesn't count."""
    item_ids = {line.variant_id for line in header.lines if line.variant_id}
    items = {
        item.item_id: item
        for item in session.scalars(select(Item).where(Item.item_id.in_(item_ids)))
    }
    totals = dict(
        session.execute(
            select(InventoryLog.order_line_id, func.sum(InventoryLog.qty_change))
            .where(InventoryLog.order_line_id.in_([line.order_line_id for line in header.lines]))
            .group_by(InventoryLog.order_line_id)
        ).all()
    )
    effects = {}
    for line in header.lines:
        item = items.get(line.variant_id)
        if item is None:
            skipped = "not_linked"
        elif not counts_for_stock(header.order_date, item):
            skipped = "before_starting_stock"
        else:
            skipped = None
        effects[line.order_line_id] = {
            "item_id": item.item_id if item else None,
            "change": totals.get(line.order_line_id, 0),
            "not_counted": skipped,
        }
    return effects
