import httpx
import pytest

from app.config import get_settings
from app.services import shopify as shopify_module
from app.services.shopify import (
    ShopifyClient,
    ShopifyConfigError,
    ShopifyError,
    get_shopify_client,
    normalize_store_domain,
)


def make_client(handler, **kwargs) -> tuple[ShopifyClient, list[float]]:
    sleeps: list[float] = []
    client = ShopifyClient(
        "https://test-store.myshopify.com/",
        "shpat_test",
        "2026-10",
        transport=httpx.MockTransport(handler),
        sleep=sleeps.append,
        **kwargs,
    )
    return client, sleeps


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

    with pytest.raises(ShopifyError, match="SHOPIFY_ADMIN_API_ACCESS_TOKEN") as exc_info:
        client.get("/shop.json")
    assert exc_info.value.body == "Invalid token"
    assert sleeps == []


def test_graphql_errors_raise():
    client, _ = make_client(
        lambda request: httpx.Response(200, json={"errors": [{"message": "x"}]})
    )
    with pytest.raises(ShopifyError, match="GraphQL"):
        client.graphql("{ shop { name } }")


def test_get_shopify_client_requires_token(monkeypatch):
    settings = get_settings().model_copy(
        update={"shopify_store_domain": "a.myshopify.com", "shopify_admin_api_access_token": ""}
    )
    monkeypatch.setattr(shopify_module, "get_settings", lambda: settings)
    with pytest.raises(ShopifyConfigError, match="SHOPIFY_ADMIN_API_ACCESS_TOKEN"):
        get_shopify_client()
