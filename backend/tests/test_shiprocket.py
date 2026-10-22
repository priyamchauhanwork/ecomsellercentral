"""Shiprocket connection and token management (#57), against a fake Shiprocket."""

import base64
import json
import logging
import threading
import time

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select, text

from app.config import get_settings
from app.db import SessionLocal
from app.main import app
from app.models import ChannelAccount
from app.services import shiprocket, shiprocket_connection
from app.services.shiprocket import (
    LOGIN_COOLDOWN,
    TOKEN_LIFETIME,
    TOKEN_RENEW_MARGIN,
    ShiprocketAuthError,
    ShiprocketClient,
    ShiprocketError,
    TokenManager,
)

client = TestClient(app)
BASE = "https://api-sandbox.shiprocket.in/v1/external"
EMAIL = "api+oms@example.com"
PASSWORD = "s3cret-api-password"


def jwt(exp: float) -> str:
    def part(data):
        return base64.urlsafe_b64encode(json.dumps(data).encode()).rstrip(b"=").decode()

    return f"{part({'alg': 'HS256'})}.{part({'exp': int(exp)})}.signature"


class FakeShiprocket:
    """Shiprocket's login and a few read endpoints, with switches for failures."""

    def __init__(self):
        self.password = PASSWORD
        self.logins = 0
        self.valid_tokens: set[str] = set()
        self.calls: list[tuple[str, str]] = []
        # Next non-login responses to fail: a status code or "timeout".
        self.failures: list[int | str] = []
        self.wallet_status = 200
        self.login_delay = 0.0

    def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path.removeprefix("/v1/external")
        self.calls.append((request.method, path))
        if self.failures and path != "/auth/login":
            failure = self.failures.pop(0)
            if failure == "timeout":
                raise httpx.ConnectTimeout("timed out", request=request)
            return httpx.Response(failure, json={"message": f"failure {failure}"})
        if path == "/auth/login":
            body = json.loads(request.content)
            time.sleep(self.login_delay)
            if body != {"email": EMAIL, "password": self.password}:
                return httpx.Response(
                    400,
                    json={"message": "Invalid email and password combination", "status_code": 400},
                )
            self.logins += 1
            token = f"token-{self.logins}"
            self.valid_tokens.add(token)
            return httpx.Response(200, json={"id": 1, "email": EMAIL, "token": token})
        if path == "/auth/logout":
            self.valid_tokens.discard(request.headers["Authorization"].removeprefix("Bearer "))
            return httpx.Response(200, json={"message": "Successfully logged out"})
        token = request.headers.get("Authorization", "").removeprefix("Bearer ")
        if token not in self.valid_tokens:
            return httpx.Response(401, json={"message": "Token has expired", "status_code": 401})
        if path == "/settings/company/pickup":
            return httpx.Response(
                200,
                json={
                    "data": {
                        "shipping_address": [
                            {
                                "pickup_location": "Primary",
                                "address": "12 MG Road",
                                "address_2": "Floor 2",
                                "city": "Bengaluru",
                                "state": "Karnataka",
                                "pin_code": 560001,
                                "phone_verified": 1,
                                "is_primary_location": 1,
                            },
                            {
                                "pickup_location": "Warehouse 2",
                                "pin_code": "110001",
                                "phone_verified": 0,
                            },
                        ]
                    }
                },
            )
        if path == "/account/details/wallet-balance":
            if self.wallet_status != 200:
                return httpx.Response(self.wallet_status, json={"message": "Unauthorized module"})
            return httpx.Response(200, json={"data": {"balance_amount": "1520.50"}})
        if path == "/orders/embedded-error":
            return httpx.Response(
                200, json={"status_code": 422, "message": "Order id already exists"}
            )
        return httpx.Response(404, json={"message": "Not found"})


class Clock:
    def __init__(self, now=1_800_000_000.0):
        self.now = now

    def __call__(self):
        return self.now


@pytest.fixture
def fake():
    return FakeShiprocket()


def make(fake, clock=None, base=BASE):
    transport = httpx.MockTransport(fake.handler)
    tokens = TokenManager(base, EMAIL, PASSWORD, transport=transport, clock=clock or Clock())
    return ShiprocketClient(tokens, transport=transport, sleep=lambda _: None)


# --- Token management ---------------------------------------------------------------------


def test_logs_in_once_and_reuses_the_token(fake):
    with make(fake) as sr:
        sr.get("/settings/company/pickup")
        sr.get("/settings/company/pickup")
    assert fake.logins == 1
    assert fake.calls.count(("POST", "/auth/login")) == 1


