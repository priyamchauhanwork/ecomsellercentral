"""Shared-secret protection for API routes (Phase 0 has no user login).

Callers send `X-API-Key: <API_SHARED_SECRET>`. Every data router depends on
require_api_key; only /health and /webhooks (Shopify-signed) are open. Locally, an empty
API_SHARED_SECRET disables the check; in production it must be set.
"""

import hmac

from fastapi import HTTPException, Security
from fastapi.security import APIKeyHeader

from app.config import get_settings

# Declared as a security scheme so /docs shows an "Authorize" button for the key.
api_key_header = APIKeyHeader(name="X-API-Key", auto_error=False)


def require_api_key(api_key: str | None = Security(api_key_header)) -> None:
    settings = get_settings()
    secret = settings.api_shared_secret
    if not secret:
        if settings.is_production:
            raise HTTPException(503, "API_SHARED_SECRET is not configured")
        return
    if not hmac.compare_digest((api_key or "").encode(), secret.encode()):
        raise HTTPException(401, "Missing or invalid X-API-Key")
