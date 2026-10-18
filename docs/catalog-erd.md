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

### Relation to the order tables

- `ecs_order_header.store_id` and `ecs_channel_connection.store_id` mean the seller; they become foreign keys to `ecs_sellers` once a seller row exists (Phase 0 uses store 1).
- `ecs_channel_connection` (order sync: cursor, webhooks) and `ecs_channel_accounts` (catalog listings) both describe a seller's account on a channel. Linking them, for example a `channel_account_id` on `ecs_channel_connection`, is a follow-up decision.
- `ecs_order_line.variant_id` / `product_id` will point at `ecs_items` / `ecs_articles` when the product import (#13) links order lines to the catalog.

