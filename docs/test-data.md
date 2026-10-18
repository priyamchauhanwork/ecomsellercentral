# Test orders

`backend/scripts/seed_orders.py` fills the **local** database with realistic test orders, for trying out the order screens (list, filters, detail, status changes) without creating orders in Shopify, and removes them afterwards.

## Before you start

- Docker Desktop is running and the database container is up (`docker start mvp-postgres`).
- The tables exist: `uv run alembic upgrade head` in `backend/` (see [SETUP.md](../SETUP.md)).
- Run the commands below from the `backend/` folder. The script prints which database it is using before it changes anything.

## Create test orders

```bash
uv run python -m scripts.seed_orders
```

This creates **1000 orders**, then prints a summary, for example:

```
Database: mvp_db on localhost
Created 1000 seed orders with 1878 lines: 364 pending, 286 shipped, 254 delivered, 96 cancelled
```

Open http://localhost:5173/orders (backend and frontend running) to see them.

### Options

| Command | What it does |
|---|---|
| `uv run python -m scripts.seed_orders --count 200` | Creates 200 orders instead of 1000 (1–9999). |
| `uv run python -m scripts.seed_orders --replace` | Deletes the existing test orders, then creates a new set. |
| `uv run python -m scripts.seed_orders --seed 7` | Uses a different random seed. The same seed always produces the same orders; the default is 42. |

Options can be combined, e.g. `--replace --count 500 --seed 7`.

If test orders already exist, the script stops without changing anything and asks you to use `--replace` or `--delete`, so you never get two batches.

## Delete test orders

```bash
uv run python -m scripts.seed_orders --delete
```

```
Database: mvp_db on localhost
Deleted 1000 seed orders.
```

This removes every test order together with its line items and history. Running it when there are no test orders is harmless.

## How test orders are told apart from real ones

Every test order has **all** of the following, which real Shopify orders never have:

- order number `SEED-0001`, `SEED-0002`, …
- external order id starting with `seed-`
- channel `website` and ingestion method `direct`

`--delete` removes only orders matching these, so orders imported from Shopify (import, sync or webhooks) are never touched.

## What the test orders contain

| Aspect | Mix |
|---|---|
| Status | about 35% pending, 30% shipped, 25% delivered, 10% cancelled; item statuses match the order |
| Items per order | about 45% one item, 30% two, 15% three, 10% four |
| Dates | spread over the last 120 days, including orders placed today and at 23:50 / 00:10 Indian time (to check the date filters' day boundaries) |
| Customers | named customers in 8 Indian cities; about 5% guest orders with no name, email or phone; one customer called `50%_Off Watch Store` (to check that search treats `%` and `_` literally) |
| Money | about 3% in USD, the rest in INR; GST at 18% (CGST + SGST for Karnataka addresses, IGST elsewhere); some discount codes; free shipping above ₹999 |
| Shipping | shipped and delivered orders have a courier and tracking number; some pending orders are partly shipped |
| Payment | paid, cash-on-delivery pending, and voided or refunded for cancelled orders |
| History | every order has a "created" entry; shipped, delivered and cancelled orders also have the status change |

The proportions are drawn at random, so actual counts vary slightly around these figures. To change them, edit `STATUS_WEIGHTS` or `LINE_COUNT_WEIGHTS` at the top of the script.

## Safety

- The script **refuses to run when `ENVIRONMENT=production`**, both for creating and deleting.
- Test orders are written straight to the database. They are not sent to Shopify, and syncing with Shopify neither changes nor removes them.
- Automated tests never use this data: `uv run pytest` runs against a separate `mvp_test` database.
