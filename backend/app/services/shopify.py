"""Shopify Admin API client: the only place the backend talks to Shopify.

Usage:
    with get_shopify_client() as shopify:
        shop = shopify.get("/shop.json")["shop"]
        for order in shopify.paginate("/orders.json", "orders", {"status": "any"}):
            ...
"""

import logging
import time
from collections.abc import Callable, Iterator
from typing import Any

import httpx

from app.config import get_settings

logger = logging.getLogger(__name__)

PAGE_LIMIT = 250  # Shopify's maximum page size for REST list endpoints
RETRY_STATUSES = {429, 500, 502, 503, 504}


class ShopifyConfigError(RuntimeError):
    """Shopify settings are missing from the environment."""


class ShopifyError(RuntimeError):
    """Shopify answered with an error, or kept failing after retries."""

    def __init__(self, message: str, status_code: int | None = None, body: str = "") -> None:
        super().__init__(message)
        self.status_code = status_code
        self.body = body


def normalize_store_domain(domain: str) -> str:
    """'https://store.myshopify.com/' -> 'store.myshopify.com'."""
    domain = domain.strip().removeprefix("https://").removeprefix("http://")
    return domain.rstrip("/")


class ShopifyClient:
    def __init__(
        self,
        store_domain: str,
        access_token: str,
        api_version: str,
        *,
        timeout: float = 30.0,
        max_retries: int = 5,
        transport: httpx.BaseTransport | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.store_domain = normalize_store_domain(store_domain)
        self.api_version = api_version
        self.max_retries = max_retries
        self._sleep = sleep
        self._http = httpx.Client(
            base_url=f"https://{self.store_domain}/admin/api/{api_version}",
            headers={
                "X-Shopify-Access-Token": access_token,
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
            timeout=timeout,
            transport=transport,
        )

    def __enter__(self) -> "ShopifyClient":
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    def close(self) -> None:
        self._http.close()

    def request(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        """Send a request, retrying rate limits (429) and transient 5xx errors with backoff.

        `path` is relative to the API base (e.g. "/orders.json") or an absolute URL
        (pagination links).
        """
        for attempt in range(self.max_retries + 1):
            response = self._http.request(method, path, **kwargs)
            if response.status_code not in RETRY_STATUSES:
                break
            if attempt == self.max_retries:
                break
            delay = self._retry_delay(response, attempt)
            logger.warning(
                "Shopify %s %s returned %s; retrying in %.1fs (attempt %d/%d)",
                method,
                path,
                response.status_code,
                delay,
                attempt + 1,
                self.max_retries,
            )
            self._sleep(delay)

        if response.is_error:
            raise ShopifyError(
                f"Shopify {method} {path} failed with {response.status_code}"
                + self._hint(response.status_code),
                status_code=response.status_code,
                body=response.text[:1000],
            )
        return response

    def get(self, path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        return self.request("GET", path, params=params).json()

    def paginate(
        self, path: str, key: str, params: dict[str, Any] | None = None
    ) -> Iterator[dict[str, Any]]:
        """Yield every record from a REST list endpoint, following the `Link: rel="next"` header.

        `key` is the JSON field holding the records, e.g. "orders" or "products".
        """
        url: str | None = path
        page_params: dict[str, Any] | None = {**(params or {}), "limit": PAGE_LIMIT}
        while url:
            response = self.request("GET", url, params=page_params)
            yield from response.json()[key]
            url = response.links.get("next", {}).get("url")
            # The next-page URL already carries page_info and limit; Shopify rejects
            # other filters alongside page_info.
            page_params = None

    def graphql(self, query: str, variables: dict[str, Any] | None = None) -> dict[str, Any]:
        """Run a GraphQL Admin API query and return its `data`."""
        response = self.request(
            "POST", "/graphql.json", json={"query": query, "variables": variables or {}}
        )
        payload = response.json()
        if payload.get("errors"):
            raise ShopifyError(f"Shopify GraphQL error: {payload['errors']}", response.status_code)
        return payload["data"]

    @staticmethod
    def _retry_delay(response: httpx.Response, attempt: int) -> float:
        retry_after = response.headers.get("Retry-After")
        if retry_after:
            try:
                return max(float(retry_after), 0.5)
            except ValueError:
                pass
        return float(min(2**attempt, 30))  # 1, 2, 4, 8, 16, 30 seconds

    @staticmethod
    def _hint(status_code: int) -> str:
        hints = {
            401: " (check SHOPIFY_ADMIN_API_ACCESS_TOKEN)",
            403: " (the app is missing an access scope for this call)",
            404: " (check SHOPIFY_STORE_DOMAIN and SHOPIFY_API_VERSION)",
        }
        return hints.get(status_code, "")


def get_shopify_client() -> ShopifyClient:
    """A client built from settings. Use it as a context manager so it gets closed."""
    settings = get_settings()
    missing = [
        name
        for name, value in {
            "SHOPIFY_STORE_DOMAIN": settings.shopify_store_domain,
            "SHOPIFY_ADMIN_API_ACCESS_TOKEN": settings.shopify_admin_api_access_token,
        }.items()
        if not value
    ]
    if missing:
        raise ShopifyConfigError(f"Missing in .env: {', '.join(missing)}")
    return ShopifyClient(
        settings.shopify_store_domain,
        settings.shopify_admin_api_access_token,
        settings.shopify_api_version,
    )
