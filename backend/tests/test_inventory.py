"""Inventory API (#19), stock sync to Shopify (#20) and stock kept in step with orders (#21)."""

import copy
from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError

from app.db import SessionLocal
from app.main import app
from app.models import Article, ChannelListingItem, InventoryLog, Item, OrderHeader
from app.services import inventory, order_ingestion, product_import, stock_sync
from app.services.shopify import ShopifyConfigError, ShopifyError
from tests.shopify_orders import line_item, shopify_order
from tests.test_order_ingestion import import_orders
from tests.test_product_import import run, strap, watch

pytestmark = pytest.mark.usefixtures("clean_db")
client = TestClient(app)


class FakeShopify:
    """Shopify's inventory as the stock sync sees it: one level per variant."""

    def __init__(self):
        self.levels: dict[str, int] = {}  # variant id → available
        self.untracked: set[str] = set()
        self.fail: str | None = None  # "refuse" or "raise" for inventorySetQuantities
        self.on_set = None  # called before a set is applied (simulates a concurrent change)
        self.calls: list[str] = []

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return None

    def graphql(self, query, variables=None):
        variables = variables or {}
        if "nodes(ids" in query:
            self.calls.append("drift")
            return {"nodes": [self.node(gid) for gid in variables["ids"]]}
        if "inventorySetQuantities" in query:
            self.calls.append("set")
            assert "@idempotent(key: $key)" in query and variables["key"]
            change = variables["input"]["quantities"][0]
            variant = change["inventoryItemId"].rsplit("/", 1)[-1]
            if self.fail == "raise":
                raise ShopifyError("Couldn't reach Shopify: timed out")
            if self.fail == "refuse":
                return {"inventorySetQuantities": {"userErrors": [{"message": "Access denied"}]}}
            if self.on_set:
                self.on_set()
                self.on_set = None
            if change["changeFromQuantity"] != self.levels.get(variant, 0):
                return {"inventorySetQuantities": {"userErrors": [{"message": "stale"}]}}
            self.levels[variant] = change["quantity"]
            return {"inventorySetQuantities": {"userErrors": []}}
        if "inventoryLevel" in query:
            variant = variables["item"].rsplit("/", 1)[-1]
            quantities = [{"quantity": self.levels.get(variant, 0)}]
            return {"inventoryItem": {"inventoryLevel": {"quantities": quantities}}}
        if "productVariant" in query:
            self.calls.append("variant")
            variant = variables["id"].rsplit("/", 1)[-1]
            return {
                "productVariant": {
                    "id": variables["id"],
                    "product": {"id": "gid://shopify/Product/7001"},
                    "inventoryItem": {"id": f"gid://shopify/InventoryItem/{variant}"},
                }
            }
        if "location" in query:
            self.calls.append("location")
            return {"location": {"id": "gid://shopify/Location/1"}}
        raise AssertionError(query)

    def node(self, global_id):
        variant = global_id.rsplit("/", 1)[-1]
        if variant not in self.levels:
            return None
        return {
            "id": global_id,
            "inventoryItem": {
                "id": f"gid://shopify/InventoryItem/{variant}",
                "tracked": variant not in self.untracked,
                "inventoryLevel": {"quantities": [{"quantity": self.levels[variant]}]},
            },
        }


@pytest.fixture
def shopify(monkeypatch):
    fake = FakeShopify()
    monkeypatch.setattr(product_import, "shopify_client_factory", lambda: fake)
    stock_sync.clear_cache()
    yield fake
    stock_sync.clear_cache()


@pytest.fixture
def watches(shopify):
    """The watch (4 variants, 7 each; SLV-40 oversold at −2, SLV-42 untracked) and a
    strap, imported from Shopify, with Shopify showing the same figures."""
    run([watch(), strap()])
    shopify.levels = {"8001": 7, "8002": 7, "8003": 0, "8004": 7, "8101": 7}
    shopify.untracked = {"8004"}
    return items()


def items():
    with SessionLocal() as session:
        return {item.sku or item.item_id: item for item in session.scalars(select(Item))}


def stock(sku):
    return items()[sku].stock_qty


def logs(item_id=None):
    with SessionLocal() as session:
        query = select(InventoryLog).order_by(InventoryLog.inventory_log_id)
        if item_id:
            query = query.where(InventoryLog.item_id == item_id)
        return list(session.scalars(query))


def adjust(item_id, change, reason="restock", **extra):
    return client.post(
        "/inventory/adjust",
        json={"item_id": item_id, "quantity_change": change, "reason": reason, **extra},
    )


