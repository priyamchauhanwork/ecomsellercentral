"""Create or update the seller's Shopify account (ecs_channel_accounts).

Run from backend/:  uv run python -m scripts.connect_shopify
"""

import sys

from app.db import SessionLocal
from app.services.order_ingestion import ensure_shopify_account
from app.services.shopify import ShopifyConfigError


def main() -> int:
    try:
        with SessionLocal.begin() as session:
            account = ensure_shopify_account(session)
            print(f"Account:     {account.channel_account_id} ({account.account_name})")
            print(f"Seller:      {account.seller_id}")
            print(f"Credentials: {account.credential_ref}")
            print(f"Webhooks:    verified with {account.webhook_secret_ref}")
    except ShopifyConfigError as exc:
        print(f"Not configured: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
