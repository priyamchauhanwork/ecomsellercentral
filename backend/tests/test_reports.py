"""Dashboard figures (#24): IST day boundary, what counts as an order and as revenue,
stock counts equal to the inventory page, recent orders and the lowest stock."""

from datetime import date, timedelta

import pytest
from sqlalchemy import select, text, update

from app.db import SessionLocal
from app.models import Article, OrderHeader, Return
from tests.shopify_orders import shopify_order
from tests.stock_helpers import adjust, client, import_orders, item_id, order_id

pytestmark = pytest.mark.usefixtures("clean_db")


def dashboard():
    response = client.get("/reports/dashboard")
    assert response.status_code == 200, response.json()
    return response.json()


def today_ist() -> date:
    """The database's idea of today in India (the dashboard uses the same clock)."""
    with SessionLocal() as session:
        return session.scalar(text("SELECT (now() AT TIME ZONE 'Asia/Kolkata')::date"))


def placed(order_number: int, when: str, **overrides):
    return shopify_order(
        order_id=order_number + 5000,
        created_at=when,
        updated_at=when,
        processed_at=when,
        **overrides,
    )


def test_empty_database():
    body = dashboard()
    assert body["orders"] == {
        "orders_today": 0,
        "revenue_today": "0",
        "refunded_today": "0",
        "orders_total": 0,
        "revenue_total": "0",
        "refunded_total": "0",
        "cancelled_total": 0,
        "other_currency_orders": 0,
        "needs_attention": 0,
    }
    assert body["stock"] == {"low_stock": 0, "out_of_stock": 0, "skus": 0, "products": 0}
    assert body["recent_orders"] == [] and body["low_stock_items"] == []
    assert body["returns_awaiting_qc"] == 0
    assert body["today"] == str(today_ist()) and body["currency"] == "INR"
    assert body["low_stock_threshold"] == 5


def test_today_is_the_ist_calendar_day():
    today = today_ist()
    yesterday = today - timedelta(days=1)
    import_orders(
        [
            placed(1, f"{today}T00:10:00+05:30"),  # still yesterday in UTC
            placed(2, f"{today}T23:30:00+05:30"),
            placed(3, f"{yesterday}T23:50:00+05:30"),  # today in UTC, yesterday in India
        ]
    )
    orders = dashboard()["orders"]
    assert orders["orders_today"] == 2 and orders["orders_total"] == 3
    assert orders["revenue_today"] == "5898.00" and orders["revenue_total"] == "8847.00"


def test_cancelled_deleted_and_other_currency_orders():
    today = today_ist()
    import_orders(
        [
            placed(1, f"{today}T10:00:00+05:30"),
            placed(2, f"{today}T11:00:00+05:30", cancelled_at=f"{today}T11:05:00+05:30"),
            placed(3, f"{today}T12:00:00+05:30", currency="USD"),
            placed(4, f"{today}T13:00:00+05:30"),
        ]
    )
    with SessionLocal.begin() as session:
        session.execute(
            update(OrderHeader).where(OrderHeader.order_number == "#1004").values(is_deleted=True)
        )
        session.execute(
            update(OrderHeader)
            .where(OrderHeader.order_number == "#1001")
            .values(refunded_amount=500)
        )
    orders = dashboard()["orders"]
    assert orders["orders_today"] == 3  # placed, cancelled one included; deleted one not
    assert orders["revenue_today"] == "2949.00"  # only #1001: not cancelled, INR
    assert orders["refunded_today"] == "500.00" and orders["refunded_total"] == "500.00"
    assert orders["cancelled_total"] == 1 and orders["other_currency_orders"] == 1
    assert [o["order_number"] for o in dashboard()["recent_orders"]] == ["#1003", "#1002", "#1001"]


def test_recent_orders_are_the_five_newest_with_stable_ties():
    today = today_ist()
    import_orders([placed(n, f"{today}T0{n}:00:00+05:30") for n in range(1, 7)])
    import_orders([placed(7, f"{today}T06:00:00+05:30")])  # same time as #1006
    recent = dashboard()["recent_orders"]
    assert [o["order_number"] for o in recent] == ["#1007", "#1006", "#1005", "#1004", "#1003"]
    first = recent[0]
    assert first["order_header_id"] == order_id("5007") and first["line_count"] == 1
    assert first["total_amount"] == "2949.00" and first["currency"] == "INR"
    assert first["order_status"] == "pending" and first["attention_reason"] is None


def test_stock_counts_match_the_inventory_page(watches):
    adjust(item_id("WATCH-BLK-40"), -3, "correction")  # 4 left: low
    with SessionLocal.begin() as session:
        session.execute(
            update(Article).where(Article.name == "Leather Strap").values(status="archived")
        )
    body = dashboard()
    summary = client.get("/inventory").json()["summary"]
    assert body["stock"] == {
        "low_stock": summary["low_stock"],
        "out_of_stock": summary["out_of_stock"],
        "skus": summary["items"],
        "products": 1,
    }
    assert (
        body["stock"]["low_stock"] + body["stock"]["out_of_stock"]
        == (client.get("/inventory", params={"low": True}).json()["total"])
    )
    lowest = [(row["sku"], row["available"]) for row in body["low_stock_items"]]
    assert lowest == [("WATCH-SLV-40", -2), ("WATCH-BLK-40", 4)]  # oversold first


def test_lowest_stock_lists_at_most_five(watches):
    for sku in ("WATCH-BLK-40", "WATCH-BLK-42", "WATCH-SLV-42"):
        adjust(item_id(sku), -7, "correction")
    created = client.post(
        "/products",
        json={
            "article_code": "MUG",
            "name": "Mug",
            "items": [
                {"sku": f"MUG-{n}", "variant_values": {"Size": str(n)}, "stock_qty": n}
                for n in range(1, 4)
            ],
            "variation_axes": ["Size"],
        },
    )
    assert created.status_code == 201, created.json()
    rows = dashboard()["low_stock_items"]
    assert len(rows) == 5
    assert [row["available"] for row in rows] == sorted(row["available"] for row in rows)
    assert rows[0]["sku"] == "WATCH-SLV-40" and rows[0]["item_id"]


def test_needs_attention_and_returns_awaiting_qc(watches):
    import_orders([shopify_order()])
    with SessionLocal.begin() as session:
        header = session.scalars(select(OrderHeader)).one()
        header.attention_reason = "cancelled_after_acknowledgement"
        for status in ("open", "received", "qc_done", "requested"):
            session.add(
                Return(
                    seller_id=1,
                    order_header_id=header.order_header_id,
                    channel="shopify",
                    return_number=f"R-{status}",
                    return_type="rto",
                    status=status,
                    version_no=1,
                )
            )
    body = dashboard()
    assert body["orders"]["needs_attention"] == 1 and body["returns_awaiting_qc"] == 2
    assert body["recent_orders"][0]["attention_reason"] == "cancelled_after_acknowledgement"
    assert client.get("/returns", params={"pending": True}).json()["total"] == 2


def test_needs_the_api_key(monkeypatch):
    from app.config import get_settings

    monkeypatch.setattr(get_settings(), "api_shared_secret", "secret")
    assert client.get("/reports/dashboard").status_code == 401
    assert client.get("/reports/dashboard", headers={"X-API-Key": "secret"}).status_code == 200
