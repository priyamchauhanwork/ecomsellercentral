"""Courier aggregator tables (#58): seeds, constraints, partial unique indexes, triggers.
Everything runs in one transaction that is rolled back."""

from decimal import Decimal

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError

from app.db import SessionLocal
from app.models import (
    AggregatorAccount,
    AggregatorApiCall,
    AggregatorStatusMapping,
    AggregatorWebhookEvent,
    CourierAggregator,
    OrderHeader,
    OrderLine,
    Seller,
    Shipment,
    ShipmentDocument,
    ShipmentItem,
    ShipmentTrackingEvent,
    Warehouse,
    WarehouseAggregatorLink,
)
from app.models.shipping import TERMINAL_STATUSES
from tests.conftest import DB_AVAILABLE

pytestmark = pytest.mark.skipif(not DB_AVAILABLE, reason="needs the local Postgres")


@pytest.fixture
def db():
    session = SessionLocal()
    try:
        yield session
    finally:
        session.rollback()
        session.close()


def refused(db, row=None, statement=None):
    """The insert / statement is rejected by the database; the session stays usable."""
    with pytest.raises(IntegrityError), db.begin_nested():
        if row is not None:
            db.add(row)
        else:
            db.execute(text(statement))
        db.flush()


@pytest.fixture
def world(db):
    """Seller 1's order with one line, a Shiprocket account and a warehouse."""
    aggregator = db.scalars(
        select(CourierAggregator).where(CourierAggregator.code == "shiprocket")
    ).one()
    header = OrderHeader(
        order_number="#T-58",
        seller_id=1,
        channel="shopify",
        ingestion_method="direct",
        order_date=text("now()"),
    )
    header.lines = [
        OrderLine(line_number=1, product_name="Watch", ordered_qty=2, unit_price=Decimal("999"))
    ]
    account = AggregatorAccount(
        seller_id=1,
        aggregator_id=aggregator.aggregator_id,
        api_username="api@example.com",
        is_default=True,
    )
    warehouse = Warehouse(
        seller_id=1,
        name="Okhla",
        contact_name="Ravi",
        phone="9876543210",
        address_line1="B-12, Phase 2",
        city="New Delhi",
        state="Delhi",
        pincode="110020",
        is_default=True,
    )
    db.add_all([header, account, warehouse])
    db.flush()
    return {"aggregator": aggregator, "header": header, "account": account, "warehouse": warehouse}


def shipment(world, **overrides):
    values = {
        "seller_id": 1,
        "order_header_id": world["header"].order_header_id,
        "aggregator_account_id": world["account"].aggregator_account_id,
        "warehouse_id": world["warehouse"].warehouse_id,
        "idempotency_key": "order-58-1",
        "payment_mode": "prepaid",
        "declared_value": Decimal("1998.00"),
        "dead_weight_g": 500,
        "length_cm": Decimal("20"),
        "breadth_cm": Decimal("15"),
        "height_cm": Decimal("10"),
        "ship_to": {"name": "Rahul", "pincode": "201301", "country": "IN"},
    }
    values.update(overrides)
    return Shipment(**values)


def test_shiprocket_is_seeded_with_its_status_codes(db):
    aggregator = db.scalars(
        select(CourierAggregator).where(CourierAggregator.code == "shiprocket")
    ).one()
    assert (aggregator.auth_type, aggregator.token_ttl_hours) == ("credentials_token", 240)
    assert aggregator.api_base_url == "https://apiv2.shiprocket.in/v1/external"
    mappings = {
        m.external_status_code: m
        for m in db.scalars(
            select(AggregatorStatusMapping).where(
                AggregatorStatusMapping.aggregator_id == aggregator.aggregator_id
            )
        )
    }
    assert mappings["42"].canonical_status == "picked_up"  # webhook scan sr-status 42
    assert mappings["18"].canonical_status == "in_transit"
    assert mappings["7"].canonical_status == "delivered" and mappings["7"].is_terminal
    assert "16" not in mappings  # cancellation *requested*: informational, not mapped
    for mapping in mappings.values():
        assert mapping.is_terminal == (mapping.canonical_status in TERMINAL_STATUSES)


def test_a_valid_shipment_with_items_documents_and_events(db, world):
    row = shipment(world)
    db.add(row)
    db.flush()
    assert (row.status, row.version, row.cod_amount) == ("draft", 1, 0)
    line = world["header"].lines[0]
    db.add(ShipmentItem(shipment_id=row.shipment_id, order_line_id=line.order_line_id, quantity=2))
    db.add(
        ShipmentDocument(
            shipment_id=row.shipment_id,
            doc_type="label",
            source="aggregator",
            external_url="https://x/label.pdf",
        )
    )
    db.flush()


def test_shipment_checks(db, world):
    refused(db, shipment(world, status="teleported"))
    refused(db, shipment(world, cod_amount=Decimal("100")))  # COD amount on a prepaid shipment
    refused(db, shipment(world, status="awb_assigned"))  # needs an AWB
    refused(db, shipment(world, dead_weight_g=0))
    refused(db, shipment(world, length_cm=Decimal("0")))
    refused(db, shipment(world, payment_mode="upi"))
    db.add(
        shipment(
            world,
            payment_mode="cod",
            cod_amount=Decimal("1998"),
            status="awb_assigned",
            awb_number="AWB1",
        )
    )
    db.flush()
    refused(db, shipment(world, idempotency_key="order-58-1"))  # same idempotency key
    refused(
        db, shipment(world, idempotency_key="order-58-2", awb_number="AWB1")
    )  # same AWB on the account


