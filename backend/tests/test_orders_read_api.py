"""GET /orders, GET /orders/{id} and POST /orders/{id}/status (#10)."""

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select, update

from app.db import SessionLocal
from app.main import app
from app.models import OrderHeader, OrderHistory, OrderLineHistory
from tests.shopify_orders import line_item, shopify_order
from tests.test_order_ingestion import import_orders

pytestmark = pytest.mark.usefixtures("clean_db")
client = TestClient(app)


def seed():
    """Three orders: #1001 on 17 Oct 23:30 IST, #1002 on 18 Oct 00:30 IST (2 lines),
    #1003 on 20 Oct, cancelled."""
    import_orders(
        [
            shopify_order(5001, created_at="2026-10-17T23:30:00+05:30"),
            shopify_order(
                5002,
                created_at="2026-10-18T00:30:00+05:30",
                email="rahul@example.com",
                customer={"first_name": "Rahul", "last_name": "Mehta", "phone": "+91 99999 11111"},
                line_items=[line_item(9101), line_item(9102, sku="STRAP-01", title="Strap")],
            ),
            shopify_order(
                5003,
                created_at="2026-10-20T10:00:00+05:30",
                cancelled_at="2026-10-20T11:00:00+05:30",
                line_items=[line_item(9201)],
            ),
        ]
    )
    with SessionLocal() as session:
        return {
            h.order_number: h.order_header_id for h in session.scalars(select(OrderHeader)).all()
        }


def numbers(response):
    return [order["order_number"] for order in response.json()["orders"]]


# --- GET /orders --------------------------------------------------------------------------


def test_list_is_newest_first_with_totals_and_line_counts():
    seed()
    body = client.get("/orders").json()
    assert body["total"] == 3
    assert [o["order_number"] for o in body["orders"]] == ["#1003", "#1002", "#1001"]
    assert [o["line_count"] for o in body["orders"]] == [1, 2, 1]
    assert body["orders"][0]["order_status"] == "cancelled"
    assert body["orders"][1]["total_amount"] == "2949.00"


def test_status_filter():
    seed()
    assert numbers(client.get("/orders?status=pending")) == ["#1002", "#1001"]
    assert numbers(client.get("/orders?status=cancelled")) == ["#1003"]
    assert client.get("/orders?status=shipped").json() == {"total": 0, "orders": []}


def test_date_filters_use_ist_day_boundaries():
    seed()
    # 23:30 IST on the 17th is 18:00 UTC; it must count as the 17th, not the 18th.
    assert numbers(client.get("/orders?from=2026-10-17&to=2026-10-17")) == ["#1001"]
    assert numbers(client.get("/orders?from=2026-10-18&to=2026-10-18")) == ["#1002"]
    assert numbers(client.get("/orders?from=2026-10-18")) == ["#1003", "#1002"]
    assert numbers(client.get("/orders?to=2026-10-18")) == ["#1002", "#1001"]


def test_search_by_number_name_email_and_phone():
    seed()
    assert numbers(client.get("/orders?q=1002")) == ["#1002"]
    assert numbers(client.get("/orders?q=rahul")) == ["#1002"]  # name, case-insensitive
    assert numbers(client.get("/orders?q=RAHUL@EXAMPLE")) == ["#1002"]
    assert numbers(client.get("/orders?q=99999")) == ["#1002"]
    assert numbers(client.get("/orders?q=%20%20")) == ["#1003", "#1002", "#1001"]  # blank


def test_search_wildcards_are_literal():
    seed()
    assert client.get("/orders?q=%25").json()["total"] == 0  # "%"
    assert client.get("/orders?q=_").json()["total"] == 0


def test_limit_keeps_the_full_total():
    seed()
    response = client.get("/orders?limit=1")
    assert response.json()["total"] == 3 and numbers(response) == ["#1003"]


def test_deleted_orders_are_hidden():
    ids = seed()
    with SessionLocal.begin() as session:
        session.execute(
            update(OrderHeader)
            .where(OrderHeader.order_header_id == ids["#1001"])
            .values(is_deleted=True)
        )
    assert client.get("/orders").json()["total"] == 2
    assert client.get(f"/orders/{ids['#1001']}").status_code == 404


@pytest.mark.parametrize(
    "query",
    [
        "status=lost",
        "from=17-10-2026",
        "from=2026-10-20&to=2026-10-18",
        "limit=0",
        "limit=1001",
        "q=" + "x" * 101,
    ],
)
def test_invalid_list_parameters_are_422(query):
    assert client.get(f"/orders?{query}").status_code == 422


# --- GET /orders/{id} ---------------------------------------------------------------------


def test_detail_has_lines_in_order_and_history_oldest_first():
    ids = seed()
    body = client.get(f"/orders/{ids['#1002']}").json()
    assert body["order_number"] == "#1002" and body["line_count"] == 2
    assert [line["line_number"] for line in body["lines"]] == [1, 2]
    assert body["lines"][1]["sku"] == "STRAP-01"
    assert body["ship_city"] == "Bengaluru" and body["customer_name"] == "Rahul Mehta"
    assert [(h["action_type"], h["change_source"]) for h in body["history"]] == [
        ("created", "manual_sync")
    ]


