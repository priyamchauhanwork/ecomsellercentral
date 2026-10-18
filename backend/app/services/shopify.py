"""Shopify Admin API client: the only place the backend talks to Shopify.

Authentication:
- Dev Dashboard apps (the default since 2026): SHOPIFY_API_KEY + SHOPIFY_API_SECRET are
  exchanged for a 24-hour access token (client credentials grant), renewed automatically.
- Legacy custom apps created in the Shopify admin: a fixed SHOPIFY_ADMIN_API_ACCESS_TOKEN.

Usage:
    with get_shopify_client() as shopify:
        shop = shopify.get("/shop.json")["shop"]
        for order in shopify.paginate("/orders.json", "orders", {"status": "any"}):
            ...
"""

import logging
import threading
import time
from collections.abc import Callable, Generator, Iterator
from functools import lru_cache
from typing import Any

import httpx

from app.config import get_settings

logger = logging.getLogger(__name__)

PAGE_LIMIT = 250  # Shopify's maximum page size for REST list endpoints
RETRY_STATUSES = {429, 500, 502, 503, 504}
TOKEN_HEADER = "X-Shopify-Access-Token"
TOKEN_REFRESH_MARGIN = 300  # renew client-credentials tokens 5 minutes before they expire


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


class StaticTokenAuth(httpx.Auth):
    """A fixed Admin API token (legacy custom apps created in the Shopify admin)."""

    def __init__(self, access_token: str) -> None:
        self._access_token = access_token

    def auth_flow(self, request: httpx.Request) -> Generator[httpx.Request, httpx.Response]:
        request.headers[TOKEN_HEADER] = self._access_token
        yield request


class ClientCredentialsAuth(httpx.Auth):
    """Dev Dashboard apps: exchange the client ID and secret for a 24-hour access token.

    Only works when the app and the store belong to the same Shopify organization.
    The token is cached, renewed shortly before it expires, and renewed once more if
    Shopify answers 401 (e.g. the token was revoked).
    """

    def __init__(
        self,
        store_domain: str,
        client_id: str,
        client_secret: str,
        *,
        transport: httpx.BaseTransport | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.store_domain = normalize_store_domain(store_domain)
        self._client_id = client_id
        self._client_secret = client_secret
        self._transport = transport
        self._clock = clock
        self._access_token: str | None = None
        self._expires_at = 0.0
        self._lock = threading.Lock()

    def auth_flow(self, request: httpx.Request) -> Generator[httpx.Request, httpx.Response]:
        request.headers[TOKEN_HEADER] = self._get_token()
        response = yield request
        if response.status_code == 401:
            request.headers[TOKEN_HEADER] = self._get_token(force_refresh=True)
            yield request

    def _get_token(self, force_refresh: bool = False) -> str:
        with self._lock:
            expiring = self._clock() >= self._expires_at - TOKEN_REFRESH_MARGIN
            if force_refresh or self._access_token is None or expiring:
                self._access_token, self._expires_at = self._fetch_token()
            return self._access_token

    def _fetch_token(self) -> tuple[str, float]:
        with httpx.Client(transport=self._transport, timeout=30.0) as http:
            response = http.post(
                f"https://{self.store_domain}/admin/oauth/access_token",
                data={
                    "grant_type": "client_credentials",
                    "client_id": self._client_id,
                    "client_secret": self._client_secret,
                },
            )
        if response.is_error:
            raise ShopifyError(
                f"Could not get a Shopify access token ({response.status_code}): check "
                "SHOPIFY_API_KEY and SHOPIFY_API_SECRET, and that the app is installed on "
                f"{self.store_domain} and both belong to the same Shopify organization",
                status_code=response.status_code,
                body=response.text[:1000],
            )
        payload = response.json()
        logger.info("Obtained a Shopify access token for %s", self.store_domain)
        return payload["access_token"], self._clock() + float(payload.get("expires_in", 86399))


class ShopifyClient:
    def __init__(
        self,
        store_domain: str,
        api_version: str,
        auth: httpx.Auth,
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
            auth=auth,
            headers={"Content-Type": "application/json", "Accept": "application/json"},
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
        """Send a request, retrying rate limits (429), transient 5xx errors and network
        failures (timeouts, refused connections) with backoff. Raises ShopifyError when
        Shopify answers with an error or can't be reached after the retries.

        `path` is relative to the API base (e.g. "/orders.json") or an absolute URL
        (pagination links). Retrying writes is safe for the mutations this app sends:
        each is idempotent (fixed handle, idempotency key, or setting a status).
        """
        for attempt in range(self.max_retries + 1):
            try:
                response = self._http.request(method, path, **kwargs)
            except httpx.TransportError as exc:
                if attempt == self.max_retries:
                    raise ShopifyError(f"Couldn't reach Shopify: {exc}") from exc
                delay = float(min(2**attempt, 30))
                logger.warning(
                    "Shopify %s %s failed (%s); retrying in %.1fs", method, path, exc, delay
                )
                self._sleep(delay)
                continue
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

    def access_scopes(self) -> set[str]:
        """The access scopes granted to the app on this store (not tied to an API version)."""
        response = self.request(
            "GET", f"https://{self.store_domain}/admin/oauth/access_scopes.json"
        )
        return {scope["handle"] for scope in response.json().get("access_scopes", [])}

    def graphql(self, query: str, variables: dict[str, Any] | None = None) -> dict[str, Any]:
        """Run a GraphQL Admin API query and return its `data`."""
        response = self.request(
            "POST", "/graphql.json", json={"query": query, "variables": variables or {}}
        )
        payload = response.json()
        if payload.get("errors"):
            messages = (
                "; ".join(
                    str(error.get("message", error)) if isinstance(error, dict) else str(error)
                    for error in payload["errors"]
                )
                if isinstance(payload["errors"], list)
                else str(payload["errors"])
            )
            raise ShopifyError(f"Shopify GraphQL error: {messages}", response.status_code)
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
            401: " (check the Shopify credentials in .env)",
            403: " (the app is missing an access scope, or protected customer data access, "
            "for this call)",
            404: " (check SHOPIFY_STORE_DOMAIN and SHOPIFY_API_VERSION)",
        }
        return hints.get(status_code, "")


@lru_cache
def _client_credentials_auth(
    store_domain: str, client_id: str, client_secret: str
) -> ClientCredentialsAuth:
    """One shared auth per app/store, so the 24-hour token is reused across requests."""
    return ClientCredentialsAuth(store_domain, client_id, client_secret)


def get_shopify_client() -> ShopifyClient:
    """A client built from settings. Use it as a context manager so it gets closed."""
    settings = get_settings()
    if not settings.shopify_store_domain:
        raise ShopifyConfigError("Missing in .env: SHOPIFY_STORE_DOMAIN")

    auth: httpx.Auth
    if settings.shopify_admin_api_access_token:
        auth = StaticTokenAuth(settings.shopify_admin_api_access_token)
    elif settings.shopify_api_key and settings.shopify_api_secret:
        auth = _client_credentials_auth(
            settings.shopify_store_domain, settings.shopify_api_key, settings.shopify_api_secret
        )
    else:
        raise ShopifyConfigError(
            "Missing in .env: SHOPIFY_API_KEY and SHOPIFY_API_SECRET (Dev Dashboard app), "
            "or SHOPIFY_ADMIN_API_ACCESS_TOKEN (legacy custom app)"
        )
    return ShopifyClient(settings.shopify_store_domain, settings.shopify_api_version, auth)
