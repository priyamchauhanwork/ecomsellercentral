# Inventory: supply & demand model

Source: *Inventory ERD: Supply & Demand Model* (19 Oct 2026). This file is the implementation reference: the model, the decisions taken on its open questions, every scenario with its effect on stock, and the edge cases. Tickets: epic #49, #50–#55.

## Model

Stock for the single ship node (Shopify's primary location) is held as **supply** (what the seller has or will get back) and **demand** (what orders have claimed).

| Side | Type | Meaning | Sellable |
| --- | --- | --- | --- |
| Supply | `ONHAND` | Units the seller owns, including shipped units until delivered | Yes |
| Supply | `RETURN_PENDING` | Units on an open return or RTO, coming back; dated with `eta` | No |
| Supply | `DAMAGED` | Units that failed QC or were found damaged | No |
| Demand | `OPEN` | Ordered, not yet acknowledged by the team | — |
| Demand | `ALLOCATED` | Acknowledged (order *processing*) | — |
| Demand | `SHIPPED` | Shipped, not yet delivered | — |

```
available        = ONHAND − (OPEN + ALLOCATED + SHIPPED)
future_available = available + RETURN_PENDING        (display / planning only)
on the shelf     = ONHAND − SHIPPED                   (what a physical count should find)
```

- `available` is what Shopify gets (floored at 0; 0 for a SKU disabled on Shopify). Negative = oversold.
- `ecs_items.stock_qty` is a cache of `available`, rewritten in the same transaction as every change. The view `ecs_item_stock` returns every figure per item.
- Each change to a supply or demand row writes one `ecs_inventory_log` row in the same transaction; the rows of one event share a `transaction_id`.

## Decisions on the open questions

| Question | Decision |
| --- | --- |
| Return ETA | `expected_at` = the day the return is first seen open (approved) + `ecs_sellers.return_eta_days` (default **10**). Editable per return. |
| RTO source | Entered by the team in the app ("Mark RTO" on shipped, undelivered units). A courier integration can create the same records later. |
| Shopify cancels an acknowledged order | Applied (demand released, units sellable again) **and** the order is flagged *Cancelled after acknowledgement* so the team stops packing. Same flag for an edit that removes acknowledged units. The team dismisses the flag. |
| Disposition types | `RESTOCKED` → `ONHAND`; `MISSING` → closed, no supply; `PROCESSING_REQUIRED` → stays `RETURN_PENDING` (QC still to do); every other type (`NOT_RESTOCKED`, …) → `DAMAGED`. |

## Tables

New: `ecs_inventory_supply`, `ecs_inventory_demand`, `ecs_returns`, `ecs_return_lines`, view `ecs_item_stock`. Changed: `ecs_inventory_log`, `ecs_order_header`, `ecs_order_line`, `ecs_sellers`. Conventions as elsewhere: BIGINT keys, TIMESTAMPTZ, CHECK constraints, composite FKs for copied `seller_id`, no cascading deletes.

### ecs_inventory_supply
`supply_id`, `seller_id`, `item_id` (composite FK `(item_id, seller_id)` → `ecs_items`), `supply_type` (`ONHAND` / `RETURN_PENDING` / `DAMAGED`; later `PO_PLACED`), `quantity`, `eta` (DATE, `RETURN_PENDING` only), `return_line_id` → `ecs_return_lines` (`RETURN_PENDING` only), timestamps.
- `UNIQUE NULLS NOT DISTINCT (item_id, supply_type, return_line_id)`: one `ONHAND` and one `DAMAGED` row per item, one `RETURN_PENDING` row per return line.
- `CHECK ((supply_type = 'RETURN_PENDING') = (return_line_id IS NOT NULL))`, `CHECK (eta IS NULL OR supply_type = 'RETURN_PENDING')`, `CHECK (supply_type = 'ONHAND' OR quantity >= 0)`.
- `ONHAND` / `DAMAGED` rows are created with the item and stay at 0; a `RETURN_PENDING` row is deleted when it reaches 0.

### ecs_inventory_demand
`demand_id`, `seller_id`, `item_id`, `order_header_id`, `order_line_id` (composite FK `(order_line_id, order_header_id)`), `demand_type` (`OPEN` / `ALLOCATED` / `SHIPPED`), `quantity > 0` (row deleted at 0), timestamps. `UNIQUE (order_line_id, demand_type)`; index `(item_id, demand_type)`.

### ecs_returns
`return_id`, `seller_id`, `order_header_id`, `channel`, `channel_account_id` (nullable), `external_return_id` (`UNIQUE (channel_account_id, external_return_id)`), `return_number`, `return_type` (`customer_return` / `rto`), `status` (`requested`, `open`, `received`, `qc_done`, `closed`, `declined`, `cancelled`), `external_status` (the channel's own status), `expected_at`, `received_at`, `qc_completed_at`, `qc_by`, `created_by_import_id`, `last_import_id`, `external_created_at`, `external_updated_at`, `version_no`, timestamps.

### ecs_return_lines
`return_line_id`, `return_id`, `order_line_id` (`UNIQUE (return_id, order_line_id)`), `item_id` (null when the order line isn't linked to the catalog), `external_return_line_id`, `external_disposal_ref` (Shopify reverse fulfilment order line item id), `return_qty > 0`, `received_qty`, `accepted_qty`, `rejected_qty`, `missing_qty`, `opening_accepted_qty`, `opening_rejected_qty`, `return_reason`, `customer_note`, `qc_note`, `status` (`pending`, `received`, `qc_done`, `cancelled`).
- `CHECK (accepted_qty + rejected_qty <= received_qty AND received_qty <= return_qty AND accepted_qty + rejected_qty + missing_qty <= return_qty)`.
- **Additions to the source ERD:** `missing_qty` (units Shopify marks `MISSING`: neither pending nor stock) and `opening_*_qty` (outcomes that happened before the item's starting stock was taken and are therefore already in it; they never move supply), `external_status`, `external_created_at`.

### ecs_inventory_log (extended)
New columns `side` (`SUPPLY` / `DEMAND`), `inv_type`, `transaction_id` (UUID), `return_line_id`. `stock_after` = that type's quantity after the change (per item for supply, per order line for demand). New reasons: `order_allocated`, `order_shipped`, `order_delivered`, `order_rto`, `return_opened`, `return_accepted`, `return_rejected`, `return_cancelled`, `return_closed`, `damaged_repaired`, `damaged_written_off`, `opening_stock`, `migration`. The manual reason `damage` now moves units from ONHAND to DAMAGED. Rows written before the cutover keep `side` empty.

### Other changes
- `ecs_order_line.delivered_qty` (≥ 0, ≤ `shipped_qty`).
- `ecs_order_header.order_status` gains `processing`; `attention_reason` / `attention_at` hold the flag above.
- `ecs_sellers.return_eta_days` (> 0, default 10).
- `ecs_order_history.action_type` gains `acknowledged`, `rto`, `attention_cleared`.

## How quantities are kept right (idempotency)

Nothing is incremented blindly. For every **counted** order line (linked to an item, order placed after the item's `stock_baseline_at`) the service computes targets and writes only the differences:

```
rto_qty        = Σ return_qty of the line's RTO return lines that aren't cancelled          (≤ shipped)
returned_qty   = Σ return_qty of the line's customer return lines that are open or later    (approved)
delivered_eff  = min(max(delivered_qty, returned_qty), shipped − rto_qty)                    (a return implies delivery)
OPEN/ALLOCATED = ordered − cancelled − shipped          (ALLOCATED when the line is acknowledged: allocated / packed)
SHIPPED        = shipped − delivered_eff − rto_qty
ONHAND (line)  = −delivered_eff (reason order_delivered) − rto_qty (reason order_rto)           compared with the line's logged ONHAND rows
```

For every **return line** with an item:

```
RETURN_PENDING = return_qty − accepted − rejected − missing      while the return is open or received; 0 otherwise
ONHAND         = accepted − opening_accepted   (reason return_accepted)   compared with the line's logged rows
DAMAGED        = rejected − opening_rejected   (reason return_rejected)
```

Replays, re-imports, out-of-order webhooks and repeated clicks therefore change nothing. Rows are locked per item (`FOR NO KEY UPDATE`, item-id order), so concurrent events for one SKU queue instead of losing updates or deadlocking.

## Scenarios

`q` = quantity. "Push" = the app sets Shopify's available quantity to the new `available` afterwards (only for changes Shopify didn't make itself).

### Orders

| # | Scenario | Demand | Supply | available | Push |
| --- | --- | --- | --- | --- | --- |
| O1 | New order (webhook, sync or import) | `OPEN` +q | — | −q | no (Shopify deducted) |
| O2 | Same order delivered again, re-imported, out of order | — | — | — | — |
| O3 | Order edited: quantity up / line added | `OPEN` (or `ALLOCATED`) +q | — | −q | no |
| O4 | Order edited: quantity down / line removed, before shipping | `OPEN`/`ALLOCATED` −q | — | +q | no |
| O5 | Acknowledge in the app (pending → processing) | `OPEN` −q, `ALLOCATED` +q | — | — | no |
| O6 | Partly shipped (Shopify fulfilment or Mark shipped) | `OPEN`/`ALLOCATED` −s, `SHIPPED` +s | — | — | no |
| O7 | Fully shipped | remaining → `SHIPPED` | — | — | no |
| O8 | Delivered (shipment status *delivered*, or Mark delivered), also in parts | `SHIPPED` −d | `ONHAND` −d | — | no |
| O9 | Cancelled before shipping (Shopify, or in the app) | `OPEN`/`ALLOCATED` −q | — | +q | only when cancelled in the app |
| O10 | Cancelled after partial shipping | unshipped part released; shipped part stays `SHIPPED` | — | +unshipped | as O9 |
| O11 | Shopify cancels an acknowledged order | as O9 | — | +q | no; order flagged *Cancelled after acknowledgement* |
| O12 | Shopify edit removes units from an acknowledged line | as O4 | — | +q | no; flagged *Edited after acknowledgement* |
| O13 | RTO entered in the app on shipped, undelivered units | `SHIPPED` −q | `ONHAND` −q, `RETURN_PENDING` +q (eta) | — | no (unchanged) |
| O14 | Order placed before the starting stock (backfill) | none, ever | none (delivery deducts nothing) | — | — |
| O15 | Line not linked to the catalog | none | none | — | — |
| O16 | Refund (with or without restock) | — | — | — | refunds no longer move stock; only returns do |
| O17 | Order is fulfilled and delivered in Shopify in one go before we see it | `SHIPPED` created and removed in the same reconcile | `ONHAND` −q | −q (the order) | no |
| O18 | Delivery reported for more than was shipped (bad data) | capped at shipped | capped | — | — |
| O19 | Two orders for the same SKU at the same moment | both applied (row lock) | — | −q each | no |

### Returns (customer)

| # | Scenario | Supply | available | Push |
| --- | --- | --- | --- | --- |
| R1 | Return requested (awaiting approval) | — | — | — |
| R2 | Return approved / created by the seller (open) | `RETURN_PENDING` +q, eta = seen-open day + `return_eta_days`; if the units weren't delivered yet in our data, delivery is inferred: `SHIPPED` −q, `ONHAND` −q | — | — |
| R3 | Return declined (from requested) | — | — | — |
| R4 | Return cancelled | `RETURN_PENDING` −remaining | — | — |
| R5 | Return closed with units not received | `RETURN_PENDING` −remaining | — | — |
| R6 | Return reopened | `RETURN_PENDING` +remaining | — | — |
| R7 | Units received (in the app) | — (`received_qty` only) | — | — |
| R8 | QC in the app: accepted / rejected (Shopify return) | pending → `ONHAND` (accepted) / `DAMAGED` (rejected); reported to Shopify first with `reverseFulfillmentOrderDispose` (`RESTOCKED` / `NOT_RESTOCKED` at the primary location) | +accepted | yes |
| R9 | QC recorded in Shopify (dispose webhook) | as R8 per disposition type; `PROCESSING_REQUIRED` stays pending, `MISSING` → closed units | +restocked | yes (no-op if equal) |
| R10 | Dispose webhook after QC done in the app | none (targets already met) | — | — |
| R11 | QC in several steps | each step adds; totals never go down (Shopify dispositions are final) | | yes |
| R12 | Return of a backfill order (placed before the starting stock) | `RETURN_PENDING` +q; accepted → `ONHAND` (the units physically come back) | +accepted | yes |
| R13 | Return of a line not linked to the catalog | return recorded, no supply | — | — |
| R14 | Return first seen already dispositioned, created before the item's starting stock | outcomes recorded as *opening* quantities, no supply (already in the starting stock) | — | — |
| R15 | Two returns for one order line | one `RETURN_PENDING` row per return line | | |
| R16 | Return webhook for an order not imported yet | the row fails with a clear message; the next polling sync imports the order and reads its returns | | |
| R17 | Missed return webhook | the polling sync reads the returns of every updated, fulfilled order | | |

### RTO (entered in the app)

| # | Scenario | Effect |
| --- | --- | --- |
| T1 | Mark RTO on q shipped, undelivered units | O13; return `rto`, status open, eta set |
| T2 | RTO more than shipped − delivered − already RTO'd | refused (422) |
| T3 | RTO on a backfill line | `RETURN_PENDING` +q only (those units were never in our `ONHAND`) |
| T4 | RTO units received and QC'd | as R7 / R8 (no Shopify report: Shopify has no RTO) |
| T5 | RTO entered by mistake → cancelled | `RETURN_PENDING` −q, `SHIPPED` +q, `ONHAND` +q (back to shipped) |
| T6 | RTO closed, units never came back | `RETURN_PENDING` −remaining (lost in transit) |
| T7 | Courier later reports the RTO'd units delivered | delivery is capped at shipped − RTO, so nothing is deducted twice |

### Manual and damaged

| # | Scenario | Supply | Push |
| --- | --- | --- | --- |
| M1 | Restock / correction / customer return without a return record / other: ±q | `ONHAND` ±q; a removal may not exceed what is on the shelf (`ONHAND − SHIPPED`) | yes |
| M2 | Unit found damaged on the shelf | `ONHAND` −q, `DAMAGED` +q (≤ shelf) | yes |
| M3 | Damaged unit repaired | `DAMAGED` −q, `ONHAND` +q (≤ `DAMAGED`) | yes |
| M4 | Damaged unit written off | `DAMAGED` −q | no (not sellable) |
| M5 | Removal that leaves `available` negative (units promised to orders) | allowed (the shelf has them) but the response warns that the SKU is now oversold | yes (0) |
| M6 | New product created in the app / imported from Shopify | `ONHAND` = opening stock, `DAMAGED` = 0 (`opening_stock`) | — |

### Shopify sync, drift, failures

| # | Scenario | Behaviour |
| --- | --- | --- |
| S1 | Push after an app change | absolute `available` (≥ 0), compare-and-swap, idempotency key (#20) |
| S2 | SKU disabled on Shopify | stays 0 there |
| S3 | Push fails | change kept; error on the listing item; retried by `/inventory/sync-pending` |
| S4 | QC dispose to Shopify fails (no `write_returns`, network) | nothing saved, 502 with the reason; retry |
| S5 | Drift report | compares Shopify with `available` |
| S6 | Return webhooks signed with the app secret while order webhooks use the store secret | either configured secret is accepted |

## Cutover from `stock_qty`

Migration `inventory_supply_demand` (one transaction, per seller):
1. `delivered_qty` = `shipped_qty` where `delivered_at` is set, else 0.
2. Every counted line gets demand for what isn't delivered: `OPEN`/`ALLOCATED` = ordered − cancelled − shipped, `SHIPPED` = shipped − delivered.
3. Per item: `ONHAND` = `stock_qty` + that item's demand; `DAMAGED` = 0. So `available` equals the old `stock_qty` and nothing changes on Shopify.
4. One `migration` log row per supply and demand row.
5. Units delivered before the cutover already left `stock_qty`; they are logged per line as `order_delivered` (and the ONHAND `migration` row is the figure before them), so the delivery rule doesn't deduct them again.
6. Stops with an error if any refund-based restock (`order_returned`) was logged, since the new returns flow would count those units again. (None exist on local or Render.)

After deploying: `POST /returns/sync` reads Shopify's returns of fulfilled orders (step 4 of the source ERD), then the drift report should match.

## Out of scope / later
Purchase orders (`PO_PLACED`), several locations, courier-fed RTO and delivery, return refunds and exchanges, editing a return's lines in the app.

## API

| Endpoint | Purpose |
| --- | --- |
| `GET /inventory`, `GET /inventory/{item_id}` | Every figure per item (onhand, open, allocated, shipped, committed, return_pending, next_eta, damaged, available, future_available, on_shelf) |
| `POST /inventory/adjust` | Manual ONHAND change; `damage` moves to DAMAGED; removal ≤ on the shelf; warns when oversold |
| `POST /inventory/{item_id}/damaged` | `repair` (DAMAGED → ONHAND) or `write_off` |
| `GET /inventory/{item_id}/history` | Log rows with side, type, transaction, order and return |
| `POST /orders/{id}/status` | `processing` = Acknowledge; shipped / delivered / cancelled |
| `POST /orders/{id}/attention/clear` | Dismiss the *Needs attention* flag; `GET /orders?attention=true` lists flagged orders |
| `POST /orders/{id}/rto` | Mark shipped, undelivered units as RTO |
| `GET /returns`, `GET /returns/{id}` | Returns and RTOs (filters: status, type, pending, q) |
| `POST /returns/sync` | Read Shopify returns of shipped orders (all, or `order_id`) |
| `POST /returns/{id}/receive`, `/qc`, `PATCH /returns/{id}` | Receive, QC (reported to Shopify for customer returns), ETA |
| `POST /returns/{id}/cancel`, `/close` | RTO only: cancel (units shipped again) or close (not received) |

## Setup

- Shopify app scopes: `read_returns` (granted) and **`write_returns`** (for reporting QC).
- Return webhooks: run `uv run python -m scripts.register_webhooks https://<backend>/webhooks/orders`. They are app subscriptions, signed with the app's client secret; the endpoint accepts that and `SHOPIFY_WEBHOOK_SECRET`.
- After deploying the cutover: `POST /returns/sync` once, then check `GET /inventory/drift`.

