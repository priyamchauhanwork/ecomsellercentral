"""The seller's Shiprocket connection (#57): its account row, status and test.

The account is an ecs_channel_accounts row on the SHIPROCKET channel (one per API user).
Credentials stay in env vars (credential_ref names them) and the token in memory, so the
database never holds a secret.
"""

from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session, sessionmaker

from app.config import get_settings
from app.db import SessionLocal
from app.models import Channel, ChannelAccount
from app.services import shiprocket
from app.services.order_mapping import clip
from app.services.shiprocket import (
    ShiprocketAuthError,
    ShiprocketClient,
    ShiprocketConfigError,
    ShiprocketError,
)

CHANNEL_CODE = "SHIPROCKET"  # ecs_channels.code
CREDENTIAL_REF = "env:SHIPROCKET_EMAIL,SHIPROCKET_PASSWORD"

# Swapped out in tests.
client_factory: Callable[[], ShiprocketClient] = shiprocket.get_shiprocket_client


def find_account(session: Session) -> ChannelAccount | None:
    settings = get_settings()
    return session.scalars(
        select(ChannelAccount)
        .join(Channel, Channel.channel_id == ChannelAccount.channel_id)
        .where(
            Channel.code == CHANNEL_CODE,
            ChannelAccount.seller_id == settings.seller_id,
            ChannelAccount.external_seller_id == settings.shiprocket_email,
        )
    ).one_or_none()


def ensure_account(session: Session) -> ChannelAccount:
    """The Shiprocket account row for the configured API user, created if missing."""
    settings = get_settings()
    session.execute(
        pg_insert(Channel)
        .values(code=CHANNEL_CODE, name="Shiprocket")
        .on_conflict_do_nothing(index_elements=["code"])
    )
    channel_id = session.scalar(select(Channel.channel_id).where(Channel.code == CHANNEL_CODE))
    session.execute(
        pg_insert(ChannelAccount)
        .values(
            seller_id=settings.seller_id,
            channel_id=channel_id,
            account_name=clip(f"Shiprocket · {settings.shiprocket_email}", 100),
            external_seller_id=clip(settings.shiprocket_email, 100),
            credential_ref=CREDENTIAL_REF,
            sync_enabled=False,  # nothing is synced on a schedule yet
        )
        .on_conflict_do_nothing()
    )
    return find_account(session)


def record_outcome(session_factory: sessionmaker[Session], error: Exception | None) -> None:
    """Keep the outcome of the latest call on the account row (own transaction)."""
    with session_factory.begin() as session:
        account = ensure_account(session)
        if error is None:
            account.connection_status = "active"
            account.last_error = None
            account.last_synced_at = datetime.now(UTC)
        else:
            account.connection_status = (
                "auth_expired" if isinstance(error, ShiprocketAuthError) else "error"
            )
            account.last_error = str(error)[:2000]


def pickup_locations(payload: Any) -> list[dict[str, Any]]:
    """Shiprocket's pickup locations, reduced to what the app shows."""
    data = payload.get("data") if isinstance(payload, dict) else None
    rows = data.get("shipping_address") if isinstance(data, dict) else None
    result = []
    for row in rows or []:
        if not isinstance(row, dict):
            continue
        verified = row.get("phone_verified")
        result.append(
            {
                "name": str(row.get("pickup_location") or ""),
                "address": " ".join(
                    str(part) for part in (row.get("address"), row.get("address_2")) if part
                ),
                "city": row.get("city"),
                "state": row.get("state"),
                "pincode": str(row.get("pin_code") or "") or None,
                "phone_verified": None if verified is None else bool(int(verified)),
                "is_primary": bool(row.get("is_primary_location")),
            }
        )
    return result


def wallet_balance(payload: Any) -> str | None:
    data = payload.get("data") if isinstance(payload, dict) else None
    balance = data.get("balance_amount") if isinstance(data, dict) else None
    return None if balance is None else str(balance)


def test_connection(session_factory: sessionmaker[Session] = SessionLocal) -> dict[str, Any]:
    """Log in afresh (proves the credentials) and read the pickup locations and wallet
    balance. The outcome is recorded on the account; errors are raised."""
    try:
        with client_factory() as client:
            client.tokens.token(renew=True)
            locations = pickup_locations(client.get("/settings/company/pickup"))
            try:
                balance, balance_error = (
                    wallet_balance(client.get("/account/details/wallet-balance")),
                    None,
                )
            except ShiprocketAuthError:
                raise
            except ShiprocketError as exc:  # the API user may lack the Account module
                balance, balance_error = None, str(exc)
            expires_at = client.tokens.expires_at
    except ShiprocketConfigError:
        raise
    except ShiprocketError as exc:
        record_outcome(session_factory, exc)
        raise
    record_outcome(session_factory, None)
    return {
        "pickup_locations": locations,
        "wallet_balance": balance,
        "wallet_error": balance_error,
        "token_expires_at": expires_at,
    }


def status(session: Session) -> dict[str, Any]:
    """What the settings page shows; no call to Shiprocket."""
    settings = get_settings()
    try:
        shiprocket.check_config()
        configured, config_error = True, None
    except ShiprocketConfigError as exc:
        configured, config_error = False, str(exc)
    account = find_account(session) if settings.shiprocket_email else None
    tokens = (
        shiprocket.token_manager(
            settings.shiprocket_base_url, settings.shiprocket_email, settings.shiprocket_password
        )
        if configured
        else None
    )
    return {
        "configured": configured,
        "config_error": config_error,
        "environment": "sandbox" if settings.shiprocket_is_sandbox else "production",
        "base_url": settings.shiprocket_base_url,
        "api_user": settings.shiprocket_email or None,
        "webhook_token_set": bool(settings.shiprocket_webhook_token),
        "token_expires_at": tokens.expires_at if tokens else None,
        "connection_status": account.connection_status if account else None,
        "last_connected_at": account.last_synced_at if account else None,
        "last_error": account.last_error if account else None,
    }
