"""Database rules from docs/catalog-erd.md, against the migrated test database.

Each test runs in a transaction that is rolled back.
"""

from decimal import Decimal

import pytest
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.db import engine
from app.models import (
    Article,
    Channel,
    ChannelAccount,
    ChannelListing,
    ChannelListingItem,
    Item,
    Seller,
)
from tests.conftest import DB_AVAILABLE

pytestmark = pytest.mark.skipif(not DB_AVAILABLE, reason="needs the local Postgres")


@pytest.fixture
def session():
    with engine.connect() as conn:
        transaction = conn.begin()
        with Session(bind=conn, join_transaction_mode="create_savepoint") as s:
            yield s
        transaction.rollback()


@pytest.fixture
def catalog(session):
    """A seller with a Colour × Size T-shirt (4 items) and accounts on two channels."""
    seller = Seller(name="Asha Apparel")
    amazon = Channel(code="AMAZON", name="Amazon")  # one listing for all variants
    myntra = Channel(code="MYNTRA", name="Myntra", default_split_axes=["Colour"])
    session.add_all([seller, amazon, myntra])
    session.flush()
    article = Article(
        seller_id=seller.seller_id,
        article_code="TSHIRT-BASIC",
        name="Basic T-shirt",
        variation_axes=["Colour", "Size"],
        attributes={"Fabric": "Cotton"},
    )
    article.items = [
        Item(
            seller_id=seller.seller_id,
            sku=f"TS-{c[0]}-{s}",
            variant_values={"Colour": c, "Size": s},
            mrp=Decimal("599.00"),
            stock_qty=10,
        )
        for c in ("Black", "White")
        for s in ("M", "L")
    ]
    accounts = [
        ChannelAccount(
            seller_id=seller.seller_id, channel_id=amazon.channel_id, account_name="Amazon A"
        ),
        ChannelAccount(
            seller_id=seller.seller_id, channel_id=myntra.channel_id, account_name="Myntra A"
        ),
    ]
    session.add_all([article, *accounts])
    session.flush()
    return {
        "session": session,
        "seller": seller,
        "article": article,
        "accounts": accounts,
        "channels": (amazon, myntra),
    }


def list_item(listing, item, **overrides):
    values = {
        "listing_id": listing.listing_id,
        "item_id": item.item_id,
        "channel_account_id": listing.channel_account_id,
        **overrides,
    }
    return ChannelListingItem(**values)


def test_defaults(catalog):
    session, seller, article = catalog["session"], catalog["seller"], catalog["article"]
    session.expire_all()
    assert seller.status == "active"
    assert article.status == "draft" and article.images == []
    assert catalog["channels"][0].default_split_axes == []
    assert catalog["accounts"][0].is_active is True
    assert article.items[0].status == "active"


def test_listings_follow_the_channel_split(catalog):
    """Amazon: one listing with all 4 items. Myntra (split by Colour): one listing per colour."""
    session, article = catalog["session"], catalog["article"]
    amazon_account, myntra_account = catalog["accounts"]

    amazon = ChannelListing(
        article_id=article.article_id, channel_account_id=amazon_account.channel_account_id
    )
    by_colour = {
        colour: ChannelListing(
            article_id=article.article_id,
            channel_account_id=myntra_account.channel_account_id,
            split_key={"Colour": colour},
            title=f"{colour} Basic Tee",
        )
        for colour in ("Black", "White")
    }
    session.add_all([amazon, *by_colour.values()])
    session.flush()
    for item in article.items:
        session.add(list_item(amazon, item, channel_sku=item.sku, selling_price=Decimal("499.00")))
        session.add(list_item(by_colour[item.variant_values["Colour"]], item))
    session.flush()
    session.expire_all()

    assert len(amazon.items) == 4 and amazon.split_key == {} and amazon.title is None
    assert [len(by_colour[c].items) for c in ("Black", "White")] == [2, 2]
    assert amazon.items[0].status == "inactive"  # not live until published


def test_same_split_key_twice_on_an_account_is_rejected(catalog):
    session, article = catalog["session"], catalog["article"]
    account = catalog["accounts"][1]
    for _ in range(2):
        session.add(
            ChannelListing(
                article_id=article.article_id,
                channel_account_id=account.channel_account_id,
                split_key={"Colour": "Black"},
            )
        )
    with pytest.raises(IntegrityError, match="uq_ecs_channel_listings_article_id_channel_account"):
        session.flush()


