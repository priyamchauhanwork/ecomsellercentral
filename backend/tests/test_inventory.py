"""Inventory with the supply & demand model (#19–#21, #49–#52): list and item figures,
manual and damaged changes, the Shopify push and drift, every order scenario (O1–O19 in
docs/inventory-erd.md), the cutover migration and the table constraints."""

from concurrent.futures import ThreadPoolExecutor

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import select, text, update
from sqlalchemy.exc import IntegrityError

from app.config import BACKEND_DIR
from app.db import SessionLocal, engine
from app.models import Article, InventoryDemand, InventorySupply, Item, OrderHeader, OrderLine
from app.services import order_ingestion, product_import
from app.services.order_mapping import map_shopify_order
from app.services.shopify import ShopifyConfigError
from tests.shopify_orders import fulfilled
from tests.stock_helpers import (
    adjust,
    client,
    disable_on_shopify,
    figures,
    import_orders,
    item_id,
    line_item,
    lines,
    logs,
    order,
    order_id,
    set_baseline,
    state,
)

pytestmark = pytest.mark.usefixtures("clean_db")
BEFORE_REDESIGN = "a7c79995f837"  # the migration before the supply & demand cutover


def reasons(**filters):
    return [(entry.inv_type, entry.qty_change, entry.reason) for entry in logs(**filters)]


def damaged(action, quantity):
    return client.post(
        f"/inventory/{item_id()}/damaged", json={"action": action, "quantity": quantity}
    )


# --- List and item ------------------------------------------------------------------------


def test_list_shows_every_figure_and_a_summary(watches):
    import_orders([order()])  # 2 × BLK-40 open
    body = client.get("/inventory").json()
    assert body["summary"] == {
        "items": 5,
        "low_stock": 1,  # BLK-40: 5 available
        "out_of_stock": 1,  # SLV-40: −2
        "returning": 0,
        "damaged": 0,
    }
    row = next(row for row in body["items"] if row["sku"] == "WATCH-BLK-40")
    assert row["onhand"] == 7 and row["open"] == 2 and row["committed"] == 2
    assert row["available"] == row["stock_qty"] == 5 and row["stock_status"] == "low"
    assert row["future_available"] == 5 and row["on_shelf"] == 7 and row["next_eta"] is None
    assert row["shopify"]["status"] == "live" and row["image_url"] == "https://cdn/black.jpg"


@pytest.mark.parametrize(
    ("onhand", "badge"), [(10, "ok"), (6, "ok"), (5, "low"), (1, "low"), (0, "out"), (-1, "out")]
)
def test_badge_follows_available(watches, onhand, badge):
    with SessionLocal.begin() as session:
        session.execute(
            update(InventorySupply)
            .where(InventorySupply.item_id == item_id(), InventorySupply.supply_type == "ONHAND")
            .values(quantity=onhand)
        )
    row = client.get("/inventory", params={"q": "WATCH-BLK-40"}).json()["items"][0]
    assert row["stock_status"] == badge


def test_search_and_low_filter(watches):
    with SessionLocal.begin() as session:
        session.execute(update(Item).where(Item.sku == "WATCH-BLK-42").values(barcode="8901"))

    def skus(**params):
        return [row["sku"] for row in client.get("/inventory", params=params).json()["items"]]

    assert skus(q="watch-blk-42") == ["WATCH-BLK-42"]
    assert skus(q="8901") == ["WATCH-BLK-42"]
    assert len(skus(q="classic")) == 4 and len(skus(q="strap")) == 1
    assert skus(q="100%") == []
    assert skus(low=True) == ["WATCH-SLV-40"]
    body = client.get("/inventory", params={"q": "zzz"}).json()
    assert body["total"] == 0 and body["summary"]["items"] == 5


def test_inactive_items_and_archived_products_are_not_listed(watches):
    with SessionLocal.begin() as session:
        session.execute(update(Item).where(Item.sku == "WATCH-BLK-40").values(status="inactive"))
        session.execute(
            update(Article).where(Article.name == "Leather Strap").values(status="archived")
        )
    assert len(client.get("/inventory").json()["items"]) == 3
    assert client.get("/inventory/999999").status_code == 404


