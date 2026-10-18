"""Products list (#43), detail (#44), create (#45), publish (#46), enable / disable (#47)."""

import copy

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from app.db import SessionLocal
from app.main import app
from app.models import Article, ChannelListing, ChannelListingItem, Item
from app.services import product_import
from app.services.shopify import ShopifyError
from tests.test_product_import import run, strap, variant, watch

pytestmark = pytest.mark.usefixtures("clean_db")
client = TestClient(app)


class FakeShopify:
    """Shopify's GraphQL Admin API, as far as the catalog writes need it."""

    def __init__(self, fail: dict[str, str] | None = None):
        self.calls: list[tuple[str, dict]] = []
        self.fail = fail or {}  # operation name → error message
        self.next_product = 9_000_000
        self.available = 7  # the variant's available quantity at the location
        self.bump_before_set = 0  # simulate someone changing stock between read and write

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return None

    def graphql(self, query, variables=None):
        name = next(
            op
            for op in (
                "productSet",
                "productUpdate",
                "publishablePublish",
                "productVariantsBulkUpdate",
                "inventoryActivate",
                "inventorySetQuantities",
                "inventoryLevel",
                "productVariant",
                "publications",
                "location",
            )
            if op in query
        )
        self.calls.append((name, copy.deepcopy(variables or {})))
        if name in ("inventoryActivate", "inventorySetQuantities"):  # required since 2026-04
            assert "@idempotent(key: $key)" in query and (variables or {}).get("key"), name
        if name == "inventoryLevel":
            return {
                "inventoryItem": {"inventoryLevel": {"quantities": [{"quantity": self.available}]}}
            }
        if name == "inventorySetQuantities" and name not in self.fail:
            change = variables["input"]["quantities"][0]
            assert "changeFromQuantity" in change, "required since 2026-04"
            if self.bump_before_set:
                self.available += self.bump_before_set
                self.bump_before_set = 0
            if change["changeFromQuantity"] != self.available:
                return {
                    name: {"userErrors": [{"field": None, "message": "compare quantity is stale"}]}
                }
            self.available = change["quantity"]
        if name in self.fail:
            if self.fail[name] == "raise":
                raise ShopifyError("Shopify GraphQL error: network down")
            return {name: {"userErrors": [{"field": None, "message": self.fail[name]}]}}
        if name == "location":
            return {"location": {"id": "gid://shopify/Location/1"}}
        if name == "publications":
            return {
                "publications": {
                    "nodes": [{"id": "gid://shopify/Publication/7", "name": "Online Store"}]
                }
            }
        if name == "productVariant":
            return {
                "productVariant": {
                    "id": f"gid://shopify/ProductVariant/{variables['id'].rsplit('/', 1)[-1]}",
                    "product": {"id": "gid://shopify/Product/7001"},
                    "inventoryItem": {"id": "gid://shopify/InventoryItem/55"},
                }
            }
        if name == "productSet":
            data = variables["input"]
            self.next_product += 1
            nodes = [
                {
                    "id": f"gid://shopify/ProductVariant/{self.next_product}{i}",
                    "sku": v["sku"],
                    "selectedOptions": [
                        {"name": o["optionName"], "value": o["name"]} for o in v["optionValues"]
                    ],
                }
                for i, v in enumerate(data["variants"])
            ]
            return {
                "productSet": {
                    "product": {
                        "id": f"gid://shopify/Product/{self.next_product}",
                        "handle": data["handle"],
                        "variants": {"nodes": nodes},
                    },
                    "userErrors": [],
                }
            }
        return {name: {"userErrors": []}}

    def named(self, name):
        return [variables for op, variables in self.calls if op == name]


@pytest.fixture
def shopify(monkeypatch):
    fake = FakeShopify()
    monkeypatch.setattr(product_import, "shopify_client_factory", lambda: fake)
    return fake


def tshirt(**overrides):
    product = {
        "article_code": "TSHIRT-BASIC",
        "name": "Basic T-shirt",
        "brand": "Asha Apparel",
        "category": "T-shirts",
        "variation_axes": ["Colour", "Size"],
        "attributes": {"Fabric": "Cotton"},
        "images": [
            {"url": "https://cdn.example.com/tee.jpg"},
            {"url": "https://cdn.example.com/tee-black.jpg", "filter": {"Colour": "Black"}},
        ],
        "items": [
            {
                "sku": f"TS-{c[0]}-{s}",
                "variant_values": {"Colour": c, "Size": s},
                "mrp": "599.00",
                "stock_qty": 10,
                "barcode": "890123" if c == "Black" and s == "M" else None,
                "weight": "180",
            }
            for c in ("Black", "White")
            for s in ("M", "L")
        ],
    }
    product.update(overrides)
    return product


