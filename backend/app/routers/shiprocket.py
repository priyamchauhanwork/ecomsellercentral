"""Shiprocket connection (#57): status and a connection test."""

from datetime import datetime
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.auth import require_api_key
from app.db import get_db
from app.services import shiprocket_connection
from app.services.shiprocket import ShiprocketAuthError, ShiprocketConfigError, ShiprocketError

router = APIRouter(
    prefix="/shiprocket", tags=["shiprocket"], dependencies=[Depends(require_api_key)]
)


class ConnectionStatus(BaseModel):
    configured: bool
    config_error: str | None
    environment: Literal["sandbox", "production"]
    base_url: str
    api_user: str | None
    webhook_token_set: bool
    token_expires_at: datetime | None  # null until the first login in this process
    connection_status: str | None  # active / auth_expired / error; null before a test
    last_connected_at: datetime | None
    last_error: str | None


class PickupLocation(BaseModel):
    name: str
    address: str
    city: str | None
    state: str | None
    pincode: str | None
    phone_verified: bool | None
    is_primary: bool


class ConnectionTest(BaseModel):
    pickup_locations: list[PickupLocation]
    wallet_balance: str | None
    wallet_error: str | None  # e.g. the API user lacks the Account module
    token_expires_at: datetime | None
    status: ConnectionStatus


@router.get("/status", response_model=ConnectionStatus)
def get_status(db: Session = Depends(get_db)):
    """Whether Shiprocket is configured and how the last call went (no call made)."""
    return shiprocket_connection.status(db)


@router.post("/test", response_model=ConnectionTest)
def test(db: Session = Depends(get_db)):
    """Log in with the API user and read its pickup locations and wallet balance."""
    db.close()
    try:
        result = shiprocket_connection.test_connection()
    except ShiprocketConfigError as exc:
        raise HTTPException(503, f"Shiprocket is not configured: {exc}") from exc
    except ShiprocketAuthError as exc:
        raise HTTPException(502, str(exc)) from exc
    except ShiprocketError as exc:
        raise HTTPException(502, f"Shiprocket: {exc}") from exc
    return ConnectionTest(**result, status=shiprocket_connection.status(db))