# --- Manual and damaged (M1–M6) -----------------------------------------------------------


def test_m1_adjust_on_hand_logs_and_pushes(watches, shopify):
    response = adjust(item_id(), -5, "correction", note="  count  ")
    assert response.status_code == 200, response.json()
    body = response.json()
    assert body["item"]["onhand"] == 2 and body["synced"] is True and body["warning"] is None
    assert [(e["inv_type"], e["qty_change"], e["note"]) for e in body["entries"]] == [
        ("ONHAND", -5, "count")
    ]
    assert shopify.levels["8001"] == 2 and shopify.levels["8002"] == 7


def test_m1_removal_may_not_exceed_the_shelf(watches):
    import_orders([fulfilled(order())])  # 2 shipped: on hand 7, on the shelf 5
    response = adjust(item_id(), -6, "correction")
    assert response.status_code == 422
    assert response.json()["detail"][0]["msg"] == "Only 5 on the shelf, so 6 can't be removed."
    assert adjust(item_id(), -5, "correction").status_code == 200


@pytest.mark.parametrize(
    "payload",
    [
        {"quantity_change": 0, "reason": "restock"},
        {"quantity_change": 1.5, "reason": "restock"},
        {"quantity_change": "3", "reason": "restock"},
        {"quantity_change": True, "reason": "restock"},
        {"quantity_change": 3, "reason": "stolen"},
        {"quantity_change": 3, "reason": "order_created"},
        {"quantity_change": 3},
        {"quantity_change": 3, "reason": "other", "note": "x" * 501},
        {"quantity_change": 2_000_000, "reason": "restock"},
    ],
)
def test_invalid_adjustments_change_nothing(watches, payload):
    response = client.post("/inventory/adjust", json={"item_id": item_id(), **payload})
    assert response.status_code == 422
    assert state() == figures() and logs(reason="restock") == []


def test_unknown_item_is_404(watches):
    assert adjust(999999, 1).status_code == 404


def test_m2_m3_m4_damaged_units(watches, shopify):
    body = adjust(item_id(), -3, "damage").json()  # found damaged on the shelf
    assert state() == figures(onhand=4, damaged=3) and shopify.levels["8001"] == 4
    assert [e["inv_type"] for e in body["entries"]] == ["DAMAGED", "ONHAND"]  # newest first
    assert adjust(item_id(), 2, "damage").status_code == 422  # use repair instead

    assert damaged("repair", 1).status_code == 200
    assert state() == figures(onhand=5, damaged=2) and shopify.levels["8001"] == 5
    sets = shopify.calls.count("set")
    assert damaged("write_off", 2).status_code == 200
    assert state() == figures(onhand=5, damaged=0)
    assert shopify.calls.count("set") == sets  # available unchanged: nothing pushed
    too_many = damaged("repair", 1)
    assert too_many.status_code == 422 and "Only 0 damaged" in too_many.json()["detail"][0]["msg"]
    assert damaged("burn", 1).status_code == 422 and damaged("repair", 0).status_code == 422


def test_m5_removal_that_oversells_is_allowed_with_a_warning(watches, shopify):
    import_orders([order(**lines(line_item(quantity=6, current_quantity=6)))])
    body = adjust(item_id(), -3, "correction").json()
    assert body["item"]["available"] == -2
    assert body["warning"] == "Now oversold: 6 committed to orders but 4 on hand."
    assert shopify.levels["8001"] == 0  # never below 0 on Shopify


def test_m6_opening_stock_of_new_items(shopify):
    created = client.post(
        "/products",
        json={
            "article_code": "MUG",
            "name": "Mug",
            "items": [{"sku": "MUG-1", "variant_values": {}, "stock_qty": 4}],
        },
    ).json()
    new_item = created["items"][0]["item_id"]
    body = client.get(f"/inventory/{new_item}").json()
    assert (body["onhand"], body["damaged"], body["available"]) == (4, 0, 4)
    assert reasons(item_id=new_item) == [("ONHAND", 4, "opening_stock")]
    adjusted = adjust(new_item, 2).json()
    assert adjusted["item"]["available"] == 6 and adjusted["synced"] is None  # not on Shopify