def create(**overrides):
    response = client.post("/products", json=tshirt(**overrides))
    assert response.status_code == 201, response.json()
    return response.json()


def count(model):
    with SessionLocal() as session:
        return session.scalar(select(func.count()).select_from(model))


# --- List (#43) ---------------------------------------------------------------------------


def names(response):
    return [p["name"] for p in response.json()["products"]]


def test_list_shows_products_with_stock_price_and_channels():
    run([watch(), strap()])
    create()
    body = client.get("/products?sort=name").json()
    assert body["total"] == 3
    by_name = {p["name"]: p for p in body["products"]}
    watch_row = by_name["Classic Watch"]
    assert (watch_row["item_count"], watch_row["total_stock"]) == (4, 7 + 7 - 2 + 7)
    assert (watch_row["price_min"], watch_row["price_max"]) == ("1499.00", "1499.00")
    assert (
        watch_row["channels"][0]["status"] == "live"
        and watch_row["channels"][0]["channel_code"] == "SHOPIFY"
    )
    assert watch_row["image_url"] == "https://cdn/w.jpg"
    tee = by_name["Basic T-shirt"]
    assert tee["channels"] == [] and tee["status"] == "draft"
    assert (tee["price_min"], tee["price_max"]) == ("599.00", "599.00")  # MRP: not listed yet


def test_list_filters_search_sort_and_paging():
    run(
        [
            watch(),
            strap(),
            watch(
                id=7003,
                handle="old",
                title="Old Watch",
                status="archived",
                published_at=None,
                variants=[variant(8301, "OLD-1", inventory_quantity=2)],
                options=[{"name": "Title", "values": ["Default Title"]}],
            ),
        ]
    )
    create()
    assert names(client.get("/products?status=draft")) == ["Basic T-shirt"]
    assert sorted(names(client.get("/products?channel_status=live"))) == [
        "Classic Watch",
        "Leather Strap",
    ]
    assert names(client.get("/products?channel_status=inactive")) == ["Old Watch"]
    assert names(client.get("/products?channel_status=unlisted")) == ["Basic T-shirt"]
    assert names(client.get("/products?q=ts-w-l")) == ["Basic T-shirt"]  # SKU, any case
    assert names(client.get("/products?q=890123")) == ["Basic T-shirt"]  # barcode
    assert names(client.get("/products?q=velocis")) == ["Classic Watch", "Old Watch"] or sorted(
        names(client.get("/products?q=velocis"))
    ) == ["Classic Watch", "Old Watch"]
    assert client.get("/products?q=%25").json()["total"] == 0  # "%" is literal
    assert sorted(names(client.get("/products?low_stock=true"))) == ["Classic Watch", "Old Watch"]
    assert names(client.get("/products?sort=stock&limit=1")) == ["Old Watch"]
    page = client.get("/products?sort=name&limit=2&offset=2").json()
    assert page["total"] == 4 and [p["name"] for p in page["products"]] == [
        "Leather Strap",
        "Old Watch",
    ]


@pytest.mark.parametrize(
    "query",
    [
        "status=deleted",
        "channel_status=x",
        "sort=price",
        "limit=0",
        "limit=201",
        "offset=-1",
        "q=" + "x" * 101,
    ],
)
def test_list_rejects_bad_parameters(query):
    assert client.get(f"/products?{query}").status_code == 422


# --- Detail (#44) -------------------------------------------------------------------------


