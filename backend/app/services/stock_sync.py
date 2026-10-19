"""Push stock to Shopify and compare the two (#20).

The app's figure is the truth: Shopify's available quantity for a SKU is set to its
stock_qty (absolute, never a delta), only for SKUs enabled there; a SKU disabled from
the app (#47) stays at 0. Changes made in the app (manual adjustments, an order
cancelled in the app) are pushed; changes that came from Shopify (orders, cancellations,
returns) aren't, because Shopify already made them.

A failed push keeps the local change and stores Shopify's error on the listing item
(last_sync_error); POST /inventory/sync-pending, called by the polling job, retries.
"""

import logging
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from app.db import SessionLocal
from app.models import Article, Channel, ChannelAccount, ChannelListingItem, Item
from app.services import product_import
from app.services.shopify import ShopifyClient, ShopifyConfigError, ShopifyError
from app.services.shopify_catalog import (
    VARIANT_INVENTORY,
    gid,
    numeric,
    primary_location,
    set_available,
)

logger = logging.getLogger(__name__)

SHOPIFY = "SHOPIFY"
DRIFT_BATCH = 100  # variants per Shopify query (keeps the query cost under the limit)
PUSH_ATTEMPTS = 3  # re-push when stock changed while pushing, at most this often

DRIFT_QUERY = """
query ($ids: [ID!]!, $location: ID!) {
  nodes(ids: $ids) {
    ... on ProductVariant {
      id
      inventoryItem {
        id
        tracked
        inventoryLevel(locationId: $location) { quantities(names: ["available"]) { quantity } }
      }
    }
  }
}"""

# Shopify ids that never change, looked up once per process instead of being stored.
_location: dict[str, str] = {}
_inventory_items: dict[str, str] = {}  # variant id → inventory item gid


def clear_cache() -> None:
    _location.clear()
    _inventory_items.clear()


def location(shopify: ShopifyClient) -> str:
    if "id" not in _location:
        _location["id"] = primary_location(shopify)
    return _location["id"]


def inventory_item(shopify: ShopifyClient, variant_id: str) -> str:
    """The inventory item of this variant (never variants[0] of its product)."""
    if variant_id not in _inventory_items:
        variant = shopify.graphql(VARIANT_INVENTORY, {"id": gid("ProductVariant", variant_id)})[
            "productVariant"
        ]
        if variant is None:
            raise ShopifyError("This variant no longer exists on Shopify.")
        _inventory_items[variant_id] = variant["inventoryItem"]["id"]
    return _inventory_items[variant_id]


def shopify_target(stock_qty: int, listing_status: str) -> int:
    """What Shopify should show as available: the stock, or 0 for a disabled SKU."""
    return max(stock_qty, 0) if listing_status == "live" else 0


def shopify_listing_query(*filters):
    """Listing items on the Shopify account that are linked to a Shopify variant."""
    return (
        select(ChannelListingItem)
        .join(
            ChannelAccount,
            ChannelAccount.channel_account_id == ChannelListingItem.channel_account_id,
        )
        .join(Channel, Channel.channel_id == ChannelAccount.channel_id)
        .where(Channel.code == SHOPIFY, ChannelListingItem.channel_item_id.is_not(None), *filters)
    )


# --- Push ---------------------------------------------------------------------------------


@dataclass
class SyncOutcome:
    item_id: int
    synced: bool | None  # None: nothing to push (not on Shopify, or disabled there)
    error: str | None = None


def sync_items(
    item_ids: set[int] | list[int], *, session_factory: sessionmaker[Session] = SessionLocal
) -> dict[int, SyncOutcome]:
    """Push the stock of these items to Shopify, after their change was committed.
    Never raises: failures are stored on the listing items and returned."""
    with session_factory() as session:
        targets = [
            (listing_item.listing_item_id, listing_item.item_id, listing_item.channel_item_id)
            for listing_item in session.scalars(
                shopify_listing_query(
                    ChannelListingItem.item_id.in_(list(item_ids)),
                    ChannelListingItem.status == "live",
                ).order_by(ChannelListingItem.listing_item_id)
            )
        ]
    outcomes = {item_id: SyncOutcome(item_id, None) for item_id in item_ids}
    if not targets:
        return outcomes
    try:
        with product_import.shopify_client_factory() as shopify:
            for listing_item_id, item_id, variant_id in targets:
                outcomes[item_id] = push(session_factory, shopify, listing_item_id, variant_id)
    except ShopifyConfigError as exc:
        message = f"Shopify is not configured: {exc}"
        for listing_item_id, item_id, _ in targets:
            record(session_factory, listing_item_id, message)
            outcomes[item_id] = SyncOutcome(item_id, False, message)
    return outcomes


