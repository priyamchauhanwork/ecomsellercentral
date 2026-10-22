"""Shiprocket API client (#57): the only place the backend talks to Shiprocket.

Authentication: the seller's *API user* (Shiprocket → Settings → API; not the panel
login) logs in with email and password (`POST /auth/login`) and gets a bearer token,
valid for 240 hours per Shiprocket. The token is kept in memory only (never stored or
logged), renewed a day before it expires, and renewed once more if Shiprocket answers
401. Wrong credentials aren't retried in a loop: after a refused login, further logins
with the same credentials are held back for a minute.

Usage:
    with get_shiprocket_client() as shiprocket:
        locations = shiprocket.get("/settings/company/pickup")
"""

import base64
import json
import logging
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import httpx

from app.config import get_settings

logger = logging.getLogger(__name__)

TOKEN_LIFETIME = 240 * 3600  # seconds, per Shiprocket; a JWT's own expiry wins if present
TOKEN_RENEW_MARGIN = 24 * 3600  # renew this long before the token expires
LOGIN_COOLDOWN = 60.0  # seconds to hold back logins after Shiprocket refused the credentials
RETRY_STATUSES = {429, 500, 502, 503, 504}
NOT_ALLOWED = 498  # Shiprocket's sandbox answers this to networks it hasn't allow-listed


class ShiprocketConfigError(RuntimeError):
    """Shiprocket settings are missing from the environment."""


class ShiprocketError(RuntimeError):
    """Shiprocket answered with an error, or couldn't be reached after retries."""

    def __init__(self, message: str, status_code: int | None = None, body: str = "") -> None:
        super().__init__(message)
        self.status_code = status_code
        self.body = body


class ShiprocketAuthError(ShiprocketError):
    """Shiprocket refused the API user's email and password."""


@dataclass
class Token:
    value: str
    expires_at: float  # time.time()


def token_expiry(token: str, now: float) -> float:
    """The token's expiry: the JWT's `exp` claim if it has one, else 240 hours from now."""
    try:
        payload = token.split(".")[1]
        claims = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
        return float(claims["exp"])
    except (IndexError, ValueError, KeyError, TypeError):
        return now + TOKEN_LIFETIME