def test_an_item_sits_in_one_listing_per_account(catalog):
    session, article = catalog["session"], catalog["article"]
    account = catalog["accounts"][0]
    first, second = (
        ChannelListing(
            article_id=article.article_id,
            channel_account_id=account.channel_account_id,
            split_key=key,
        )
        for key in ({}, {"Colour": "Black"})
    )
    session.add_all([first, second])
    session.flush()
    item = article.items[0]
    session.add_all([list_item(first, item), list_item(second, item)])
    with pytest.raises(
        IntegrityError, match="uq_ecs_channel_listing_items_item_id_channel_account"
    ):
        session.flush()


def test_listing_item_account_must_match_its_listing(catalog):
    session, article = catalog["session"], catalog["article"]
    amazon_account, myntra_account = catalog["accounts"]
    listing = ChannelListing(
        article_id=article.article_id, channel_account_id=amazon_account.channel_account_id
    )
    session.add(listing)
    session.flush()
    session.add(
        list_item(listing, article.items[0], channel_account_id=myntra_account.channel_account_id)
    )
    with pytest.raises(IntegrityError, match="fk_ecs_channel_listing_items_listing_account"):
        session.flush()


def test_sku_is_unique_per_seller_only(catalog):
    session, article, seller = catalog["session"], catalog["article"], catalog["seller"]
    other = Seller(name="Other Seller")
    session.add(other)
    session.flush()
    other_article = Article(seller_id=other.seller_id, article_code="TSHIRT-BASIC", name="Tee")
    other_article.items = [Item(seller_id=other.seller_id, sku="TS-B-M")]  # same code and SKU
    session.add(other_article)
    session.flush()  # fine: a different seller

    session.add(Item(article_id=article.article_id, seller_id=seller.seller_id, sku="TS-B-M"))
    with pytest.raises(IntegrityError, match="uq_ecs_items_seller_id_sku"):
        session.flush()


def test_article_code_is_unique_per_seller(catalog):
    session, seller = catalog["session"], catalog["seller"]
    session.add(Article(seller_id=seller.seller_id, article_code="TSHIRT-BASIC", name="Copy"))
    with pytest.raises(IntegrityError, match="uq_ecs_articles_seller_id_article_code"):
        session.flush()


def test_item_seller_must_be_its_articles_seller(catalog):
    session, article = catalog["session"], catalog["article"]
    other = Seller(name="Other Seller")
    session.add(other)
    session.flush()
    session.add(Item(article_id=article.article_id, seller_id=other.seller_id, sku="X-1"))
    with pytest.raises(IntegrityError, match="fk_ecs_items_article_seller"):
        session.flush()


def test_account_name_is_unique_per_seller(catalog):
    session, seller = catalog["session"], catalog["seller"]
    amazon = catalog["channels"][0]
    session.add(
        ChannelAccount(
            seller_id=seller.seller_id, channel_id=amazon.channel_id, account_name="Amazon A"
        )
    )
    with pytest.raises(IntegrityError, match="uq_ecs_channel_accounts_seller_id_account_name"):
        session.flush()


def test_channel_code_is_unique(catalog):
    catalog["session"].add(Channel(code="AMAZON", name="Amazon again"))
    with pytest.raises(IntegrityError, match="uq_ecs_channels_code"):
        catalog["session"].flush()


@pytest.mark.parametrize(
    ("make", "constraint"),
    [
        (lambda c: Seller(name="S", status="deleted"), "ck_ecs_sellers_status"),
        (
            lambda c: Article(
                seller_id=c["seller"].seller_id, article_code="A2", name="A", status="live"
            ),
            "ck_ecs_articles_status",
        ),
        (
            lambda c: Item(
                article_id=c["article"].article_id,
                seller_id=c["seller"].seller_id,
                sku="N-1",
                mrp=Decimal("-1"),
            ),
            "ck_ecs_items_mrp_non_negative",
        ),
        (
            lambda c: Item(
                article_id=c["article"].article_id,
                seller_id=c["seller"].seller_id,
                sku="N-2",
                weight=Decimal("-0.5"),
            ),
            "ck_ecs_items_weight_non_negative",
        ),
        (
            lambda c: Item(
                article_id=c["article"].article_id,
                seller_id=c["seller"].seller_id,
                sku="N-3",
                status="live",
            ),
            "ck_ecs_items_status",
        ),
        (
            lambda c: ChannelListing(
                article_id=c["article"].article_id,
                channel_account_id=c["accounts"][0].channel_account_id,
                status="published",
            ),
            "ck_ecs_channel_listings_status",
        ),
    ],
)
def test_values_outside_the_allowed_lists_are_rejected(catalog, make, constraint):
    catalog["session"].add(make(catalog))
    with pytest.raises(IntegrityError, match=constraint):
        catalog["session"].flush()


def test_stock_may_go_negative_when_an_order_oversells(catalog):
    item = catalog["article"].items[0]
    item.stock_qty = -2
    catalog["session"].flush()
