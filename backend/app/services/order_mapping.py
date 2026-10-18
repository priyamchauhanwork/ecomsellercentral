"""Map a Shopify REST order (JSON) onto ecs_order_header / ecs_order_line values.

Pure functions with no database access, shared by the import (#9), webhooks (#11) and
polling (#12). The mapping rules are documented in docs/order-erd.md ("Shopify mapping").
"""

import re
from dataclasses import dataclass, field
from datetime import datetime
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Any

CENT = Decimal("0.01")
ZERO = Decimal("0.00")
MAX_TAX_RATE = Decimal("999.99")  # NUMERIC(5,2)
GSTIN_PATTERN = re.compile(r"^\d{2}[A-Z]{5}\d{4}[A-Z][A-Z\d]Z[A-Z\d]$")

# Ranks for "status never moves backwards". Cancelled and returned are terminal.
LINE_STATUS_RANK = {
    "pending": 0,
    "allocated": 1,
    "packed": 2,
    "shipped": 3,
    "delivered": 4,
    "return_requested": 5,
    "returned": 6,
    "cancelled": 7,
}
ORDER_STATUSES = ("pending", "shipped", "delivered", "cancelled")


class OrderMappingError(ValueError):
    """The order can't be mapped; `messages` lists every problem found."""

    def __init__(self, messages: list[str]) -> None:
        super().__init__("; ".join(messages))
        self.messages = messages


@dataclass
class MappedOrder:
    external_order_id: str
    external_updated_at: datetime | None
    header: dict[str, Any]
    lines: list[dict[str, Any]] = field(default_factory=list)


# --- Small parsers ----------------------------------------------------------------------


def clip(value: Any, length: int) -> str | None:
    """Text for a VARCHAR(length) column: stripped, empty → None, cut to fit."""
    if value is None:
        return None
    text = str(value).strip()
    return text[:length] if text else None


def money(value: Any, problems: list[str], label: str) -> Decimal:
    if value in (None, ""):
        return ZERO
    try:
        return Decimal(str(value)).quantize(CENT, rounding=ROUND_HALF_UP)
    except InvalidOperation:
        problems.append(f"{label} is not a number: {value!r}")
        return ZERO


def timestamp(value: Any, problems: list[str], label: str) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        problems.append(f"{label} is not a valid timestamp: {value!r}")
        return None
    if parsed.tzinfo is None:
        problems.append(f"{label} has no time zone: {value!r}")
        return None
    return parsed


def quantity(value: Any, problems: list[str], label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int | str):
        problems.append(f"{label} is not a whole number: {value!r}")
        return 0
    try:
        number = int(value)
    except ValueError:
        problems.append(f"{label} is not a whole number: {value!r}")
        return 0
    if number < 0:
        problems.append(f"{label} is negative: {number}")
        return 0
    return number


def shop_amount(price_set: Any) -> Any:
    """Amount in the shop currency from a Shopify `*_set` object."""
    if isinstance(price_set, dict):
        return (price_set.get("shop_money") or {}).get("amount")
    return None


def full_name(*sources: dict[str, Any] | None) -> str | None:
    for source in sources:
        if not source:
            continue
        name = " ".join(
            part.strip()
            for part in (source.get("first_name"), source.get("last_name"))
            if isinstance(part, str) and part.strip()
        )
        if name:
            return name
        if isinstance(source.get("name"), str) and source["name"].strip():
            return source["name"].strip()
    return None


# --- Addresses --------------------------------------------------------------------------

ADDRESS_FIELDS = {
    "name": ("name", 150),
    "phone": ("phone", 20),
    "address_line1": ("address1", 255),
    "address_line2": ("address2", 255),
    "city": ("city", 100),
    "state": ("province", 100),
    "state_code": ("province_code", 10),
    "pincode": ("zip", 10),
    "country": ("country", 60),
}


