import httpx
import pytest

from app.config import get_settings
from app.services import shopify as shopify_module
from app.services.shopify import (
    ClientCredentialsAuth,
    ShopifyClient,
    ShopifyConfigError,
    ShopifyError,
    StaticTokenAuth,
    get_shopify_client,
    normalize_store_domain,
)

TOKEN_URL = "https://test-store.myshopify.com/admin/oauth/access_token"


def make_client(handler, auth=None, **kwargs) -> tuple[ShopifyClient, list[float]]:
    sleeps: list[float] = []
    client = ShopifyClient(
        "https://test-store.myshopify.com/",
        "2026-10",
        auth or StaticTokenAuth("shpat_test"),
        transport=httpx.MockTransport(handler),
        sleep=sleeps.append,
        **kwargs,
    )
    return client, sleeps


class FakeShopify:
    """Issues numbered tokens from the token endpoint and accepts only the latest one."""

    def __init__(self) -> None:
        self.tokens_issued = 0
        self.token_requests: list[httpx.Request] = []
        self.valid_token: str | None = None

    def handler(self, request: httpx.Request) -> httpx.Response:
        if str(request.url) == TOKEN_URL:
            self.token_requests.append(request)
            self.tokens_issued += 1
            self.valid_token = f"token-{self.tokens_issued}"
            return httpx.Response(
                200,
                json={
                    "access_token": self.valid_token,
                    "scope": "read_orders",
                    "expires_in": 86399,
                },
            )
        if request.headers.get("X-Shopify-Access-Token") != self.valid_token:
            return httpx.Response(401, text="Invalid API key or access token")
        return httpx.Response(200, json={"shop": {"name": "Test"}})


def make_cc_client(fake: FakeShopify, clock) -> ShopifyClient:
    transport = httpx.MockTransport(fake.handler)
    auth = ClientCredentialsAuth(
        "test-store.myshopify.com", "client-id", "shpss_secret", transport=transport, clock=clock
    )
    client, _ = make_client(fake.handler, auth=auth)
    return client


def test_client_credentials_fetches_token_once_and_reuses_it():
    fake = FakeShopify()
    client = make_cc_client(fake, clock=lambda: 1000.0)

    client.get("/shop.json")
    client.get("/shop.json")

    assert fake.tokens_issued == 1
    form = dict(httpx.QueryParams(fake.token_requests[0].content.decode()))
    assert form == {
        "grant_type": "client_credentials",
        "client_id": "client-id",
        "client_secret": "shpss_secret",
    }


def test_client_credentials_renews_before_expiry():
    fake = FakeShopify()
    now = [0.0]
    client = make_cc_client(fake, clock=lambda: now[0])

    client.get("/shop.json")
    now[0] = 86399 - 200  # inside the 5-minute renewal margin
    client.get("/shop.json")

    assert fake.tokens_issued == 2


def test_client_credentials_renews_once_on_401():
    fake = FakeShopify()
    client = make_cc_client(fake, clock=lambda: 0.0)
    client.get("/shop.json")
    fake.valid_token = "revoked"  # Shopify no longer accepts our cached token-1

    assert client.get("/shop.json")["shop"]["name"] == "Test"
    assert fake.tokens_issued == 2


def test_client_credentials_bad_secret_raises_clear_error():
    def handler(request):
        return httpx.Response(400, json={"error": "invalid_client"})

    auth = ClientCredentialsAuth(
        "test-store.myshopify.com", "id", "wrong", transport=httpx.MockTransport(handler)
    )
    client, _ = make_client(handler, auth=auth)
    with pytest.raises(ShopifyError, match="SHOPIFY_API_SECRET"):
        client.get("/shop.json")


def test_normalize_store_domain():
    assert normalize_store_domain("https://a.myshopify.com/") == "a.myshopify.com"
    assert normalize_store_domain(" a.myshopify.com ") == "a.myshopify.com"


