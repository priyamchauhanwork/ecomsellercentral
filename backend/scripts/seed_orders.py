"""Create, or delete, realistic test orders directly in the database.

Run from backend/:
    uv run python -m scripts.seed_orders                  # 1000 orders
    uv run python -m scripts.seed_orders --count 200
    uv run python -m scripts.seed_orders --replace        # delete old seed data first
    uv run python -m scripts.seed_orders --delete         # remove all seed data

Seeded orders are numbered SEED-0001…, have external ids "seed-…", channel "website"
and ingestion method "direct", so they are easy to spot and --delete removes exactly
them (with their lines and history), never Shopify orders. Refuses to run in
production. The same --seed gives the same orders.
"""

import argparse
import random
import sys
from datetime import UTC, datetime, time, timedelta
from decimal import ROUND_HALF_UP, Decimal
from zoneinfo import ZoneInfo

from sqlalchemy import delete, func, select
from sqlalchemy.engine import make_url

from app.config import get_settings
from app.db import SessionLocal
from app.models import OrderHeader, OrderHistory, OrderLine, OrderLineHistory

SEED_PREFIX = "seed-"
IST = ZoneInfo("Asia/Kolkata")
CENT = Decimal("0.01")
DAYS_BACK = 120

STATUS_WEIGHTS = {"pending": 35, "shipped": 30, "delivered": 25, "cancelled": 10}
# Share of orders (%) by number of line items.
LINE_COUNT_WEIGHTS = {1: 45, 2: 30, 3: 15, 4: 10}
PRODUCTS = [
    ("Classic Chronograph Watch", "Black / 42mm", "WATCH-CHR-BLK-42", "1499.00"),
    ("Classic Chronograph Watch", "Silver / 40mm", "WATCH-CHR-SLV-40", "1499.00"),
    ("Minimal Leather Watch", "Brown", "WATCH-MIN-BRN", "999.00"),
    ("Smart Fitness Band", None, "BAND-FIT-01", "2499.00"),
    ("Leather Strap", "Tan / 20mm", "STRAP-LTH-TAN-20", "349.00"),
    ("Steel Mesh Strap", "Silver / 22mm", "STRAP-MESH-SLV-22", "449.00"),
    ("Watch Winder Box", None, "BOX-WINDER-01", "3299.00"),
    ("Screen Protector (2 pack)", None, "ACC-SCREEN-2", "199.00"),
]
FIRST_NAMES = [
    "Asha",
    "Rahul",
    "Priya",
    "Vikram",
    "Neha",
    "Arjun",
    "Kavya",
    "Rohan",
    "Isha",
    "Karan",
    "Meera",
    "Aditya",
    "Sneha",
    "Farhan",
    "Ananya",
    "Dev",
]
LAST_NAMES = [
    "Verma",
    "Sharma",
    "Iyer",
    "Reddy",
    "Gupta",
    "Nair",
    "Khan",
    "Patel",
    "Das",
    "Menon",
    "Singh",
    "Bose",
    "Joshi",
    "Kulkarni",
]
CITIES = [  # city, state, state code, pincode prefix
    ("Bengaluru", "Karnataka", "KA", "5600"),
    ("Mumbai", "Maharashtra", "MH", "4000"),
    ("New Delhi", "Delhi", "DL", "1100"),
    ("Chennai", "Tamil Nadu", "TN", "6000"),
    ("Hyderabad", "Telangana", "TS", "5000"),
    ("Kolkata", "West Bengal", "WB", "7000"),
    ("Pune", "Maharashtra", "MH", "4110"),
    ("Jaipur", "Rajasthan", "RJ", "3020"),
]
SELLER_STATE_CODE = "KA"  # intra-state orders get CGST + SGST, others IGST
STREETS = ["MG Road", "Park Street", "Linking Road", "Anna Salai", "Brigade Road"]
COURIERS = ["Delhivery", "Blue Dart", "DTDC", "Ecom Express"]
GATEWAYS = ["razorpay", "cash_on_delivery", "payu", "shopify_payments"]
CANCEL_REASONS = ["customer", "inventory", "fraud", "declined"]


def money(value: Decimal) -> Decimal:
    return value.quantize(CENT, rounding=ROUND_HALF_UP)


