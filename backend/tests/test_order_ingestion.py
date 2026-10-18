"""The shared processing step and import runs, against the mvp_test database."""

import copy
from datetime import timedelta

import httpx
import pytest
from sqlalchemy import func, select, update

from app.db import SessionLocal
from app.models import (
    ChannelConnection,
    OrderHeader,
    OrderHistory,
    OrderImport,
    OrderImportRow,
    OrderLine,
    OrderLineHistory,
)
from app.services import order_ingestion as ingestion
from app.services.shopify import ShopifyClient, ShopifyError, StaticTokenAuth
from tests.shopify_orders import fulfilled, line_item, shopify_order

pytestmark = pytest.mark.usefixtures("clean_db")


class FakeShopify:
    """Stands in for ShopifyClient: serves orders, optionally failing after some."""

    def __init__(self, orders, fail_after=None, scopes=("read_orders", "read_all_orders")):
        self.orders = orders
        self.fail_after = fail_after
        self.scopes = set(scopes)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return None

    def access_scopes(self):
        return self.scopes

    def paginate(self, path, key, params=None):
        for index, order in enumerate(self.orders):
            if self.fail_after is not None and index == self.fail_after:
                raise ShopifyError("Shopify GET /orders.json failed with 503", 503, "Unavailable")
            yield copy.deepcopy(order)


def new_run(method="manual_sync") -> int:
    with SessionLocal.begin() as session:
        connection = ingestion.ensure_shopify_connection(session)
        return ingestion.start_import(
            session, connection.channel_connection_id, method
        ).order_import_id


def import_orders(orders, **fake_kwargs) -> OrderImport:
    run_id = new_run()
    ingestion.run_shopify_order_import(
        run_id, client_factory=lambda: FakeShopify(orders, **fake_kwargs)
    )
    with SessionLocal() as session:
        return session.get(OrderImport, run_id)


def counts(run: OrderImport) -> dict[str, int]:
    return {
        k: getattr(run, k)
        for k in (
            "total_records",
            "processed_records",
            "created_count",
            "updated_count",
            "unchanged_count",
            "skipped_count",
            "failed_count",
        )
    }


def order_by_external_id(session, external_id="5001") -> OrderHeader:
    return session.scalars(
        select(OrderHeader).where(OrderHeader.external_order_id == external_id)
    ).one()


def test_import_creates_orders_lines_and_history():
    run = import_orders([shopify_order(5001), shopify_order(5002)])

    assert run.status == "completed" and run.started_at and run.completed_at
    assert counts(run) == {
        "total_records": 2,
        "processed_records": 2,
        "created_count": 2,
        "updated_count": 0,
        "unchanged_count": 0,
        "skipped_count": 0,
        "failed_count": 0,
    }
    with SessionLocal() as session:
        order = order_by_external_id(session)
        assert order.channel == "shopify" and order.ingestion_method == "manual_sync"
        assert order.created_by_import_id == run.order_import_id == order.last_import_id
        assert order.channel_connection_id is not None and order.store_id == 1
        assert len(order.lines) == 1 and order.lines[0].ordered_qty == 2
        history = order.history
        assert [(h.action_type, h.change_source) for h in history] == [("created", "manual_sync")]
        assert history[0].order_import_id == run.order_import_id
        assert history[0].order_import_row_id is not None
        line_history = session.scalars(select(OrderLineHistory)).all()
        assert {h.action_type for h in line_history} == {"created"}
        rows = session.scalars(select(OrderImportRow).order_by(OrderImportRow.record_number)).all()
        assert [r.action_taken for r in rows] == ["created", "created"]
        assert rows[0].order_header_id == order.order_header_id
        assert rows[0].raw_payload["id"] == 5001 and len(rows[0].payload_hash) == 64


def test_second_import_reports_unchanged_and_creates_no_duplicates():
    import_orders([shopify_order(5001), shopify_order(5002)])
    run = import_orders([shopify_order(5001), shopify_order(5002)])

    assert run.status == "completed"
    assert (run.unchanged_count, run.created_count, run.updated_count) == (2, 0, 0)
    with SessionLocal() as session:
        assert session.scalar(select(func.count()).select_from(OrderHeader)) == 2
        assert session.scalar(select(func.count()).select_from(OrderHistory)) == 2


def test_newer_version_updates_order_and_writes_history():
    import_orders([shopify_order()])
    newer = fulfilled(shopify_order(updated_at="2026-10-18T10:05:00+05:30", note="Leave at door"))
    run = import_orders([newer])

    assert run.updated_count == 1
    with SessionLocal() as session:
        order = order_by_external_id(session)
        assert order.order_status == "shipped" and order.version_no == 2
        assert order.customer_notes == "Leave at door"
        assert order.last_import_id == run.order_import_id
        latest = order.history[-1]
        assert latest.action_type == "status_change"
        assert (latest.old_status, latest.new_status) == ("pending", "shipped")
        assert latest.changed_fields["customer_notes"] == ["Gift wrap please", "Leave at door"]
        line = order.lines[0]
        assert line.line_status == "shipped" and line.version_no == 2
        assert line.history[-1].action_type == "shipped"