def set_baseline(when="2026-10-01T00:00:00+05:30"):
    """Pretend the starting stock was taken before the test orders were placed."""
    with SessionLocal.begin() as session:
        session.execute(update(Item).values(stock_baseline_at=when))


def listing_item(item_id):
    with SessionLocal() as session:
        return session.scalars(
            select(ChannelListingItem).where(ChannelListingItem.item_id == item_id)
        ).one()


# --- List and item (#19) ------------------------------------------------------------------


def test_list_with_summary_badges_and_shopify_state(watches):
    body = client.get("/inventory").json()
    assert body["summary"] == {"items": 5, "low_stock": 0, "out_of_stock": 1}
    assert body["total"] == 5
    rows = {row["sku"]: row for row in body["items"]}
    assert rows["WATCH-SLV-40"]["stock_status"] == "out"
    assert rows["WATCH-BLK-40"]["stock_status"] == "ok"
    assert rows["WATCH-BLK-40"]["variant_values"] == {"Colour": "Black", "Size": "40mm"}
    assert rows["WATCH-BLK-40"]["image_url"] == "https://cdn/black.jpg"
    assert rows["WATCH-BLK-40"]["shopify"]["status"] == "live"
    assert [row["article_name"] for row in body["items"]][0] == "Classic Watch"  # by name


@pytest.mark.parametrize(
    ("stock_qty", "badge"), [(10, "ok"), (6, "ok"), (5, "low"), (1, "low"), (0, "out"), (-1, "out")]
)
def test_badge_thresholds(watches, stock_qty, badge):
    with SessionLocal.begin() as session:
        session.execute(update(Item).where(Item.sku == "WATCH-BLK-40").values(stock_qty=stock_qty))
    row = client.get("/inventory", params={"q": "WATCH-BLK-40"}).json()["items"][0]
    assert row["stock_status"] == badge


def test_search_by_sku_name_or_barcode_and_low_filter(watches):
    with SessionLocal.begin() as session:
        session.execute(update(Item).where(Item.sku == "WATCH-BLK-42").values(barcode="8901"))
        session.execute(update(Item).where(Item.sku == "WATCH-BLK-40").values(stock_qty=3))

    def skus(**params):
        return [row["sku"] for row in client.get("/inventory", params=params).json()["items"]]

    assert skus(q="watch-blk-42") == ["WATCH-BLK-42"]
    assert skus(q="8901") == ["WATCH-BLK-42"]
    assert len(skus(q="classic")) == 4
    assert len(skus(q="strap")) == 1  # by product name; the strap has no Shopify SKU
    assert skus(q="100%") == []  # wildcards are literal
    assert sorted(skus(low=True)) == ["WATCH-BLK-40", "WATCH-SLV-40"]
    body = client.get("/inventory", params={"q": "zzz"}).json()
    assert body["total"] == 0 and body["summary"]["items"] == 5  # summary isn't filtered


def test_inactive_items_and_archived_products_are_not_listed(watches):
    with SessionLocal.begin() as session:
        session.execute(update(Item).where(Item.sku == "WATCH-BLK-40").values(status="inactive"))
        session.execute(
            update(Article).where(Article.name == "Leather Strap").values(status="archived")
        )
    skus = [row["sku"] for row in client.get("/inventory").json()["items"]]
    assert "WATCH-BLK-40" not in skus and len(skus) == 3


def test_get_one_item(watches):
    item_id = watches["WATCH-BLK-40"].item_id
    assert client.get(f"/inventory/{item_id}").json()["stock_qty"] == 7
    assert client.get("/inventory/999999").status_code == 404


# --- Adjust and history (#19) -------------------------------------------------------------


def test_adjust_lowers_stock_logs_once_and_syncs_to_shopify(watches, shopify):
    item_id = watches["WATCH-BLK-40"].item_id
    response = adjust(item_id, -5, "damage", note="  dropped  ")
    assert response.status_code == 200, response.json()
    body = response.json()
    assert body["item"]["stock_qty"] == 2 and body["synced"] is True and body["sync_error"] is None
    assert body["entry"]["qty_change"] == -5 and body["entry"]["stock_after"] == 2
    assert body["entry"]["note"] == "dropped"
    assert stock("WATCH-BLK-40") == 2 and len(logs()) == 1
    assert shopify.levels["8001"] == 2  # this variant only
    assert shopify.levels["8002"] == 7