def order_time(rng: random.Random, index: int, now: datetime) -> datetime:
    """Mostly spread over the last DAYS_BACK days; some today, and some just either side
    of midnight in India, to exercise the date filters' day boundaries."""
    today = now.astimezone(IST).date()
    if index % 25 == 0:  # today
        day, clock = today, time(rng.randint(0, max(now.astimezone(IST).hour - 1, 0)), 15)
    elif index % 25 == 1:  # 23:50 the previous night
        day, clock = today - timedelta(days=rng.randint(1, 30)), time(23, 50)
    elif index % 25 == 2:  # 00:10, just after midnight
        day, clock = today - timedelta(days=rng.randint(1, 30)), time(0, 10)
    else:
        day = today - timedelta(days=rng.randint(0, DAYS_BACK))
        clock = time(rng.randint(8, 22), rng.randint(0, 59))
    placed = datetime.combine(day, clock, IST)
    return min(placed, now).astimezone(UTC)


def build_order(rng: random.Random, index: int, now: datetime, seller_id: int) -> OrderHeader:
    status = rng.choices(list(STATUS_WEIGHTS), weights=STATUS_WEIGHTS.values())[0]
    placed = order_time(rng, index, now)
    city, state, state_code, pin = rng.choice(CITIES)
    intra_state = state_code == SELLER_STATE_CODE
    usd = rng.random() < 0.03
    guest = rng.random() < 0.05

    first, last = rng.choice(FIRST_NAMES), rng.choice(LAST_NAMES)
    name = None if guest else f"{first} {last}"
    if index == 7:  # search must treat % and _ literally
        name = "50%_Off Watch Store"
    phone = f"+91 9{rng.randint(100000000, 999999999)}"

    shipped_at = placed + timedelta(days=rng.randint(1, 3))
    delivered_at = shipped_at + timedelta(days=rng.randint(2, 6))
    lines = []
    line_count = rng.choices(list(LINE_COUNT_WEIGHTS), weights=LINE_COUNT_WEIGHTS.values())[0]
    for number in range(1, line_count + 1):
        product, variant, sku, price = rng.choice(PRODUCTS)
        unit_price = Decimal(price) if not usd else money(Decimal(price) / 84)
        qty = rng.choice([1, 1, 1, 2, 3])
        discount = money(unit_price * qty * Decimal("0.1")) if rng.random() < 0.2 else Decimal("0")
        taxable = money(unit_price * qty - discount)
        tax = money(taxable * Decimal("0.18"))
        half = money(tax / 2)
        line = OrderLine(
            line_number=number,
            external_line_id=f"{SEED_PREFIX}{index}-{number}",
            sku=sku,
            product_name=product,
            variant_name=variant,
            ordered_qty=qty,
            unit_price=unit_price,
            line_discount_amount=discount,
            tax_rate=Decimal("18.00"),
            cgst_amount=half if intra_state else Decimal("0"),
            sgst_amount=tax - half if intra_state else Decimal("0"),
            igst_amount=Decimal("0") if intra_state else tax,
            line_total_amount=taxable,
            line_status=status,
            version_no=1 if status == "pending" else 2,
        )
        if status in ("shipped", "delivered"):
            line.shipped_qty = qty
            line.shipped_at = shipped_at
            line.courier_name = rng.choice(COURIERS)
            line.tracking_number = f"TRK{rng.randint(10**9, 10**10 - 1)}"
        if status == "delivered":
            line.delivered_at = delivered_at
        if status == "cancelled":
            line.cancelled_qty = qty
        lines.append(line)

    # A few pending orders are partly shipped: one line on its way, the rest not yet.
    if status == "pending" and len(lines) > 1 and rng.random() < 0.3:
        lines[0].shipped_qty = lines[0].ordered_qty
        lines[0].shipped_at = shipped_at
        lines[0].line_status = "shipped"

    subtotal = sum((line.line_total_amount for line in lines), Decimal("0"))
    discount = sum((line.line_discount_amount for line in lines), Decimal("0"))
    tax = sum(
        (line.cgst_amount + line.sgst_amount + line.igst_amount for line in lines), Decimal("0")
    )
    shipping = Decimal("0") if subtotal >= 999 or usd else Decimal("50.00")
    total = subtotal + shipping + tax
    gateway = rng.choice(GATEWAYS)
    if status == "cancelled":
        payment = rng.choice(["voided", "refunded"])
    elif gateway == "cash_on_delivery":
        payment = "paid" if status == "delivered" else "pending"
    else:
        payment = "paid"
    paid = total if payment in ("paid", "refunded") else Decimal("0")

    order = OrderHeader(
        seller_id=seller_id,
        order_number=f"SEED-{index:04d}",
        channel="website",
        external_order_id=f"{SEED_PREFIX}{index:06d}",
        ingestion_method="direct",
        order_date=placed,
        customer_name=name,
        customer_email=None if guest else f"{first}.{last}{index}@example.com".lower(),
        customer_phone=None if guest else phone,
        ship_name=name or "Guest",
        ship_phone=phone,
        ship_address_line1=f"{rng.randint(1, 400)}, {rng.choice(STREETS)}",
        ship_city=city,
        ship_state=state,
        ship_state_code=state_code,
        ship_pincode=f"{pin}{rng.randint(10, 99)}",
        ship_country="India",
        bill_same_as_ship=True,
        order_status=status,
        payment_status=payment,
        fulfillment_status={"pending": "unfulfilled", "cancelled": "unfulfilled"}.get(
            status, "fulfilled"
        ),
        currency="USD" if usd else "INR",
        subtotal_amount=subtotal,
        discount_amount=discount,
        coupon_code="WELCOME10" if discount else None,
        shipping_amount=shipping,
        tax_amount=tax,
        total_amount=total,
        paid_amount=paid,
        refunded_amount=paid if payment == "refunded" else Decimal("0"),
        payment_gateway=gateway,
        paid_at=placed if paid else None,
        customer_notes=rng.choice([None, None, None, "Please gift wrap", "Call before delivery"]),
        cancelled_at=placed + timedelta(hours=6) if status == "cancelled" else None,
        cancel_reason=rng.choice(CANCEL_REASONS) if status == "cancelled" else None,
        version_no=1 if status == "pending" else 2,
        lines=lines,
    )
    order.history = [
        OrderHistory(
            version_no=1,
            action_type="created",
            new_status="pending",
            change_source="system",
            changed_at=placed,
        )
    ]
    if status != "pending":
        changed_at = {"shipped": shipped_at, "delivered": delivered_at}.get(
            status, placed + timedelta(hours=6)
        )
        order.history.append(
            OrderHistory(
                version_no=2,
                action_type="cancelled" if status == "cancelled" else "status_change",
                old_status="pending",
                new_status=status,
                change_source="admin_ui",
                remarks="Seed data",
                changed_at=changed_at,
            )
        )
    return order


