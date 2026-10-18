"""Register (or remove) the Shopify order webhooks for this store.

Run from backend/:
    uv run python -m scripts.register_webhooks https://<public-host>/webhooks/orders
    uv run python -m scripts.register_webhooks --remove

Shopify can't reach localhost: locally, use a tunnel URL (cloudflared / ngrok);
in production, the Render URL. Re-running is safe: existing subscriptions are kept
or pointed at the new URL.
"""

import argparse
import sys

from app.db import SessionLocal
from app.services.order_ingestion import ensure_shopify_connection
from app.services.shopify import ShopifyConfigError, ShopifyError, get_shopify_client

TOPICS = {"ORDERS_CREATE": "orders/create", "ORDERS_UPDATED": "orders/updated"}

LIST = "{ webhookSubscriptions(first: 100) { nodes { id topic uri } } }"
CREATE = """mutation ($topic: WebhookSubscriptionTopic!, $uri: String!) {
  webhookSubscriptionCreate(topic: $topic, webhookSubscription: {uri: $uri, format: JSON}) {
    webhookSubscription { id } userErrors { field message } } }"""
UPDATE = """mutation ($id: ID!, $uri: String!) {
  webhookSubscriptionUpdate(id: $id, webhookSubscription: {uri: $uri}) {
    webhookSubscription { id } userErrors { field message } } }"""
DELETE = """mutation ($id: ID!) {
  webhookSubscriptionDelete(id: $id) {
    deletedWebhookSubscriptionId userErrors { field message } } }"""


def run_mutation(shopify, query: str, variables: dict, name: str) -> None:
    errors = shopify.graphql(query, variables)[name]["userErrors"]
    if errors:
        raise ShopifyError(f"{name} failed: {errors}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("url", nargs="?", help="public https URL ending in /webhooks/orders")
    parser.add_argument("--remove", action="store_true", help="delete the order subscriptions")
    args = parser.parse_args()
    if not args.remove and not (args.url or "").startswith("https://"):
        parser.error("give the public https URL of /webhooks/orders, or --remove")

    try:
        with get_shopify_client() as shopify:
            existing = {
                node["topic"]: node
                for node in shopify.graphql(LIST)["webhookSubscriptions"]["nodes"]
                if node["topic"] in TOPICS
            }
            for topic in TOPICS:
                current = existing.get(topic)
                if args.remove:
                    if current:
                        run_mutation(
                            shopify, DELETE, {"id": current["id"]}, "webhookSubscriptionDelete"
                        )
                    print(f"{topic}: removed" if current else f"{topic}: not registered")
                elif current and current["uri"] == args.url:
                    print(f"{topic}: already registered")
                elif current:
                    run_mutation(
                        shopify,
                        UPDATE,
                        {"id": current["id"], "uri": args.url},
                        "webhookSubscriptionUpdate",
                    )
                    print(f"{topic}: moved from {current['uri']}")
                else:
                    run_mutation(
                        shopify,
                        CREATE,
                        {"topic": topic, "uri": args.url},
                        "webhookSubscriptionCreate",
                    )
                    print(f"{topic}: registered")

        with SessionLocal.begin() as session:
            connection = ensure_shopify_connection(session)
            connection.webhook_enabled = not args.remove
            connection.webhook_topics = [] if args.remove else list(TOPICS.values())
    except (ShopifyConfigError, ShopifyError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    print("Webhooks " + ("removed." if args.remove else f"point at {args.url}"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
