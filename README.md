# ecomsellercentral

A lightweight order and inventory management system (OMS) for Shopify sellers.

## Goal

Give small Shopify sellers one place to see orders, keep stock accurate, and print
invoices, packing slips and shipping labels, without spreadsheets or manual copy-paste.

## Phase 0 scope (MVP Lite)

- **Shopify integration:** import products, variants and orders; real-time order
  webhooks (HMAC-verified) with a polling fallback.
- **Orders:** list with status/date filters, order detail, mark as shipped.
- **Inventory:** stock list with low-stock alerts, manual adjustments with history,
  auto-deduct on new orders, sync stock back to Shopify.
- **Documents:** invoice PDF, packing slip PDF, 4x6" shipping label PDF.
- **Dashboard & reports:** key stats, top-10 products this month, low stock;
  CSV export of orders and inventory.
- **Ops:** deployed on Render, error logging with Sentry, verified database backups.

Out of scope for Phase 0: multi-channel (non-Shopify) sales, multi-user roles,
carrier integrations.

## Tech stack

| Layer      | Technology                            |
|------------|---------------------------------------|
| Backend    | Python 3.13, FastAPI, SQLAlchemy + Alembic, uv |
| Database   | PostgreSQL (Render managed)           |
| Frontend   | React (Vite)                          |
| Integration| Shopify Admin API + webhooks          |
| PDFs       | ReportLab                             |
| Email      | SendGrid                              |
| Monitoring | Sentry                                |
| Hosting    | Render (auto-deploys from `main`)     |

## Repository layout

```
backend/    FastAPI app, Shopify integration, PDF generation, Alembic migrations
frontend/   React (Vite) app
database/   ERD, seed data and SQL notes
docs/       Wireframes, guides and other documentation
```

## Getting started

See [SETUP.md](SETUP.md) for local development setup. Copy `.env.example` to `.env`
and fill in your own values.
