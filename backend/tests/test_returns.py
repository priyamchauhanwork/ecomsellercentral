"""Returns and RTOs with the supply & demand model (#53–#55): scenarios R1–R17 and
T1–T7 of docs/inventory-erd.md."""

from datetime import timedelta

import pytest
from sqlalchemy import select

from app.db import SessionLocal
from app.models import OrderImportRow, Return, ReturnLine
from app.services import order_ingestion
from app.services.returns import today
from tests.shopify_orders import fulfilled
from tests.stock_helpers import (
    client,
    figures,
    import_orders,
    line_item,
    lines,
    logs,
    order,
    order_id,
    set_baseline,
    state,
    sync_orders,
)

pytestmark = pytest.mark.usefixtures("clean_db")


def shopify_return(
    return_id=601,
    status="OPEN",
    qty=1,
    line_item_id=9001,
    dispositions=(),
    created="2026-10-18T10:00:00Z",
    reverse=True,
):
    """A Shopify Return node as the GraphQL query returns it."""
    line = {"lineItem": {"id": f"gid://shopify/LineItem/{line_item_id}"}}
    return {
        "id": f"gid://shopify/Return/{return_id}",
        "name": f"#1001-R{return_id - 600}",
        "status": status,
        "createdAt": created,
        "closedAt": None,
        "returnLineItems": {
            "nodes": [
                {
                    "id": f"gid://shopify/ReturnLineItem/{return_id}1",
                    "quantity": qty,
                    "customerNote": "Too big",
                    "returnReason": "SIZE_TOO_LARGE",
                    "fulfillmentLineItem": line,
                }
            ]
        },
        "reverseFulfillmentOrders": {
            "nodes": [
                {
                    "id": f"gid://shopify/ReverseFulfillmentOrder/{return_id}",
                    "lineItems": {
                        "nodes": [
                            {
                                "id": f"gid://shopify/ReverseFulfillmentOrderLineItem/{return_id}9",
                                "totalQuantity": qty,
                                "dispositions": [
                                    {"type": kind, "quantity": units}
                                    for kind, units in dispositions
                                ],
                                "fulfillmentLineItem": line,
                            }
                        ]
                    },
                }
            ]
            if reverse
            else []
        },
    }


def webhook(topic="returns/approve", delivery="wh-r1", payload=None):
    """A return webhook, recorded and processed as the endpoint does."""
    payload = payload or {"id": 601, "order": {"id": 5001}}
    with SessionLocal.begin() as session:
        run_id, row_id = order_ingestion.record_webhook_delivery(session, topic, delivery, payload)
    order_ingestion.run_webhook_import(run_id, row_id)
    with SessionLocal() as session:
        return session.get(OrderImportRow, row_id)


def shopify_says(shopify, *returns_, order_external="5001", delivery=None):
    shopify.returns[order_external] = list(returns_)
    return webhook(delivery=delivery or f"wh-{len(shopify.calls)}")


def the_return(number=None):
    with SessionLocal() as session:
        query = select(Return)
        if number:
            query = query.where(Return.return_number == number)
        ret = session.scalars(query.order_by(Return.return_id)).first()
        return client.get(f"/returns/{ret.return_id}").json()


@pytest.fixture
def delivered(watches):
    """2 × BLK-40 delivered: on hand 5."""
    import_orders([fulfilled(order(), delivered=True)])
    assert state() == figures(onhand=5)


# --- Shopify returns (R1–R17) -------------------------------------------------------------


def test_r1_requested_moves_nothing(delivered, shopify):
    shopify_says(shopify, shopify_return(status="REQUESTED"))
    assert the_return()["status"] == "requested" and state() == figures(onhand=5)


def test_r2_approved_return_is_pending_with_an_eta(delivered, shopify):
    row = shopify_says(shopify, shopify_return())
    assert row.action_taken == "updated"
    ret = the_return()
    assert ret["status"] == "open" and ret["return_type"] == "customer_return"
    assert ret["expected_at"] == str(today() + timedelta(days=10))
    assert (
        ret["lines"][0]["pending_qty"] == 1 and ret["lines"][0]["return_reason"] == "SIZE_TOO_LARGE"
    )
    assert state() == figures(onhand=5, return_pending=1)
    body = client.get(f"/inventory/{ret['lines'][0]['item_id']}").json()
    assert body["future_available"] == 6 and body["next_eta"] == ret["expected_at"]


def test_r2_a_return_implies_delivery(watches, shopify):
    import_orders([fulfilled(order())])  # shipped, never marked delivered
    assert state() == figures(shipped=2)
    shopify_says(shopify, shopify_return())
    assert state() == figures(onhand=6, shipped=1, return_pending=1)