def test_base_url_and_auth_header():
    seen: list[httpx.Request] = []

    def handler(request):
        seen.append(request)
        return httpx.Response(200, json={"shop": {"name": "Test"}})

    client, _ = make_client(handler)
    assert client.get("/shop.json")["shop"]["name"] == "Test"
    request = seen[0]
    assert str(request.url) == "https://test-store.myshopify.com/admin/api/2026-10/shop.json"
    assert request.headers["X-Shopify-Access-Token"] == "shpat_test"


def test_paginate_follows_link_header_past_250():
    base = "https://test-store.myshopify.com/admin/api/2026-10/orders.json"
    pages = {
        None: (range(0, 250), f'<{base}?limit=250&page_info=p2>; rel="next"'),
        "p2": (
            range(250, 500),
            f'<{base}?limit=250&page_info=p1>; rel="previous", '
            f'<{base}?limit=250&page_info=p3>; rel="next"',
        ),
        "p3": (range(500, 610), f'<{base}?limit=250&page_info=p2>; rel="previous"'),
    }
    requests: list[httpx.Request] = []

    def handler(request):
        requests.append(request)
        ids, link = pages[request.url.params.get("page_info")]
        return httpx.Response(
            200, json={"orders": [{"id": i} for i in ids]}, headers={"Link": link}
        )

    client, _ = make_client(handler)
    orders = list(client.paginate("/orders.json", "orders", {"status": "any"}))

    assert [o["id"] for o in orders] == list(range(610))
    assert len(requests) == 3
    # First page carries the filters; later pages only page_info + limit.
    assert requests[0].url.params["status"] == "any"
    assert requests[0].url.params["limit"] == "250"
    assert "status" not in requests[1].url.params


def test_429_is_retried_using_retry_after():
    responses = iter(
        [
            httpx.Response(429, headers={"Retry-After": "2.0"}),
            httpx.Response(429),
            httpx.Response(200, json={"count": 7}),
        ]
    )
    client, sleeps = make_client(lambda request: next(responses))

    assert client.get("/products/count.json") == {"count": 7}
    assert sleeps == [2.0, 2.0]  # Retry-After, then exponential backoff (2**1)


def test_retries_give_up_with_shopify_error():
    client, sleeps = make_client(lambda request: httpx.Response(429), max_retries=3)

    with pytest.raises(ShopifyError) as exc_info:
        client.get("/shop.json")
    assert exc_info.value.status_code == 429
    assert len(sleeps) == 3


def test_401_raises_with_hint_and_no_retry():
    client, sleeps = make_client(lambda request: httpx.Response(401, text="Invalid token"))

    with pytest.raises(ShopifyError, match="credentials") as exc_info:
        client.get("/shop.json")
    assert exc_info.value.body == "Invalid token"
    assert sleeps == []


def test_graphql_errors_raise():
    client, _ = make_client(
        lambda request: httpx.Response(200, json={"errors": [{"message": "x"}]})
    )
    with pytest.raises(ShopifyError, match="GraphQL"):
        client.graphql("{ shop { name } }")


def use_settings(monkeypatch, **overrides):
    base = {
        "shopify_store_domain": "a.myshopify.com",
        "shopify_admin_api_access_token": "",
        "shopify_api_key": "",
        "shopify_api_secret": "",
    }
    settings = get_settings().model_copy(update={**base, **overrides})
    monkeypatch.setattr(shopify_module, "get_settings", lambda: settings)


def test_get_shopify_client_requires_credentials(monkeypatch):
    use_settings(monkeypatch)
    with pytest.raises(ShopifyConfigError, match="SHOPIFY_API_KEY and SHOPIFY_API_SECRET"):
        get_shopify_client()


def test_get_shopify_client_picks_auth(monkeypatch):
    use_settings(monkeypatch, shopify_api_key="id", shopify_api_secret="secret")
    with get_shopify_client() as first, get_shopify_client() as second:
        assert isinstance(first._http.auth, ClientCredentialsAuth)
        assert first._http.auth is second._http.auth  # token cache shared between clients

    use_settings(monkeypatch, shopify_admin_api_access_token="shpat_x", shopify_api_key="id")
    with get_shopify_client() as client:
        assert isinstance(client._http.auth, StaticTokenAuth)
