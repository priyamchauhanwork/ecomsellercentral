"""POST /sync/orders and the sync cursor (#12)."""

from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from app.config import get_settings
from app.db import SessionLocal
from app.main import app
from app.models import ChannelAccount, OrderHeader, OrderImport
from app.services import order_ingestion as ingestion
from tests.shopify_orders import shopify_order
from tests.test_order_ingestion import FakeShopify, import_orders, new_run

pytestmark = pytest.mark.usefixtures("clean_db")
client = TestClient(app)


@pytest.fixture(autouse=True)
def no_api_key(monkeypatch):
    monkeypatch.setattr(get_settings(), "api_shared_secret", "")


def use_shopify(monkeypatch, orders, **kwargs) -> FakeShopify:
    fake = FakeShopify(orders, **kwargs)
    monkeypatch.setattr(ingestion, "shopify_client_factory", lambda: fake)
    return fake


def sync(trigger=None, **headers):
    url = "/sync/orders" + (f"?trigger={trigger}" if trigger else "")
    return client.post(url, headers=headers)


def connection() -> ChannelAccount:
    with SessionLocal() as session:
        return session.scalars(select(ChannelAccount)).one()


def set_connection(**values):
    with SessionLocal.begin() as session:
        conn = ingestion.ensure_shopify_account(session)
        for key, value in values.items():
            setattr(conn, key, value)


def test_first_sync_fetches_everything_and_saves_the_cursor(monkeypatch):
    fake = use_shopify(
        monkeypatch,
        [
            shopify_order(5001, updated_at="2026-10-18T09:00:00+05:30"),
            shopify_order(5002, updated_at="2026-10-18T11:00:00+05:30"),
        ],
    )
    response = sync()
    assert response.status_code == 202
    body = response.json()
    assert body["result"] == "started" and body["run"]["ingestion_method"] == "scheduled_sync"
    assert fake.params == {"status": "any"}  # nothing to resume from

    conn = connection()
    assert conn.sync_cursor == "2026-10-18T05:30:00+00:00"  # newest Shopify updated_at seen
    assert conn.last_sync_status == "completed" and conn.last_synced_at is not None
    with SessionLocal() as session:
        run = session.get(OrderImport, body["run"]["order_import_id"])
        assert run.created_count == 2 and run.cursor_start is None
        assert run.cursor_end == conn.sync_cursor


def test_next_sync_resumes_from_cursor_minus_overlap(monkeypatch):
    use_shopify(monkeypatch, [shopify_order(5001, updated_at="2026-10-18T11:00:00+05:30")])
    sync()
    fake = use_shopify(
        monkeypatch,
        [
            shopify_order(5001, updated_at="2026-10-18T11:00:00+05:30"),  # in the overlap
            shopify_order(5002, updated_at="2026-10-18T12:00:00+05:30"),  # missed webhook
        ],
    )
    body = sync("manual").json()
    assert fake.params["updated_at_min"] == "2026-10-18T05:25:00+00:00"  # cursor − 5 min
    with SessionLocal() as session:
        run = session.get(OrderImport, body["run"]["order_import_id"])
        assert (run.created_count, run.unchanged_count) == (1, 1)  # never duplicated
        assert run.sync_from == datetime(2026, 10, 18, 5, 25, tzinfo=UTC)
        assert session.scalar(select(func.count()).select_from(OrderHeader)) == 2
    assert connection().sync_cursor == "2026-10-18T06:30:00+00:00"


def test_without_a_cursor_it_resumes_from_the_newest_stored_order(monkeypatch):
    import_orders([shopify_order(5001, updated_at="2026-10-18T11:00:00+05:30")])
    set_connection(sync_cursor=None)
    fake = use_shopify(monkeypatch, [])
    sync("manual")
    assert fake.params["updated_at_min"] == "2026-10-18T05:25:00+00:00"


def test_an_unreadable_cursor_falls_back_to_the_newest_stored_order(monkeypatch):
    import_orders([shopify_order(5001, updated_at="2026-10-18T11:00:00+05:30")])
    set_connection(sync_cursor="garbage")
    fake = use_shopify(monkeypatch, [])
    sync("manual")
    assert fake.params["updated_at_min"] == "2026-10-18T05:25:00+00:00"


def test_the_order_import_also_moves_the_cursor():
    import_orders([shopify_order(5001, updated_at="2026-10-18T11:00:00+05:30")])
    assert connection().sync_cursor == "2026-10-18T05:30:00+00:00"


