"""Write the catalog to Shopify: publish a product created in the app (#46) and enable /
disable products and SKUs (#47).

Shopify first: local rows change only after Shopify accepted the change, so the two
never disagree. Phase 0 limitation: Shopify can't hide a single variant, so a disabled
SKU is shown as sold out (stock tracked, overselling off, quantity 0).
"""

import re
import uuid
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Article, ChannelAccount, ChannelListing, ChannelListingItem, Item
from app.services.shopify import ShopifyClient, ShopifyError

MAX_OPTIONS = 3
DEFAULT_OPTION = ("Title", "Default Title")  # how Shopify names a product without options
ONLINE_STORE = "Online Store"

PRODUCT_SET = """
mutation ($input: ProductSetInput!, $identifier: ProductSetIdentifiers) {
  productSet(input: $input, identifier: $identifier, synchronous: true) {
    product { id handle variants(first: 250) { nodes { id sku selectedOptions { name value } } } }
    userErrors { field message }
  }
}"""
PRODUCT_UPDATE = """
mutation ($product: ProductUpdateInput!) {
  productUpdate(product: $product) { product { id status } userErrors { field message } }
}"""
PUBLISH = """
mutation ($id: ID!, $input: [PublicationInput!]!) {
  publishablePublish(id: $id, input: $input) { userErrors { field message } }
}"""
VARIANT_INVENTORY = """
query ($id: ID!) { productVariant(id: $id) { id product { id } inventoryItem { id } } }"""
VARIANTS_UPDATE = """
mutation ($productId: ID!, $variants: [ProductVariantsBulkInput!]!) {
  productVariantsBulkUpdate(productId: $productId, variants: $variants) {
    userErrors { field message }
  }
}"""
# Inventory mutations require an idempotency key since API 2026-04. A new key per change;
# the client's retries (e.g. after a 429) resend the same request and key, so Shopify
# applies the change once.
INVENTORY_ACTIVATE = """
mutation ($item: ID!, $location: ID!, $key: String!) {
  inventoryActivate(inventoryItemId: $item, locationId: $location) @idempotent(key: $key) {
    userErrors { field message }
  }
}"""
INVENTORY_SET = """
mutation ($input: InventorySetQuantitiesInput!, $key: String!) {
  inventorySetQuantities(input: $input) @idempotent(key: $key) { userErrors { field message } }
}"""
INVENTORY_LEVEL = """
query ($item: ID!, $location: ID!) {
  inventoryItem(id: $item) {
    inventoryLevel(locationId: $location) { quantities(names: ["available"]) { quantity } }
  }
}"""
LOCATION = "{ location { id } }"
PUBLICATIONS = "{ publications(first: 20) { nodes { id name } } }"


class CatalogConflict(Exception):
    """The request conflicts with the current state (e.g. already published)."""


class CatalogInvalid(ValueError):
    """The product can't be sent to Shopify as it is."""


def gid(kind: str, numeric_id: str) -> str:
    return f"gid://shopify/{kind}/{numeric_id}"


def numeric(global_id: str) -> str:
    return global_id.rsplit("/", 1)[-1]


def mutate(shopify: ShopifyClient, query: str, variables: dict[str, Any], name: str) -> dict:
    """Run a mutation; Shopify's userErrors become a ShopifyError."""
    result = shopify.graphql(query, variables)[name]
    if result.get("userErrors"):
        messages = "; ".join(error["message"] for error in result["userErrors"])
        raise ShopifyError(f"Shopify refused the change: {messages}")
    return result


def primary_location(shopify: ShopifyClient) -> str:
    return shopify.graphql(LOCATION)["location"]["id"]


def online_store(shopify: ShopifyClient) -> str:
    for publication in shopify.graphql(PUBLICATIONS)["publications"]["nodes"]:
        if publication["name"] == ONLINE_STORE:
            return publication["id"]
    raise ShopifyError("The store has no Online Store sales channel")


def slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")


# --- Publish (#46) ------------------------------------------------------------------------


@dataclass
class PublishResult:
    listing_id: int
    shopify_product_id: str
    variants: int
    enabled: bool
    warnings: list[str] = field(default_factory=list)