@pytest.mark.parametrize(
    "payload",
    [
        {"quantity_change": 0, "reason": "restock"},
        {"quantity_change": 1.5, "reason": "restock"},
        {"quantity_change": "3", "reason": "restock"},
        {"quantity_change": True, "reason": "restock"},
        {"quantity_change": 3, "reason": "stolen"},
        {"quantity_change": 3, "reason": "order_created"},  # order reasons are not manual
        {"quantity_change": 3},
        {"quantity_change": 3, "reason": "other", "note": "x" * 501},
        {"quantity_change": 2_000_000, "reason": "restock"},
    ],
)
def test_invalid_adjustments_are_422_and_change_nothing(watches, payload):
    item_id = watches["WATCH-BLK-40"].item_id
    assert client.post("/inventory/adjust", json={"item_id": item_id, **payload}).status_code == 422
    assert stock("WATCH-BLK-40") == 7 and logs() == []


def test_unknown_item_is_404(watches):
    assert adjust(999999, 1).status_code == 404 and logs() == []


def test_removal_may_not_go_below_zero_but_adding_to_oversold_stock_works(watches):
    item_id = watches["WATCH-BLK-40"].item_id
    response = adjust(item_id, -8, "damage")
    assert response.status_code == 422
    assert "Only 7 in stock" in response.json()["detail"][0]["msg"]
    assert stock("WATCH-BLK-40") == 7

    oversold = watches["WATCH-SLV-40"].item_id  # at −2
    assert adjust(oversold, 1).json()["item"]["stock_qty"] == -1
    assert adjust(oversold, -1, "correction").status_code == 422


def test_a_failing_log_insert_rolls_back_the_stock_change(watches):
    item_id = watches["WATCH-BLK-40"].item_id
    with pytest.raises(IntegrityError), SessionLocal.begin() as session:
        inventory.adjust_stock(session, 1, item_id, 5, "not-a-reason", None)
    assert stock("WATCH-BLK-40") == 7 and logs() == []


def test_history_is_newest_first_and_names_the_order(watches):
    set_baseline()
    item_id = watches["WATCH-BLK-40"].item_id
    adjust(item_id, 3, "restock", note="supplier")
    import_orders([shopify_order()])  # 2 × variant 8001, after the baseline
    adjust(item_id, -1, "correction")

    history = client.get(f"/inventory/{item_id}/history").json()
    assert [(e["qty_change"], e["reason"]) for e in history] == [
        (-1, "correction"),
        (-2, "order_created"),
        (3, "restock"),
    ]
    assert [e["stock_after"] for e in history] == [7, 8, 10]
    assert history[1]["order_number"] and history[1]["order_header_id"]
    assert history[2]["note"] == "supplier" and history[2]["order_number"] is None
    assert client.get("/inventory/999999/history").status_code == 404


def test_adjusting_an_item_not_on_shopify_needs_no_sync(shopify):
    product = client.post(
        "/products",
        json={
            "article_code": "MUG",
            "name": "Mug",
            "items": [{"sku": "MUG-1", "variant_values": {}, "stock_qty": 4}],
        },
    ).json()
    body = adjust(product["items"][0]["item_id"], 2).json()
    assert body["item"]["stock_qty"] == 6 and body["synced"] is None
    assert body["item"]["shopify"] is None and "set" not in shopify.calls


# --- Sync to Shopify (#20) ----------------------------------------------------------------


def test_failed_sync_keeps_the_change_and_retry_clears_the_error(watches, shopify):
    item_id = watches["WATCH-BLK-40"].item_id
    shopify.fail = "refuse"
    body = adjust(item_id, 4).json()
    assert body["item"]["stock_qty"] == 11
    assert body["synced"] is False and "Access denied" in body["sync_error"]
    assert "Access denied" in body["item"]["shopify"]["last_sync_error"]
    assert shopify.levels["8001"] == 7

    shopify.fail = "raise"
    assert client.post("/inventory/sync-pending").json()["failed"][0]["item_id"] == item_id

    shopify.fail = None
    assert client.post("/inventory/sync-pending").json() == {
        "retried": 1,
        "synced": 1,
        "failed": [],
    }
    assert shopify.levels["8001"] == 11 and listing_item(item_id).last_sync_error is None
    assert client.post("/inventory/sync-pending").json()["retried"] == 0