def address_columns(prefix: str, address: dict[str, Any] | None) -> dict[str, Any]:
    address = address or {}
    columns = {
        f"{prefix}_{column}": clip(address.get(key), length)
        for column, (key, length) in ADDRESS_FIELDS.items()
    }
    if not columns[f"{prefix}_name"]:
        columns[f"{prefix}_name"] = clip(full_name(address), 150)
    columns[f"{prefix}_landmark"] = None  # Shopify has no landmark field
    return columns


def same_address(shipping: dict[str, Any] | None, billing: dict[str, Any] | None) -> bool:
    if not billing:
        return True
    if not shipping:
        return False
    keys = [key for key, _ in ADDRESS_FIELDS.values()]
    return all((shipping.get(k) or "") == (billing.get(k) or "") for k in keys)


def gstin_from(address: dict[str, Any] | None) -> str | None:
    """Indian B2B stores often put the GSTIN in the billing company field."""
    company = ((address or {}).get("company") or "").strip().upper().replace(" ", "")
    return company if GSTIN_PATTERN.match(company) else None


# --- Lines ------------------------------------------------------------------------------


def classify_taxes(tax_lines: list[dict[str, Any]], problems: list[str], label: str):
    """Split Shopify tax lines into CGST / SGST / IGST. Taxes with any other title
    (e.g. a plain "GST" or "VAT") are a single-rate tax and go to IGST."""
    amounts = {"cgst_amount": ZERO, "sgst_amount": ZERO, "igst_amount": ZERO}
    rate = Decimal("0")
    for tax in tax_lines or []:
        title = str(tax.get("title") or "").upper()
        amount = money(tax.get("price"), problems, f"{label} tax {title or '?'}")
        if "CGST" in title:
            amounts["cgst_amount"] += amount
        elif "SGST" in title or "UTGST" in title:
            amounts["sgst_amount"] += amount
        else:
            amounts["igst_amount"] += amount
        try:
            rate += Decimal(str(tax.get("rate") or 0)) * 100
        except InvalidOperation:
            problems.append(f"{label} tax rate is not a number: {tax.get('rate')!r}")
    tax_rate = min(rate.quantize(CENT, rounding=ROUND_HALF_UP), MAX_TAX_RATE)
    return amounts, tax_rate


def line_status_for(active_qty: int, shipped_qty: int, returned_qty: int, delivered: bool) -> str:
    if active_qty <= 0:
        return "cancelled"
    if returned_qty > 0 and returned_qty >= active_qty:
        return "returned"
    if shipped_qty >= active_qty:
        return "delivered" if delivered else "shipped"
    return "pending"  # includes partially shipped lines