def test_detail_has_items_images_and_listings():
    run([watch()])
    article_id = client.get("/products").json()["products"][0]["article_id"]
    body = client.get(f"/products/{article_id}").json()
    assert body["variation_axes"] == ["Colour", "Size"] and body["description"] == "<p>Steel</p>"
    items = {i["sku"]: i for i in body["items"]}
    assert items["WATCH-BLK-40"]["image_url"] == "https://cdn/black.jpg"
    assert items["WATCH-SLV-40"]["image_url"] == "https://cdn/w.jpg"
    assert (items["WATCH-SLV-40"]["stock_qty"], items["WATCH-SLV-40"]["stock_status"]) == (
        -2,
        "out",
    )
    assert items["WATCH-BLK-40"]["stock_status"] == "ok"
    [listing] = body["listings"]
    assert (listing["channel_parent_id"], listing["status"], listing["channel_name"]) == (
        "7001",
        "live",
        "Shopify",
    )
    assert {li["channel_item_id"] for li in listing["items"]} == {"8001", "8002", "8003", "8004"}


def test_detail_unknown_other_seller_or_invalid_id():
    assert client.get("/products/999999").status_code == 404
    assert client.get("/products/abc").status_code == 422
    with SessionLocal.begin() as session:
        from app.models import Seller

        other = Seller(name="Other")
        session.add(other)
        session.flush()
        article = Article(seller_id=other.seller_id, article_code="X", name="Not yours")
        session.add(article)
        session.flush()
        other_id = article.article_id
    assert client.get(f"/products/{other_id}").status_code == 404


# --- Create (#45) -------------------------------------------------------------------------


def test_create_builds_a_draft_with_every_variant():
    body = create()
    assert body["status"] == "draft" and body["listings"] == []
    assert len(body["items"]) == 4
    black_m = next(i for i in body["items"] if i["sku"] == "TS-B-M")
    assert black_m["variant_values"] == {"Colour": "Black", "Size": "M"}
    assert (
        black_m["barcode"] == "890123"
        and black_m["mrp"] == "599.00"
        and black_m["weight"] == "180.000"
    )
    assert black_m["stock_baseline_at"] is not None
    assert black_m["image_url"] == "https://cdn.example.com/tee-black.jpg"


def test_create_a_product_without_options():
    body = create(
        article_code="MUG",
        name="Mug",
        variation_axes=[],
        images=[],
        items=[{"sku": "MUG-1", "stock_qty": 3}],
    )
    assert body["variation_axes"] == [] and body["items"][0]["variant_values"] == {}


def errors(response):
    """Field → every message for it (a field can have several)."""
    assert response.status_code == 422, response.json()
    found: dict[str, str] = {}
    for e in response.json()["detail"]:
        field = ".".join(str(p) for p in e["loc"][1:])
        found[field] = f"{found[field]} | {e['msg']}" if field in found else e["msg"]
    return found


def test_create_reports_every_problem_with_its_field_and_saves_nothing():
    create()
    bad = tshirt(article_code="tshirt-basic")  # same code, other case
    bad["items"][1]["sku"] = bad["items"][0]["sku"]  # repeated within the form
    bad["items"][2]["variant_values"] = {"Colour": "White"}  # missing Size
    bad["items"][3]["variant_values"] = {"Colour": "Black", "Size": "M"}  # repeats variant 1
    found = errors(client.post("/products", json=bad))
    assert "already uses this code" in found["article_code"]
    assert "Same SKU as variant 1" in found["items.1.sku"]
    assert "already uses this SKU" in found["items.0.sku"]
    assert "exactly these options" in found["items.2.variant_values"]
    assert "Same options as variant 1" in found["items.3.variant_values"]
    assert count(Article) == 1 and count(Item) == 4


@pytest.mark.parametrize(
    ("overrides", "field"),
    [
        ({"variation_axes": ["Colour", "colour"]}, "variation_axes"),
        ({"variation_axes": [], "items": [{"sku": "A"}, {"sku": "B"}]}, "items"),
        (
            {"images": [{"url": "https://x.com/a.jpg", "filter": {"Colour": "Red"}}]},
            "images.0.filter",
        ),
    ],
)
def test_create_validation_rules(overrides, field):
    assert field in errors(client.post("/products", json=tshirt(**overrides)))


@pytest.mark.parametrize(
    "overrides",
    [
        {"items": []},
        {"name": "  "},
        {"images": [{"url": "http://insecure.com/a.jpg"}]},
        {"items": [{"sku": "A", "variant_values": {"Colour": "B", "Size": "M"}, "mrp": "-1"}]},
        {"items": [{"sku": "A", "variant_values": {"Colour": "B", "Size": "M"}, "stock_qty": -5}]},
    ],
)
def test_create_rejects_malformed_input(overrides):
    assert client.post("/products", json=tshirt(**overrides)).status_code == 422
    assert count(Article) == 0