def test_shipment_must_belong_to_its_orders_seller(db, world):
    db.add(Seller(seller_id=2, name="Other seller"))
    db.flush()
    refused(db, shipment(world, seller_id=2))


def test_one_default_account_and_warehouse(db, world):
    aggregator_id = world["aggregator"].aggregator_id
    refused(
        db,
        AggregatorAccount(
            seller_id=1, aggregator_id=aggregator_id, label="second", is_default=True
        ),
    )
    refused(
        db, AggregatorAccount(seller_id=1, aggregator_id=aggregator_id)
    )  # duplicate label "default"
    db.add(AggregatorAccount(seller_id=1, aggregator_id=aggregator_id, label="second"))
    other = dict(contact_name="A", phone="9", address_line1="X", city="C", state="S")
    refused(db, Warehouse(seller_id=1, name="Second", pincode="110001", is_default=True, **other))
    refused(db, Warehouse(seller_id=1, name="Okhla", pincode="110001", **other))  # duplicate name
    refused(db, Warehouse(seller_id=1, name="Bad pin", pincode="012345", **other))
    db.add(
        Warehouse(
            seller_id=1,
            name="Old default",
            pincode="110001",
            is_default=True,
            is_active=False,
            **other,
        )
    )
    db.flush()  # an inactive default doesn't count


def test_warehouse_links_are_unique(db, world):
    ids = dict(
        warehouse_id=world["warehouse"].warehouse_id,
        aggregator_account_id=world["account"].aggregator_account_id,
    )
    db.add(WarehouseAggregatorLink(external_location_code="Okhla", **ids))
    db.flush()
    refused(db, WarehouseAggregatorLink(external_location_code="Other", **ids))
    refused(db, WarehouseAggregatorLink(**ids, external_location_code="Okhla", sync_status="done"))


def test_documents_tracking_webhooks_and_api_calls(db, world):
    row = shipment(world)
    db.add(row)
    db.flush()
    sid = row.shipment_id
    refused(
        db, ShipmentDocument(shipment_id=sid, doc_type="label", source="aggregator")
    )  # nowhere to fetch
    db.add(
        ShipmentDocument(shipment_id=sid, doc_type="label", source="aggregator", external_url="u1")
    )
    db.flush()
    refused(
        db,
        ShipmentDocument(shipment_id=sid, doc_type="label", source="aggregator", external_url="u2"),
    )
    db.add(
        ShipmentDocument(
            shipment_id=sid,
            doc_type="label",
            source="aggregator",
            external_url="u0",
            is_current=False,
        )
    )
    event = dict(
        shipment_id=sid,
        canonical_status="picked_up",
        event_at=text("now()"),
        source="webhook",
        dedupe_key="k1",
    )
    db.add(ShipmentTrackingEvent(**event))
    db.flush()
    refused(db, ShipmentTrackingEvent(**event))  # same scan twice
    refused(db, ShipmentTrackingEvent(**{**event, "dedupe_key": "k2", "source": "fax"}))
    webhook = dict(
        aggregator_id=world["aggregator"].aggregator_id,
        payload={"awb": "1"},
        auth_valid=True,
        dedupe_key="h1",
    )
    db.add(AggregatorWebhookEvent(**webhook))
    db.flush()
    refused(db, AggregatorWebhookEvent(**webhook))  # exact duplicate body
    call = dict(
        aggregator_account_id=world["account"].aggregator_account_id,
        http_method="POST",
        endpoint="/auth/login",
        success=True,
    )
    db.add(AggregatorApiCall(operation="auth", **call))
    db.flush()
    refused(db, AggregatorApiCall(operation="hack", **call))
    refused(
        db,
        ShipmentItem(
            shipment_id=sid, order_line_id=world["header"].lines[0].order_line_id, quantity=0
        ),
    )


def test_updated_at_is_kept_by_the_database(db, world):
    db.flush()
    before = db.execute(
        text("SELECT updated_at FROM ecs_warehouses WHERE warehouse_id = :id"),
        {"id": world["warehouse"].warehouse_id},
    ).scalar_one()
    db.execute(text("SELECT pg_sleep(0.01)"))
    db.execute(
        text("UPDATE ecs_warehouses SET phone = '9999999999' WHERE warehouse_id = :id"),
        {"id": world["warehouse"].warehouse_id},
    )
    after = db.execute(
        text("SELECT updated_at FROM ecs_warehouses WHERE warehouse_id = :id"),
        {"id": world["warehouse"].warehouse_id},
    ).scalar_one()
    assert after == before  # same transaction: now() is the transaction start time
    assert (
        db.execute(
            text("SELECT count(*) FROM pg_trigger WHERE tgname = 'trg_ecs_warehouses_updated'")
        ).scalar_one()
        == 1
    )
