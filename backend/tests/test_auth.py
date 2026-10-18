"""API key protection (#14): every route needs X-API-Key except the explicitly open ones."""

import re

import pytest
from fastapi.testclient import TestClient

from app import db
from app.config import get_settings
from app.main import app

KEY = "test-api-key"
# Open on purpose: health checks, and Shopify webhooks (protected by their HMAC signature).
OPEN_ROUTES = {"/health", "/webhooks/orders"}

client = TestClient(app)


def protected_routes():
    """Every API endpoint in the OpenAPI schema except the open ones, with path
    parameters filled in. A new endpoint is covered automatically, so it can't ship
    unprotected by accident."""
    return [
        (method.upper(), re.sub(r"\{[^}]+\}", "1", path))
        for path, operations in app.openapi()["paths"].items()
        if path not in OPEN_ROUTES
        for method in operations
    ]


@pytest.fixture
def key_required(monkeypatch):
    monkeypatch.setattr(get_settings(), "api_shared_secret", KEY)


def test_protected_routes_are_found():
    found = {path for _, path in protected_routes()}
    assert {"/orders", "/orders/1", "/orders/1/status", "/orders/import", "/sync/orders"} <= found


@pytest.mark.usefixtures("key_required")
@pytest.mark.parametrize(("method", "path"), protected_routes())
@pytest.mark.parametrize("headers", [{}, {"X-API-Key": "wrong"}, {"X-API-Key": ""}])
def test_data_routes_reject_missing_or_wrong_key(method, path, headers):
    response = client.request(method, path, headers=headers, json={"status": "shipped"})
    assert response.status_code == 401
    assert response.json() == {"detail": "Missing or invalid X-API-Key"}


@pytest.mark.usefixtures("key_required", "clean_db")
def test_correct_key_is_accepted():
    response = client.get("/orders", headers={"X-API-Key": KEY})
    assert response.status_code == 200 and response.json() == {"total": 0, "orders": []}


@pytest.mark.usefixtures("key_required")
def test_open_routes_need_no_key(monkeypatch):
    monkeypatch.setattr(db, "check_db", lambda: True)
    assert client.get("/health").status_code == 200
    # The webhook is judged by Shopify's signature, not the API key.
    response = client.post(
        "/webhooks/orders", content=b"{}", headers={"X-Shopify-Hmac-Sha256": "x"}
    )
    assert response.json() == {"detail": "Invalid webhook signature"}


def test_production_without_a_key_configured_refuses(monkeypatch):
    monkeypatch.setattr(get_settings(), "environment", "production")
    response = client.get("/orders")
    assert response.status_code == 503
    assert response.json() == {"detail": "API_SHARED_SECRET is not configured"}


@pytest.mark.usefixtures("clean_db")
def test_development_without_a_key_configured_is_open():
    assert client.get("/orders").status_code == 200


def test_docs_offer_an_authorize_button():
    schemes = client.get("/openapi.json").json()["components"]["securitySchemes"]
    assert schemes["APIKeyHeader"] == {"type": "apiKey", "in": "header", "name": "X-API-Key"}