# --- Publish (#46) ------------------------------------------------------------------------


def test_publish_creates_the_product_on_shopify_and_links_it(shopify):
    product = create()
    response = client.post(
        f"/products/{product['article_id']}/publish",
        json={"prices": {str(product["items"][0]["item_id"]): "499"}},
    )
    assert response.status_code == 200, response.json()
    body = response.json()
    assert (body["variants"], body["enabled"], body["warnings"]) == (4, False, [])

    [sent] = shopify.named("productSet")
    data = sent["input"]
    assert sent["identifier"] == {"handle": f"tshirt-basic-{product['article_id']}"}
    assert data["status"] == "DRAFT" and data["vendor"] == "Asha Apparel"
    assert data["productOptions"] == [
        {"name": "Colour", "values": [{"name": "Black"}, {"name": "White"}]},
        {"name": "Size", "values": [{"name": "M"}, {"name": "L"}]},
    ]
    first = data["variants"][0]
    assert (first["sku"], first["price"], first["compareAtPrice"], first["barcodes"]) == (
        "TS-B-M",
        "499.00",
        "599.00",
        ["890123"],
    )
    assert first["inventoryQuantities"][0]["quantity"] == 10
    assert first["inventoryItem"]["measurement"]["weight"] == {"value": 180.0, "unit": "GRAMS"}
    assert data["variants"][1]["price"] == "599.00" and "compareAtPrice" not in data["variants"][1]
    assert [f["originalSource"] for f in data["files"]] == [
        "https://cdn.example.com/tee.jpg",
        "https://cdn.example.com/tee-black.jpg",
    ]
    assert shopify.named("publishablePublish") == []  # draft until enabled

    listing = body["product"]["listings"][0]
    assert listing["status"] == "draft" and listing["channel_parent_id"].isdigit()
    assert len(listing["items"]) == 4 and body["product"]["status"] == "active"
    assert {li["selling_price"] for li in listing["items"]} == {"499.00", "599.00"}


def test_publish_and_enable_shows_it_on_the_online_store(shopify):
    product = create()
    body = client.post(f"/products/{product['article_id']}/publish", json={"enable": True}).json()
    assert body["enabled"] and shopify.named("productSet")[0]["input"]["status"] == "ACTIVE"
    assert shopify.named("publishablePublish")[0]["input"] == [
        {"publicationId": "gid://shopify/Publication/7"}
    ]


def test_publish_when_the_online_store_step_fails_keeps_the_product_as_draft(shopify):
    shopify.fail["publishablePublish"] = "Publication is not available"
    product = create()
    body = client.post(f"/products/{product['article_id']}/publish", json={"enable": True}).json()
    assert not body["enabled"] and "not shown on the Online Store" in body["warnings"][0]
    assert body["product"]["listings"][0]["status"] == "draft"


def test_publishing_a_product_without_options(shopify):
    product = create(
        article_code="MUG",
        name="Mug",
        variation_axes=[],
        images=[],
        items=[{"sku": "MUG-1", "mrp": "249", "stock_qty": 3}],
    )
    client.post(f"/products/{product['article_id']}/publish", json={})
    data = shopify.named("productSet")[0]["input"]
    assert data["productOptions"] == [{"name": "Title", "values": [{"name": "Default Title"}]}]
    assert data["variants"][0]["optionValues"] == [{"optionName": "Title", "name": "Default Title"}]
    assert count(ChannelListingItem) == 1


def test_publish_refusals(shopify):
    product = create()
    article_id = product["article_id"]
    client.post(f"/products/{article_id}/publish", json={})
    assert client.post(f"/products/{article_id}/publish", json={}).status_code == 409
    assert client.post("/products/999999/publish", json={}).status_code == 404

    no_price = create(
        article_code="NOPRICE",
        items=[{"sku": "NP-1", "variant_values": {"Colour": "Black", "Size": "M"}}],
    )
    response = client.post(f"/products/{no_price['article_id']}/publish", json={})
    assert response.status_code == 422 and "NP-1" in response.json()["detail"]
    other = client.post(
        f"/products/{no_price['article_id']}/publish", json={"prices": {"123456": "10"}}
    )
    assert other.status_code == 422
    assert (
        client.post(
            f"/products/{no_price['article_id']}/publish",
            json={"prices": {str(no_price["items"][0]["item_id"]): "0"}},
        ).status_code
        == 422
    )

    too_many = create(
        article_code="FOUR",
        variation_axes=["A", "B", "C", "D"],
        images=[],
        items=[
            {"sku": "F-1", "mrp": "1", "variant_values": {"A": "1", "B": "1", "C": "1", "D": "1"}}
        ],
    )
    assert client.post(f"/products/{too_many['article_id']}/publish", json={}).status_code == 422