def test_older_version_is_skipped_as_stale():
    import_orders([shopify_order(updated_at="2026-10-18T12:00:00+05:30", note="newest")])
    run = import_orders([shopify_order(updated_at="2026-10-18T09:00:00+05:30", note="older")])

    assert run.skipped_count == 1
    with SessionLocal() as session:
        assert order_by_external_id(session).customer_notes == "newest"


def test_newer_payload_with_no_relevant_change_is_unchanged():
    import_orders([shopify_order()])
    run = import_orders(
        [shopify_order(updated_at="2026-10-18T09:00:00+05:30", tags="vip")]
    )  # a field we don't store
    assert run.unchanged_count == 1
    with SessionLocal() as session:
        order = order_by_external_id(session)
        assert order.version_no == 1 and len(order.history) == 1


def test_manual_shipped_is_not_reverted_by_shopify():
    import_orders([shopify_order()])
    with SessionLocal.begin() as session:  # what "Mark shipped" (#10/#18) will do
        order = order_by_external_id(session)
        order.lines[0].line_status = "shipped"
        order.lines[0].shipped_qty = 2
        order.order_status = "shipped"

    run = import_orders(
        [shopify_order(updated_at="2026-10-18T09:00:00+05:30", note="still unfulfilled on Shopify")]
    )
    assert run.updated_count == 1
    with SessionLocal() as session:
        order = order_by_external_id(session)
        assert order.order_status == "shipped"
        assert order.lines[0].line_status == "shipped" and order.lines[0].shipped_qty == 2
        assert order.customer_notes == "still unfulfilled on Shopify"


def test_cancellation_from_shopify():
    import_orders([shopify_order()])
    run = import_orders(
        [
            shopify_order(
                updated_at="2026-10-18T09:00:00+05:30",
                cancelled_at="2026-10-18T09:00:00+05:30",
                cancel_reason="customer",
            )
        ]
    )
    assert run.updated_count == 1
    with SessionLocal() as session:
        order = order_by_external_id(session)
        assert order.order_status == "cancelled" and order.history[-1].action_type == "cancelled"
        assert order.lines[0].line_status == "cancelled"
        assert order.lines[0].history[-1].action_type == "cancelled"


def test_order_edit_adds_and_removes_lines():
    import_orders([shopify_order()])
    edited = shopify_order(
        updated_at="2026-10-18T09:00:00+05:30",
        line_items=[line_item(9002, sku="STRAP-01", title="Strap")],
    )
    import_orders([edited])

    with SessionLocal() as session:
        order = order_by_external_id(session)
        by_id = {line.external_line_id: line for line in order.lines}
        assert by_id["9001"].line_status == "cancelled"  # gone from Shopify, kept here
        assert by_id["9001"].cancelled_qty == 2
        assert by_id["9002"].line_number == 2 and by_id["9002"].line_status == "pending"
        assert "line 2" in order.history[-1].changed_fields["lines"]


def test_catalog_link_survives_reimport():
    import_orders([shopify_order()])
    with SessionLocal.begin() as session:  # what the product import (#13) will do
        order_by_external_id(session).lines[0].variant_id = 42
    import_orders([fulfilled(shopify_order(updated_at="2026-10-18T09:00:00+05:30"))])
    with SessionLocal() as session:
        assert order_by_external_id(session).lines[0].variant_id == 42


def test_bad_order_fails_alone_and_run_is_partially_completed():
    run = import_orders(
        [shopify_order(5001), shopify_order(5002, total_price="abc"), shopify_order(5003)]
    )

    assert run.status == "partially_completed"
    assert (run.created_count, run.failed_count) == (2, 1)
    assert run.error_summary == "1 of 3 orders failed."
    with SessionLocal() as session:
        failed = session.scalars(
            select(OrderImportRow).where(OrderImportRow.status == "failed")
        ).one()
        assert failed.external_order_id == "5002" and failed.action_taken == "failed"
        assert "total_price is not a number" in failed.error_messages[0]
        assert failed.order_header_id is None


def test_every_order_failing_fails_the_run():
    run = import_orders([shopify_order(5001, id=None)])
    assert run.status == "failed" and run.failed_count == 1


def test_order_number_clash_with_a_different_order_fails_that_row():
    run = import_orders([shopify_order(5001), shopify_order(5002, name="#1001")])
    assert (run.created_count, run.failed_count) == (1, 1)
    with SessionLocal() as session:
        failed = session.scalars(
            select(OrderImportRow).where(OrderImportRow.status == "failed")
        ).one()
        assert failed.error_messages == ["Order number is already used by a different order"]


