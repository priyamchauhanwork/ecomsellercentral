"""POST /webhooks/orders (#11) and the webhook registration script."""

import base64
import hashlib
import hmac
import json
import sys

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.orm import selectinload

from app.config import get_settings
from app.db import SessionLocal
from app.main import app
from app.models import ChannelConnection, OrderHeader, OrderImport, OrderImportRow
from app.services import order_ingestion as ingestion
from scripts import register_webhooks
from tests.shopify_orders import shopify_order

pytestmark = pytest.mark.usefixtures("clean_db")
client = TestClient(app)
SECRET = "test-webhook-secret"
SHOP = "test-store.myshopify.com"


@pytest.fixture(autouse=True)
def shopify_settings(monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "shopify_api_secret", SECRET)
    monkeypatch.setattr(settings, "shopify_webhook_secret", "")
    monkeypatch.setattr(settings, "shopify_store_domain", SHOP)
    return settings


def sign(body: bytes, secret: str = SECRET) -> str:
    return base64.b64encode(hmac.new(secret.encode(), body, hashlib.sha256).digest()).decode()


def deliver(payload, topic="orders/create", delivery_id="wh-1", signature=None, shop=SHOP):
    body = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
    headers = {
        "Content-Type": "application/json",
        "X-Shopify-Topic": topic,
        "X-Shopify-Shop-Domain": shop,
        "X-Shopify-Webhook-Id": delivery_id,
        "X-Shopify-Hmac-Sha256": sign(body) if signature is None else signature,
    }
    return client.post("/webhooks/orders", content=body, headers=headers)


def count(model) -> int:
    with SessionLocal() as session:
        return session.scalar(select(func.count()).select_from(model))


def the_order() -> OrderHeader:
    with SessionLocal() as session:
        return session.scalars(
            select(OrderHeader).options(
                selectinload(OrderHeader.lines), selectinload(OrderHeader.history)
            )
        ).one()


def test_new_order_is_recorded_and_processed():
    response = deliver(shopify_order())
    assert response.status_code == 200 and response.json()["status"] == "accepted"

    order = the_order()
    assert order.order_number == "#1001" and order.ingestion_method == "webhook"
    assert len(order.lines) == 1
    assert [(h.action_type, h.change_source) for h in order.history] == [("created", "webhook")]
    with SessionLocal() as session:
        run = session.get(OrderImport, response.json()["order_import_id"])
        assert (run.ingestion_method, run.event_topic, run.external_event_id) == (
            "webhook",
            "orders/create",
            "wh-1",
        )
        assert (run.status, run.created_count, run.total_records) == ("completed", 1, 1)


def test_update_then_older_update_is_stale():
    deliver(shopify_order())
    newer = shopify_order(updated_at="2026-10-18T12:00:00+05:30", note="newer")
    assert deliver(newer, "orders/updated", "wh-2").json()["status"] == "accepted"
    older = shopify_order(updated_at="2026-10-18T09:00:00+05:30", note="older")
    deliver(older, "orders/updated", "wh-3")

    order = the_order()
    assert order.customer_notes == "newer" and order.version_no == 2
    assert order.history[-1].change_source == "webhook"
    with SessionLocal() as session:
        actions = session.scalars(
            select(OrderImportRow.action_taken).order_by(OrderImportRow.order_import_row_id)
        ).all()
    assert actions == ["created", "updated", "skipped_stale"]


@pytest.mark.parametrize(
    "signature",
    ["", "not-base64", sign(b"a different body"), sign(b"{}", "wrong-secret")],
)
def test_missing_or_invalid_signature_is_401_and_writes_nothing(signature):
    response = deliver(shopify_order(), signature=signature)
    assert response.status_code == 401
    assert count(OrderImport) == 0 and count(ChannelConnection) == 0


def test_repeat_delivery_is_recorded_once():
    assert deliver(shopify_order(), delivery_id="same").json()["status"] == "accepted"
    assert deliver(shopify_order(), delivery_id="same").json() == {"status": "duplicate"}
    assert count(OrderImport) == 1 and count(OrderHeader) == 1


def test_same_order_in_different_deliveries_is_not_duplicated():
    deliver(shopify_order(), delivery_id="a")
    deliver(shopify_order(), "orders/updated", delivery_id="b")
    assert count(OrderImport) == 2 and count(OrderHeader) == 1


def test_unsupported_topic_is_acknowledged_and_ignored():
    response = deliver({"id": 1}, topic="customers/create")
    assert response.status_code == 200 and response.json()["status"] == "ignored"
    assert count(OrderImport) == 0


def test_other_store_is_ignored():
    response = deliver(shopify_order(), shop="someone-else.myshopify.com")
    assert response.json()["status"] == "ignored" and count(OrderImport) == 0


