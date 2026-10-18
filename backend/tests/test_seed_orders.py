"""scripts/seed_orders: test data for the order screens."""

from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.orm import selectinload

from app.config import get_settings
from app.db import SessionLocal
from app.main import app
from app.models import OrderHeader, OrderHistory, OrderLine, OrderLineHistory
from app.services.order_mapping import order_status_from_lines
from scripts import seed_orders
from tests.shopify_orders import shopify_order
from tests.test_order_ingestion import import_orders

pytestmark = pytest.mark.usefixtures("clean_db")


def count(model) -> int:
    with SessionLocal() as session:
        return session.scalar(select(func.count()).select_from(model))


def seeded() -> list[OrderHeader]:
    with SessionLocal() as session:
        return session.scalars(
            select(OrderHeader)
            .where(OrderHeader.external_order_id.like("seed-%"))
            .options(selectinload(OrderHeader.lines), selectinload(OrderHeader.history))
        ).all()


def test_creates_consistent_orders_in_every_status():
    assert seed_orders.main(["--count", "200"]) == 0
    orders = seeded()
    assert len(orders) == 200
    assert {o.order_status for o in orders} == {"pending", "shipped", "delivered", "cancelled"}
    assert any(o.currency == "USD" for o in orders)
    assert any(o.customer_name is None for o in orders)  # guests

    for order in orders:
        lines = order.lines
        assert lines and order.order_number.startswith("SEED-")
        assert order.channel == "website" and order.ingestion_method == "direct"
        # the header status is what the lines say, as the ERD requires
        assert order.order_status == order_status_from_lines([line.line_status for line in lines])
        assert order.subtotal_amount == sum(line.line_total_amount for line in lines)
        tax = sum(line.cgst_amount + line.sgst_amount + line.igst_amount for line in lines)
        assert order.tax_amount == tax
        assert order.total_amount == order.subtotal_amount + order.shipping_amount + tax
        for line in lines:
            assert line.line_total_amount == (
                line.unit_price * line.ordered_qty - line.line_discount_amount
            ).quantize(Decimal("0.01"))
        assert order.history[0].action_type == "created"
        assert len(order.history) == (1 if order.order_status == "pending" else 2)

    assert count(OrderLineHistory) == count(OrderLine)


def test_seeded_orders_work_with_the_orders_api():
    seed_orders.main(["--count", "60"])
    client = TestClient(app)
    assert client.get("/orders").json()["total"] == 60
    # the % and _ in this customer's name must be matched literally
    found = client.get("/orders", params={"q": "50%_Off"}).json()
    assert [o["customer_name"] for o in found["orders"]] == ["50%_Off Watch Store"]
    pending = client.get("/orders?status=pending").json()["orders"]
    assert pending and all(o["order_status"] == "pending" for o in pending)


def test_same_seed_gives_the_same_orders():
    seed_orders.main(["--count", "20", "--seed", "7"])
    first = [(o.order_number, o.order_status, o.total_amount) for o in seeded()]
    seed_orders.main(["--replace", "--count", "20", "--seed", "7"])
    again = [(o.order_number, o.order_status, o.total_amount) for o in seeded()]
    assert sorted(first) == sorted(again)


def test_refuses_to_seed_twice_without_replace():
    seed_orders.main(["--count", "10"])
    assert seed_orders.main(["--count", "10"]) == 1
    assert len(seeded()) == 10
    assert seed_orders.main(["--replace", "--count", "30"]) == 0
    assert len(seeded()) == 30


def test_delete_removes_seed_data_only():
    import_orders([shopify_order(5001)])  # a real Shopify order
    seed_orders.main(["--count", "50"])

    assert seed_orders.main(["--delete"]) == 0
    assert seeded() == []
    with SessionLocal() as session:
        remaining = session.scalars(select(OrderHeader)).all()
    assert [o.order_number for o in remaining] == ["#1001"]
    assert count(OrderLine) == 1 and count(OrderHistory) == 1 and count(OrderLineHistory) == 1
    assert seed_orders.main(["--delete"]) == 0  # nothing left: still fine


def test_refuses_production(monkeypatch):
    monkeypatch.setattr(get_settings(), "environment", "production")
    assert seed_orders.main(["--count", "5"]) == 1
    assert seed_orders.main(["--delete"]) == 1
    assert seeded() == []


@pytest.mark.parametrize("count_arg", ["0", "10000"])
def test_count_must_be_in_range(count_arg):
    with pytest.raises(SystemExit):
        seed_orders.main(["--count", count_arg])


def test_line_items_per_order_follow_the_weights():
    seed_orders.main(["--count", "1000"])
    sizes = [len(order.lines) for order in seeded()]
    for line_count, percent in seed_orders.LINE_COUNT_WEIGHTS.items():
        share = 100 * sizes.count(line_count) / len(sizes)
        assert abs(share - percent) <= 4, (line_count, share)
    assert set(sizes) == {1, 2, 3, 4}