def test_r3_declined_moves_nothing(delivered, shopify):
    shopify_says(shopify, shopify_return(status="REQUESTED"))
    shopify_says(shopify, shopify_return(status="DECLINED"))
    assert the_return()["status"] == "declined" and state() == figures(onhand=5)


def test_r4_r5_r6_cancel_close_reopen(delivered, shopify):
    shopify_says(shopify, shopify_return())
    shopify_says(shopify, shopify_return(status="CANCELED"))
    assert the_return()["status"] == "cancelled" and state() == figures(onhand=5)
    shopify_says(shopify, shopify_return(status="OPEN"))
    assert state() == figures(onhand=5, return_pending=1)
    shopify_says(shopify, shopify_return(status="CLOSED"))
    assert the_return()["status"] == "closed" and state() == figures(onhand=5)
    shopify_says(shopify, shopify_return(status="OPEN"))  # reopened
    assert state() == figures(onhand=5, return_pending=1)
    assert [e.reason for e in logs(inv_type="RETURN_PENDING")] == [
        "return_opened",
        "return_cancelled",
        "return_opened",
        "return_closed",
        "return_opened",
    ]


def test_r7_receive(delivered, shopify):
    shopify_says(shopify, shopify_return(qty=2))
    ret = the_return()
    line = ret["lines"][0]["return_line_id"]
    url = f"/returns/{ret['return_id']}/receive"
    assert (
        client.post(url, json={"lines": [{"return_line_id": line, "received_qty": 3}]}).status_code
        == 422
    )
    assert (
        client.post(url, json={"lines": [{"return_line_id": 999, "received_qty": 1}]}).status_code
        == 422
    )
    body = client.post(url, json={"lines": [{"return_line_id": line, "received_qty": 2}]}).json()
    assert body["status"] == "received" and body["received_at"]
    assert state() == figures(onhand=5, return_pending=2)  # stock moves at QC


def test_r8_r10_qc_in_the_app_reports_to_shopify_and_pushes(delivered, shopify):
    shopify.levels["8001"] = 5
    shopify_says(shopify, shopify_return(qty=2))
    ret = the_return()
    line = ret["lines"][0]["return_line_id"]
    body = client.post(
        f"/returns/{ret['return_id']}/qc",
        json={
            "lines": [{"return_line_id": line, "accepted": 1, "rejected": 1}],
            "note": "box torn",
        },
    ).json()
    assert body["status"] == "qc_done" and body["lines"][0]["qc_note"] == "box torn"
    assert [(d["quantity"], d["dispositionType"]) for d in shopify.disposed] == [
        (1, "RESTOCKED"),
        (1, "NOT_RESTOCKED"),
    ]
    assert shopify.disposed[0]["locationId"] == "gid://shopify/Location/1"
    assert state() == figures(onhand=6, damaged=1)
    assert shopify.levels["8001"] == 6  # pushed

    # Shopify's dispose webhook that follows changes nothing.
    restocked = shopify_return(
        qty=2, status="CLOSED", dispositions=[("RESTOCKED", 1), ("NOT_RESTOCKED", 1)]
    )
    shopify_says(shopify, restocked)
    assert state() == figures(onhand=6, damaged=1) and the_return()["status"] == "qc_done"
    assert len(shopify.disposed) == 2


def test_r8_shopify_refusing_the_qc_saves_nothing(delivered, shopify):
    shopify_says(shopify, shopify_return())
    ret = the_return()
    shopify.dispose_error = "Access denied: write_returns"
    response = client.post(
        f"/returns/{ret['return_id']}/qc",
        json={"lines": [{"return_line_id": ret["lines"][0]["return_line_id"], "accepted": 1}]},
    )
    assert response.status_code == 502 and "write_returns" in response.json()["detail"]
    assert the_return()["lines"][0]["accepted_qty"] == 0
    assert state() == figures(onhand=5, return_pending=1)


def test_r8_qc_needs_shopifys_reverse_fulfilment(delivered, shopify):
    shopify_says(shopify, shopify_return(reverse=False))
    ret = the_return()
    assert ret["lines"][0]["can_report_qc"] is False
    response = client.post(
        f"/returns/{ret['return_id']}/qc",
        json={"lines": [{"return_line_id": ret["lines"][0]["return_line_id"], "accepted": 1}]},
    )
    assert response.status_code == 409 and "approve it in Shopify" in response.json()["detail"]


