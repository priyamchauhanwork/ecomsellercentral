import pytest
from fastapi.testclient import TestClient

from app.db import SessionLocal
from app.main import app
from app.services import order_ingestion as ingestion
from app.services.shopify import ShopifyConfigError, ShopifyError
from tests.shopify_orders import shopify_order
from tests.test_order_ingestion import FakeShopify, new_run

pytestmark = pytest.mark.usefixtures("clean_db")
client = TestClient(app)


def use_shopify(monkeypatch, fake):
    monkeypatch.setattr(ingestion, "shopify_client_factory", lambda: fake)


def test_post_import_runs_in_background_and_reports_progress(monkeypatch):
    use_shopify(
        monkeypatch, FakeShopify([shopify_order(5001), shopify_order(5002, total_price="x")])
    )

    response = client.post("/orders/import")
    assert response.status_code == 202
    started = response.json()
    assert started["status"] == "pending" and started["warnings"] == []

    # TestClient runs background tasks before returning, so the run has finished.
    run = client.get(f"/orders/imports/{started['order_import_id']}").json()
    assert run["status"] == "partially_completed"
    assert (run["total_records"], run["created_count"], run["failed_count"]) == (2, 1, 1)

    failed = client.get(f"/orders/imports/{started['order_import_id']}/rows?status=failed").json()
    assert len(failed) == 1 and failed[0]["external_order_id"] == "5002"
    assert "total_price" in failed[0]["error_messages"][0]


def test_missing_read_all_orders_scope_returns_a_warning(monkeypatch):
    use_shopify(monkeypatch, FakeShopify([], scopes=["read_orders"]))
    response = client.post("/orders/import")
    assert response.status_code == 202
    assert "60 days" in response.json()["warnings"][0]


def test_missing_read_orders_scope_is_rejected(monkeypatch):
    use_shopify(monkeypatch, FakeShopify([], scopes=["read_products"]))
    response = client.post("/orders/import")
    assert response.status_code == 503 and "read_orders" in response.json()["detail"]


def test_second_import_while_one_runs_is_409(monkeypatch):
    use_shopify(monkeypatch, FakeShopify([]))
    running = new_run()
    response = client.post("/orders/import")
    assert response.status_code == 409
    assert response.json()["detail"]["order_import_id"] == running


def test_shopify_not_configured_is_503(monkeypatch):
    def not_configured():
        raise ShopifyConfigError("Missing in .env: SHOPIFY_STORE_DOMAIN")

    monkeypatch.setattr(ingestion, "shopify_client_factory", not_configured)
    response = client.post("/orders/import")
    assert response.status_code == 503 and "SHOPIFY_STORE_DOMAIN" in response.json()["detail"]


def test_shopify_rejecting_credentials_is_502(monkeypatch):
    class Rejected(FakeShopify):
        def access_scopes(self):
            raise ShopifyError("Could not get a Shopify access token (401)", 401)

    use_shopify(monkeypatch, Rejected([]))
    assert client.post("/orders/import").status_code == 502


def test_unknown_import_run_is_404():
    assert client.get("/orders/imports/999999").status_code == 404
    assert client.get("/orders/imports/999999/rows").status_code == 404


def test_no_run_is_created_when_the_request_is_rejected(monkeypatch):
    use_shopify(monkeypatch, FakeShopify([], scopes=[]))
    client.post("/orders/import")
    with SessionLocal() as session:
        from sqlalchemy import func, select

        from app.models import OrderImport

        assert session.scalar(select(func.count()).select_from(OrderImport)) == 0
