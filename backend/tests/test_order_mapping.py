from datetime import UTC, datetime
from decimal import Decimal

import pytest

from app.services.order_mapping import (
    OrderMappingError,
    map_shopify_order,
    order_status_from_lines,
)
from tests.shopify_orders import fulfilled, line_item, shopify_order


def test_maps_header_and_line():
    mapped = map_shopify_order(shopify_order())
    h, line = mapped.header, mapped.lines[0]

    assert mapped.external_order_id == "5001"
    assert mapped.external_updated_at == datetime(2026, 10, 17, 13, 1, tzinfo=UTC)
    assert h["order_number"] == "#1001"
    assert h["order_date"] == datetime(2026, 10, 17, 13, 0, tzinfo=UTC)  # Shopify date, not now
    assert h["customer_name"] == "Asha Verma"
    assert (h["ship_address_line1"], h["ship_city"], h["ship_pincode"]) == (
        "12 MG Road",
        "Bengaluru",
        "560001",
    )
    assert h["ship_state_code"] == "KA" and h["bill_same_as_ship"] is True
    assert h["total_amount"] == Decimal("2949.00") and h["shipping_amount"] == Decimal("50.00")
    assert h["paid_amount"] == Decimal("2949.00") and h["coupon_code"] == "DIWALI100"
    assert h["payment_status"] == "paid" and h["fulfillment_status"] == "unfulfilled"
    assert h["order_status"] == "pending" and h["payment_gateway"] == "razorpay"

    assert line["external_line_id"] == "9001" and line["line_number"] == 1
    assert line["product_name"] == "Classic Watch" and line["variant_name"] == "Black / 42mm"
    assert line["ordered_qty"] == 2 and line["unit_price"] == Decimal("1499.50")
    assert line["line_discount_amount"] == Decimal("100.00")
    assert line["line_total_amount"] == Decimal("2899.00")
    assert (line["cgst_amount"], line["sgst_amount"], line["igst_amount"]) == (
        Decimal("125.96"),
        Decimal("125.96"),
        Decimal("0.00"),
    )
    assert line["tax_rate"] == Decimal("18.00")
    assert line["variant_id"] is None  # linked by the product import (#13)


def test_guest_order_without_customer_or_addresses():
    order = shopify_order(
        customer=None,
        shipping_address=None,
        billing_address=None,
        email=None,
        contact_email="guest@example.com",
    )
    h = map_shopify_order(order).header
    assert h["customer_name"] is None
    assert h["customer_email"] == "guest@example.com"
    assert h["ship_city"] is None and h["bill_same_as_ship"] is True


def test_customer_name_falls_back_to_shipping_address():
    order = shopify_order(customer={"first_name": None, "last_name": None})
    assert map_shopify_order(order).header["customer_name"] == "Asha Verma"


def test_separate_billing_address_and_gstin_from_company():
    order = shopify_order()
    order["billing_address"] = {
        **order["billing_address"],
        "city": "Mumbai",
        "company": "29abcde1234f1z5",
    }
    h = map_shopify_order(order).header
    assert h["bill_same_as_ship"] is False and h["bill_city"] == "Mumbai"
    assert h["bill_gstin"] == "29ABCDE1234F1Z5"


def test_long_values_are_clipped_to_column_sizes():
    order = shopify_order(name="#" + "9" * 50)
    order["line_items"] = [line_item(title="W" * 400, sku="S" * 300)]
    order["shipping_address"]["phone"] = "+91 " + "9" * 40
    mapped = map_shopify_order(order)
    assert len(mapped.header["order_number"]) == 30
    assert len(mapped.header["ship_phone"]) == 20
    assert len(mapped.lines[0]["product_name"]) == 255 and len(mapped.lines[0]["sku"]) == 100


def test_igst_and_unknown_tax_titles():
    order = shopify_order()
    order["line_items"] = [
        line_item(tax_lines=[{"title": "IGST", "rate": 0.18, "price": "250"}]),
        line_item(9002, tax_lines=[{"title": "VAT", "rate": 0.05, "price": "70"}]),
    ]
    lines = map_shopify_order(order).lines
    assert lines[0]["igst_amount"] == Decimal("250.00") and lines[0]["cgst_amount"] == 0
    assert lines[1]["igst_amount"] == Decimal("70.00") and lines[1]["tax_rate"] == Decimal("5.00")


