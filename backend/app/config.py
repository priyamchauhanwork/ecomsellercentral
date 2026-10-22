"""Application settings, read from environment variables and the repo-root .env file."""

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict

BACKEND_DIR = Path(__file__).resolve().parent.parent
REPO_ROOT = BACKEND_DIR.parent


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        # Real environment variables (e.g. on Render) take precedence over these files.
        env_file=(REPO_ROOT / ".env", BACKEND_DIR / ".env"),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # --- Server ---
    # Phase 0 is single-seller: every row belongs to this seller (ecs_sellers).
    seller_id: int = 1
    port: int = 8000
    environment: Literal["development", "production"] = "development"
    cors_origins: str = "http://localhost:5173"
    api_shared_secret: str = ""

    # --- Database ---
    database_url: str

    # --- Shopify ---
    shopify_store_domain: str = ""
    shopify_admin_api_access_token: str = ""
    shopify_api_key: str = ""
    shopify_api_secret: str = ""
    shopify_api_version: str = "2026-10"
    shopify_webhook_secret: str = ""

    # --- Shiprocket (#57) ---
    # The API user created in Shiprocket → Settings → API (not the panel login).
    shiprocket_email: str = ""
    shiprocket_password: str = ""
    shiprocket_webhook_token: str = ""  # sent by Shiprocket as x-api-key (#62)
    # Production by default; the sandbox URL from Shiprocket in development and tests.
    shiprocket_base_url: str = "https://apiv2.shiprocket.in/v1/external"

    # --- Seller details (invoices and labels) ---
    seller_name: str = ""
    seller_gstin: str = ""
    seller_address: str = ""
    seller_phone: str = ""
    seller_email: str = ""

    # --- Integrations ---
    sendgrid_api_key: str = ""
    sentry_dsn: str = ""

    @property
    def is_production(self) -> bool:
        return self.environment == "production"

    @property
    def shopify_webhook_signing_secret(self) -> str:
        """Dev Dashboard apps sign webhooks with the client secret; an explicit
        SHOPIFY_WEBHOOK_SECRET overrides it."""
        return self.shopify_webhook_secret or self.shopify_api_secret

    @property
    def shopify_webhook_secrets(self) -> list[str]:
        """Every secret a webhook may be signed with: subscriptions made in the Shopify
        admin use the store's secret, those made by the app (return topics, #53) the
        app's client secret."""
        return [s for s in (self.shopify_webhook_secret, self.shopify_api_secret) if s]

    @property
    def shiprocket_is_sandbox(self) -> bool:
        return "sandbox" in self.shiprocket_base_url.lower()

    @property
    def cors_origin_list(self) -> list[str]:
        return [origin.strip() for origin in self.cors_origins.split(",") if origin.strip()]

    @property
    def sqlalchemy_database_url(self) -> str:
        """DATABASE_URL with the psycopg 3 driver, whatever scheme Render or .env uses."""
        url = self.database_url
        for prefix in ("postgres://", "postgresql://"):
            if url.startswith(prefix):
                return "postgresql+psycopg://" + url[len(prefix) :]
        return url


@lru_cache
def get_settings() -> Settings:
    return Settings()
