"""Realistic Shopify REST order payloads for tests (shape of /admin/api/2026-10/orders.json)."""

import copy
from typing import Any

ADDRESS = {
    "first_name": "Asha",
    "last_name": "Verma",
    "name": "Asha Verma",
    "phone": "+91 98765 43210",
    "address1": "12 MG Road",
    "address2": "Flat 4B",
    "city": "Bengaluru",
    "province": "Karnataka",
    "province_code": "KA",
    "zip": "560001",
    "country": "India",
    "company": None,
}


def line_item(line_id: int = 9001, **overrides: Any) -> dict[str, Any]:
    item = {
        "id": line_id,
        "product_id": 7001,
        "variant_id": 8001,
        "sku": "WATCH-BLK-42",
        "title": "Classic Watch",
        "variant_title": "Black / 42mm",
        "name": "Classic Watch - Black / 42mm",
        "quantity": 2,
        "current_quantity": 2,
        "fulfillable_quantity": 2,
        "price": "1499.50",
        "discount_allocations": [{"amount": "100.00"}],
        "tax_lines": [
            {"title": "CGST", "rate": 0.09, "price": "125.96"},
            {"title": "SGST", "rate": 0.09, "price": "125.96"},
        ],
    }
    item.update(overrides)
    return item


def shopify_order(order_id: int = 5001, **overrides: Any) -> dict[str, Any]:
    order = {
        "id": order_id,
        "name": f"#{order_id - 4000}",
        "order_number": order_id - 4000,
        "created_at": "2026-10-17T18:30:00+05:30",
        "updated_at": "2026-10-17T18:31:00+05:30",
        "processed_at": "2026-10-17T18:30:05+05:30",
        "email": "asha@example.com",
        "phone": None,
        "currency": "INR",
        "financial_status": "paid",
        "fulfillment_status": None,
        "subtotal_price": "2899.00",
        "total_discounts": "100.00",
        "total_shipping_price_set": {"shop_money": {"amount": "50.00", "currency_code": "INR"}},
        "total_tax": "251.92",
        "total_price": "2949.00",
        "total_outstanding": "0.00",
        "discount_codes": [{"code": "DIWALI100", "amount": "100.00"}],
        "payment_gateway_names": ["razorpay"],
        "note": "Gift wrap please",
        "cancelled_at": None,
        "cancel_reason": None,
        "customer": {
            "first_name": "Asha",
            "last_name": "Verma",
            "email": "asha@example.com",
            "phone": "+91 98765 43210",
        },
        "shipping_address": copy.deepcopy(ADDRESS),
        "billing_address": copy.deepcopy(ADDRESS),
        "line_items": [line_item()],
        "fulfillments": [],
        "refunds": [],
    }
    order.update(overrides)
    return order


def fulfilled(order: dict[str, Any], *, delivered: bool = False, qty: int | None = None):
    """Add a successful fulfillment covering every line item (or `qty` of each)."""
    order["fulfillments"] = [
        {
            "id": 3001,
            "status": "success",
            "shipment_status": "delivered" if delivered else "in_transit",
            "tracking_company": "Delhivery",
            "tracking_number": "DL123456789IN",
            "created_at": "2026-10-18T10:00:00+05:30",
            "updated_at": "2026-10-19T15:00:00+05:30",
            "line_items": [
                {"id": item["id"], "quantity": qty if qty is not None else item["quantity"]}
                for item in order["line_items"]
            ],
        }
    ]
    order["fulfillment_status"] = "fulfilled" if qty is None else "partial"
    return order