def test_fulfilled_and_delivered_lines():
    shipped = map_shopify_order(fulfilled(shopify_order()))
    assert shipped.lines[0]["line_status"] == "shipped"
    assert shipped.lines[0]["shipped_qty"] == 2
    assert shipped.lines[0]["tracking_number"] == "DL123456789IN"
    assert shipped.lines[0]["courier_name"] == "Delhivery"
    assert shipped.header["order_status"] == "shipped"

    delivered = map_shopify_order(fulfilled(shopify_order(), delivered=True))
    assert delivered.lines[0]["line_status"] == "delivered"
    assert delivered.lines[0]["delivered_at"] is not None
    assert delivered.header["order_status"] == "delivered"


def test_partial_shipment_stays_pending():
    mapped = map_shopify_order(fulfilled(shopify_order(), qty=1))
    assert mapped.lines[0]["shipped_qty"] == 1 and mapped.lines[0]["line_status"] == "pending"
    assert mapped.header["order_status"] == "pending"
    assert mapped.header["fulfillment_status"] == "partial"


def test_failed_fulfillment_is_ignored():
    order = fulfilled(shopify_order())
    order["fulfillments"][0]["status"] = "cancelled"
    assert map_shopify_order(order).lines[0]["shipped_qty"] == 0


def test_cancelled_order_cancels_unshipped_quantity():
    order = shopify_order(
        cancelled_at="2026-10-18T09:00:00+05:30",
        cancel_reason="customer",
        financial_status="voided",
    )
    mapped = map_shopify_order(order)
    assert mapped.lines[0]["cancelled_qty"] == 2 and mapped.lines[0]["line_status"] == "cancelled"
    assert mapped.header["order_status"] == "cancelled"
    assert mapped.header["cancel_reason"] == "customer"
    assert mapped.header["paid_amount"] == 0


def test_item_removed_by_order_edit_counts_as_cancelled():
    order = shopify_order()
    order["line_items"] = [line_item(current_quantity=0), line_item(9002, current_quantity=2)]
    mapped = map_shopify_order(order)
    assert mapped.lines[0]["line_status"] == "cancelled" and mapped.lines[0]["cancelled_qty"] == 2
    assert mapped.header["order_status"] == "pending"  # one live line left


def test_returned_and_refunded_quantities():
    order = fulfilled(shopify_order(), delivered=True)
    order["line_items"][0]["current_quantity"] = 0
    order["financial_status"] = "refunded"
    order["refunds"] = [
        {
            "created_at": "2026-10-25T12:00:00+05:30",
            "refund_line_items": [
                {
                    "line_item_id": 9001,
                    "quantity": 2,
                    "restock_type": "return",
                    "subtotal": "2899.00",
                }
            ],
            "transactions": [{"kind": "refund", "status": "success", "amount": "2949.00"}],
        }
    ]
    mapped = map_shopify_order(order)
    line = mapped.lines[0]
    assert line["returned_qty"] == 2 and line["cancelled_qty"] == 0
    assert line["line_status"] == "returned"
    assert line["line_refunded_amount"] == Decimal("2899.00")
    assert mapped.header["refunded_amount"] == Decimal("2949.00")
    assert mapped.header["order_status"] == "delivered"


def test_order_with_no_line_items():
    mapped = map_shopify_order(shopify_order(line_items=[]))
    assert mapped.lines == [] and mapped.header["order_status"] == "cancelled"


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"id": None}, "Order has no id"),
        ({"created_at": None}, "Order has no created_at"),
        ({"created_at": "yesterday"}, "created_at is not a valid timestamp"),
        ({"total_price": "abc"}, "total_price is not a number"),
        ({"line_items": "nope"}, "no line_items list"),
        ({"line_items": [line_item(quantity=-1)]}, "Line 1 quantity is negative"),
        ({"line_items": [line_item(quantity=1.5)]}, "Line 1 quantity is not a whole number"),
        ({"line_items": [line_item(id=None)]}, "Line 1 has no id"),
        ({"line_items": [line_item(title=None, name=None, sku=None)]}, "Line 1 has no title"),
        ({"line_items": [line_item(), line_item()]}, "duplicate line item ids"),
    ],
)
def test_invalid_orders_list_every_problem(overrides, message):
    with pytest.raises(OrderMappingError) as exc_info:
        map_shopify_order(shopify_order(**overrides))
    assert any(message in m for m in exc_info.value.messages)


def test_payload_must_be_an_object():
    with pytest.raises(OrderMappingError):
        map_shopify_order(["not", "an", "order"])


@pytest.mark.parametrize(
    ("statuses", "expected"),
    [
        (["pending", "shipped"], "pending"),
        (["shipped", "delivered"], "shipped"),
        (["delivered", "returned", "cancelled"], "delivered"),
        (["cancelled", "cancelled"], "cancelled"),
        ([], "cancelled"),
    ],
)
def test_order_status_from_lines(statuses, expected):
    assert order_status_from_lines(statuses) == expected