def test_publish_rejected_by_shopify_saves_nothing_and_can_be_retried(shopify):
    product = create()
    shopify.fail["productSet"] = "Handle has already been taken"
    response = client.post(f"/products/{product['article_id']}/publish", json={})
    assert (
        response.status_code == 502 and "Handle has already been taken" in response.json()["detail"]
    )
    assert count(ChannelListing) == 0

    del shopify.fail["productSet"]
    assert client.post(f"/products/{product['article_id']}/publish", json={}).status_code == 200
    handles = {call["identifier"]["handle"] for call in shopify.named("productSet")}
    assert len(handles) == 1  # the retry targets the same Shopify product


# --- Enable / disable (#47) ---------------------------------------------------------------


def imported_watch():
    run([watch()])
    body = client.get("/products").json()["products"][0]
    detail = client.get(f"/products/{body['article_id']}").json()
    return detail, detail["listings"][0]


def test_disable_and_enable_a_product(shopify):
    detail, listing = imported_watch()
    url = f"/products/{detail['article_id']}/listings/{listing['listing_id']}/status"

    body = client.post(url, json={"status": "inactive"}).json()
    assert body["listings"][0]["status"] == "inactive"
    assert shopify.named("productUpdate")[-1] == {
        "product": {"id": "gid://shopify/Product/7001", "status": "DRAFT"}
    }
    assert all(
        li["status"] == "live" for li in body["listings"][0]["items"]
    )  # SKUs keep their state

    body = client.post(url, json={"status": "live"}).json()
    assert body["listings"][0]["status"] == "live"
    assert shopify.named("productUpdate")[-1]["product"]["status"] == "ACTIVE"
    assert shopify.named("publishablePublish")[-1]["id"] == "gid://shopify/Product/7001"


def test_product_status_change_failing_on_shopify_changes_nothing(shopify):
    detail, listing = imported_watch()
    shopify.fail["productUpdate"] = "Access denied for productUpdate"
    url = f"/products/{detail['article_id']}/listings/{listing['listing_id']}/status"
    response = client.post(url, json={"status": "inactive"})
    assert response.status_code == 502 and "Access denied" in response.json()["detail"]
    after = client.get(f"/products/{detail['article_id']}").json()["listings"][0]
    assert after["status"] == "live"
    assert all("Access denied" in li["last_sync_error"] for li in after["items"])

    del shopify.fail["productUpdate"]
    body = client.post(url, json={"status": "inactive"}).json()
    assert all(li["last_sync_error"] is None for li in body["listings"][0]["items"])


def test_disable_and_enable_one_sku(shopify):
    detail, listing = imported_watch()
    target = next(li for li in listing["items"] if li["channel_item_id"] == "8001")
    url = f"/products/{detail['article_id']}/listing-items/{target['listing_item_id']}/status"

    body = client.post(url, json={"status": "inactive"}).json()
    statuses = {li["channel_item_id"]: li["status"] for li in body["listings"][0]["items"]}
    assert statuses == {"8001": "inactive", "8002": "live", "8003": "live", "8004": "live"}
    assert shopify.named("productVariantsBulkUpdate")[-1]["variants"] == [
        {
            "id": "gid://shopify/ProductVariant/8001",
            "inventoryPolicy": "DENY",
            "inventoryItem": {"tracked": True},
        }
    ]
    assert shopify.named("inventorySetQuantities")[-1]["input"]["quantities"][0]["quantity"] == 0

    client.post(url, json={"status": "live"})
    assert (
        shopify.named("inventorySetQuantities")[-1]["input"]["quantities"][0]["quantity"] == 7
    )  # stock_qty
    keys = [call["key"] for call in shopify.named("inventorySetQuantities")]
    assert len(set(keys)) == len(keys)  # a new idempotency key per change