def map_line(
    item: dict[str, Any],
    line_number: int,
    order: dict[str, Any],
    problems: list[str],
) -> dict[str, Any]:
    label = f"Line {line_number}"
    line_id = item.get("id")
    if line_id in (None, ""):
        problems.append(f"{label} has no id")
    product_name = clip(item.get("title") or item.get("name") or item.get("sku"), 255)
    if not product_name:
        problems.append(f"{label} has no title")

    ordered_qty = quantity(item.get("quantity"), problems, f"{label} quantity")
    unit_price = money(item.get("price"), problems, f"{label} price")
    discount = sum(
        (
            money(a.get("amount"), problems, f"{label} discount")
            for a in item.get("discount_allocations") or []
        ),
        ZERO,
    )
    taxes, tax_rate = classify_taxes(item.get("tax_lines"), problems, label)

    # Fulfillments: shipped quantity, tracking, delivery.
    shipped_qty, delivered, shipment = 0, True, None
    for fulfillment in order.get("fulfillments") or []:
        if fulfillment.get("status") != "success":
            continue
        for fulfilled in fulfillment.get("line_items") or []:
            if fulfilled.get("id") != line_id:
                continue
            shipped_qty += quantity(fulfilled.get("quantity"), problems, f"{label} shipped qty")
            delivered = delivered and fulfillment.get("shipment_status") == "delivered"
            shipment = shipment or fulfillment
    if shipped_qty == 0:
        delivered = False

    # Refunds: returned and cancelled quantities, refunded amount.
    returned_qty = refund_cancelled = 0
    refunded_amount, refunded_at = ZERO, None
    for refund in order.get("refunds") or []:
        for refunded in refund.get("refund_line_items") or []:
            if refunded.get("line_item_id") != line_id:
                continue
            qty = quantity(refunded.get("quantity"), problems, f"{label} refund qty")
            if refunded.get("restock_type") == "return":
                returned_qty += qty
            elif refunded.get("restock_type") == "cancel":
                refund_cancelled += qty
            refunded_amount += money(refunded.get("subtotal"), problems, f"{label} refund")
            refunded_at = timestamp(refund.get("created_at"), problems, f"{label} refund date")

    # Cancelled quantity: removed by an order edit or refund-cancel; on a cancelled
    # order, everything not shipped.
    cancelled_qty = refund_cancelled
    current = item.get("current_quantity")
    if isinstance(current, int) and not isinstance(current, bool):
        cancelled_qty = max(cancelled_qty, ordered_qty - current - returned_qty)
    if order.get("cancelled_at"):
        cancelled_qty = max(cancelled_qty, ordered_qty - shipped_qty)
    cancelled_qty = max(0, min(cancelled_qty, ordered_qty))
    shipped_qty = min(shipped_qty, ordered_qty)
    returned_qty = min(returned_qty, ordered_qty)
    active_qty = ordered_qty - cancelled_qty

    shipped_at = (
        timestamp(shipment.get("created_at"), problems, f"{label} shipped date")
        if shipment
        else None
    )
    delivered_at = (
        timestamp(shipment.get("updated_at"), problems, f"{label} delivered date")
        if shipment and delivered
        else None
    )

    return {
        "external_line_id": clip(line_id, 100),
        "line_number": line_number,
        "product_id": None,  # linked to the catalog by #13
        "variant_id": None,
        "sku": clip(item.get("sku"), 100),
        "product_name": product_name,
        "variant_name": clip(item.get("variant_title"), 255),
        "hsn_code": None,  # not on Shopify orders
        "ordered_qty": ordered_qty,
        "shipped_qty": shipped_qty,
        "cancelled_qty": cancelled_qty,
        "returned_qty": returned_qty,
        "unit_price": unit_price,
        "line_discount_amount": discount,
        "tax_rate": tax_rate,
        **taxes,
        "line_total_amount": (unit_price * ordered_qty - discount).quantize(CENT),
        "line_paid_amount": ZERO,
        "line_refunded_amount": refunded_amount,
        "refund_reference": None,
        "refunded_at": refunded_at,
        "courier_name": clip((shipment or {}).get("tracking_company"), 100),
        "tracking_number": clip((shipment or {}).get("tracking_number"), 100),
        "shipped_at": shipped_at,
        "delivered_at": delivered_at,
        "line_status": line_status_for(active_qty, shipped_qty, returned_qty, delivered),
    }


def order_status_from_lines(line_statuses: list[str]) -> str:
    """Header status calculated from the lines (docs/order-erd.md)."""
    live = [status for status in line_statuses if status != "cancelled"]
    if not live:
        return "cancelled"
    if all(status in ("delivered", "returned") for status in live):
        return "delivered"
    if all(status in ("shipped", "delivered", "returned") for status in live):
        return "shipped"
    return "pending"


# --- Order ------------------------------------------------------------------------------


