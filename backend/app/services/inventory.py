"""Stock reads and manual changes (#19, #51): the inventory list and item with every
supply and demand figure, manual adjustments of on-hand stock, damaged units, and the
stock history. The figures come from the ecs_item_stock view (docs/inventory-erd.md).
"""

from typing import Any

from sqlalchemy import Date, Integer, column, func, or_, select, table
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
    Return,
    ReturnLine,
)
from app.services import stock
from app.services.catalog import LOW_STOCK, like_pattern, stock_status
from app.services.product_import import image_for

SHOPIFY = "SHOPIFY"  # ecs_channels.code
HISTORY_LIMIT = 100
FIGURES = (
    "onhand",
    "open",
    "allocated",
    "shipped",
    "return_pending",
    "damaged",
    "available",
    "future_available",
    "on_shelf",
)

item_stock = table(
    "ecs_item_stock",
    column("item_id", Integer),
    *(column(name, Integer) for name in FIGURES),
    column("next_eta", Date),
)


class ItemNotFound(LookupError):
    pass


# --- List and detail ----------------------------------------------------------------------


def inventory_rows(
    session: Session,
    filters: list,
    limit: int | None = None,
    offset: int = 0,
    order_by: tuple | None = None,
) -> list[dict[str, Any]]:
    """Items with their article, stock figures and Shopify listing item."""
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
            *(item_stock.c[name] for name in FIGURES),
            item_stock.c.next_eta,
        )
        .join(Article, Article.article_id == Item.article_id)
        .join(item_stock, item_stock.c.item_id == Item.item_id)
        .outerjoin(shopify_listing, shopify_listing.c.item_id == Item.item_id)
        .where(*filters)
        .order_by(*(order_by or (Article.name, Item.sku)))
        .limit(limit)
        .offset(offset)
    ).all()
    result = []
    for row in rows:
        item, name, images, listing_item_id, listing_status, sync_error = row[:6]
        figures = dict(zip(FIGURES, row[6 : 6 + len(FIGURES)], strict=True))
        result.append(
            {
                "item_id": item.item_id,
                "article_id": item.article_id,
                "article_name": name,
                "sku": item.sku,
                "barcode": item.barcode,
                "variant_values": item.variant_values,
                "image_url": image_for(images, item.variant_values),
                "stock_qty": figures["available"],
                "stock_status": stock_status(figures["available"]),
                **figures,
                "committed": figures["open"] + figures["allocated"] + figures["shipped"],
                "next_eta": row[-1],
                "status": item.status,
                "shopify": None
                if listing_item_id is None
                else {
                    "listing_item_id": listing_item_id,
                    "status": listing_status,
                    "last_sync_error": sync_error,
                },
            }
        )
    return result


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
    filtered ones), so the summary bar doesn't change while searching. Low and out of
    stock are judged on `available`."""
    filters = listed_filters(seller_id)
    base = (
        select(func.count())
        .select_from(Item)
        .join(Article, Article.article_id == Item.article_id)
        .join(item_stock, item_stock.c.item_id == Item.item_id)
    )
    available = item_stock.c.available
    total, low_count, out_count, returning, damaged = session.execute(
        base.add_columns(
            func.count().filter(available > 0, available <= LOW_STOCK),
            func.count().filter(available <= 0),
            func.coalesce(func.sum(item_stock.c.return_pending), 0),
            func.coalesce(func.sum(item_stock.c.damaged), 0),
        ).where(*filters)
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
        filters.append(available <= LOW_STOCK)
    return {
        "summary": {
            "items": total,
            "low_stock": low_count,
            "out_of_stock": out_count,
            "returning": returning,
            "damaged": damaged,
        },
        "total": session.scalar(base.where(*filters)),
        "items": inventory_rows(session, filters, limit, offset),
    }


def inventory_item(session: Session, seller_id: int, item_id: int) -> dict[str, Any]:
    rows = inventory_rows(session, [Item.seller_id == seller_id, Item.item_id == item_id])
    if not rows:
        raise ItemNotFound(item_id)
    return rows[0]


def item_for_update(session: Session, seller_id: int, item_id: int) -> Item:
    item = session.get(Item, item_id)
    if item is None or item.seller_id != seller_id:
        raise ItemNotFound(item_id)
    return item


# --- Manual changes -----------------------------------------------------------------------


def adjust_stock(
    session: Session,
    seller_id: int,
    item_id: int,
    quantity_change: int,
    reason: str,
    note: str | None,
) -> stock.StockChange:
    """Change on-hand stock by hand (reason `damage` moves units to DAMAGED)."""
    item = item_for_update(session, seller_id, item_id)
    return stock.adjust_onhand(session, item, quantity_change, reason, note)


def change_damaged(
    session: Session, seller_id: int, item_id: int, action: str, quantity: int, note: str | None
) -> stock.StockChange:
    item = item_for_update(session, seller_id, item_id)
    return stock.change_damaged(session, item, action, quantity, note)


# --- History ------------------------------------------------------------------------------


def stock_history(session: Session, seller_id: int, item_id: int) -> list[dict[str, Any]]:
    """The item's latest stock changes, newest first, with the order or return behind
    order- and return-caused ones. Rows of one event share a transaction id."""
    item_for_update(session, seller_id, item_id)
    rows = session.execute(
        select(
            InventoryLog,
            OrderHeader.order_header_id,
            OrderHeader.order_number,
            Return.return_id,
            Return.return_number,
        )
        .outerjoin(ReturnLine, ReturnLine.return_line_id == InventoryLog.return_line_id)
        .outerjoin(Return, Return.return_id == ReturnLine.return_id)
        .outerjoin(
            OrderLine,
            OrderLine.order_line_id
            == func.coalesce(InventoryLog.order_line_id, ReturnLine.order_line_id),
        )
        .outerjoin(OrderHeader, OrderHeader.order_header_id == OrderLine.order_header_id)
        .where(InventoryLog.item_id == item_id)
        .order_by(InventoryLog.created_at.desc(), InventoryLog.inventory_log_id.desc())
        .limit(HISTORY_LIMIT)
    ).all()
    return [
        {
            "inventory_log_id": entry.inventory_log_id,
            "side": entry.side,
            "inv_type": entry.inv_type,
            "qty_change": entry.qty_change,
            "stock_after": entry.stock_after,
            "reason": entry.reason,
            "note": entry.note,
            "transaction_id": str(entry.transaction_id) if entry.transaction_id else None,
            "order_header_id": order_header_id,
            "order_number": order_number,
            "return_id": return_id,
            "return_number": return_number,
            "created_at": entry.created_at,
        }
        for entry, order_header_id, order_number, return_id, return_number in rows
    ]