def test_renews_a_day_before_expiry(fake):
    clock = Clock()
    sr = make(fake, clock)
    sr.get("/settings/company/pickup")
    clock.now += TOKEN_LIFETIME - TOKEN_RENEW_MARGIN - 60
    sr.get("/settings/company/pickup")
    assert fake.logins == 1
    clock.now += 120  # now within a day of expiry
    sr.get("/settings/company/pickup")
    assert fake.logins == 2


def test_jwt_expiry_wins_over_the_default():
    now = 1_800_000_000.0
    assert shiprocket.token_expiry(jwt(now + 3600), now) == now + 3600
    assert shiprocket.token_expiry("not-a-jwt", now) == now + TOKEN_LIFETIME


def test_a_401_renews_the_token_once_and_retries(fake):
    sr = make(fake)
    sr.get("/settings/company/pickup")
    fake.valid_tokens.clear()  # revoked or expired early at Shiprocket
    assert sr.get("/settings/company/pickup")["data"]
    assert fake.logins == 2


def test_a_second_401_is_reported(fake):
    sr = make(fake)
    sr.get("/settings/company/pickup")
    fake.failures = [401, 401]
    with pytest.raises(ShiprocketAuthError, match="refused the token twice"):
        sr.get("/settings/company/pickup")


def test_wrong_credentials_are_clear_and_not_retried_in_a_loop(fake):
    clock = Clock()
    fake.password = "changed-in-shiprocket"
    sr = make(fake, clock)
    with pytest.raises(ShiprocketAuthError, match="Shiprocket rejected the API user") as caught:
        sr.get("/settings/company/pickup")
    assert "Invalid email and password combination" in str(caught.value)
    with pytest.raises(ShiprocketAuthError):
        sr.get("/settings/company/pickup")
    assert fake.calls.count(("POST", "/auth/login")) == 1  # held back during the cooldown
    fake.password = PASSWORD
    clock.now += LOGIN_COOLDOWN + 1
    assert sr.get("/settings/company/pickup")["data"]