def map_shopify_order(order: Any) -> MappedOrder:
    """Map one Shopify order. Raises OrderMappingError listing every problem found."""
    if not isinstance(order, dict):
        raise OrderMappingError(["Order payload is not a JSON object"])
    problems: list[str] = []

    order_id = order.get("id")
    if order_id in (None, ""):
        problems.append("Order has no id")
    order_date = timestamp(order.get("created_at"), problems, "created_at")
    if order.get("created_at") in (None, ""):
        problems.append("Order has no created_at")
    updated_at = timestamp(order.get("updated_at"), problems, "updated_at")

    order_number = clip(
        order.get("name")
        or (f"#{order['order_number']}" if order.get("order_number") else None)
        or order_id,
        30,
    )

    customer = order.get("customer") or {}
    shipping = order.get("shipping_address")
    billing = order.get("billing_address")

    total = money(order.get("total_price"), problems, "total_price")
    shipping_amount = shop_amount(order.get("total_shipping_price_set"))
    if shipping_amount is None:
        shipping_amount = sum(
            (
                money(line.get("price"), problems, "shipping line")
                for line in order.get("shipping_lines") or []
            ),
            ZERO,
        )
    financial_status = clip(order.get("financial_status"), 20) or "pending"
    outstanding = money(order.get("total_outstanding"), problems, "total_outstanding")
    paid = (
        max(total - outstanding, ZERO)
        if financial_status in ("paid", "partially_paid", "partially_refunded", "refunded")
        else ZERO
    )
    refunded = sum(
        (
            money(t.get("amount"), problems, "refund amount")
            for refund in order.get("refunds") or []
            for t in refund.get("transactions") or []
            if t.get("kind") == "refund" and t.get("status") == "success"
        ),
        ZERO,
    )
    discount_codes = order.get("discount_codes") or []

    line_items = order.get("line_items")
    if not isinstance(line_items, list):
        problems.append("Order has no line_items list")
        line_items = []
    lines = [map_line(item, number, order, problems) for number, item in enumerate(line_items, 1)]
    line_ids = [line["external_line_id"] for line in lines if line["external_line_id"]]
    if len(line_ids) != len(set(line_ids)):
        problems.append("Order has duplicate line item ids")

    if problems:
        raise OrderMappingError(problems)

    header = {
        "order_number": order_number,
        "order_date": order_date,
        "customer_name": clip(full_name(customer, shipping, billing), 150),
        "customer_email": clip(
            order.get("email") or order.get("contact_email") or customer.get("email"), 255
        ),
        "customer_phone": clip(
            order.get("phone") or customer.get("phone") or (shipping or {}).get("phone"), 20
        ),
        **address_columns("ship", shipping),
        **address_columns("bill", billing or shipping),
        "bill_gstin": gstin_from(billing),
        "bill_same_as_ship": same_address(shipping, billing),
        "order_status": order_status_from_lines([line["line_status"] for line in lines]),
        "payment_status": financial_status,
        "fulfillment_status": clip(order.get("fulfillment_status"), 20) or "unfulfilled",
        "currency": (clip(order.get("currency"), 3) or "INR").upper(),
        "subtotal_amount": money(order.get("subtotal_price"), problems, "subtotal_price"),
        "discount_amount": money(order.get("total_discounts"), problems, "total_discounts"),
        "coupon_code": clip(discount_codes[0].get("code"), 50) if discount_codes else None,
        "shipping_amount": money(shipping_amount, problems, "shipping"),
        "tax_amount": money(order.get("total_tax"), problems, "total_tax"),
        "total_amount": total,
        "paid_amount": paid,
        "refunded_amount": refunded,
        "payment_method": None,
        "payment_gateway": clip(", ".join(order.get("payment_gateway_names") or []), 50),
        "payment_reference": None,
        "paid_at": (
            timestamp(order.get("processed_at"), problems, "processed_at") if paid else None
        ),
        "customer_notes": order.get("note") or None,
        "cancel_reason": clip(order.get("cancel_reason"), 1000),
        "cancelled_at": timestamp(order.get("cancelled_at"), problems, "cancelled_at"),
    }
    if problems:  # from the header parsing above
        raise OrderMappingError(problems)

    return MappedOrder(
        external_order_id=str(order_id),
        external_updated_at=updated_at,
        header=header,
        lines=lines,
    )
