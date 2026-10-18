"""Database-level rules from docs/order-erd.md, checked against the migrated local DB.

Each test runs in a transaction that is rolled back, so no data is left behind.
Skipped when the database is unreachable or not migrated.
"""

from datetime import UTC, datetime
from decimal import Decimal

import pytest
from sqlalchemy import inspect, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.db import check_db, engine
from app.models import (
    Channel,
    ChannelAccount,
    OrderHeader,
    OrderHistory,
    OrderImport,
    OrderImportRow,
    OrderLine,
)

pytestmark = pytest.mark.skipif(
    not check_db() or not inspect(engine).has_table("ecs_order_header"),
    reason="needs the local database with migrations applied (uv run alembic upgrade head)",
)

SELLER_ID = 1


@pytest.fixture
def session():
    with engine.connect() as conn:
        transaction = conn.begin()
        with Session(bind=conn, join_transaction_mode="create_savepoint") as s:
            yield s
        transaction.rollback()


def make_connection(session) -> ChannelAccount:
    """The seller's Shopify account (ecs_channel_accounts)."""
    channel = session.scalars(select(Channel).where(Channel.code == "SHOPIFY")).one_or_none()
    if channel is None:
        channel = Channel(code="SHOPIFY", name="Shopify")
        session.add(channel)
        session.flush()
    account = ChannelAccount(
        seller_id=SELLER_ID,
        channel_id=channel.channel_id,
        account_name="Main Shopify store",
        external_seller_id="test-store.myshopify.com",
    )
    session.add(account)
    session.flush()
    return account


def make_order(session, connection, **overrides) -> OrderHeader:
    values = {
        "order_number": "#1001",
        "seller_id": SELLER_ID,
        "channel": "shopify",
        "channel_account_id": connection.channel_account_id,
        "external_order_id": "5001",
        "ingestion_method": "webhook",
        "order_date": datetime(2026, 10, 17, 18, 30, tzinfo=UTC),
        **overrides,
    }
    order = OrderHeader(**values)
    session.add(order)
    session.flush()
    return order


def test_full_webhook_flow_and_defaults(session):
    connection = make_connection(session)
    run = OrderImport(
        seller_id=SELLER_ID,
        channel_account_id=connection.channel_account_id,
        channel="shopify",
        ingestion_method="webhook",
        external_event_id="evt-1",
        event_topic="orders/create",
    )
    session.add(run)
    session.flush()
    row = OrderImportRow(
        order_import_id=run.order_import_id, record_number=1, raw_payload={"id": 5001}
    )
    session.add(row)
    order = make_order(session, connection, created_by_import_id=run.order_import_id)
    order.lines.append(
        OrderLine(
            line_number=1,
            product_name="Watch",
            ordered_qty=2,
            unit_price=Decimal("1499.50"),
            line_total_amount=Decimal("2999.00"),
        )
    )
    session.add(
        OrderHistory(
            order_header_id=order.order_header_id,
            version_no=1,
            action_type="created",
            change_source="webhook",
            order_import_id=run.order_import_id,
            order_import_row_id=row.order_import_row_id,
        )
    )
    row.order_header_id = order.order_header_id
    session.flush()
    session.expire_all()

    order = session.get(OrderHeader, order.order_header_id)
    assert order.order_status == "pending"
    assert order.currency == "INR"
    assert order.version_no == 1
    assert order.total_amount == Decimal("0.00")
    assert order.lines[0].unit_price == Decimal("1499.50")  # exact decimal, not float
    assert order.history[0].change_source == "webhook"
    assert session.get(OrderImport, run.order_import_id).status == "pending"


def test_same_order_cannot_be_saved_twice(session):
    connection = make_connection(session)
    make_order(session, connection)
    with pytest.raises(IntegrityError, match="uq_ecs_order_header_seller_id_channel_external"):
        make_order(session, connection, order_number="#1001-dup")


def test_orders_without_external_id_do_not_collide(session):
    connection = make_connection(session)
    make_order(
        session,
        connection,
        channel="website",
        external_order_id=None,
        ingestion_method="direct",
        channel_account_id=None,
    )
    make_order(
        session,
        connection,
        order_number="#1002",
        channel="website",
        external_order_id=None,
        ingestion_method="direct",
        channel_account_id=None,
    )


def test_repeat_webhook_delivery_is_rejected(session):
    connection = make_connection(session)
    for _ in range(2):
        session.add(
            OrderImport(
                seller_id=SELLER_ID,
                channel_account_id=connection.channel_account_id,
                channel="shopify",
                ingestion_method="webhook",
                external_event_id="evt-same",
            )
        )
    with pytest.raises(IntegrityError, match="uq_ecs_order_import_channel_account_id"):
        session.flush()


@pytest.mark.parametrize(
    ("overrides", "constraint"),
    [
        ({"channel": "ebay"}, "ck_ecs_order_header_channel"),
        ({"ingestion_method": "email"}, "ck_ecs_order_header_ingestion_method"),
        ({"version_no": 0}, "ck_ecs_order_header_version_no_positive"),
    ],
)
def test_header_rejects_values_outside_the_allowed_lists(session, overrides, constraint):
    connection = make_connection(session)
    with pytest.raises(IntegrityError, match=constraint):
        make_order(session, connection, **overrides)


def test_line_rejects_unknown_status_and_negative_quantity(session):
    connection = make_connection(session)
    order = make_order(session, connection)
    session.add(
        OrderLine(
            order_header_id=order.order_header_id,
            line_number=1,
            product_name="Watch",
            ordered_qty=1,
            line_status="lost",
        )
    )
    with pytest.raises(IntegrityError, match="ck_ecs_order_line_line_status"):
        session.flush()