def test_imported_items_open_with_shopifys_figure(watches):
    assert state("WATCH-SLV-40") == figures(onhand=-2)  # oversold on Shopify already
    assert ("ONHAND", 7, "opening_stock") in reasons(item_id=item_id())


def test_item_without_supply_rows_gets_them_from_its_cached_stock(watches):
    with SessionLocal.begin() as session:
        session.execute(
            text("DELETE FROM ecs_inventory_supply WHERE item_id = :i"), {"i": item_id()}
        )
    adjust(item_id(), 1)
    assert state() == figures(onhand=8)


def test_history_is_newest_first_with_types_and_orders(watches):
    adjust(item_id(), 3, "restock", note="supplier")
    import_orders([order()])
    history = client.get(f"/inventory/{item_id()}/history").json()
    assert [(e["side"], e["inv_type"], e["qty_change"], e["reason"]) for e in history[:2]] == [
        ("DEMAND", "OPEN", 2, "order_created"),
        ("SUPPLY", "ONHAND", 3, "restock"),
    ]
    assert history[0]["order_number"] == "#1001" and history[1]["note"] == "supplier"
    assert client.get("/inventory/999999/history").status_code == 404


# --- Shopify push (S1–S3, S5) -------------------------------------------------------------


def test_failed_push_keeps_the_change_and_retry_clears_it(watches, shopify):
    shopify.fail = "refuse"
    body = adjust(item_id(), 4).json()
    assert body["item"]["available"] == 11 and body["synced"] is False
    assert "Access denied" in body["item"]["shopify"]["last_sync_error"]
    shopify.fail = None
    assert client.post("/inventory/sync-pending").json() == {
        "retried": 1,
        "synced": 1,
        "failed": [],
    }
    assert shopify.levels["8001"] == 11


def test_shopify_not_configured_keeps_the_change(watches, monkeypatch):
    def broken():
        raise ShopifyConfigError("Missing in .env: SHOPIFY_STORE_DOMAIN")

    monkeypatch.setattr(product_import, "shopify_client_factory", broken)
    body = adjust(item_id(), 1).json()
    assert body["item"]["available"] == 8 and body["synced"] is False


def test_push_stocks_an_unstocked_location_and_turns_tracking_on(watches, shopify):
    del shopify.levels["8002"]  # e.g. stocked only at another (Collective) location
    shopify.untracked.add("8002")
    body = adjust(item_id("WATCH-BLK-42"), 3).json()
    assert body["synced"] is True and body["sync_error"] is None
    assert shopify.levels["8002"] == 10 and "8002" not in shopify.untracked
    assert shopify.calls.count("track") == 1
    adjust(item_id("WATCH-BLK-42"), 1)
    assert shopify.levels["8002"] == 11 and shopify.calls.count("track") == 1  # known now


def test_unexpected_push_errors_are_recorded(watches, shopify, monkeypatch):
    from app.services import stock_sync

    def broken(*args):
        raise KeyError("inventoryItem")

    monkeypatch.setattr(stock_sync, "set_available", broken)
    response = adjust(item_id(), 1)
    assert response.status_code == 200
    body = response.json()
    assert body["synced"] is False and "Unexpected error: KeyError" in body["sync_error"]
    assert body["item"]["available"] == 8


def test_disabled_sku_stays_at_zero(watches, shopify):
    disable_on_shopify(item_id())
    shopify.levels["8001"] = 0
    assert adjust(item_id(), 5).json()["synced"] is None and shopify.levels["8001"] == 0


def test_stock_changed_while_pushing_is_pushed_again(watches, shopify):
    shopify.on_set = lambda: import_orders([order()])  # 2 committed while pushing
    adjust(item_id(), 3)  # pushes 10, but available is 8 by then
    assert shopify.levels["8001"] == 8