def test_concurrent_requests_share_one_login(fake):
    fake.login_delay = 0.2
    sr = make(fake)
    threads = [
        threading.Thread(target=sr.get, args=("/settings/company/pickup",)) for _ in range(5)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert fake.logins == 1


def test_logout_invalidates_the_token(fake):
    sr = make(fake)
    sr.get("/settings/company/pickup")
    sr.tokens.logout()
    assert fake.valid_tokens == set() and sr.tokens.expires_at is None
    sr.get("/settings/company/pickup")
    assert fake.logins == 2


# --- Errors and retries -------------------------------------------------------------------


def test_network_failures_and_5xx_are_retried(fake):
    sr = make(fake)
    sr.get("/settings/company/pickup")
    fake.failures = ["timeout", 503, 429]
    assert sr.get("/settings/company/pickup")["data"]


def test_retries_give_up_with_a_readable_error(fake):
    sr = make(fake)
    sr.get("/settings/company/pickup")
    fake.failures = ["timeout"] * 4
    with pytest.raises(ShiprocketError, match="Couldn't reach Shiprocket"):
        sr.get("/settings/company/pickup")
    fake.failures = [500] * 4
    with pytest.raises(ShiprocketError, match=r"failed \(500\): failure 500"):
        sr.get("/settings/company/pickup")


def test_error_in_a_200_body_is_an_error(fake):
    with pytest.raises(ShiprocketError, match=r"\(422\): Order id already exists"):
        make(fake).get("/orders/embedded-error")


def test_field_errors_are_listed():
    response = httpx.Response(
        422,
        json={"message": "The given data was invalid.", "errors": {"pincode": ["Invalid pincode"]}},
    )
    assert (
        shiprocket.error_message(response)
        == "The given data was invalid. (pincode: Invalid pincode)"
    )


def test_network_not_allow_listed(fake):
    fake.failures = [498]
    with pytest.raises(ShiprocketError, match="allow-listed") as caught:
        make(fake).get("/settings/company/pickup")
    assert caught.value.status_code == 498


# --- Configuration ------------------------------------------------------------------------


@pytest.fixture
def settings(monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "shiprocket_email", EMAIL)
    monkeypatch.setattr(settings, "shiprocket_password", PASSWORD)
    monkeypatch.setattr(settings, "shiprocket_base_url", BASE)
    monkeypatch.setattr(settings, "shiprocket_webhook_token", "")
    return settings


def test_missing_settings(settings, monkeypatch):
    monkeypatch.setattr(settings, "shiprocket_password", "")
    with pytest.raises(shiprocket.ShiprocketConfigError, match="SHIPROCKET_PASSWORD"):
        shiprocket.get_shiprocket_client()


def test_production_refuses_the_sandbox(settings, monkeypatch):
    monkeypatch.setattr(settings, "environment", "production")
    with pytest.raises(shiprocket.ShiprocketConfigError, match="sandbox in production"):
        shiprocket.get_shiprocket_client()


def test_one_token_manager_per_api_user(settings, monkeypatch):
    first = shiprocket.get_shiprocket_client().tokens
    assert shiprocket.get_shiprocket_client().tokens is first
    monkeypatch.setattr(settings, "shiprocket_password", "rotated")
    assert shiprocket.get_shiprocket_client().tokens is not first


# --- Endpoints ----------------------------------------------------------------------------


@pytest.fixture
def connected(settings, fake, monkeypatch, clean_db):
    monkeypatch.setattr(shiprocket_connection, "client_factory", lambda: make(fake))
    return fake


def account():
    with SessionLocal() as session:
        return session.scalars(select(ChannelAccount)).one_or_none()


def test_status_when_not_configured(monkeypatch, clean_db):
    settings = get_settings()
    monkeypatch.setattr(settings, "shiprocket_email", "")
    monkeypatch.setattr(settings, "shiprocket_password", "")
    body = client.get("/shiprocket/status").json()
    assert body["configured"] is False and "SHIPROCKET_EMAIL" in body["config_error"]
    assert body["connection_status"] is None and body["environment"] == "production"
    response = client.post("/shiprocket/test")
    assert response.status_code == 503 and "not configured" in response.json()["detail"]


def test_connection_test_succeeds_and_is_recorded(connected):
    response = client.post("/shiprocket/test")
    assert response.status_code == 200, response.json()
    body = response.json()
    assert body["pickup_locations"][0] == {
        "name": "Primary",
        "address": "12 MG Road Floor 2",
        "city": "Bengaluru",
        "state": "Karnataka",
        "pincode": "560001",
        "phone_verified": True,
        "is_primary": True,
    }
    assert body["pickup_locations"][1]["phone_verified"] is False
    assert body["wallet_balance"] == "1520.50" and body["wallet_error"] is None
    assert body["token_expires_at"]
    status = body["status"]
    assert status["configured"] and status["environment"] == "sandbox"
    assert status["api_user"] == EMAIL and status["connection_status"] == "active"
    assert status["last_connected_at"] and status["last_error"] is None
    row = account()
    assert row.credential_ref == "env:SHIPROCKET_EMAIL,SHIPROCKET_PASSWORD"
    assert row.external_seller_id == EMAIL and row.connection_status == "active"


def test_test_always_logs_in_afresh(connected):
    client.post("/shiprocket/test")
    client.post("/shiprocket/test")
    assert connected.logins == 2


def test_wallet_without_the_account_module_is_not_a_failure(connected):
    connected.wallet_status = 403
    body = client.post("/shiprocket/test").json()
    assert body["wallet_balance"] is None and "Unauthorized module" in body["wallet_error"]
    assert body["status"]["connection_status"] == "active"


def test_wrong_credentials_are_recorded(connected):
    connected.password = "changed"
    response = client.post("/shiprocket/test")
    assert response.status_code == 502 and "rejected the API user" in response.json()["detail"]
    status = client.get("/shiprocket/status").json()
    assert status["connection_status"] == "auth_expired"
    assert "rejected the API user" in status["last_error"]
    assert account().connection_status == "auth_expired"


def test_outage_is_recorded_as_error(connected):
    connected.failures = ["timeout"] * 4
    assert client.post("/shiprocket/test").status_code == 502
    assert client.get("/shiprocket/status").json()["connection_status"] == "error"


def test_no_secret_reaches_responses_logs_or_the_database(connected, caplog):
    caplog.set_level(logging.DEBUG)
    bodies = client.post("/shiprocket/test").text + client.get("/shiprocket/status").text
    with SessionLocal() as session:
        dump = " ".join(
            str(row) for row in session.execute(text("SELECT * FROM ecs_channel_accounts")).all()
        )
    for secret in (PASSWORD, "token-1"):
        assert secret not in bodies and secret not in dump and secret not in caplog.text


def test_needs_the_api_key(monkeypatch):
    monkeypatch.setattr(get_settings(), "api_shared_secret", "secret")
    assert client.get("/shiprocket/status").status_code == 401