def test_detail_unknown_or_invalid_id():
    assert client.get("/orders/999999").status_code == 404
    assert client.get("/orders/999999").json() == {"detail": "Order not found"}
    assert client.get("/orders/abc").status_code == 422


# --- POST /orders/{id}/status -------------------------------------------------------------


def status(order_id, new_status, **extra):
    return client.post(f"/orders/{order_id}/status", json={"status": new_status, **extra})


def history_counts(order_id):
    with SessionLocal() as session:
        header_rows = session.scalar(
            select(func.count())
            .select_from(OrderHistory)
            .where(OrderHistory.order_header_id == order_id)
        )
        line_rows = session.scalar(
            select(func.count())
            .select_from(OrderLineHistory)
            .where(OrderLineHistory.order_header_id == order_id)
        )
    return header_rows, line_rows


def test_mark_shipped_updates_lines_header_and_history():
    ids = seed()
    order_id = ids["#1002"]
    before = history_counts(order_id)

    response = status(order_id, "shipped", remarks="Packed by Ravi")
    assert response.status_code == 200
    body = response.json()
    assert body["order_status"] == "shipped" and body["version_no"] == 2
    for line in body["lines"]:
        assert line["line_status"] == "shipped"
        assert line["shipped_qty"] == line["ordered_qty"] and line["shipped_at"]
        assert line["version_no"] == 2
    latest = body["history"][-1]
    assert (latest["action_type"], latest["old_status"], latest["new_status"]) == (
        "status_change",
        "pending",
        "shipped",
    )
    assert latest["change_source"] == "admin_ui" and latest["order_import_id"] is None
    assert latest["remarks"] == "Packed by Ravi"
    # one header history row, one line history row per line
    assert history_counts(order_id) == (before[0] + 1, before[1] + 2)
    assert numbers(client.get("/orders?status=shipped")) == ["#1002"]


def test_shipped_then_delivered():
    ids = seed()
    status(ids["#1001"], "shipped")
    body = status(ids["#1001"], "delivered").json()
    assert body["order_status"] == "delivered"
    assert body["lines"][0]["line_status"] == "delivered" and body["lines"][0]["delivered_at"]


def test_cancel_pending_order_uses_remarks_as_reason():
    ids = seed()
    body = status(ids["#1001"], "cancelled", remarks="Customer changed mind").json()
    assert body["order_status"] == "cancelled" and body["cancelled_at"]
    assert body["cancel_reason"] == "Customer changed mind"
    assert body["history"][-1]["action_type"] == "cancelled"
    line = body["lines"][0]
    assert line["line_status"] == "cancelled" and line["cancelled_qty"] == line["ordered_qty"]


def test_same_status_again_is_a_no_op():
    ids = seed()
    status(ids["#1001"], "shipped")
    before = history_counts(ids["#1001"])
    response = status(ids["#1001"], "shipped")
    assert response.status_code == 200 and response.json()["version_no"] == 2
    assert history_counts(ids["#1001"]) == before


@pytest.mark.parametrize(
    ("first", "then"),
    [
        ("shipped", "pending"),
        ("shipped", "cancelled"),
        ("delivered", "shipped"),
        ("cancelled", "shipped"),
    ],
)
def test_backward_or_final_moves_are_409(first, then):
    ids = seed()
    status(ids["#1001"], first)
    before = history_counts(ids["#1001"])
    response = status(ids["#1001"], then)
    assert response.status_code == 409
    assert f"can't be marked {then}" in response.json()["detail"]
    assert history_counts(ids["#1001"]) == before


def test_cancelled_lines_are_left_alone():
    import_orders(
        [shopify_order(5001, line_items=[line_item(9001, current_quantity=0), line_item(9002)])]
    )
    with SessionLocal() as session:
        order_id = session.scalar(select(OrderHeader.order_header_id))
    body = status(order_id, "shipped").json()
    assert [line["line_status"] for line in body["lines"]] == ["cancelled", "shipped"]
    assert body["lines"][0]["shipped_qty"] == 0
    assert history_counts(order_id)[1] == 2 + 1  # 2 created + 1 changed line


@pytest.mark.parametrize(
    "payload",
    [{"status": "lost"}, {}, {"status": "shipped", "remarks": "x" * 501}],
)
def test_invalid_status_body_is_422(payload):
    ids = seed()
    assert client.post(f"/orders/{ids['#1001']}/status", json=payload).status_code == 422


def test_status_on_unknown_order_is_404():
    assert status(999999, "shipped").status_code == 404


def test_manual_shipped_survives_a_later_shopify_import():
    ids = seed()
    status(ids["#1001"], "shipped")
    import_orders(
        [
            shopify_order(
                5001,
                created_at="2026-10-17T23:30:00+05:30",
                updated_at="2026-10-19T09:00:00+05:30",
                note="changed on Shopify",
            )
        ]
    )
    body = client.get(f"/orders/{ids['#1001']}").json()
    assert body["order_status"] == "shipped" and body["customer_notes"] == "changed on Shopify"
