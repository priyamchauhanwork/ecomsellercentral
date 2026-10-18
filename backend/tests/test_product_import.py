"""Shopify product import into the catalog (#13)."""

import copy
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select, text

from app.db import SessionLocal, engine
from app.main import app
from app.models import Article, ChannelListing, ChannelListingItem, Item, OrderLine
from app.services import order_ingestion, product_import
from tests.shopify_orders import line_item, shopify_order
from tests.test_order_ingestion import FakeShopify, import_orders

pytestmark = pytest.mark.usefixtures("clean_db")
client = TestClient(app)


def variant(variant_id, sku, option1=None, option2=None, **extra):
    return {
        "id": variant_id,
        "sku": sku,
        "price": "1499.00",
        "compare_at_price": None,
        "barcode": None,
        "grams": 0,
        "weight": 0,
        "weight_unit": "kg",
        "inventory_quantity": 7,
        "inventory_management": "shopify",
        "option1": option1,
        "option2": option2,
        "option3": None,
        **extra,
    }


def watch(**extra):
    """A watch in 2 colours × 2 sizes, published."""
    product = {
        "id": 7001,
        "handle": "classic-watch",
        "title": "Classic Watch",
        "body_html": "<p>Steel</p>",
        "vendor": "Velocis",
        "product_type": "Watches",
        "status": "active",
        "published_at": "2026-10-01T10:00:00+05:30",
        "options": [
            {"name": "Colour", "values": ["Black", "Silver"]},
            {"name": "Size", "values": ["40mm", "42mm"]},
        ],
        "variants": [
            variant(8001, "WATCH-BLK-40", "Black", "40mm", compare_at_price="1999.00", grams=180),
            variant(8002, "WATCH-BLK-42", "Black", "42mm", weight=0.2, weight_unit="kg"),
            variant(8003, "WATCH-SLV-40", "Silver", "40mm", inventory_quantity=-2),
            variant(8004, "WATCH-SLV-42", "Silver", "42mm", inventory_management=None),
        ],
        "images": [
            {"id": 1, "src": "https://cdn/w.jpg", "variant_ids": []},
            {"id": 2, "src": "https://cdn/black.jpg", "variant_ids": [8001, 8002]},
        ],
    }
    product.update(extra)
    return product


def strap(**extra):
    """No real options (Shopify's "Default Title"), no SKU."""
    product = {
        "id": 7002,
        "handle": "leather-strap",
        "title": "Leather Strap",
        "body_html": None,
        "vendor": "",
        "product_type": "",
        "status": "active",
        "published_at": "2026-10-01T10:00:00Z",
        "options": [{"name": "Title", "values": ["Default Title"]}],
        "variants": [variant(8101, "", "Default Title", price="349.00")],
        "images": [],
    }
    product.update(extra)
    return product


def run(products, **fake_kwargs):
    with SessionLocal.begin() as session:
        account = order_ingestion.ensure_shopify_account(session)
        session.expunge(account)
    fake = FakeShopify(copy.deepcopy(products), **fake_kwargs)
    return product_import.import_products(account, client_factory=lambda: fake)


def items_by_sku():
    with SessionLocal() as session:
        return {item.sku: item for item in session.scalars(select(Item))}


def listing_items():
    with SessionLocal() as session:
        return {li.channel_item_id: li for li in session.scalars(select(ChannelListingItem))}


def count(model):
    with SessionLocal() as session:
        return session.scalar(select(func.count()).select_from(model))


