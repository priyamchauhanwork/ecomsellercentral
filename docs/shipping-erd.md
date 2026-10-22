# Shipping: courier aggregator schema

Source: *Courier Aggregator Integration — Database Schema & Functional Spec* v1.0 (23 Oct 2026) and its DDL. This file is the implementation reference: the tables as built (migration `f3b659645cf0`, local and Render), the decisions that adapt the spec to this database, the status model and the rules the services must enforce. Tickets: epic #56, schema #58, credentials #78, then #59–#64.

Scope: forward shipments through a courier aggregator (Shiprocket first; the schema is aggregator-agnostic). Out of scope for now: rates / serviceability cache, NDR actions, Shiprocket return pickups, COD remittance, weight disputes, claims, customer notifications (spec §11; they attach to `ecs_shipments` later).

## Decisions (adapting the spec)

| Spec | Built | Why |
| --- | --- | --- |
| UUID keys; `sellers`, `orders`, `order_items` | `ecs_` tables with BIGINT identity keys; FKs to `ecs_sellers`, `ecs_order_header`, `ecs_order_line` | Same conventions as every other table (decided 23 Oct 2026) |
| Money in paise (`BIGINT`) | `NUMERIC(12,2)` rupees: `cod_amount`, `declared_value`, `shipping_charge` | Same as all other money columns; no ×100 in joins or reports |
| `storage_key` preferred for documents | Aggregator URL + expiry; regenerated when expired; `storage_key` kept for later | No object storage on Render yet |
| Credentials encrypted in the DB | Same (decided): API password and token AES-GCM encrypted by the app with a key from env; webhook token stored as a hash. Built in #78; until then #57 keeps them in env vars | Seller connects from Settings; several accounts possible |
| `webhook_events`, `api_calls` | `ecs_aggregator_webhook_events`, `ecs_aggregator_api_calls` | Distinguishes them from Shopify webhooks (`ecs_order_import`) |
| `updated_at` trigger | Trigger `ecs_set_updated_at()` on the four mutable tables, plus the ORM's `onupdate` | Raw SQL updates keep `updated_at` right |
| Seller of a shipment | Composite FKs `(order_header_id, seller_id)`, `(aggregator_account_id, seller_id)`, `(warehouse_id, seller_id)` | A shipment can't mix sellers (the DB refuses it) |
| One aggregator order per OMS order or per shipment (open item 6) | **Per shipment** | Split shipments and re-booking after a cancellation; Shiprocket refuses a reused `order_id` |
| Order fulfilment rollup column (open item 5) | Derived (not stored) from shipments and remaining quantities; `ecs_order_header.fulfillment_status` stays Shopify's | Avoids a second source of truth; decided in #63 |

## Tables

| Table | Purpose | Key constraints |
| --- | --- | --- |
| `ecs_courier_aggregators` | Supported aggregators; seeded (`shiprocket`, `credentials_token`, base URL, token TTL 240 h) | `code` unique; `auth_type` in (`credentials_token`, `api_key`, `oauth2`) |
| `ecs_aggregator_accounts` | A seller's connection: `label`, `api_username`, `api_secret_encrypted`, `access_token_encrypted`, `token_expires_at`, `webhook_token_hash`, `webhook_path_key`, `status` (`active` / `auth_failed` / `disconnected`), `is_default`, `last_auth_at`, `last_error` | unique (seller, aggregator, label); **one default per seller** (partial unique); `webhook_path_key` unique |
| `ecs_warehouses` | Pickup locations: name, contact, phone, email, address, city, state, `pincode` (`^[1-9][0-9]{5}$`), country, GSTIN, `is_default`, `is_active` (soft delete) | unique (seller, name); **one active default per seller** |
| `ecs_warehouse_aggregator_links` | A warehouse's registration on an account: `external_location_code` (Shiprocket nickname, ≤ 36 chars), `external_location_id`, `sync_status` (`pending` / `synced` / `failed`), `last_synced_at`, `last_error` | unique (warehouse, account); unique (account, nickname) |
| `ecs_couriers` | Carriers seen in API responses: `external_courier_id`, name, `mode` (`surface` / `air` / `other`) | unique (aggregator, external id) |
| `ecs_shipments` | One package / AWB: order, account, warehouse, courier, `idempotency_key`, `external_order_id`, `external_shipment_id`, `awb_number`, `courier_name_snapshot`, canonical `status` + last raw code / label, `status_updated_at`, `last_event_at`, `payment_mode`, `cod_amount`, `declared_value`, `dead_weight_g`, L/B/H cm, `charged_weight_g`, `shipping_charge`, `ship_to` (JSONB address snapshot), tracking URL, pickup date / token, EDD, picked-up / delivered times, cancellation fields, `created_by`, `version` | `idempotency_key` unique; AWB unique per account; external shipment id unique per account; COD amount only for COD; AWB required from `awb_assigned` to `delivered`; positive weights / dimensions; indexes for order, seller + status, AWB lookup, and active shipments by `status_updated_at` (poller) |
| `ecs_shipment_items` | Order lines and quantities in a package | PK (shipment, order line); quantity > 0 |
| `ecs_shipment_documents` | Label / manifest / invoice / packing slip: source (`aggregator` / `oms`), `external_url`, `storage_key`, size, `is_current`, `generated_at`, `expires_at` | one **current** document per (shipment, type); URL or storage key required |
| `ecs_shipment_tracking_events` | Append-only scans: canonical status, raw code / label, activity, location, `event_at`, `received_at`, `source` (`webhook` / `poll` / `api_response` / `manual`), webhook event, `dedupe_key` | unique (shipment, dedupe key); index (shipment, event_at desc) |
| `ecs_aggregator_status_mappings` | Raw code → canonical status, `is_terminal`; Shiprocket seeded (below) | PK (aggregator, code) |
| `ecs_aggregator_webhook_events` | Inbox: account (from the URL key), shipment, AWB, headers (`x-api-key` redacted), payload, `auth_valid`, `dedupe_key` (hash of the body), `processing_status` (`pending` / `processing` / `processed` / `ignored` / `failed`), attempts, `next_attempt_at`, `last_error` | `dedupe_key` unique; queue index on pending / failed |
| `ecs_aggregator_api_calls` | Every outbound call: operation (`auth`, `create_order`, `assign_awb`, `generate_pickup`, `generate_manifest`, `generate_label`, `generate_invoice`, `cancel`, `track`, `create_pickup_location`, `other`), method, endpoint, request / response (secrets redacted), HTTP status, success, error, duration | index (shipment, created_at desc); failures index; purge after 90 days |