def test_shopify_failure_midway_keeps_processed_orders():
    run = import_orders(
        [shopify_order(5001), shopify_order(5002), shopify_order(5003)], fail_after=2
    )
    assert run.status == "failed" and run.created_count == 2
    assert "Stopped after 2 orders" in run.error_summary and "503" in run.error_summary


def test_protected_customer_data_error_gets_a_fix_hint():
    class Blocked(FakeShopify):
        def paginate(self, *args, **kwargs):
            raise ShopifyError(
                "Shopify GET /orders.json failed with 403",
                403,
                '{"errors":"This app is not approved to access REST endpoints '
                'with protected customer data."}',
            )
            yield  # pragma: no cover

    run_id = new_run()
    ingestion.run_shopify_order_import(run_id, client_factory=lambda: Blocked([]))
    with SessionLocal() as session:
        run = session.get(OrderImport, run_id)
    assert run.status == "failed" and "request protected customer data access" in run.error_summary


def test_more_than_250_orders_through_the_real_client():
    """Real ShopifyClient pagination over a mocked Shopify with 3 pages."""
    orders = [shopify_order(10_000 + i, name=f"#{10_000 + i}") for i in range(260)]
    base = "https://test-store.myshopify.com/admin/api/2026-10/orders.json"

    def handler(request):
        page = request.url.params.get("page_info", "1")
        start = (int(page) - 1) * 250
        headers = (
            {"Link": f'<{base}?limit=250&page_info={int(page) + 1}>; rel="next"'}
            if page == "1"
            else {}
        )
        return httpx.Response(200, json={"orders": orders[start : start + 250]}, headers=headers)

    def client():
        return ShopifyClient(
            "test-store.myshopify.com",
            "2026-10",
            StaticTokenAuth("t"),
            transport=httpx.MockTransport(handler),
        )

    run_id = new_run()
    ingestion.run_shopify_order_import(run_id, client_factory=client)
    with SessionLocal() as session:
        run = session.get(OrderImport, run_id)
        assert (run.status, run.created_count) == ("completed", 260)
        assert session.scalar(select(func.count()).select_from(OrderHeader)) == 260


def test_nul_characters_in_payload_are_stripped():
    run = import_orders([shopify_order(note="bad\x00byte")])
    assert run.created_count == 1
    with SessionLocal() as session:
        assert order_by_external_id(session).customer_notes == "badbyte"


def test_only_one_import_at_a_time():
    first = new_run()
    with pytest.raises(ingestion.ImportAlreadyRunning) as exc_info:
        new_run()
    assert exc_info.value.order_import_id == first


def test_abandoned_run_is_failed_and_a_new_one_starts():
    first = new_run()
    with SessionLocal.begin() as session:
        session.execute(
            update(OrderImport)
            .where(OrderImport.order_import_id == first)
            .values(status="processing", updated_at=func.now() - timedelta(hours=1))
        )
    second = new_run()
    with SessionLocal() as session:
        abandoned = session.get(OrderImport, first)
        assert abandoned.status == "failed" and "Abandoned" in abandoned.error_summary
        assert second != first


def test_processing_a_row_twice_does_nothing_the_second_time():
    run_id = new_run()
    row_id = ingestion.record_import_row(SessionLocal, run_id, 1, shopify_order())
    assert ingestion.process_import_row(row_id) == "created"
    assert ingestion.process_import_row(row_id) == "created"
    with SessionLocal() as session:
        assert session.get(OrderImport, run_id).created_count == 1
        assert session.scalar(select(func.count()).select_from(OrderLine)) == 1


def test_concurrently_created_order_is_retried_as_an_update(monkeypatch):
    """Simulate a webhook creating the same order between our lookup and our insert."""
    import_orders([shopify_order()])
    run_id = new_run()
    row_id = ingestion.record_import_row(
        SessionLocal, run_id, 1, shopify_order(updated_at="2026-10-18T09:00:00+05:30", note="x")
    )
    original = ingestion.apply_order_row
    calls = {"n": 0}

    def first_call_sees_no_order(session, run, row):
        calls["n"] += 1
        if calls["n"] == 1:  # pretend the lookup missed: inserting collides
            mapped = ingestion.map_shopify_order(row.raw_payload)
            ingestion.create_order(session, run, row, mapped)
            session.flush()
        return original(session, run, row)

    monkeypatch.setattr(ingestion, "apply_order_row", first_call_sees_no_order)
    assert ingestion.process_import_row(row_id) == "updated"
    assert calls["n"] == 2


def test_connection_row_is_created_once_and_names_env_vars():
    with SessionLocal.begin() as session:
        first = ingestion.ensure_shopify_connection(session)
        again = ingestion.ensure_shopify_connection(session)
        assert first.channel_connection_id == again.channel_connection_id
        assert first.credential_ref.startswith("env:") and "shpss" not in first.credential_ref
    with SessionLocal() as session:
        assert session.scalar(select(func.count()).select_from(ChannelConnection)) == 1