def test_first_import_maps_products_variants_and_listings():
    summary = run([watch(), strap()])
    assert summary.complete and summary.errors == []
    assert (summary.products, summary.articles_created, summary.items_created) == (2, 2, 5)
    assert summary.untracked_stock == 1

    with SessionLocal() as session:
        article = session.scalars(
            select(Article).where(Article.article_code == "classic-watch")
        ).one()
        assert (article.name, article.brand, article.category, article.status) == (
            "Classic Watch",
            "Velocis",
            "Watches",
            "active",
        )
        assert article.variation_axes == ["Colour", "Size"]
        assert article.images == [
            {"url": "https://cdn/w.jpg", "filter": {}},
            {
                "url": "https://cdn/black.jpg",
                "filter": {"Colour": "Black"},
            },  # shared by its variants
        ]
        strap_article = session.scalars(
            select(Article).where(Article.article_code == "leather-strap")
        ).one()
        assert strap_article.variation_axes == [] and strap_article.brand is None
        listing = session.scalars(
            select(ChannelListing).where(ChannelListing.channel_parent_id == "7001")
        ).one()
        assert (listing.split_key, listing.status, listing.title) == ({}, "live", None)

    items = items_by_sku()
    black = items["WATCH-BLK-40"]
    assert black.variant_values == {"Colour": "Black", "Size": "40mm"}
    assert black.mrp == Decimal("1999.00")  # compare-at price above the selling price
    assert black.weight == Decimal("180.000") and black.stock_qty == 7
    assert black.stock_baseline_at is not None
    assert items["WATCH-BLK-42"].weight == Decimal("200.000")  # 0.2 kg
    assert items["WATCH-SLV-40"].stock_qty == -2  # kept as Shopify reports it
    assert items["shopify-8101"].variant_values == {}  # blank SKU → shopify-<variant id>

    li = listing_items()
    assert (li["8001"].channel_sku, li["8001"].selling_price, li["8001"].status) == (
        "WATCH-BLK-40",
        Decimal("1499.00"),
        "live",
    )
    assert li["8101"].channel_sku is None


def test_reimport_updates_without_duplicating_or_touching_stock():
    run([watch(), strap()])
    with SessionLocal.begin() as session:  # the app adjusted stock meanwhile
        session.scalars(select(Item).where(Item.sku == "WATCH-BLK-40")).one().stock_qty = 3

    renamed = watch(title="Classic Watch II", handle="classic-watch-ii")
    renamed["variants"][0].update(price="1299.00", inventory_quantity=50, barcode="8901234567890")
    summary = run([renamed, strap()])

    assert (summary.articles_created, summary.articles_updated) == (0, 2)
    assert (summary.items_created, summary.items_updated) == (0, 5)
    assert count(Article) == 2 and count(Item) == 5 and count(ChannelListingItem) == 5
    black = items_by_sku()["WATCH-BLK-40"]
    assert black.stock_qty == 3  # not reset to Shopify's 50
    assert black.barcode == "8901234567890"
    assert listing_items()["8001"].selling_price == Decimal("1299.00")
    with SessionLocal() as session:
        article = session.get(Article, black.article_id)
        assert article.name == "Classic Watch II"
        assert article.article_code == "classic-watch"  # the code doesn't follow handle changes


def test_sku_changes_and_conflicts():
    duplicate = watch()
    duplicate["variants"][1]["sku"] = "WATCH-BLK-40"  # same SKU on two variants in Shopify
    summary = run([duplicate])
    assert "shopify-8002" in items_by_sku()
    assert any("WATCH-BLK-40 is already used" in w for w in summary.warnings)

    renamed = watch()
    renamed["variants"][1]["sku"] = "WATCH-BLK-42-NEW"
    run([renamed])
    assert "WATCH-BLK-42-NEW" in items_by_sku() and "shopify-8002" not in items_by_sku()


def test_removed_variants_and_products_are_deactivated():
    run([watch(), strap()])
    fewer = watch()
    fewer["variants"] = fewer["variants"][:3]
    summary = run([fewer])  # 8004 gone, strap gone

    assert (summary.variants_deactivated, summary.products_deactivated) == (1, 1)
    li = listing_items()
    assert li["8004"].status == "inactive" and li["8101"].status == "inactive"
    with SessionLocal() as session:
        strap_article = session.scalars(
            select(Article).where(Article.article_code == "leather-strap")
        ).one()
        assert strap_article.status == "archived"
    assert count(Item) == 5  # nothing deleted: orders may reference them


def test_nothing_is_deactivated_when_shopify_fails_midway():
    run([watch(), strap()])
    summary = run([watch(), strap()], fail_after=1)
    assert not summary.complete and summary.products_deactivated == 0
    assert any("Import stopped after 1 products" in e for e in summary.errors)
    assert listing_items()["8101"].status == "live"