def test_drift_compares_shopify_with_available(watches, shopify):
    import_orders([order()])
    shopify.levels["8001"] = 5  # Shopify deducted the order too
    report = client.get("/inventory/drift").json()
    assert report["mismatched"] == 0 and report["checked"] == 4
    shopify.levels["8001"] = 7
    drifted = [r for r in client.get("/inventory/drift").json()["items"] if r["state"] == "drift"]
    assert [(r["sku"], r["expected_on_shopify"], r["shopify_qty"]) for r in drifted] == [
        ("WATCH-BLK-40", 5, 7)
    ]
    assert client.post(f"/inventory/{item_id()}/sync").json()["synced"] is True
    assert shopify.levels["8001"] == 5


# --- Orders (O1–O19) ----------------------------------------------------------------------


def test_o1_o2_new_order_is_open_demand_once(watches, shopify):
    import_orders([order()])
    assert state() == figures(open=2)
    assert reasons(side="DEMAND") == [("OPEN", 2, "order_created")]
    for _ in range(3):
        import_orders([order()])
    import_orders([order(minute=1, note="changed")])
    assert state() == figures(open=2) and len(logs(side="DEMAND")) == 1
    assert "set" not in shopify.calls  # Shopify deducted it itself


def test_o3_o4_edits_up_and_down(watches):
    import_orders([order()])
    import_orders([order(minute=1, **lines(line_item(quantity=3, current_quantity=3)))])
    assert state() == figures(open=3)
    import_orders([order(minute=2, **lines(line_item(quantity=3, current_quantity=1)))])
    assert state() == figures(open=1)
    assert reasons(side="DEMAND") == [
        ("OPEN", 2, "order_created"),
        ("OPEN", 1, "order_edited"),
        ("OPEN", -2, "order_cancelled"),
    ]


def test_o5_acknowledge_moves_demand_and_survives_shopify_updates(watches):
    import_orders([order()])
    response = client.post(f"/orders/{order_id()}/status", json={"status": "processing"})
    assert response.status_code == 200 and response.json()["order_status"] == "processing"
    assert response.json()["history"][-1]["action_type"] == "acknowledged"
    assert state() == figures(allocated=2)
    assert reasons(side="DEMAND")[-2:] == [
        ("OPEN", -2, "order_allocated"),
        ("ALLOCATED", 2, "order_allocated"),
    ]
    import_orders([order(minute=1, note="newer")])  # Shopify knows no "processing"
    with SessionLocal() as session:
        header = session.get(OrderHeader, order_id())
        assert header.order_status == "processing" and header.lines[0].line_status == "allocated"
    assert state() == figures(allocated=2)


def test_o6_o7_o8_ship_in_parts_then_deliver_in_parts(watches):
    import_orders([order()])
    client.post(f"/orders/{order_id()}/status", json={"status": "processing"})
    import_orders([fulfilled(order(minute=1), qty=1)])
    assert state() == figures(allocated=1, shipped=1)
    import_orders([fulfilled(order(minute=2))])
    assert state() == figures(shipped=2)

    partly = order(minute=3)
    partly["fulfillments"] = [
        {
            "id": 1,
            "status": "success",
            "shipment_status": "delivered",
            "line_items": [{"id": 9001, "quantity": 1}],
        },
        {
            "id": 2,
            "status": "success",
            "shipment_status": "in_transit",
            "line_items": [{"id": 9001, "quantity": 1}],
        },
    ]
    import_orders([partly])
    assert state() == figures(onhand=6, shipped=1)
    import_orders([fulfilled(order(minute=4), delivered=True)])
    assert state() == figures(onhand=5)
    assert ("ONHAND", -1, "order_delivered") in reasons(side="SUPPLY")


def test_o8_mark_delivered_in_the_app(watches, shopify):
    import_orders([fulfilled(order())])
    sets = shopify.calls.count("set")
    client.post(f"/orders/{order_id()}/status", json={"status": "delivered"})
    assert state() == figures(onhand=5)
    assert shopify.calls.count("set") == sets  # available unchanged
    import_orders([fulfilled(order(minute=1))])  # Shopify still says in transit
    assert state() == figures(onhand=5)