def test_shopify_not_configured_keeps_the_change(watches, monkeypatch):
    def broken():
        raise ShopifyConfigError("Missing in .env: SHOPIFY_STORE_DOMAIN")

    monkeypatch.setattr(product_import, "shopify_client_factory", broken)
    body = adjust(watches["WATCH-BLK-40"].item_id, 1).json()
    assert body["item"]["stock_qty"] == 8 and body["synced"] is False
    assert "not configured" in body["sync_error"]


def test_a_sku_disabled_on_shopify_stays_at_zero_there(watches, shopify):
    item_id = watches["WATCH-BLK-40"].item_id
    with SessionLocal.begin() as session:
        session.execute(
            update(ChannelListingItem)
            .where(ChannelListingItem.item_id == item_id)
            .values(status="inactive")
        )
    shopify.levels["8001"] = 0
    body = adjust(item_id, 5).json()
    assert body["item"]["stock_qty"] == 12 and body["synced"] is None
    assert shopify.levels["8001"] == 0


def test_every_variant_syncs_to_its_own_inventory_item_and_lookups_are_cached(watches, shopify):
    for sku, change in (("WATCH-BLK-40", 1), ("WATCH-BLK-42", 2), ("WATCH-BLK-40", 1)):
        assert adjust(watches[sku].item_id, change).json()["synced"] is True
    assert shopify.levels["8001"] == 9 and shopify.levels["8002"] == 9
    assert shopify.calls.count("variant") == 2 and shopify.calls.count("location") == 1


def test_stock_changed_while_pushing_is_pushed_again(watches, shopify):
    item_id = watches["WATCH-BLK-40"].item_id

    def order_arrives():
        with SessionLocal.begin() as session:
            session.execute(update(Item).where(Item.item_id == item_id).values(stock_qty=8))

    shopify.on_set = order_arrives
    adjust(item_id, 3)  # pushes 10, but stock is 8 by then
    assert shopify.levels["8001"] == 8


def test_drift_report_and_fixing_one_sku(watches, shopify):
    report = client.get("/inventory/drift").json()
    by_sku = {row["sku"]: row for row in report["items"]}
    assert report["mismatched"] == 0 and report["accuracy_pct"] == 100.0
    assert by_sku["WATCH-SLV-40"]["expected_on_shopify"] == 0  # −2 here shows as 0 there
    assert by_sku["WATCH-SLV-42"]["state"] == "untracked"

    shopify.levels["8002"] = 4  # sold or edited on Shopify without us knowing
    report = client.get("/inventory/drift").json()
    drifted = [row for row in report["items"] if row["state"] == "drift"]
    assert [(r["sku"], r["stock_qty"], r["shopify_qty"]) for r in drifted] == [
        ("WATCH-BLK-42", 7, 4)
    ]
    assert report["mismatched"] == 1 and report["checked"] == 4

    fixed = client.post(f"/inventory/{watches['WATCH-BLK-42'].item_id}/sync").json()
    assert fixed["synced"] is True and shopify.levels["8002"] == 7
    assert client.get("/inventory/drift").json()["mismatched"] == 0


def test_drift_reports_variants_deleted_on_shopify(watches, shopify):
    del shopify.levels["8001"]
    rows = {row["sku"]: row for row in client.get("/inventory/drift").json()["items"]}
    assert (
        rows["WATCH-BLK-40"]["state"] == "missing" and rows["WATCH-BLK-40"]["shopify_qty"] is None
    )


# --- Orders (#21) -------------------------------------------------------------------------


def order(**overrides):
    """2 × WATCH-BLK-40 (variant 8001), placed after the test baseline."""
    return shopify_order(**overrides)


def later(order_payload, minute):
    order_payload["updated_at"] = f"2026-10-18T09:{minute:02d}:00+05:30"
    return order_payload


def test_new_order_deducts_once_however_often_it_arrives(watches, shopify):
    set_baseline()
    import_orders([order()])
    assert stock("WATCH-BLK-40") == 5
    entries = logs()
    assert [(e.qty_change, e.reason) for e in entries] == [(-2, "order_created")]
    assert entries[0].note == "Order #1001" and entries[0].order_line_id

    import_orders([order()])  # same payload again
    import_orders([order(), order()])
    import_orders([later(order(note="changed"), 1)])  # an update that changes no quantity
    assert stock("WATCH-BLK-40") == 5 and len(logs()) == 1
    assert "set" not in shopify.calls  # Shopify already deducted it