def test_failed_sync_keeps_cursor_and_last_synced_at(monkeypatch):
    use_shopify(monkeypatch, [shopify_order(5001, updated_at="2026-10-18T11:00:00+05:30")])
    sync()
    before = connection()
    use_shopify(
        monkeypatch,
        [shopify_order(5002, updated_at="2026-10-18T13:00:00+05:30"), shopify_order(5003)],
        fail_after=1,
    )
    assert sync("manual").json()["result"] == "started"
    after = connection()
    assert after.sync_cursor == before.sync_cursor
    assert after.last_synced_at == before.last_synced_at
    assert after.last_sync_status == "failed"


def test_sync_where_every_order_fails_keeps_the_cursor(monkeypatch):
    use_shopify(
        monkeypatch, [shopify_order(5001, updated_at="2026-10-18T11:00:00+05:30", total_price="x")]
    )
    sync()
    conn = connection()
    assert conn.sync_cursor is None and conn.last_sync_status == "failed"


def test_partially_completed_sync_moves_the_cursor(monkeypatch):
    use_shopify(
        monkeypatch,
        [
            shopify_order(5001, updated_at="2026-10-18T11:00:00+05:30"),
            shopify_order(5002, updated_at="2026-10-18T12:00:00+05:30", total_price="x"),
        ],
    )
    sync()
    conn = connection()
    assert conn.last_sync_status == "partially_completed"
    assert conn.sync_cursor == "2026-10-18T06:30:00+00:00"


def test_cursor_never_moves_backwards(monkeypatch):
    set_connection(sync_cursor="2026-10-18T10:00:00+00:00")
    use_shopify(monkeypatch, [shopify_order(5001, updated_at="2026-10-18T15:20:00+05:30")])
    sync("manual")
    assert connection().sync_cursor == "2026-10-18T10:00:00+00:00"


def test_scheduled_sync_waits_for_the_interval_but_manual_does_not(monkeypatch):
    use_shopify(monkeypatch, [])
    assert sync().status_code == 202
    response = sync()
    assert response.status_code == 200 and response.json()["result"] == "not_due"
    assert response.json()["next_due_at"] is not None
    assert sync("manual").status_code == 202

    set_connection(last_synced_at=datetime.now(UTC) - timedelta(minutes=14, seconds=30))
    assert sync().status_code == 202  # within the 1-minute grace of the 15-minute interval


def test_overlapping_call_returns_the_running_run():
    running = new_run()
    response = sync("manual")
    assert response.status_code == 200
    assert response.json()["result"] == "already_running"
    assert response.json()["run"]["order_import_id"] == running
    with SessionLocal() as session:
        assert session.scalar(select(func.count()).select_from(OrderImport)) == 1


def test_disabled_sync_does_nothing(monkeypatch):
    fake = use_shopify(monkeypatch, [shopify_order()])
    set_connection(sync_enabled=False)
    response = sync("manual")
    assert response.status_code == 200 and response.json() == {
        "result": "disabled",
        "run": None,
        "next_due_at": None,
    }
    assert fake.params is None


def test_invalid_trigger_is_422():
    assert sync("hourly").status_code == 422


def test_shopify_not_configured_is_503(monkeypatch):
    monkeypatch.setattr(get_settings(), "shopify_store_domain", "")
    assert sync().status_code == 503


@pytest.mark.parametrize(
    ("header", "expected"),
    [(None, 401), ("wrong", 401), ("sync-key", 202)],
)
def test_api_key_is_required_when_configured(monkeypatch, header, expected):
    use_shopify(monkeypatch, [])
    monkeypatch.setattr(get_settings(), "api_shared_secret", "sync-key")
    headers = {"X-API-Key": header} if header else {}
    assert sync(**headers).status_code == expected


def test_production_without_api_key_configured_is_503(monkeypatch):
    monkeypatch.setattr(get_settings(), "environment", "production")
    assert sync().status_code == 503


def test_a_non_object_entry_fails_alone(monkeypatch):
    use_shopify(monkeypatch, ["not an order", shopify_order(5001)])
    run = sync().json()["run"]
    with SessionLocal() as session:
        run = session.get(OrderImport, run["order_import_id"])
    assert (run.status, run.created_count, run.failed_count) == ("partially_completed", 1, 1)
