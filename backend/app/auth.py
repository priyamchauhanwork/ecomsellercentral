"""Shared-secret protection for API routes (Phase 0 has no user login).

Callers send `X-API-Key: <API_SHARED_SECRET>`. Locally, an empty API_SHARED_SECRET
disables the check; in production it must be set. #14 applies this to every data route.
"""

import hmac

from fastapi import Header, HTTPException

from app.config import get_settings


def require_api_key(x_api_key: str = Header("", include_in_schema=False)) -> None:
    secret = get_settings().api_shared_secret
    if not secret:
        if get_settings().is_production:
            raise HTTPException(503, "API_SHARED_SECRET is not configured")
        return
    if not hmac.compare_digest(x_api_key.encode(), secret.encode()):
        raise HTTPException(401, "Missing or invalid X-API-Key")