def test_archived_and_draft_products():
    run(
        [
            watch(
                id=7003,
                handle="old-watch",
                status="archived",
                published_at=None,
                variants=[variant(8301, "OLD-1")],
                options=[{"name": "Title", "values": ["Default Title"]}],
            ),
            strap(status="draft", published_at=None),
        ]
    )
    with SessionLocal() as session:
        statuses = dict(
            session.execute(select(ChannelListing.channel_parent_id, ChannelListing.status)).all()
        )
        archived = session.scalars(select(Article).where(Article.article_code == "old-watch")).one()
    assert statuses == {"7003": "inactive", "7002": "draft"}
    assert archived.status == "archived"
    assert listing_items()["8301"].status == "live"  # the SKU is enabled; the product isn't on sale


def test_invalid_product_is_skipped_and_the_rest_imports():
    broken = watch(id=7009, handle="broken")
    broken["variants"][1].update(option1="Black", option2="40mm")  # same values as variant 1
    summary = run([broken, strap()])
    assert summary.articles_created == 1 and len(summary.errors) == 1
    assert "two variants share the option values" in summary.errors[0]
    assert count(Article) == 1  # nothing half-imported


def test_handle_already_used_by_another_article_gets_a_fallback_code():
    with SessionLocal.begin() as session:
        session.add(Article(seller_id=1, article_code="classic-watch", name="Made by hand"))
    run([watch()])
    with SessionLocal() as session:
        codes = set(session.scalars(select(Article.article_code)))
    assert codes == {"classic-watch", "shopify-7001"}


def test_variant_moved_to_another_product():
    run([watch(), strap()])
    moved = strap()
    moved["variants"].append(watch()["variants"][3] | {"option1": "Default Title", "option2": None})
    moved["variants"][0]["option1"] = "A"
    moved["variants"][1]["option1"] = "B"
    moved["options"] = [{"name": "Style", "values": ["A", "B"]}]
    shrunk = watch()
    shrunk["variants"] = shrunk["variants"][:3]
    run([shrunk, moved])
    with SessionLocal() as session:
        item = session.scalars(select(Item).where(Item.sku == "WATCH-SLV-42")).one()
        strap_article = session.scalars(
            select(Article).where(Article.article_code == "leather-strap")
        ).one()
        assert item.article_id == strap_article.article_id
        assert item.variant_values == {"Style": "B"}
    assert listing_items()["8004"].status == "live"


def test_existing_orders_are_linked_and_new_ones_on_arrival():
    import_orders([shopify_order(5001)])  # line 9001 for Shopify variant 8001
    with SessionLocal() as session:
        assert session.scalar(select(OrderLine.variant_id)) is None

    summary = run([watch(), strap()])
    assert summary.order_lines_linked == 1
    blk = items_by_sku()["WATCH-BLK-40"]
    with SessionLocal() as session:
        line = session.scalars(select(OrderLine)).one()
        assert (line.variant_id, line.product_id) == (blk.item_id, blk.article_id)

    # A new order after the import is linked as it arrives; SKU fallback for unknown ids.
    import_orders(
        [
            shopify_order(
                5002,
                name="#1002",
                line_items=[
                    line_item(9101, variant_id=8002, sku="X"),
                    line_item(9102, variant_id=None, sku="WATCH-SLV-40"),
                    line_item(9103, variant_id=999, sku="NOT-IN-CATALOG"),
                ],
            )
        ]
    )
    with SessionLocal() as session:
        links = dict(
            session.execute(
                select(OrderLine.external_line_id, OrderLine.variant_id).where(
                    OrderLine.external_line_id != "9001"
                )
            ).all()
        )
    items = items_by_sku()
    assert links == {
        "9101": items["WATCH-BLK-42"].item_id,
        "9102": items["WATCH-SLV-40"].item_id,
        "9103": None,
    }


def test_only_one_import_at_a_time():
    with SessionLocal.begin() as session:
        account_id = order_ingestion.ensure_shopify_account(session).channel_account_id
    with engine.connect() as other:
        other.execute(text("SELECT pg_advisory_lock(:key)"), {"key": 13_000_000 + account_id})
        with pytest.raises(product_import.ImportInProgress):
            run([watch()])
        other.execute(text("SELECT pg_advisory_unlock(:key)"), {"key": 13_000_000 + account_id})
    assert run([watch()]).articles_created == 1  # the lock was released