def test_r9_qc_recorded_in_shopify(delivered, shopify):
    shopify.levels["8001"] = 5
    dispositions = [
        ("RESTOCKED", 1),
        ("NOT_RESTOCKED", 1),
        ("PROCESSING_REQUIRED", 1),
        ("MISSING", 1),
    ]
    import_orders(
        [
            fulfilled(
                order(minute=1, **lines(line_item(quantity=4, current_quantity=4))), delivered=True
            )
        ]
    )
    shopify_says(shopify, shopify_return(qty=4, dispositions=dispositions))
    line = the_return()["lines"][0]
    assert (
        line["accepted_qty"],
        line["rejected_qty"],
        line["missing_qty"],
        line["pending_qty"],
    ) == (1, 1, 1, 1)
    assert state() == figures(onhand=4, damaged=1, return_pending=1)
    assert shopify.levels["8001"] == 4


def test_r11_qc_in_steps_and_never_more_than_outstanding(delivered, shopify):
    shopify_says(shopify, shopify_return(qty=2))
    ret = the_return()
    url, line = f"/returns/{ret['return_id']}/qc", ret["lines"][0]["return_line_id"]
    assert (
        client.post(url, json={"lines": [{"return_line_id": line, "accepted": 1}]}).json()["status"]
        == "open"  # 1 of 2 units in
    )
    too_many = client.post(url, json={"lines": [{"return_line_id": line, "accepted": 2}]})
    assert too_many.status_code == 422 and "only 1 units still to QC" in too_many.json()["detail"]
    assert client.post(url, json={"lines": [{"return_line_id": line}]}).status_code == 422
    assert (
        client.post(url, json={"lines": [{"return_line_id": line, "rejected": 1}]}).json()["status"]
        == "qc_done"
    )
    assert state() == figures(onhand=6, damaged=1)
    done = client.post(url, json={"lines": [{"return_line_id": line, "accepted": 1}]})
    assert done.status_code == 409


def test_r12_return_of_an_order_placed_before_the_starting_stock(watches, shopify):
    set_baseline("2026-10-20T00:00:00+05:30")
    import_orders([fulfilled(order(), delivered=True)])
    assert state() == figures()
    shopify_says(shopify, shopify_return(created="2026-10-21T10:00:00Z"))
    assert state() == figures(return_pending=1)
    ret = the_return()
    client.post(
        f"/returns/{ret['return_id']}/qc",
        json={"lines": [{"return_line_id": ret["lines"][0]["return_line_id"], "accepted": 1}]},
    )
    assert state() == figures(onhand=8)  # the unit physically came back


def test_r13_unlinked_line_is_recorded_without_stock(watches, shopify):
    import_orders(
        [fulfilled(order(**lines(line_item(variant_id=999, sku="UNKNOWN"))), delivered=True)]
    )
    shopify_says(shopify, shopify_return())
    assert the_return()["lines"][0]["item_id"] is None
    assert logs(inv_type="RETURN_PENDING") == [] and state() == figures()


def test_r14_outcomes_from_before_the_starting_stock_are_already_in_it(watches, shopify):
    set_baseline("2026-10-20T00:00:00+05:30")
    import_orders([fulfilled(order(), delivered=True)])
    shopify_says(
        shopify,
        shopify_return(
            status="CLOSED", created="2026-10-19T10:00:00Z", dispositions=[("RESTOCKED", 1)]
        ),
    )
    with SessionLocal() as session:
        line = session.scalars(select(ReturnLine)).one()
        assert (line.accepted_qty, line.opening_accepted_qty) == (1, 1)
    assert state() == figures()


def test_r15_two_returns_for_one_line(delivered, shopify):
    shopify_says(shopify, shopify_return(601), shopify_return(602))
    assert state() == figures(onhand=5, return_pending=2)
    assert client.get("/returns").json()["total"] == 2


def test_r16_return_for_an_order_not_imported_yet(delivered, shopify):
    row = webhook(payload={"id": 601, "order": {"id": 9999}})
    assert row.status == "failed" and "isn't imported yet" in row.error_messages[0]


def test_r17_order_sync_reads_returns(watches, shopify):
    shopify.returns["5001"] = [shopify_return()]
    sync_orders(shopify, fulfilled(order(), delivered=True))
    assert state() == figures(onhand=5, return_pending=1)
    assert client.post("/returns/sync").json() == {"orders": 1, "changed_orders": 0, "errors": []}


def test_webhook_replays_change_nothing(delivered, shopify):
    shopify_says(shopify, shopify_return(), delivery="wh-a")
    shopify_says(shopify, shopify_return(), delivery="wh-b")
    assert len(logs(inv_type="RETURN_PENDING")) == 1


def test_eta_can_be_corrected(delivered, shopify):
    shopify_says(shopify, shopify_return())
    ret = the_return()
    when = str(today() + timedelta(days=3))
    assert (
        client.patch(f"/returns/{ret['return_id']}", json={"expected_at": when}).json()[
            "expected_at"
        ]
        == when
    )
    body = client.get(f"/inventory/{ret['lines'][0]['item_id']}").json()
    assert body["next_eta"] == when