def test_o9_cancel_before_shipping(watches, shopify):
    import_orders([order()])
    import_orders([order(minute=1, cancelled_at="2026-10-18T09:01:00+05:30")])
    assert state() == figures() and "set" not in shopify.calls  # Shopify restocked itself

    import_orders([order(order_id=5002)])
    shopify.levels["8001"] = 5
    client.post(f"/orders/{order_id('5002')}/status", json={"status": "cancelled"})
    assert state() == figures() and shopify.levels["8001"] == 7  # cancelled in the app


def test_o10_cancel_after_partial_shipping(watches):
    import_orders([fulfilled(order(), qty=1)])
    import_orders([fulfilled(order(minute=1, cancelled_at="2026-10-18T09:01:00+05:30"), qty=1)])
    assert state() == figures(shipped=1)


def test_o11_shopify_cancels_an_acknowledged_order(watches):
    import_orders([order()])
    client.post(f"/orders/{order_id()}/status", json={"status": "processing"})
    import_orders([order(minute=1, cancelled_at="2026-10-18T09:01:00+05:30")])
    assert state() == figures()
    detail = client.get(f"/orders/{order_id()}").json()
    assert detail["attention_reason"] == "cancelled_after_acknowledgement"
    flagged = client.get("/orders", params={"attention": True}).json()
    assert [o["order_number"] for o in flagged["orders"]] == ["#1001"]

    cleared = client.post(f"/orders/{order_id()}/attention/clear").json()
    assert cleared["attention_reason"] is None
    assert cleared["history"][-1]["action_type"] == "attention_cleared"
    assert client.get("/orders", params={"attention": True}).json()["total"] == 0


def test_o12_shopify_edit_cuts_acknowledged_units(watches):
    import_orders([order()])
    client.post(f"/orders/{order_id()}/status", json={"status": "processing"})
    import_orders([order(minute=1, **lines(line_item(current_quantity=1)))])
    assert state() == figures(allocated=1)
    detail = client.get(f"/orders/{order_id()}").json()
    assert detail["attention_reason"] == "edited_after_acknowledgement"


def test_o14_orders_before_the_starting_stock_never_count(watches):
    set_baseline("2026-10-20T00:00:00+05:30")
    import_orders([order()])
    import_orders([fulfilled(order(minute=1), delivered=True)])
    assert state() == figures()
    assert logs(side="DEMAND") == [] and logs(reason="order_delivered") == []
    line = client.get(f"/orders/{order_id()}").json()["lines"][0]["stock"]
    assert line["not_counted"] == "before_starting_stock"


def test_o15_unlinked_lines_are_skipped(watches):
    import_orders([order(**lines(line_item(variant_id=999, sku="UNKNOWN")))])
    assert state() == figures() and logs(side="DEMAND") == []
    line = client.get(f"/orders/{order_id()}").json()["lines"][0]
    assert line["stock"]["not_counted"] == "not_linked"


def test_o16_refunds_no_longer_move_stock(watches):
    import_orders([fulfilled(order(), delivered=True)])
    refunded = fulfilled(order(minute=1), delivered=True)
    refunded["refunds"] = [
        {
            "id": 1,
            "created_at": "2026-10-18T09:01:00+05:30",
            "refund_line_items": [
                {
                    "line_item_id": 9001,
                    "quantity": 1,
                    "restock_type": "return",
                    "subtotal": "1499.50",
                }
            ],
        }
    ]
    import_orders([refunded])
    assert state() == figures(onhand=5)


def test_o17_first_seen_already_delivered(watches):
    import_orders([fulfilled(order(), delivered=True)])
    assert state() == figures(onhand=5)
    assert logs(side="DEMAND") == []
    assert reasons(reason="order_delivered") == [("ONHAND", -2, "order_delivered")]


