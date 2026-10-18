"""Create or update the store's Shopify connection (ecs_channel_connection).

Run from backend/:  uv run python -m scripts.connect_shopify
"""

import sys

from app.db import SessionLocal
from app.services.order_ingestion import ensure_shopify_connection
from app.services.shopify import ShopifyConfigError


def main() -> int:
    try:
        with SessionLocal.begin() as session:
            connection = ensure_shopify_connection(session)
            print(f"Connection:  {connection.channel_connection_id} ({connection.connection_name})")
            print(f"Store:       {connection.store_id}")
            print(f"Credentials: {connection.credential_ref}")
            print(f"Webhooks:    verified with {connection.webhook_secret_ref}")
    except ShopifyConfigError as exc:
        print(f"Not configured: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
