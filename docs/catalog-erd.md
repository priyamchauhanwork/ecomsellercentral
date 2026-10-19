# ECS Product Catalog ERD (MVP)

Oct 18, 2026 · @priyam

Implemented in `backend/app/models/catalog.py` and migration
`backend/alembic/versions/3a489a0fb404_product_catalog_tables.py`. See
[Implementation notes](#implementation-notes) for the choices the ERD left open.

## Overview

The MVP catalog runs on 7 tables: a seller's products (articles and their items) are kept once, and each sales channel account gets its own listings that say what is live there and under what name.

| Table | Holds | One row per |
| --- | --- | --- |
| ecs\_sellers | Tenants of the app | Seller |
| ecs\_channels | Marketplaces and their default grouping rule | Channel (Amazon, Flipkart, Shopify...) |
| ecs\_channel\_accounts | A seller's accounts on each channel | Seller account on a channel |
| ecs\_articles | Master product, its variation axes and shared attributes | Product |
| ecs\_items | Sellable variants (SKUs) with their axis values | SKU |
| ecs\_channel\_listings | An article's listing on one account, with name and description overrides | Article × account × split value |
| ecs\_channel\_listing\_items | Which items are in a listing, their channel IDs, price and status | Item × account |

The design stays category-neutral: an article declares its own variation axes (Colour, Size, Flavour, Storage...), and each channel's `default_split_axes` decides which axes become separate listings on that channel.

## Relationships

Every relationship is one-to-many; the only many-to-many link (items to channel accounts) is resolved through `ecs_channel_listing_items`.

| Parent | Child | Cardinality | Foreign key |
| --- | --- | --- | --- |
| ecs\_sellers | ecs\_channel\_accounts | 1 : N | seller\_id |
| ecs\_sellers | ecs\_articles | 1 : N | seller\_id |
| ecs\_channels | ecs\_channel\_accounts | 1 : N | channel\_id |
| ecs\_articles | ecs\_items | 1 : N | article\_id |
| ecs\_articles | ecs\_channel\_listings | 1 : N | article\_id |
| ecs\_channel\_accounts | ecs\_channel\_listings | 1 : N | channel\_account\_id |
| ecs\_channel\_listings | ecs\_channel\_listing\_items | 1 : N | listing\_id |
| ecs\_items | ecs\_channel\_listing\_items | 1 : N (max 1 per account) | item\_id |
| ecs\_channel\_accounts | ecs\_channel\_listing\_items | 1 : N | channel\_account\_id (denormalised) |

## Table definitions

Types assume PostgreSQL; all tables also carry `created_at` and `updated_at` (TIMESTAMPTZ) unless noted.

### ecs\_sellers

| Column | Type | Key | Notes |
| --- | --- | --- | --- |
| seller\_id | BIGINT | PK |  |
| name | VARCHAR(255) |  |  |
| status | VARCHAR(20) |  | active / suspended |

### ecs\_channels

| Column | Type | Key | Notes |
| --- | --- | --- | --- |
| channel\_id | BIGINT | PK |  |
| code | VARCHAR(50) | UNIQUE | AMAZON, FLIPKART, MYNTRA, SHOPIFY |
| name | VARCHAR(100) |  | Display name |
| default\_split\_axes | JSONB |  | Axes that become separate listings, e.g. \[\] or \["Colour"\] |

### ecs\_channel\_accounts

| Column | Type | Key | Notes |
| --- | --- | --- | --- |
| channel\_account\_id | BIGINT | PK |  |
| seller\_id | BIGINT | FK → ecs\_sellers |  |
| channel\_id | BIGINT | FK → ecs\_channels |  |
| account\_name | VARCHAR(100) |  | "Account A"; UNIQUE (seller\_id, account\_name) |
| external\_seller\_id | VARCHAR(100) |  | Seller ID on the marketplace |
| is\_active | BOOLEAN |  | Default true |

### ecs\_articles

| Column | Type | Key | Notes |
| --- | --- | --- | --- |
| article\_id | BIGINT | PK |  |
| seller\_id | BIGINT | FK → ecs\_sellers |  |
| article\_code | VARCHAR(100) |  | UNIQUE (seller\_id, article\_code) |
| name | VARCHAR(255) |  | Master name, e.g. "Black Basic Tshirt" |
| description | TEXT |  |  |
| brand | VARCHAR(100) |  | Plain text in MVP |
| category | VARCHAR(255) |  | Plain text in MVP |
| variation\_axes | JSONB |  | Ordered list, e.g. \["Colour", "Size"\]; \[\] for no variants |
| attributes | JSONB |  | Shared attributes, e.g. {"Fabric": "Cotton"} |
| images | JSONB |  | \[{"url": "...", "filter": {"Colour": "Black"}}\] |
| status | VARCHAR(20) |  | draft / active / archived |

### ecs\_items

| Column | Type | Key | Notes |
| --- | --- | --- | --- |
| item\_id | BIGINT | PK |  |
| article\_id | BIGINT | FK → ecs\_articles |  |
| seller\_id | BIGINT | FK → ecs\_sellers | Denormalised for the SKU constraint |
| sku | VARCHAR(100) |  | UNIQUE (seller\_id, sku) |
| barcode | VARCHAR(50) |  | EAN / UPC / GTIN |
| variant\_values | JSONB |  | e.g. {"Colour": "Black", "Size": "M"} |
| mrp | NUMERIC(12,2) |  |  |
| stock\_qty | INTEGER |  | Single stock figure in MVP |
| weight | NUMERIC(10,3) |  | Grams |
| status | VARCHAR(20) |  | active / inactive |

### ecs\_channel\_listings

| Column | Type | Key | Notes |
| --- | --- | --- | --- |
| listing\_id | BIGINT | PK |  |
| article\_id | BIGINT | FK → ecs\_articles |  |
| channel\_account\_id | BIGINT | FK → ecs\_channel\_accounts |  |
| split\_key | JSONB |  | {} or {"Colour": "Black"}; UNIQUE (article\_id, channel\_account\_id, split\_key) |
| title | VARCHAR(500) |  | NULL = use ecs\_articles.name |
| description | TEXT |  | NULL = use ecs\_articles.description |
| channel\_parent\_id | VARCHAR(100) |  | Parent ID / style ID on the marketplace |
| status | VARCHAR(20) |  | draft / live / inactive / rejected |

### ecs\_channel\_listing\_items

| Column | Type | Key | Notes |
| --- | --- | --- | --- |
| listing\_item\_id | BIGINT | PK |  |
| listing\_id | BIGINT | FK → ecs\_channel\_listings |  |
| item\_id | BIGINT | FK → ecs\_items |  |
| channel\_account\_id | BIGINT | FK → ecs\_channel\_accounts | Denormalised; UNIQUE (item\_id, channel\_account\_id) |
| channel\_sku | VARCHAR(100) |  | SKU as entered on the channel |
| channel\_item\_id | VARCHAR(100) |  | ASIN / FSN / channel SKU ID |
| selling\_price | NUMERIC(12,2) |  | Price on this account |
| status | VARCHAR(20) |  | live / inactive |
| last\_sync\_error | TEXT |  | Last error returned by the channel |

## Business rules

**Generating listings.** When an article is listed on an account, the app takes the channel's `default_split_axes` (the seller can change it) and creates one `ecs_channel_listings` row per distinct combination of those axis values among the article's items. Each item then gets one `ecs_channel_listing_items` row in the listing that matches its values.

| Channel split axes | Article: Colour (3) × Size (3) = 9 items |
| --- | --- |
| \[\] | 1 listing, 9 items, split\_key = {} |
| \["Colour"\] | 3 listings, 3 items each, split\_key = {"Colour": "Black"} etc. |
| \["Colour", "Size"\] (no variant support) | 9 listings, 1 item each |

**Availability.** An article or item is on an account only if a row exists for it there with status `live`. No row, or status `inactive`, means it is not sold on that account.

**Channel names.** `ecs_channel_listings.title` and `description` override the master values for that account; NULL falls back to `ecs_articles.name` and `description`.

**Validation rules (enforce in the app layer)**

1. Every item's `variant_values` has exactly the keys listed in its article's `variation_axes`.
2. No two items of the same article share the same `variant_values`.
3. An item may sit in only one listing per account (UNIQUE item\_id, channel\_account\_id).
4. A listing item's `item_id` must belong to the listing's `article_id`, and its `channel_account_id` must match the listing's.
5. `split_key` keys must be a subset of the article's `variation_axes`, and its values must match the items inside the listing.

## Future extensions

Several MVP columns are deliberately simple and can become their own tables without changing the core 7.

| MVP column | Becomes | When |
| --- | --- | --- |
| ecs\_articles.brand, category | ecs\_brands, ecs\_categories, channel category mappings | Marketplace category mapping is needed |
| ecs\_articles.attributes, variation\_axes | ecs\_attributes, category attributes, value mappings | Required-attribute validation per category |
| ecs\_articles.images | ecs\_media, ecs\_media\_links | Channel-specific images |
| ecs\_items.stock\_qty | ecs\_warehouses, ecs\_inventory | More than one stock location |
| ecs\_channel\_listing\_items.selling\_price | ecs\_item\_channel\_prices | Scheduled prices or price history |
| ecs\_channel\_listing\_items.last\_sync\_error | ecs\_sync\_jobs, ecs\_sync\_logs | Full sync history for debugging |

## Implementation notes

Choices made where the ERD left details open (PostgreSQL 18):

- **Copied columns are kept in step by the database.** `ecs_items.seller_id` is copied from the article (for the per-seller SKU rule), and `ecs_channel_listing_items.channel_account_id` from the listing (for the one-listing-per-account rule). Composite foreign keys, `(article_id, seller_id)` → `ecs_articles` and `(listing_id, channel_account_id)` → `ecs_channel_listings`, make a mismatch impossible. This enforces the account half of validation rule 4 in the database; the other rules stay in the app layer as specified.
- **Allowed values are `CHECK` constraints**: seller, article, item, listing and listing-item `status`. Adding a status means a small migration.
- **Defaults:** seller `active`, article `draft`, item `active`, listing `draft`, listing item `inactive` (not live until published), `is_active` true, `stock_qty` 0, JSON lists `[]` and objects `{}`.
- **Required columns:** names, codes, SKU, foreign keys, statuses and the JSON columns. Optional: descriptions, brand, category, barcode, `mrp`, `weight`, `external_seller_id`, listing `title` / `description` / `channel_parent_id`, `channel_sku`, `channel_item_id`, `selling_price`, `last_sync_error`.
- **No negative `mrp`, `weight` or `selling_price`.** `stock_qty` may go negative on purpose: an order can oversell (#21).
- **Indexes** on the foreign keys used for lookups: `ecs_channel_accounts(channel_id)`, `ecs_items(article_id)`, `ecs_channel_listings(channel_account_id)`, `ecs_channel_listing_items(listing_id)`. The unique constraints cover the other lookups.
- **`split_key` uniqueness** compares JSONB values, so `{"Colour": "Black"}` matches regardless of key order or spacing.
- **No reference data is inserted.** Channels (with their `default_split_axes`) and sellers are created by the app or a seed step.
- **Deletes are restricted** (no cascades); archive with `status` instead.

### Relation to the order tables (migration `0f7f1af4ca24`)

- **`ecs_channel_accounts` is also the order-sync connection.** The order ERD's `ecs_channel_connection` described the same thing (a seller's account on a channel) and was merged into it. The account gained the connection's columns: `marketplace_region`, `credential_ref`, `connection_status` (`active` / `auth_expired` / `disconnected` / `error`), `sync_enabled`, `sync_interval_minutes` (> 0), `sync_cursor`, `last_synced_at`, `last_sync_status`, `webhook_enabled`, `webhook_secret_ref`, `webhook_topics`, `created_by`, plus a unique `(seller_id, channel_id, external_seller_id)`; for Shopify, `external_seller_id` is the store domain. Existing connections were copied into accounts and the orders re-pointed.
- **Orders belong to sellers.** `ecs_order_header.seller_id` and `ecs_order_import.seller_id` (formerly `store_id`) are foreign keys to `ecs_sellers`; `channel_account_id` (formerly `channel_connection_id`) points at `ecs_channel_accounts`. The migration created seller 1, "Default seller", for the existing Phase 0 data; the app uses `SELLER_ID` (default 1).
- **Order lines link to the catalog.** `ecs_order_line.product_id` → `ecs_articles`, `variant_id` → `ecs_items` (foreign keys, empty until the product import, #13, links them).
- The `SHOPIFY` row in `ecs_channels` is created by the app when it first connects the store.

## Shopify product import (#13)

`POST /products/import` (`backend/app/services/product_import.py`) fills the catalog from the seller's Shopify store; `GET /products` lists the items.

| Shopify | Catalog |
| --- | --- |
| Product | `ecs_articles`: `article_code` = handle on first import (`shopify-<product id>` if the handle is taken; never changed later), `name`, `description` = body, `brand` = vendor, `category` = product type, `variation_axes` = option names (`[]` for Shopify's single "Default Title" option), `images` (an image tied to variants gets the option values they share as its `filter`), `status` = active / draft / archived |
| Product on the store | `ecs_channel_listings` on the Shopify account: `split_key = {}`, `channel_parent_id` = product id, `title` / `description` NULL, `status` = `live` (active and published), `draft`, or `inactive` |
| Variant | `ecs_items`: `sku` (`shopify-<variant id>` when blank or already used by another item, with a warning), `barcode`, `variant_values`, `mrp` = compare-at price when above the price, else the price, `weight` in grams (from `grams`, or `weight` + unit) |
| Variant on the store | `ecs_channel_listing_items`: `channel_item_id` = variant id, `channel_sku`, `selling_price` = price, `status` = `live` when the listing is live |

- **Matching on re-import** is by Shopify id (`channel_parent_id`, `channel_item_id`; both indexed), never by name or SKU: renames update, a variant moved to another product moves its item.
- **Stock** (`stock_qty`) is copied from Shopify's inventory quantity only when an item is created, and `ecs_items.stock_baseline_at` records when (#21 deducts only orders placed after it). Re-imports never overwrite stock. Untracked and negative quantities are kept as Shopify reports them; the summary counts untracked ones.
- **Removed in Shopify:** a variant's listing item becomes `inactive`; a product's listing becomes `inactive` and its article `archived`. Nothing is deleted. This happens only when every page was fetched, so a Shopify error midway never deactivates products.
- **Invalid products** (repeated option values, blank option names, missing ids) are skipped with an error in the summary; the rest import. Each product is its own transaction.
- **One import at a time** per account (PostgreSQL advisory lock); a second call gets 409.
- **Orders:** afterwards, unlinked `ecs_order_line` rows get `variant_id` / `product_id` from the Shopify variant id in their latest payload, else from the SKU. New orders and items added by order edits are linked by the order processing step as they arrive.
- Requires the Shopify `read_products` scope (503 otherwise).

## Products in the app (#43–#47)

- **Statuses.** `ecs_channel_listings.status` says whether the *product* is on sale on that channel account; `ecs_channel_listing_items.status` says whether that *SKU* is enabled there. A SKU is actually sold only when both are `live`. Taking a product off sale keeps each SKU's own state, so it comes back as it was.
- **List and detail** (`GET /products`, `GET /products/{id}`): `channel_status` filter — `live` (has a live listing), `inactive` (listed, none live), `unlisted`; low stock means an active item at or below 5.
- **Create** (`POST /products`): saved as `draft` with `stock_baseline_at` = now. Article code and SKUs are unique per seller ignoring case; ERD rules 1–2 are checked; every problem is returned with its field. At most 100 variants. Images are https URLs (no uploads: Render's disk is ephemeral).
- **Publish** (`POST /products/{id}/publish`): one `productSet` call creates the product with its options, variants (price, compare-at from MRP when higher, barcode, weight, tracked stock at the primary location, overselling off) and images, as `DRAFT` or, with `enable`, `ACTIVE` and published to the Online Store. The handle is fixed per article (`<code>-<article id>`), so a retry updates the same Shopify product instead of creating a second one, and it can't collide with an existing product's handle. The article becomes `active`. Refused when already listed (409), archived, over 3 options, or without a price for an active item (422).
- **Enable / disable** (`.../listings/{id}/status`, `.../listing-items/{id}/status`, bulk `POST /products/status`): Shopify first — the local status changes only after Shopify accepted; a failure is stored in the listing items' `last_sync_error` (cleared by the next success) and nothing changes locally. Product: `ACTIVE` + Online Store, or `DRAFT` (hidden from every sales channel); putting it on sale also makes an archived or draft article `active`. SKU: Shopify can't hide one variant, so disabling makes it sold out (stock tracked, overselling off, quantity 0) and enabling sets the quantity back to `stock_qty`.
- **Import and app-disabled SKUs.** The product import keeps a SKU disabled while Shopify still shows it sold out that way; if someone restocks it in Shopify, Shopify wins and it is enabled again.
- **Shopify scopes** for publishing and enable / disable: `write_products`, `write_inventory`, `write_publications`.
- **Shopify API 2026-04+ inventory rules.** `inventoryActivate` and `inventorySetQuantities` require an `@idempotent(key: …)` directive (a new key per change; the client's retries resend the same key), and `inventorySetQuantities` requires `changeFromQuantity` (compare-and-swap): the app reads the current available quantity, sends it, and re-reads and retries once if someone changed it meanwhile. Stock sync (#20) must do the same.
- **Network failures** (timeouts, refused connections) are retried with backoff and then reported as a Shopify error (502), never an unhandled 500.

## Inventory (#19, #20, #21)

- **`ecs_inventory_log`** (migration `a7c79995f837`): append-only stock history. `item_id` → `ecs_items`, `qty_change` (never 0), `stock_after` (the item's `stock_qty` once the change applied), `reason` (`restock`, `damage`, `correction`, `return`, `other` by hand; `order_created`, `order_cancelled`, `order_returned`, `order_edited` from orders), `note`, `order_line_id` → `ecs_order_line` (order changes), `created_by` (no users table yet), `created_at`. Indexes on `(item_id, created_at)` and `order_line_id`. Every change to `stock_qty` writes one row in the same transaction.
- **Manual adjustments** (`POST /inventory/adjust`) lock the item row and may not take stock below 0; adding is always allowed, also while stock is negative after an oversell.
- **Locks.** Stock changes lock items with `FOR NO KEY UPDATE`, in item-id order: concurrent changes to one item wait for each other, but don't deadlock with order lines being inserted (their foreign key takes a key-share lock on the item).
- **Orders (#21).** A linked order line changes stock only when its order was placed after the item's `stock_baseline_at` (or the item has none, e.g. created in the app). Each such line should hold back `ordered − cancelled − returned` units; the app compares that with what is already logged for the line and logs only the difference, so replays, re-imports and repeated updates never deduct twice. Returns are logged as `order_returned`, other give-backs as `order_cancelled`, extra units added by an edit as `order_edited`. A line linked to the catalog only later is deducted at the next update of its order, since Shopify deducted it at the time.
- **Sync to Shopify (#20).** Shopify's available quantity for a SKU is set to the app's `stock_qty` (absolute, never a delta; 0 when the stock is negative, and always 0 for a SKU disabled from the app). Changes made in the app are pushed: manual adjustments, and an order cancelled in the app (Shopify doesn't know about it). Changes that came from Shopify (orders, cancellations, returns, edits) aren't, because Shopify already applied them. If the stock changes while a push is in flight, the newer figure is pushed straight after. A failed push keeps the local change and stores the error in the listing item's `last_sync_error`; `POST /inventory/sync-pending` (called by the polling job) retries those, and success clears it. The variant's inventory item id and the store's primary location are looked up once per process and cached, not stored.
- **Drift** (`GET /inventory/drift`) reads Shopify's live available quantity for every Shopify SKU (100 variants per query) and compares it with the figure the app expects there. Variants with stock tracking off in Shopify aren't compared. `POST /inventory/{item_id}/sync` pushes one SKU's figure to fix a difference.