def line_history(order: OrderHeader) -> list[OrderLineHistory]:
    """A "created" entry per line; needs the ids, so call it after a flush."""
    return [
        OrderLineHistory(
            order_line_id=line.order_line_id,
            order_header_id=order.order_header_id,
            version_no=1,
            action_type="created",
            new_status="pending",
            change_source="system",
            changed_at=order.order_date,
        )
        for line in order.lines
    ]


def seeded_orders():
    return select(OrderHeader.order_header_id).where(
        OrderHeader.channel == "website", OrderHeader.external_order_id.like(f"{SEED_PREFIX}%")
    )


def delete_seed_data(session) -> int:
    ids = seeded_orders().scalar_subquery()
    count = session.scalar(select(func.count()).select_from(seeded_orders().subquery()))
    for model in (OrderLineHistory, OrderHistory, OrderLine):
        session.execute(delete(model).where(model.order_header_id.in_(ids)))
    session.execute(delete(OrderHeader).where(OrderHeader.order_header_id.in_(ids)))
    return count


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--count", type=int, default=1000, help="orders to create (1-9999)")
    parser.add_argument("--delete", action="store_true", help="remove all seed data")
    parser.add_argument("--replace", action="store_true", help="remove seed data, then seed")
    parser.add_argument("--seed", type=int, default=42, help="random seed (same seed, same data)")
    args = parser.parse_args(argv)
    if not 1 <= args.count <= 9999:
        parser.error("--count must be between 1 and 9999")

    settings = get_settings()
    if settings.is_production:
        print("Refusing to touch seed data in production.", file=sys.stderr)
        return 1
    url = make_url(settings.sqlalchemy_database_url)
    print(f"Database: {url.database} on {url.host}")

    with SessionLocal.begin() as session:
        existing = session.scalar(select(func.count()).select_from(seeded_orders().subquery()))
        if args.delete or args.replace:
            print(f"Deleted {delete_seed_data(session)} seed orders.")
            if args.delete:
                return 0
        elif existing:
            print(
                f"{existing} seed orders already exist. Use --replace to recreate them, "
                "or --delete to remove them.",
                file=sys.stderr,
            )
            return 1

        rng = random.Random(args.seed)
        now = datetime.now(UTC)
        orders = [build_order(rng, i, now, settings.seller_id) for i in range(1, args.count + 1)]
        session.add_all(orders)
        session.flush()
        session.add_all([entry for order in orders for entry in line_history(order)])
        by_status = {s: sum(o.order_status == s for o in orders) for s in STATUS_WEIGHTS}
        lines = sum(len(o.lines) for o in orders)
        print(
            f"Created {len(orders)} seed orders with {lines} lines: "
            + ", ".join(f"{n} {s}" for s, n in by_status.items())
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
