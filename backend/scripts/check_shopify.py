"""Check the Shopify connection: prints the shop name and product count.

Run from backend/:  uv run python -m scripts.check_shopify
"""

import sys

from app.services.shopify import ShopifyConfigError, ShopifyError, get_shopify_client


def main() -> int:
    try:
        with get_shopify_client() as shopify:
            shop = shopify.get("/shop.json")["shop"]
            product_count = shopify.get("/products/count.json")["count"]
    except ShopifyConfigError as exc:
        print(f"Not configured: {exc}", file=sys.stderr)
        return 1
    except ShopifyError as exc:
        print(f"Error: {exc}\n{exc.body}", file=sys.stderr)
        return 1

    print(f"Shop:        {shop['name']} ({shop['myshopify_domain']})")
    print(f"Products:    {product_count}")
    print(f"API version: {shopify.api_version}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