def test_o18_delivered_never_exceeds_shipped():
    payload = fulfilled(order(), qty=1, delivered=True)
    assert map_shopify_order(payload).lines[0]["delivered_qty"] == 1
    payload["fulfillments"][0]["line_items"][0]["quantity"] = 5
    mapped = map_shopify_order(payload).lines[0]
    assert mapped["delivered_qty"] <= mapped["shipped_qty"]


def test_o19_concurrent_orders_both_apply(watches):
    rows = []
    for external in (5001, 5002):
        with SessionLocal.begin() as session:
            _, row_id = order_ingestion.record_webhook_delivery(
                session, "orders/create", f"wh-{external}", order(order_id=external)
            )
        rows.append(row_id)
    with ThreadPoolExecutor(2) as pool:
        assert list(pool.map(order_ingestion.process_import_row, rows)) == ["created", "created"]
    assert state() == figures(open=4)


def test_order_page_shows_each_lines_stock(watches):
    import_orders([fulfilled(order(), qty=1)])
    line = client.get(f"/orders/{order_id()}").json()["lines"][0]
    assert line["stock"] == {
        "item_id": item_id(),
        "open": 1,
        "allocated": 0,
        "shipped": 1,
        "delivered": 0,
        "rto": 0,
        "not_counted": None,
    }
    assert line["delivered_qty"] == 0


# --- Cutover migration (#50) --------------------------------------------------------------


def demand_rows():
    with SessionLocal() as session:
        return sorted(
            (d.order_line_id, d.demand_type, d.quantity)
            for d in session.scalars(select(InventoryDemand))
        )


def test_cutover_rebuilds_supply_and_demand_from_stock_qty(watches):
    import_orders(
        [
            order(),
            fulfilled(order(order_id=5002), qty=1),
            fulfilled(order(order_id=5003), delivered=True),
        ]
    )
    before, demand_before = state(), demand_rows()
    config = Config(str(BACKEND_DIR / "alembic.ini"))
    command.downgrade(config, BEFORE_REDESIGN)
    command.upgrade(config, "head")
    assert state() == before and demand_rows() == demand_before
    # Re-processing the delivered order deducts nothing again.
    import_orders([fulfilled(order(order_id=5003, minute=5), delivered=True)])
    assert state() == before


def test_cutover_refuses_refund_based_restocks(watches):
    config = Config(str(BACKEND_DIR / "alembic.ini"))
    command.downgrade(config, BEFORE_REDESIGN)
    try:
        with engine.begin() as conn:
            conn.execute(
                text(
                    "INSERT INTO ecs_inventory_log (item_id, qty_change, stock_after, reason)"
                    " VALUES (:i, 1, 8, 'order_returned')"
                ),
                {"i": item_id()},
            )
        with pytest.raises(RuntimeError, match="order_returned"):
            command.upgrade(config, "head")
    finally:
        with engine.begin() as conn:
            conn.execute(text("DELETE FROM ecs_inventory_log WHERE reason = 'order_returned'"))
        command.upgrade(config, "head")


# --- Constraints (#50) --------------------------------------------------------------------


def test_constraints(watches):
    import_orders([order()])
    with SessionLocal() as session:
        line = session.scalars(select(OrderLine)).first()
        bad = [
            InventorySupply(seller_id=1, item_id=item_id(), supply_type="ONHAND", quantity=1),
            InventorySupply(
                seller_id=1, item_id=item_id(), supply_type="RETURN_PENDING", quantity=1
            ),
            InventorySupply(seller_id=1, item_id=item_id(), supply_type="DAMAGED", quantity=-1),
            InventoryDemand(
                seller_id=1,
                item_id=item_id(),
                order_header_id=line.order_header_id,
                order_line_id=line.order_line_id,
                demand_type="SHIPPED",
                quantity=0,
            ),
        ]
        for row in bad:
            with pytest.raises(IntegrityError), session.begin_nested():
                session.add(row)
                session.flush()
        with pytest.raises(IntegrityError), session.begin_nested():
            session.execute(update(OrderLine).values(delivered_qty=3))
