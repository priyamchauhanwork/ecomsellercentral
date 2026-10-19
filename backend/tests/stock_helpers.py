"""Shared fakes and helpers for the supply & demand tests (docs/inventory-erd.md)."""

import copy

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select, update

from app.db import SessionLocal
from app.main import app
from app.models import ChannelListingItem, InventoryLog, Item, OrderHeader
from app.services import order_ingestion, product_import, stock_sync
from app.services.shopify import ShopifyError
from tests.shopify_orders import line_item, shopify_order
from tests.test_order_ingestion import import_orders, new_run
from tests.test_product_import import run, strap, watch

client = TestClient(app)


class FakeShopify:
    """Shopify's inventory and returns, as the stock sync and returns read them."""

    def __init__(self):
        self.levels: dict[str, int] = {}  # variant id → available
        self.untracked: set[str] = set()
        self.returns: dict[str, list] = {}  # order id → Return nodes
        self.orders: list[dict] = []  # what an order sync through this client reads
        self.fail: str | None = None  # "refuse" / "raise" for inventorySetQuantities
        self.dispose_error: str | None = None
        self.on_set = None
        self.calls: list[str] = []
        self.disposed: list[dict] = []

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return None

    def graphql(self, query, variables=None):
        variables = variables or {}
        if "inventoryItemUpdate" in query:
            self.calls.append("track")
            self.untracked.discard(variables["id"].rsplit("/", 1)[-1])
            return {"inventoryItemUpdate": {"inventoryItem": {"tracked": True}, "userErrors": []}}
        if "{ tracked }" in query:
            variant = variables["id"].rsplit("/", 1)[-1]
            return {"inventoryItem": {"tracked": variant not in self.untracked}}
        if "reverseFulfillmentOrderDispose" in query:
            self.calls.append("dispose")
            if self.dispose_error:
                return {
                    "reverseFulfillmentOrderDispose": {
                        "reverseFulfillmentOrderLineItems": None,
                        "userErrors": [{"field": None, "message": self.dispose_error}],
                    }
                }
            self.disposed.extend(variables["inputs"])
            return {
                "reverseFulfillmentOrderDispose": {
                    "reverseFulfillmentOrderLineItems": [],
                    "userErrors": [],
                }
            }
        if "returns(first" in query:
            self.calls.append("returns")
            return {
                "nodes": [
                    {
                        "id": gid,
                        "returns": {
                            "nodes": copy.deepcopy(self.returns.get(gid.rsplit("/", 1)[-1], []))
                        },
                    }
                    for gid in variables["ids"]
                ]
            }
        if "node(id" in query:
            return {"node": None}
        if "nodes(ids" in query:
            self.calls.append("drift")
            return {"nodes": [self.node(gid) for gid in variables["ids"]]}
        if "inventorySetQuantities" in query:
            self.calls.append("set")
            change = variables["input"]["quantities"][0]
            variant = change["inventoryItemId"].rsplit("/", 1)[-1]
            if self.fail == "raise":
                raise ShopifyError("Couldn't reach Shopify: timed out")
            if self.fail == "refuse":
                return {"inventorySetQuantities": {"userErrors": [{"message": "Access denied"}]}}
            if self.on_set:
                self.on_set()
                self.on_set = None
            # Not stocked at the location: Shopify expects a null compare quantity.
            if change["changeFromQuantity"] != self.levels.get(variant):
                return {"inventorySetQuantities": {"userErrors": [{"message": "stale"}]}}
            self.levels[variant] = change["quantity"]
            return {"inventorySetQuantities": {"userErrors": []}}
        if "inventoryLevel" in query:
            variant = variables["item"].rsplit("/", 1)[-1]
            if variant not in self.levels:  # not stocked at the location
                return {"inventoryItem": {"inventoryLevel": None}}
            quantities = [{"quantity": self.levels[variant]}]
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

    def paginate(self, path, key, params=None):  # the order sync, when used as its client
        yield from copy.deepcopy(self.orders)


@pytest.fixture
def shopify(monkeypatch):
    fake = FakeShopify()
    monkeypatch.setattr(product_import, "shopify_client_factory", lambda: fake)
    monkeypatch.setattr(order_ingestion, "shopify_client_factory", lambda: fake)
    stock_sync.clear_cache()
    yield fake
    stock_sync.clear_cache()


@pytest.fixture
def watches(shopify):
    """The watch (4 variants, 7 each; SLV-40 oversold at −2, SLV-42 untracked) and a
    strap, imported from Shopify, with Shopify showing the same figures; the starting
    stock dated 1 Oct, so the test orders (17 Oct on) count."""
    run([watch(), strap()])
    shopify.levels = {"8001": 7, "8002": 7, "8003": 0, "8004": 7, "8101": 7}
    shopify.untracked = {"8004"}
    set_baseline()
    return items()


def items():
    with SessionLocal() as session:
        return {item.sku or item.item_id: item for item in session.scalars(select(Item))}


def item_id(sku="WATCH-BLK-40"):
    return items()[sku].item_id


def state(sku="WATCH-BLK-40"):
    """Every stock figure of the SKU, from the API."""
    body = client.get(f"/inventory/{item_id(sku)}").json()
    keys = ("onhand", "open", "allocated", "shipped", "return_pending", "damaged", "available")
    return {key: body[key] for key in keys}


def figures(onhand=7, open=0, allocated=0, shipped=0, return_pending=0, damaged=0):
    return {
        "onhand": onhand,
        "open": open,
        "allocated": allocated,
        "shipped": shipped,
        "return_pending": return_pending,
        "damaged": damaged,
        "available": onhand - open - allocated - shipped,
    }


def logs(**filters):
    with SessionLocal() as session:
        query = select(InventoryLog).order_by(InventoryLog.inventory_log_id)
        for name, value in filters.items():
            query = query.where(getattr(InventoryLog, name) == value)
        return list(session.scalars(query))


def set_baseline(when="2026-10-01T00:00:00+05:30"):
    with SessionLocal.begin() as session:
        session.execute(update(Item).values(stock_baseline_at=when))


def order(minute=None, **overrides):
    """2 × WATCH-BLK-40 (variant 8001), placed 17 Oct. `minute` makes it a newer version."""
    payload = shopify_order(**overrides)
    if minute is not None:
        payload["updated_at"] = f"2026-10-18T09:{minute:02d}:00+05:30"
    return payload


def lines(*items_):
    return {"line_items": list(items_)}


def order_id(external_id="5001"):
    with SessionLocal() as session:
        return session.scalars(
            select(OrderHeader.order_header_id).where(OrderHeader.external_order_id == external_id)
        ).one()


def sync_orders(fake, *orders_):
    """An order sync through `fake`: imports the orders, then reads their returns."""
    fake.orders = list(orders_)
    run_id = new_run()
    order_ingestion.run_shopify_order_import(run_id, client_factory=lambda: fake)


def adjust(item, change, reason="restock", **extra):
    return client.post(
        "/inventory/adjust",
        json={"item_id": item, "quantity_change": change, "reason": reason, **extra},
    )


def disable_on_shopify(item):
    with SessionLocal.begin() as session:
        session.execute(
            update(ChannelListingItem)
            .where(ChannelListingItem.item_id == item)
            .values(status="inactive")
        )


__all__ = [
    "FakeShopify",
    "adjust",
    "client",
    "disable_on_shopify",
    "figures",
    "import_orders",
    "item_id",
    "items",
    "line_item",
    "lines",
    "logs",
    "order",
    "order_id",
    "set_baseline",
    "shopify",
    "state",
    "sync_orders",
    "watches",
]