def test_list_filters_and_order_returns(delivered, shopify):
    shopify_says(shopify, shopify_return(601), shopify_return(602, status="REQUESTED"))
    assert client.get("/returns", params={"status": "open"}).json()["total"] == 1
    assert client.get("/returns", params={"pending": True}).json()["total"] == 1
    assert (
        client.get("/returns", params={"q": "#1001-R2"}).json()["returns"][0]["status"]
        == "requested"
    )
    assert client.get("/returns", params={"type": "rto"}).json()["total"] == 0
    assert [r["return_number"] for r in client.get(f"/orders/{order_id()}/returns").json()] == [
        "#1001-R1",
        "#1001-R2",
    ]
    detail = client.get(f"/orders/{order_id()}").json()
    assert [r["return_number"] for r in detail["returns"]] == ["#1001-R1", "#1001-R2"]
    assert client.get("/returns/99999").status_code == 404


# --- RTO (T1–T7) --------------------------------------------------------------------------


@pytest.fixture
def shipped(watches):
    """2 × BLK-40 shipped, not delivered."""
    import_orders([fulfilled(order())])
    assert state() == figures(shipped=2)


def rto(quantity=1, line=None):
    line = line or client.get(f"/orders/{order_id()}").json()["lines"][0]["order_line_id"]
    return client.post(
        f"/orders/{order_id()}/rto",
        json={"lines": [{"order_line_id": line, "quantity": quantity}], "note": "Refused"},
    )


def test_t1_mark_rto(shipped, shopify):
    sets = shopify.calls.count("set")
    response = rto(1)
    assert response.status_code == 201
    ret = response.json()
    assert ret["return_type"] == "rto" and ret["return_number"] == "#1001-RTO1"
    assert ret["expected_at"] == str(today() + timedelta(days=10))
    assert state() == figures(onhand=6, shipped=1, return_pending=1)  # available unchanged
    assert shopify.calls.count("set") == sets
    detail = client.get(f"/orders/{order_id()}").json()
    assert detail["history"][-1]["action_type"] == "rto" and detail["lines"][0]["stock"]["rto"] == 1


def test_t2_rto_of_more_than_shipped_and_undelivered(shipped):
    assert rto(3).status_code == 422
    rto(2)
    assert rto(1).status_code == 422
    assert rto(1, line=999999).status_code == 422


def test_t3_rto_of_an_order_before_the_starting_stock(watches):
    set_baseline("2026-10-20T00:00:00+05:30")
    import_orders([fulfilled(order())])
    rto(1)
    assert state() == figures(return_pending=1)


def test_t4_rto_received_and_qcd_without_shopify(shipped, shopify):
    ret = rto(2).json()
    line = the_return()["lines"][0]["return_line_id"]
    client.post(
        f"/returns/{ret['return_id']}/receive",
        json={"lines": [{"return_line_id": line, "received_qty": 2}]},
    )
    client.post(
        f"/returns/{ret['return_id']}/qc",
        json={"lines": [{"return_line_id": line, "accepted": 1, "rejected": 1}]},
    )
    assert state() == figures(onhand=6, damaged=1)
    assert "dispose" not in shopify.calls


def test_t5_cancel_an_rto(shipped):
    ret = rto(1).json()
    assert client.post(f"/returns/{ret['return_id']}/cancel").json()["status"] == "cancelled"
    assert state() == figures(shipped=2)
    assert client.post(f"/returns/{ret['return_id']}/cancel").status_code == 409

    second = rto(1).json()
    line = the_return(second["return_number"])["lines"][0]["return_line_id"]
    client.post(
        f"/returns/{second['return_id']}/qc",
        json={"lines": [{"return_line_id": line, "accepted": 1}]},
    )
    assert client.post(f"/returns/{second['return_id']}/cancel").status_code == 409


def test_t6_close_an_rto_whose_units_never_came_back(shipped):
    ret = rto(1).json()
    assert client.post(f"/returns/{ret['return_id']}/close").json()["status"] == "closed"
    assert state() == figures(onhand=6, shipped=1)  # the unit is lost


def test_t7_delivery_reported_after_rto_deducts_nothing_twice(shipped):
    rto(1)
    import_orders([fulfilled(order(minute=1), delivered=True)])
    assert state() == figures(onhand=5, return_pending=1)


def test_customer_returns_cant_be_cancelled_or_closed_in_the_app(delivered, shopify):
    shopify_says(shopify, shopify_return())
    ret = the_return()
    assert client.post(f"/returns/{ret['return_id']}/cancel").status_code == 409
    assert client.post(f"/returns/{ret['return_id']}/close").status_code == 409