def test_body_that_is_not_json_is_400():
    assert deliver(b"not json").status_code == 400
    assert count(OrderImport) == 0


def test_no_secret_configured_is_503(shopify_settings, monkeypatch):
    monkeypatch.setattr(shopify_settings, "shopify_api_secret", "")
    assert deliver(shopify_order()).status_code == 503


def test_explicit_webhook_secret_overrides_api_secret(shopify_settings, monkeypatch):
    monkeypatch.setattr(shopify_settings, "shopify_webhook_secret", "override")
    body = json.dumps(shopify_order()).encode()
    assert deliver(body, signature=sign(body)).status_code == 401
    assert deliver(body, signature=sign(body, "override")).status_code == 200


def test_bad_order_is_accepted_and_recorded_as_failed():
    response = deliver(shopify_order(total_price="abc"))
    assert response.status_code == 200  # we have it; Shopify must not retry
    with SessionLocal() as session:
        row = session.scalars(select(OrderImportRow)).one()
        run = session.get(OrderImport, row.order_import_id)
    assert row.status == "failed" and "total_price is not a number" in row.error_messages[0]
    assert run.status == "failed" and count(OrderHeader) == 0


def test_webhooks_and_imports_do_not_block_each_other():
    with SessionLocal.begin() as session:  # an import in progress
        connection = ingestion.ensure_shopify_connection(session)
        ingestion.start_import(session, connection.channel_connection_id, "manual_sync")
    assert deliver(shopify_order()).json()["status"] == "accepted"

    with SessionLocal.begin() as session:  # a webhook run still pending...
        ingestion.record_webhook_delivery(session, "orders/create", "wh-pending", shopify_order(2))
        session.execute(
            OrderImport.__table__.update()
            .where(OrderImport.ingestion_method == "manual_sync")
            .values(status="completed")
        )
    with SessionLocal.begin() as session:  # ...doesn't block a new import
        ingestion.start_import(session, connection.channel_connection_id, "manual_sync")


# --- scripts/register_webhooks ------------------------------------------------------------


class FakeGraphQL:
    def __init__(self, existing=()):
        self.subscriptions = {t: {"id": f"gid://{t}", "topic": t, "uri": u} for t, u in existing}
        self.calls = []

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return None

    def graphql(self, query, variables=None):
        if "webhookSubscriptions(" in query:
            return {"webhookSubscriptions": {"nodes": list(self.subscriptions.values())}}
        name = query.split("{", 2)[1].split("(")[0].strip()
        self.calls.append((name, variables))
        if name == "webhookSubscriptionCreate":
            self.subscriptions[variables["topic"]] = {
                "id": "gid://new",
                "topic": variables["topic"],
                "uri": variables["uri"],
            }
        return {name: {"userErrors": []}}


def run_script(monkeypatch, fake, *args):
    monkeypatch.setattr(register_webhooks, "get_shopify_client", lambda: fake)
    monkeypatch.setattr(sys, "argv", ["register_webhooks", *args])
    return register_webhooks.main()


def connection_flags():
    with SessionLocal() as session:
        connection = session.scalars(select(ChannelConnection)).one()
        return connection.webhook_enabled, connection.webhook_topics


def test_register_creates_both_topics_and_records_them(monkeypatch):
    fake = FakeGraphQL()
    assert run_script(monkeypatch, fake, "https://abc.example/webhooks/orders") == 0
    assert [c[0] for c in fake.calls] == ["webhookSubscriptionCreate"] * 2
    assert connection_flags() == (True, ["orders/create", "orders/updated"])


def test_register_again_keeps_or_moves_subscriptions(monkeypatch):
    url = "https://abc.example/webhooks/orders"
    same = FakeGraphQL([("ORDERS_CREATE", url), ("ORDERS_UPDATED", url)])
    assert run_script(monkeypatch, same, url) == 0 and same.calls == []

    moved = FakeGraphQL([("ORDERS_CREATE", "https://old/webhooks/orders")])
    run_script(monkeypatch, moved, url)
    assert [c[0] for c in moved.calls] == ["webhookSubscriptionUpdate", "webhookSubscriptionCreate"]


def test_remove_deletes_subscriptions(monkeypatch):
    fake = FakeGraphQL([("ORDERS_CREATE", "https://x/webhooks/orders")])
    assert run_script(monkeypatch, fake, "--remove") == 0
    assert [c[0] for c in fake.calls] == ["webhookSubscriptionDelete"]
    assert connection_flags() == (False, [])


def test_register_rejects_non_https_urls(monkeypatch):
    with pytest.raises(SystemExit):
        run_script(monkeypatch, FakeGraphQL(), "http://localhost:8000/webhooks/orders")