# --- API --------------------------------------------------------------------------------


def use_shopify(monkeypatch, products, **kwargs):
    fake = FakeShopify(copy.deepcopy(products), **kwargs)
    monkeypatch.setattr(product_import, "shopify_client_factory", lambda: fake)


def test_post_import_returns_the_summary_and_get_lists_items(monkeypatch):
    use_shopify(monkeypatch, [watch(), strap()], scopes=["read_products", "read_orders"])
    body = client.post("/products/import").json()
    assert body["complete"] and (body["items_created"], body["articles_created"]) == (5, 2)

    items = client.get("/products/items").json()
    assert [(i["article_name"], i["sku"]) for i in items] == [
        ("Classic Watch", "WATCH-BLK-40"),
        ("Classic Watch", "WATCH-BLK-42"),
        ("Classic Watch", "WATCH-SLV-40"),
        ("Classic Watch", "WATCH-SLV-42"),
        ("Leather Strap", "shopify-8101"),
    ]
    assert items[0]["variant_values"] == {"Colour": "Black", "Size": "40mm"}


def test_import_without_read_products_scope_is_503(monkeypatch):
    use_shopify(monkeypatch, [watch()], scopes=["read_orders"])
    response = client.post("/products/import")
    assert response.status_code == 503 and "read_products" in response.json()["detail"]
    assert count(Article) == 0


def test_import_while_another_runs_is_409(monkeypatch):
    use_shopify(monkeypatch, [watch()], scopes=["read_products"])
    with SessionLocal.begin() as session:
        account_id = order_ingestion.ensure_shopify_account(session).channel_account_id
    with engine.connect() as other:
        other.execute(text("SELECT pg_advisory_lock(:key)"), {"key": 13_000_000 + account_id})
        assert client.post("/products/import").status_code == 409
        other.execute(text("SELECT pg_advisory_unlock(:key)"), {"key": 13_000_000 + account_id})


# --- Images on order lines -----------------------------------------------------------------


def test_image_for_prefers_the_most_specific_matching_image():
    images = [
        {"url": "general.jpg", "filter": {}},
        {"url": "black.jpg", "filter": {"Colour": "Black"}},
        {"url": "black-42.jpg", "filter": {"Colour": "Black", "Size": "42mm"}},
        {"url": "red.jpg", "filter": {"Colour": "Red"}},
    ]
    image_for = product_import.image_for
    assert image_for(images, {"Colour": "Black", "Size": "42mm"}) == "black-42.jpg"
    assert image_for(images, {"Colour": "Black", "Size": "40mm"}) == "black.jpg"
    assert image_for(images, {"Colour": "Silver", "Size": "40mm"}) == "general.jpg"
    assert (
        image_for([{"url": "red.jpg", "filter": {"Colour": "Red"}}], {"Colour": "Blue"})
        == "red.jpg"
    )
    assert image_for([], {}) is None and image_for(None, {}) is None
    assert image_for([{"url": "", "filter": {}}], {}) is None


def test_order_detail_lines_carry_their_variant_image():
    run([watch(), strap()])
    import_orders(
        [
            shopify_order(
                5001,
                line_items=[
                    line_item(9001, variant_id=8001),  # Black / 40mm → the black image
                    line_item(
                        9002, variant_id=8003, sku="WATCH-SLV-40"
                    ),  # Silver → the general image
                    line_item(9003, variant_id=8101, sku=""),  # strap: article without images
                    line_item(9004, variant_id=999, sku="UNKNOWN"),  # not in the catalog
                ],
            )
        ]
    )
    with SessionLocal() as session:
        order_id = session.scalar(select(OrderLine.order_header_id))
    lines = client.get(f"/orders/{order_id}").json()["lines"]
    assert [line["image_url"] for line in lines] == [
        "https://cdn/black.jpg",
        "https://cdn/w.jpg",
        None,
        None,
    ]