class TokenManager:
    """One token per API user and Shiprocket host, shared by every client in the process."""

    def __init__(
        self,
        base_url: str,
        email: str,
        password: str,
        *,
        transport: httpx.BaseTransport | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.email = email
        self._password = password
        self._transport = transport
        self._clock = clock
        self._token: Token | None = None
        self._refused_at: float | None = None
        self._refused_message = ""
        self._lock = threading.Lock()

    @property
    def expires_at(self) -> datetime | None:
        if self._token is None:
            return None
        return datetime.fromtimestamp(self._token.expires_at, UTC)

    def token(self, *, renew: bool = False, stale: str | None = None) -> str:
        """A valid token, logging in when there is none, it is about to expire, or
        `renew` is asked. `stale` is the token that just got a 401: if another request
        already replaced it, that new one is used instead of logging in again."""
        with self._lock:
            now = self._clock()
            current = self._token
            if current and stale and current.value != stale:
                return current.value
            if renew or current is None or now >= current.expires_at - TOKEN_RENEW_MARGIN:
                self._token = self._login(now)
            return self._token.value

    def forget(self) -> None:
        with self._lock:
            self._token = None

    def _login(self, now: float) -> Token:
        if self._refused_at is not None and now - self._refused_at < LOGIN_COOLDOWN:
            raise ShiprocketAuthError(self._refused_message)
        try:
            with httpx.Client(transport=self._transport, timeout=30.0) as http:
                response = http.post(
                    f"{self.base_url}/auth/login",
                    json={"email": self.email, "password": self._password},
                )
        except httpx.TransportError as exc:
            raise ShiprocketError(f"Couldn't reach Shiprocket to log in: {exc}") from exc
        if response.status_code == NOT_ALLOWED:
            raise ShiprocketError(not_allowed_message(self.base_url), NOT_ALLOWED)
        token = (body_json(response) or {}).get("token")
        if response.status_code in (400, 401, 403, 422) or (response.is_success and not token):
            self._refused_at = now
            self._refused_message = (
                "Shiprocket rejected the API user: check SHIPROCKET_EMAIL and "
                "SHIPROCKET_PASSWORD (the API user from Shiprocket → Settings → API, not the "
                f"panel login). Shiprocket said: {error_message(response)}"
            )
            raise ShiprocketAuthError(self._refused_message, response.status_code)
        if response.is_error:
            raise ShiprocketError(
                f"Shiprocket login failed ({response.status_code}): {error_message(response)}",
                response.status_code,
            )
        self._refused_at = None
        logger.info("Logged in to Shiprocket as %s", self.email)
        return Token(token, token_expiry(token, now))

    def logout(self) -> None:
        """Invalidate the current token at Shiprocket (credential change, disconnect)."""
        with self._lock:
            token, self._token = self._token, None
        if token is None:
            return
        try:
            with httpx.Client(transport=self._transport, timeout=30.0) as http:
                http.post(
                    f"{self.base_url}/auth/logout",
                    headers={"Authorization": f"Bearer {token.value}"},
                )
        except httpx.TransportError:
            logger.warning("Couldn't reach Shiprocket to log out; the token expires by itself")


def body_json(response: httpx.Response) -> Any:
    try:
        return response.json()
    except ValueError:
        return None


def error_message(response: httpx.Response) -> str:
    """Shiprocket's own words: `message`, plus field errors when it lists them."""
    body = body_json(response)
    if not isinstance(body, dict):
        return (response.text or response.reason_phrase or "no details").strip()[:300]
    message = str(body.get("message") or body.get("error") or "").strip()
    errors = body.get("errors")
    if isinstance(errors, dict):
        details = "; ".join(
            f"{field}: {' '.join(map(str, problems)) if isinstance(problems, list) else problems}"
            for field, problems in errors.items()
        )
        message = f"{message} ({details})" if message else details
    return message[:500] or f"HTTP {response.status_code}"


def not_allowed_message(base_url: str) -> str:
    return (
        f"Shiprocket refused this network (HTTP 498) at {base_url}: the sandbox only "
        "accepts IP addresses Shiprocket has allow-listed. Ask Shiprocket to allow this "
        "machine's and Render's outbound IPs."
    )


class ShiprocketClient:
    def __init__(
        self,
        tokens: TokenManager,
        *,
        timeout: float = 30.0,
        max_retries: int = 3,
        transport: httpx.BaseTransport | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.tokens = tokens
        self.max_retries = max_retries
        self._sleep = sleep
        self._http = httpx.Client(
            base_url=tokens.base_url,
            headers={"Content-Type": "application/json", "Accept": "application/json"},
            timeout=timeout,
            transport=transport,
        )

    def __enter__(self) -> "ShiprocketClient":
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    def close(self) -> None:
        self._http.close()

    def request(self, method: str, path: str, **kwargs: Any) -> Any:
        """Send an authenticated request and return its JSON. A 401 renews the token and
        retries once; 429, 5xx and network failures are retried with backoff. Raises
        ShiprocketError with Shiprocket's message otherwise, also when Shiprocket
        answers 200 with an error status in the body."""
        token = self.tokens.token()
        renewed = False
        attempt = 0
        while True:
            try:
                response = self._http.request(
                    method, path, headers={"Authorization": f"Bearer {token}"}, **kwargs
                )
            except httpx.TransportError as exc:
                if attempt >= self.max_retries:
                    raise ShiprocketError(f"Couldn't reach Shiprocket: {exc}") from exc
                self._backoff(method, path, str(exc), attempt, None)
                attempt += 1
                continue
            if response.status_code == 401 and not renewed:
                token = self.tokens.token(renew=True, stale=token)
                renewed = True
                continue
            if response.status_code in RETRY_STATUSES and attempt < self.max_retries:
                self._backoff(method, path, str(response.status_code), attempt, response)
                attempt += 1
                continue
            break
        return self._result(method, path, response)

    def get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        return self.request("GET", path, params=params)

    def post(self, path: str, payload: dict[str, Any] | None = None) -> Any:
        return self.request("POST", path, json=payload or {})

    def _backoff(
        self, method: str, path: str, reason: str, attempt: int, response: httpx.Response | None
    ) -> None:
        delay = float(min(2**attempt, 30))
        retry_after = response.headers.get("Retry-After") if response is not None else None
        if retry_after:
            try:
                delay = max(float(retry_after), 0.5)
            except ValueError:
                pass
        logger.warning(
            "Shiprocket %s %s failed (%s); retrying in %.1fs", method, path, reason, delay
        )
        self._sleep(delay)

    def _result(self, method: str, path: str, response: httpx.Response) -> Any:
        if response.status_code == NOT_ALLOWED:
            raise ShiprocketError(not_allowed_message(self.tokens.base_url), NOT_ALLOWED)
        if response.status_code == 401:
            self.tokens.forget()
            raise ShiprocketAuthError(
                "Shiprocket refused the token twice: the API user may have been removed or "
                "lost access to this module.",
                401,
            )
        body = body_json(response)
        embedded = body.get("status_code") if isinstance(body, dict) else None
        if response.is_error or (isinstance(embedded, int) and embedded >= 400):
            status = response.status_code if response.is_error else embedded
            raise ShiprocketError(
                f"Shiprocket {method} {path} failed ({status}): {error_message(response)}",
                status,
                response.text[:1000],
            )
        if body is None:
            raise ShiprocketError(
                f"Shiprocket {method} {path} answered with no JSON", response.status_code
            )
        return body


_token_managers: dict[tuple[str, str, str], TokenManager] = {}
_managers_lock = threading.Lock()


def token_manager(base_url: str, email: str, password: str) -> TokenManager:
    """One token manager per API user and host, so the token is reused across requests;
    a changed password gets a new one."""
    key = (base_url.rstrip("/"), email, password)
    with _managers_lock:
        if key not in _token_managers:
            _token_managers[key] = TokenManager(base_url, email, password)
        return _token_managers[key]


def check_config() -> None:
    settings = get_settings()
    missing = [
        name
        for name, value in (
            ("SHIPROCKET_EMAIL", settings.shiprocket_email),
            ("SHIPROCKET_PASSWORD", settings.shiprocket_password),
        )
        if not value
    ]
    if missing:
        raise ShiprocketConfigError(f"Missing in .env: {', '.join(missing)}")
    if settings.is_production and settings.shiprocket_is_sandbox:
        raise ShiprocketConfigError(
            "SHIPROCKET_BASE_URL points to the Shiprocket sandbox in production"
        )


def get_shiprocket_client() -> ShiprocketClient:
    """A client built from settings. Use it as a context manager so it gets closed."""
    check_config()
    settings = get_settings()
    return ShiprocketClient(
        token_manager(
            settings.shiprocket_base_url, settings.shiprocket_email, settings.shiprocket_password
        )
    )