`ecs_order_line.warehouse_id` (from the order ERD) stays a plain BIGINT for now; shipments carry the warehouse.

## Shipment status model

Canonical statuses: `draft`, `creation_failed`*, `created`, `awb_assigned`, `pickup_scheduled`, `picked_up`, `in_transit`, `out_for_delivery`, `delivered`*, `undelivered`, `rto_initiated`, `rto_delivered`*, `cancelled`*, `lost`*, `damaged`* (* terminal).

```
draft ─► created ─► awb_assigned ─► pickup_scheduled ─► picked_up ─► in_transit ─► out_for_delivery ─► delivered
  │         │            │                │                               ▲  │                │
  ▼         ▼            ▼                ▼                               │  ▼                ▼
creation_ cancelled   cancelled       cancelled                  (re-attempt) rto_initiated ◄─ undelivered
failed                                                                          │
                                                                                ▼
                                                                          rto_delivered
any in-flight status ─► lost | damaged
```

Rules: no backward moves except `out_for_delivery → in_transit` and `undelivered → out_for_delivery`; terminal statuses never change (later events go to history only); an event changes `status` only if `event_at >= last_event_at`; cancel only from `draft`, `created`, `awb_assigned`, `pickup_scheduled`; updates use `WHERE version = ?` (optimistic lock).

### How shipments drive orders and stock (existing model, #49)

`picked_up` → the shipment's lines' `shipped_qty` rises (demand → SHIPPED); `delivered` → `delivered_qty` (ONHAND −); `rto_initiated` → an RTO return for the shipment's lines (#55), `rto_delivered` → that return is received (QC in the app). Shipments never move stock directly.

### Shiprocket status mapping (seeded)

From Shiprocket's official shipment status codes (the webhook's `shipment_status_id` and each scan's `sr-status` use the same list):

| Code | Label | Canonical | Terminal |
| --- | --- | --- | --- |
| 1, 2 | AWB Assigned, Label Generated | `awb_assigned` | |
| 3, 4, 5, 15, 19 | Pickup Scheduled / Queued, Manifest Generated, Pickup Rescheduled, Out For Pickup | `pickup_scheduled` | |
| 42 | Picked Up | `picked_up` | |
| 6, 18, 22, 38, 39 | Shipped, In Transit, Delayed, Reached Destination Hub, Misrouted | `in_transit` | |
| 17 | Out For Delivery | `out_for_delivery` | |
| 7 | Delivered | `delivered` | yes |
| 21 | Undelivered | `undelivered` | |
| 9, 40, 41, 46 | RTO Initiated, RTO NDR, RTO OFD, RTO In Transit | `rto_initiated` | |
| 10 | RTO Delivered | `rto_delivered` | yes |
| 8, 45 | Cancelled, Cancelled Before Dispatched | `cancelled` | yes |
| 12 | Lost | `lost` | yes |
| 24, 25 | Destroyed, Damaged | `damaged` | yes |

Deliberately unmapped (stored in history, status unchanged, ops alert): 11 Pending, 13 Pickup Error, 14 RTO Acknowledged, 16 Cancellation Requested, 20 Pickup Exception, 23 Partial Delivered, 26 Fulfilled, 43 Self Fulfilled, 44 Disposed Off, 47 QC Failed, 48 Reached Warehouse and any new code.

## Business rules for the services (spec §7)

R1 shipped quantity per order line ≤ ordered − cancelled (checked under `SELECT … FOR UPDATE` on the lines); R2 the account must be `active` to call the aggregator; R3 the warehouse's link must be `synced` for that account; R4–R6 the transition, terminal and ordering rules above; R7 cancel only before pickup; R8 never blind-retry `create_order` after a timeout (look the order up first); R9 one default warehouse and account (also in the DB); R10 COD amount 0 for prepaid (DB) and > 0 recommended for COD (UI); R11 unmapped status → alert, don't guess.

## Background jobs (spec §8)

Webhook processor (every few seconds, `FOR UPDATE SKIP LOCKED`, backoff 1 m, 5 m, 15 m, 1 h, 6 h, then give up); tracking poller (every 2–6 h for active shipments not updated for 6 h); token refresher (daily, tokens expiring within 48 h); draft reconciler (every 15 min, drafts older than 10 min); warehouse sync retry (hourly); log retention (daily, API calls and processed webhook events older than 90 days). On Render's free plan these run from the scheduled GitHub workflow (`docs/infra.md`) calling protected endpoints.

## Security

API password and token AES-GCM encrypted by the app (key in env, never in the DB); the webhook token only as a hash, compared in constant time; webhook URL `/webhooks/courier/{webhook_path_key}` (no "shiprocket", "kartrocket", "sr" or "kr" in it, per Shiprocket); `Authorization`, `x-api-key` and passwords redacted from logged calls and headers; `ship_to` is customer PII, scoped by seller.
