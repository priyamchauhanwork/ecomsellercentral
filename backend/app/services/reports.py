"""Dashboard figures (#24), computed in the database. Grows into the Reports API (#29).

"Today" is the Asia/Kolkata calendar day of the order date (when the order was placed),
taken from the database clock, never the server's (Render runs in UTC).
"""

from typing import Any

from sqlalchemy import Date, cast, func, select
from sqlalchemy.orm import Session

from app.models import Article, Item, OrderHeader, OrderLine, Return
from app.services import inventory
from app.services.catalog import LOW_STOCK

IST = "Asia/Kolkata"
CURRENCY = "INR"  # revenue is only added up in this currency
RECENT_ORDERS = 5
LOW_STOCK_ITEMS = 5


def ist_date(column):
    return cast(func.timezone(IST, column), Date)


def order_stats(session: Session, seller_id: int) -> dict[str, Any]:
    """Orders placed (cancelled included) and revenue (not cancelled, INR only), today
    and all time. Orders in another currency are counted and reported, never added."""
    today = ist_date(func.now())
    is_today = ist_date(OrderHeader.order_date) == today
    counts = OrderHeader.order_status != "cancelled"
    in_currency = OrderHeader.currency == CURRENCY

    def total(column, *conditions):
        return func.coalesce(func.sum(column).filter(counts, in_currency, *conditions), 0)

    row = session.execute(
        select(
            func.count().filter(is_today).label("orders_today"),
            total(OrderHeader.total_amount, is_today).label("revenue_today"),
            total(OrderHeader.refunded_amount, is_today).label("refunded_today"),
            func.count().label("orders_total"),
            total(OrderHeader.total_amount).label("revenue_total"),
            total(OrderHeader.refunded_amount).label("refunded_total"),
            func.count().filter(OrderHeader.order_status == "cancelled").label("cancelled_total"),
            func.count().filter(counts, ~in_currency).label("other_currency_orders"),
            func.count().filter(OrderHeader.attention_reason.is_not(None)).label("needs_attention"),
            today.label("today"),
            func.now().label("generated_at"),
        ).where(OrderHeader.seller_id == seller_id, OrderHeader.is_deleted.is_(False))
    ).one()
    return dict(row._mapping)


def stock_stats(session: Session, seller_id: int) -> dict[str, Any]:
    """The inventory page's counts (same scope: active SKUs of products that aren't
    archived; low = 0 < available ≤ 5, out = available ≤ 0) and the product count."""
    summary = inventory.list_inventory(session, seller_id, limit=1)["summary"]
    products = session.scalar(
        select(func.count(func.distinct(Item.article_id)))
        .join(Article, Article.article_id == Item.article_id)
        .where(*inventory.listed_filters(seller_id))
    )
    return {
        "low_stock": summary["low_stock"],
        "out_of_stock": summary["out_of_stock"],
        "skus": summary["items"],
        "products": products,
    }


def recent_orders(session: Session, seller_id: int) -> list[dict[str, Any]]:
    line_count = (
        select(func.count(OrderLine.order_line_id))
        .where(OrderLine.order_header_id == OrderHeader.order_header_id)
        .scalar_subquery()
    )
    rows = session.execute(
        select(OrderHeader, line_count)
        .where(OrderHeader.seller_id == seller_id, OrderHeader.is_deleted.is_(False))
        .order_by(OrderHeader.order_date.desc(), OrderHeader.order_header_id.desc())
        .limit(RECENT_ORDERS)
    ).all()
    return [
        {
            "order_header_id": header.order_header_id,
            "order_number": header.order_number,
            "order_date": header.order_date,
            "customer_name": header.customer_name,
            "total_amount": header.total_amount,
            "currency": header.currency,
            "order_status": header.order_status,
            "attention_reason": header.attention_reason,
            "line_count": count,
        }
        for header, count in rows
    ]


def lowest_stock(session: Session, seller_id: int) -> list[dict[str, Any]]:
    """The SKUs with the least available stock (oversold first), at most 5."""
    available = inventory.item_stock.c.available
    return inventory.inventory_rows(
        session,
        [*inventory.listed_filters(seller_id), available <= LOW_STOCK],
        limit=LOW_STOCK_ITEMS,
        order_by=(available, Article.name, Item.sku),
    )


def returns_awaiting_qc(session: Session, seller_id: int) -> int:
    """Returns and RTOs still to receive or check (the Returns page's pending view)."""
    return session.scalar(
        select(func.count()).where(
            Return.seller_id == seller_id, Return.status.in_(("open", "received"))
        )
    )


def dashboard(session: Session, seller_id: int) -> dict[str, Any]:
    orders = order_stats(session, seller_id)
    return {
        "generated_at": orders.pop("generated_at"),
        "today": orders.pop("today"),
        "currency": CURRENCY,
        "low_stock_threshold": LOW_STOCK,
        "orders": orders,
        "stock": stock_stats(session, seller_id),
        "returns_awaiting_qc": returns_awaiting_qc(session, seller_id),
        "recent_orders": recent_orders(session, seller_id),
        "low_stock_items": lowest_stock(session, seller_id),
    }