def publish_article(
    session: Session,
    shopify: ShopifyClient,
    account: ChannelAccount,
    article: Article,
    prices: dict[int, Decimal],
    enable: bool,
) -> PublishResult:
    """Create the article on Shopify and the listing rows that link it.

    Idempotent on Shopify: the handle is fixed per article (`<code>-<article id>`), so a
    retry after a failure updates the product the first attempt created instead of
    adding a second one. Raises CatalogConflict when already listed on the account.
    """
    if session.scalar(
        select(ChannelListing.listing_id).where(
            ChannelListing.article_id == article.article_id,
            ChannelListing.channel_account_id == account.channel_account_id,
        )
    ):
        raise CatalogConflict("This product is already on this Shopify store.")
    items = [
        item for item in sorted(article.items, key=lambda i: i.item_id) if item.status == "active"
    ]
    problems = []
    if article.status == "archived":
        problems.append("Archived products can't be published.")
    if len(article.variation_axes) > MAX_OPTIONS:
        problems.append(f"Shopify allows at most {MAX_OPTIONS} options.")
    if not items:
        problems.append("The product has no active variants.")
    missing_price = [i.sku for i in items if (prices.get(i.item_id) or i.mrp) is None]
    if missing_price:
        problems.append(f"Set a selling price for: {', '.join(missing_price)}.")
    if problems:
        raise CatalogInvalid(" ".join(problems))

    location = primary_location(shopify)
    axes = article.variation_axes or [DEFAULT_OPTION[0]]

    def option_values(item: Item) -> dict[str, str]:
        return (
            item.variant_values
            if article.variation_axes
            else {DEFAULT_OPTION[0]: DEFAULT_OPTION[1]}
        )

    variants = []
    for item in items:
        price = prices.get(item.item_id) or item.mrp
        variant: dict[str, Any] = {
            "optionValues": [
                {"optionName": axis, "name": option_values(item)[axis]} for axis in axes
            ],
            "price": str(price),
            "sku": item.sku,
            "inventoryPolicy": "DENY",
            "inventoryItem": {"tracked": True},
            "inventoryQuantities": [
                {"locationId": location, "name": "available", "quantity": max(item.stock_qty, 0)}
            ],
        }
        if item.mrp and item.mrp > price:
            variant["compareAtPrice"] = str(item.mrp)
        if item.barcode:
            variant["barcodes"] = [item.barcode]
        if item.weight:
            variant["inventoryItem"]["measurement"] = {
                "weight": {"value": float(item.weight), "unit": "GRAMS"}
            }
        variants.append(variant)

    handle = f"{slug(article.article_code) or 'product'}-{article.article_id}"
    product_input = {
        "handle": handle,
        "title": article.name,
        "descriptionHtml": article.description or "",
        "vendor": article.brand or "",
        "productType": article.category or "",
        "status": "ACTIVE" if enable else "DRAFT",
        "productOptions": [
            {
                "name": axis,
                "values": [
                    {"name": v} for v in dict.fromkeys(option_values(i)[axis] for i in items)
                ],
            }
            for axis in axes
        ],
        "variants": variants,
        "files": [
            {"originalSource": image["url"], "contentType": "IMAGE", "alt": article.name}
            for image in article.images or []
            if image.get("url")
        ],
    }
    product = mutate(
        shopify,
        PRODUCT_SET,
        {"input": product_input, "identifier": {"handle": handle}},
        "productSet",
    )["product"]

    by_values = {
        tuple(sorted((o["name"], o["value"]) for o in v["selectedOptions"])): v
        for v in product["variants"]["nodes"]
    }
    listing = ChannelListing(
        article_id=article.article_id,
        channel_account_id=account.channel_account_id,
        split_key={},
        channel_parent_id=numeric(product["id"]),
        status="live" if enable else "draft",
    )
    session.add(listing)
    session.flush()
    warnings = []
    linked = 0
    for item in items:
        shopify_variant = by_values.get(tuple(sorted(option_values(item).items())))
        if shopify_variant is None:
            warnings.append(f"Shopify returned no variant for {item.sku}.")
            continue
        linked += 1
        session.add(
            ChannelListingItem(
                listing_id=listing.listing_id,
                item_id=item.item_id,
                channel_account_id=account.channel_account_id,
                channel_item_id=numeric(shopify_variant["id"]),
                channel_sku=item.sku,
                selling_price=prices.get(item.item_id) or item.mrp,
                status="live",
            )
        )
    if article.status == "draft":
        article.status = "active"  # published products are active in the catalog
    session.flush()

    if enable:
        try:
            mutate(
                shopify,
                PUBLISH,
                {"id": product["id"], "input": [{"publicationId": online_store(shopify)}]},
                "publishablePublish",
            )
        except ShopifyError as exc:
            listing.status = "draft"
            warnings.append(f"Created on Shopify, but not shown on the Online Store: {exc}")
    return PublishResult(
        listing_id=listing.listing_id,
        shopify_product_id=listing.channel_parent_id,
        variants=linked,
        enabled=listing.status == "live",
        warnings=warnings,
    )