def push(
    session_factory: sessionmaker[Session],
    shopify: ShopifyClient,
    listing_item_id: int,
    variant_id: str,
) -> SyncOutcome:
    """Set Shopify's available quantity to the item's current stock. If the stock
    changed while pushing (an order, another adjustment), push the new figure, so the
    last value sent is always the latest."""
    sent = None
    try:
        for _ in range(PUSH_ATTEMPTS):
            with session_factory() as session:
                listing_item = session.get(ChannelListingItem, listing_item_id)
                item = session.get(Item, listing_item.item_id)
                target = shopify_target(item.stock_qty, listing_item.status)
            if target == sent:
                break
            set_available(shopify, inventory_item(shopify, variant_id), location(shopify), target)
            sent = target
    except ShopifyError as exc:
        logger.warning("Stock sync of listing item %s failed: %s", listing_item_id, exc)
        record(session_factory, listing_item_id, str(exc))
        return SyncOutcome(item.item_id, False, str(exc))
    record(session_factory, listing_item_id, None)
    return SyncOutcome(item.item_id, True)


def record(session_factory: sessionmaker[Session], listing_item_id: int, error: str | None) -> None:
    with session_factory.begin() as session:
        session.get(ChannelListingItem, listing_item_id).last_sync_error = (
            error[:2000] if error else None
        )


def sync_pending(*, session_factory: sessionmaker[Session] = SessionLocal) -> dict[str, Any]:
    """Retry every enabled Shopify SKU whose last sync failed."""
    with session_factory() as session:
        item_ids = {
            listing_item.item_id
            for listing_item in session.scalars(
                shopify_listing_query(
                    ChannelListingItem.last_sync_error.is_not(None),
                    ChannelListingItem.status == "live",
                )
            )
        }
    outcomes = sync_items(item_ids, session_factory=session_factory)
    failed = [outcome for outcome in outcomes.values() if outcome.synced is False]
    return {
        "retried": len(outcomes),
        "synced": len(outcomes) - len(failed),
        "failed": [{"item_id": o.item_id, "error": o.error} for o in failed],
    }


# --- Drift --------------------------------------------------------------------------------


def drift(session: Session, shopify: ShopifyClient, seller_id: int) -> dict[str, Any]:
    """Compare every Shopify-listed item's stock with Shopify's live available quantity
    (read from Shopify; nothing is stored). Untracked variants aren't compared."""
    rows = session.execute(
        shopify_listing_query(Item.seller_id == seller_id)
        .add_columns(Item, Article.name)
        .join(Item, Item.item_id == ChannelListingItem.item_id)
        .join(Article, Article.article_id == Item.article_id)
        .order_by(Article.name, Item.sku)
    ).all()
    loc = location(shopify)
    levels: dict[str, dict[str, Any]] = {}
    variant_ids = [listing_item.channel_item_id for listing_item, _, _ in rows]
    for start in range(0, len(variant_ids), DRIFT_BATCH):
        ids = [gid("ProductVariant", v) for v in variant_ids[start : start + DRIFT_BATCH]]
        for node in shopify.graphql(DRIFT_QUERY, {"ids": ids, "location": loc})["nodes"]:
            if node:
                levels[numeric(node["id"])] = node["inventoryItem"]
                _inventory_items[numeric(node["id"])] = node["inventoryItem"]["id"]

    items, checked, mismatched = [], 0, 0
    for listing_item, item, name in rows:
        level = levels.get(listing_item.channel_item_id)
        expected = shopify_target(item.stock_qty, listing_item.status)
        if level is None:
            state, shopify_qty = "missing", None  # variant deleted on Shopify
        else:
            quantities = (level.get("inventoryLevel") or {}).get("quantities") or []
            shopify_qty = quantities[0]["quantity"] if quantities else 0
            if not level.get("tracked"):
                state = "untracked"
            else:
                checked += 1
                state = "ok" if shopify_qty == expected else "drift"
                mismatched += state == "drift"
        items.append(
            {
                "item_id": item.item_id,
                "article_id": item.article_id,
                "article_name": name,
                "sku": item.sku,
                "listing_item_id": listing_item.listing_item_id,
                "stock_qty": item.stock_qty,
                "expected_on_shopify": expected,
                "shopify_qty": shopify_qty,
                "state": state,
            }
        )
    return {
        "checked": checked,
        "mismatched": mismatched,
        "accuracy_pct": round(100 * (checked - mismatched) / checked, 2) if checked else None,
        "items": items,
    }