def test_orders_placed_before_the_starting_stock_change_nothing(watches):
    import_orders([order()])  # placed 17 Oct, the import took stock today
    cancelled = later(order(cancelled_at="2026-10-18T09:00:00+05:30"), 1)
    import_orders([cancelled])
    assert stock("WATCH-BLK-40") == 7 and logs() == []


def test_cancellation_restocks_once(watches):
    set_baseline()
    import_orders([order()])
    cancelled = later(order(cancelled_at="2026-10-18T09:00:00+05:30", cancel_reason="customer"), 1)
    import_orders([cancelled])
    import_orders([later(copy.deepcopy(cancelled), 2)])
    assert stock("WATCH-BLK-40") == 7
    assert [(e.qty_change, e.reason) for e in logs()] == [
        (-2, "order_created"),
        (2, "order_cancelled"),
    ]


def test_edit_removing_one_then_returning_the_other(watches):
    set_baseline()
    import_orders([order()])
    import_orders([later(order(line_items=[line_item(current_quantity=1)]), 1)])
    assert stock("WATCH-BLK-40") == 6

    returned = later(order(line_items=[line_item(current_quantity=0)]), 2)
    returned["refunds"] = [
        {
            "id": 1,
            "created_at": "2026-10-18T09:02:00+05:30",
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
    import_orders([returned])
    assert stock("WATCH-BLK-40") == 7
    assert [(e.qty_change, e.reason) for e in logs()] == [
        (-2, "order_created"),
        (1, "order_cancelled"),
        (1, "order_returned"),
    ]


def test_quantity_increased_by_an_edit_deducts_the_difference(watches):
    set_baseline()
    import_orders([order()])
    import_orders([later(order(line_items=[line_item(quantity=3, current_quantity=3)]), 1)])
    assert stock("WATCH-BLK-40") == 4
    assert [(e.qty_change, e.reason) for e in logs()][-1] == (-1, "order_edited")


def test_two_orders_for_the_same_variant_at_once_both_deduct(watches):
    set_baseline()
    rows = []
    for order_id in (5001, 5002):
        with SessionLocal.begin() as session:
            _, row_id = order_ingestion.record_webhook_delivery(
                session, "orders/create", f"wh-{order_id}", order(order_id=order_id)
            )
        rows.append(row_id)
    with ThreadPoolExecutor(2) as pool:
        actions = list(pool.map(order_ingestion.process_import_row, rows))
    assert actions == ["created", "created"]
    assert stock("WATCH-BLK-40") == 3 and len(logs()) == 2


def test_lines_not_in_the_catalog_are_skipped(watches):
    set_baseline()
    import_orders([order(line_items=[line_item(variant_id=999, sku="UNKNOWN")])])
    assert logs() == [] and stock("WATCH-BLK-40") == 7


def test_cancelling_in_the_app_restocks_and_pushes_to_shopify(watches, shopify):
    set_baseline()
    import_orders([order()])
    shopify.levels["8001"] = 5  # Shopify deducted the order too
    with SessionLocal() as session:
        order_id = session.scalars(select(OrderHeader.order_header_id)).one()
    response = client.post(f"/orders/{order_id}/status", json={"status": "cancelled"})
    assert response.status_code == 200
    assert response.json()["lines"][0]["stock"] == {
        "item_id": watches["WATCH-BLK-40"].item_id,
        "change": 0,
        "not_counted": None,
    }
    assert stock("WATCH-BLK-40") == 7 and shopify.levels["8001"] == 7


def test_order_page_shows_each_lines_stock_effect(watches):
    set_baseline("2026-10-17T18:00:00+05:30")
    import_orders(
        [
            order(),
            order(
                order_id=5002,
                created_at="2026-10-16T10:00:00+05:30",  # before the starting stock
            ),
            order(order_id=5003, line_items=[line_item(variant_id=999, sku="UNKNOWN")]),
        ]
    )
    with SessionLocal() as session:
        ids = dict(
            session.execute(
                select(OrderHeader.external_order_id, OrderHeader.order_header_id)
            ).all()
        )

    def line_stock(external_id):
        return client.get(f"/orders/{ids[external_id]}").json()["lines"][0]["stock"]

    item_id = watches["WATCH-BLK-40"].item_id
    assert line_stock("5001") == {"item_id": item_id, "change": -2, "not_counted": None}
    assert line_stock("5002") == {
        "item_id": item_id,
        "change": 0,
        "not_counted": "before_starting_stock",
    }
    assert line_stock("5003") == {"item_id": None, "change": 0, "not_counted": "not_linked"}