def test_sku_status_change_failing_on_shopify_changes_nothing(shopify):
    detail, listing = imported_watch()
    shopify.fail["inventorySetQuantities"] = "raise"
    target = listing["items"][0]
    url = f"/products/{detail['article_id']}/listing-items/{target['listing_item_id']}/status"
    assert client.post(url, json={"status": "inactive"}).status_code == 502
    after = client.get(f"/products/{detail['article_id']}").json()["listings"][0]["items"]
    changed = next(li for li in after if li["listing_item_id"] == target["listing_item_id"])
    assert changed["status"] == "live" and "network down" in changed["last_sync_error"]


def test_status_changes_reject_wrong_ids_and_unpublished_listings(shopify):
    detail, listing = imported_watch()
    article_id = detail["article_id"]
    assert (
        client.post(
            f"/products/{article_id}/listings/999999/status", json={"status": "live"}
        ).status_code
        == 404
    )
    assert (
        client.post(
            f"/products/{article_id}/listing-items/999999/status", json={"status": "live"}
        ).status_code
        == 404
    )
    assert (
        client.post(
            f"/products/{article_id}/listings/{listing['listing_id']}/status",
            json={"status": "paused"},
        ).status_code
        == 422
    )
    with SessionLocal.begin() as session:  # a listing not yet on the channel
        session.get(ChannelListing, listing["listing_id"]).channel_parent_id = None
    response = client.post(
        f"/products/{article_id}/listings/{listing['listing_id']}/status",
        json={"status": "inactive"},
    )
    assert response.status_code == 409 and "publish it first" in response.json()["detail"]
    assert shopify.calls == []


def test_same_status_again_calls_shopify_for_nothing(shopify):
    detail, listing = imported_watch()
    client.post(
        f"/products/{detail['article_id']}/listings/{listing['listing_id']}/status",
        json={"status": "live"},
    )
    assert shopify.calls == []


def test_bulk_status_change_reports_each_product(shopify):
    run([watch(), strap()])
    draft = create()
    ids = [p["article_id"] for p in client.get("/products?sort=name").json()["products"]]
    response = client.post("/products/status", json={"article_ids": ids, "status": "inactive"})
    results = {r["article_id"]: r for r in response.json()}
    assert results[draft["article_id"]] == {
        "article_id": draft["article_id"],
        "ok": False,
        "message": "Not on any channel",
    }
    assert sum(r["ok"] for r in results.values()) == 2
    assert len(shopify.named("productUpdate")) == 2


def test_import_keeps_an_app_disabled_sku_disabled_until_restocked_on_shopify(shopify):
    detail, listing = imported_watch()
    target = next(li for li in listing["items"] if li["channel_item_id"] == "8002")
    client.post(
        f"/products/{detail['article_id']}/listing-items/{target['listing_item_id']}/status",
        json={"status": "inactive"},
    )

    sold_out = watch()
    sold_out["variants"][1].update(
        inventory_management="shopify", inventory_policy="deny", inventory_quantity=0
    )
    run([sold_out])
    with SessionLocal() as session:
        assert (
            session.scalar(
                select(ChannelListingItem.status).where(
                    ChannelListingItem.channel_item_id == "8002"
                )
            )
            == "inactive"
        )

    restocked = watch()
    restocked["variants"][1].update(
        inventory_management="shopify", inventory_policy="deny", inventory_quantity=4
    )
    run([restocked])
    with SessionLocal() as session:
        assert (
            session.scalar(
                select(ChannelListingItem.status).where(
                    ChannelListingItem.channel_item_id == "8002"
                )
            )
            == "live"
        )


def test_sku_quantity_uses_compare_and_swap_and_retries_once(shopify):
    detail, listing = imported_watch()
    target = next(li for li in listing["items"] if li["channel_item_id"] == "8001")
    url = f"/products/{detail['article_id']}/listing-items/{target['listing_item_id']}/status"
    shopify.bump_before_set = 3  # stock changes on Shopify between our read and our write
    assert client.post(url, json={"status": "inactive"}).status_code == 200
    sets = shopify.named("inventorySetQuantities")
    assert [s["input"]["quantities"][0]["changeFromQuantity"] for s in sets] == [7, 10]
    assert shopify.available == 0