# --- Enable / disable (#47) ---------------------------------------------------------------


def set_listing_status(
    session: Session, shopify: ShopifyClient, listing: ChannelListing, status: str
) -> None:
    """Enable (status ACTIVE + Online Store) or disable (status DRAFT, hidden from every
    sales channel) a product on Shopify, then record it locally. Per-SKU states are kept:
    a SKU disabled before stays disabled when the product is enabled again. A product put
    on sale is active in the catalog, so an archived or draft article becomes active."""
    product_id = gid("Product", listing.channel_parent_id)
    if status == "live":
        mutate(
            shopify,
            PRODUCT_UPDATE,
            {"product": {"id": product_id, "status": "ACTIVE"}},
            "productUpdate",
        )
        mutate(
            shopify,
            PUBLISH,
            {"id": product_id, "input": [{"publicationId": online_store(shopify)}]},
            "publishablePublish",
        )
        session.get(Article, listing.article_id).status = "active"
    else:
        mutate(
            shopify,
            PRODUCT_UPDATE,
            {"product": {"id": product_id, "status": "DRAFT"}},
            "productUpdate",
        )
    listing.status = status


def set_listing_item_status(
    session: Session, shopify: ShopifyClient, listing_item: ChannelListingItem, status: str
) -> None:
    """Disable a SKU (sold out: stock tracked, overselling off, quantity 0) or enable it
    (quantity back to the item's stock_qty), then record it locally."""
    variant = shopify.graphql(
        VARIANT_INVENTORY, {"id": gid("ProductVariant", listing_item.channel_item_id)}
    )["productVariant"]
    if variant is None:
        raise ShopifyError("This variant no longer exists on Shopify.")
    inventory_item, location = variant["inventoryItem"]["id"], primary_location(shopify)
    mutate(
        shopify,
        VARIANTS_UPDATE,
        {
            "productId": variant["product"]["id"],
            "variants": [
                {"id": variant["id"], "inventoryPolicy": "DENY", "inventoryItem": {"tracked": True}}
            ],
        },
        "productVariantsBulkUpdate",
    )
    # Stocked at the location (no-op when it already is), then the quantity.
    shopify.graphql(
        INVENTORY_ACTIVATE,
        {"item": inventory_item, "location": location, "key": str(uuid.uuid4())},
    )
    item = session.get(Item, listing_item.item_id)
    set_available(
        shopify, inventory_item, location, max(item.stock_qty, 0) if status == "live" else 0
    )
    listing_item.status = status


def available(shopify: ShopifyClient, inventory_item: str, location: str) -> int:
    level = shopify.graphql(INVENTORY_LEVEL, {"item": inventory_item, "location": location})[
        "inventoryItem"
    ]["inventoryLevel"]
    return level["quantities"][0]["quantity"] if level else 0


def set_available(
    shopify: ShopifyClient, inventory_item: str, location: str, quantity: int
) -> None:
    """Set the available quantity. Shopify requires the quantity we expect to replace
    (compare-and-swap, since 2026-04): if it changed between our read and the write,
    read it again and retry once; any other error is raised as it is."""
    for attempt in (1, 2):
        expected = available(shopify, inventory_item, location)
        try:
            mutate(
                shopify,
                INVENTORY_SET,
                {
                    "input": {
                        "name": "available",
                        "reason": "correction",
                        "quantities": [
                            {
                                "inventoryItemId": inventory_item,
                                "locationId": location,
                                "quantity": quantity,
                                "changeFromQuantity": expected,
                            }
                        ],
                    },
                    "key": str(uuid.uuid4()),
                },
                "inventorySetQuantities",
            )
            return
        except ShopifyError:
            if attempt == 2 or available(shopify, inventory_item, location) == expected:
                raise
