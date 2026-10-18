# Infrastructure

Render hosts the API, the frontend and the PostgreSQL database.

_Limits checked against Render's docs on 2026-10-17. Re-check before the pilot; they change._
Sources: [Free instance limits](https://render.com/docs/free) ·
[Postgres backups](https://render.com/docs/postgresql-backups)

## Services

| Service                  | Render type        | Plan | Notes                                         |
|--------------------------|--------------------|------|-----------------------------------------------|
| `shopify-mvp-api`        | Web Service (Python) | Free | Root dir `backend`, branch `main`. Configured in #14. |
| PostgreSQL               | Postgres           | Free | Created 2026-10-17 · **expires 2026-11-16** · deleted after 2026-11-30 if not upgraded. |
| Frontend                 | Static Site        | Free | Set up in #36.                                |

The API must use the **internal** connection string for `DATABASE_URL`. The external
one is only for `psql` / migrations from a laptop. The web service and the database
must be in the same region for the internal URL to work.

## Free-tier limits

### Web service

- **Sleeps after 15 minutes** with no inbound traffic; the next request wakes it in
  **about one minute**.
- **750 instance hours per workspace per month.** One always-on service uses ~744 h in
  a 31-day month, so a second free web service kept awake would run out. When hours run
  out, every free web service is suspended until next month.
- **Ephemeral filesystem.** Anything written to disk is lost on redeploy, restart or
  spin-down.
- **Outbound ports 25, 465 and 587 are blocked** (SMTP).
- Render may restart a free service at any time. No shell access, no persistent disk,
  rollbacks only to the last two deploys.
- Bandwidth and build minutes count against the workspace quota shown in the dashboard.
  Without a payment method, going over suspends services / disables builds for the
  rest of the month.

### PostgreSQL

- **Expires 30 days after creation.** After that it is inaccessible; there is a
  **14-day grace period** to upgrade, then Render **deletes the database and all data**.
  Render emails before expiry and before the grace period ends.
- **No backups of any kind** — no point-in-time recovery, no logical exports.
  The only option is running `pg_dump` ourselves.
- 1 GB storage, one free database per workspace, no managed connection pooling.
- Render may restart it for maintenance at any time.

## What this means for the build

- **Shopify webhooks vs. sleep (#11, #12, #14):** a webhook that hits a sleeping API
  can time out while it wakes. Shopify retries failed deliveries, and the polling
  fallback (#12) picks up anything still missed. Order intake must stay idempotent
  (#21).
- **Generated PDFs (#25–#27):** generated on request with ReportLab and returned
  directly; nothing is written to disk.
- **Email:** use SendGrid's **HTTP API**, not SMTP — the SMTP ports are blocked.
- **Logs:** with no shell access, Sentry (#32) and the Render log view are how we debug
  production.

## Order polling scheduler (#12)

**Choice: a GitHub Actions scheduled workflow** (`.github/workflows/sync-orders.yml`), free because the repo is public. An in-process scheduler would not run while the free web service sleeps, which is exactly when webhooks get missed; an outside call also wakes the service. A Render Cron Job would work too but is paid.

- Runs every 15 minutes (`*/15 * * * *`) and on demand (Actions → *Sync Shopify orders* → *Run workflow*). It calls `POST /sync/orders` with the `X-API-Key` header, allowing about a minute for a sleeping service to wake, with 3 retries.
- **Switched off until deployed:** the job runs only when the repository **variable** `ORDER_SYNC_URL` is set (e.g. `https://shopify-mvp-api.onrender.com/sync/orders`). It also needs the repository **secret** `API_SHARED_SECRET`, the same value as on Render. Set both in GitHub → Settings → Secrets and variables → Actions (#14).
- The server decides whether a sync is due: scheduled calls are skipped (`not_due`, 200) until `ecs_channel_connection.sync_interval_minutes` (default 15) has passed since the last successful sync, so the interval can change without editing the workflow.
- GitHub may start scheduled runs several minutes late under load, and **disables scheduled workflows in a public repo after 60 days without commits**; re-enable it in the Actions tab if that happens.

## Decision: paid database before the pilot

**Yes — upgrade Postgres to a paid instance before onboarding the pilot seller (#39).**

- The free database expires 30 days after creation, which falls before or during the
  pilot. Losing it means losing a real seller's orders and stock history.
- The free plan has no backups, so #33 (verify backups and test a restore) can't be done
  on it.
- Paid instances get continuous point-in-time recovery: the **past 3 days** on the Hobby
  workspace plan, **7 days** on Pro or higher, plus logical exports kept for 7 days.

**Upgrade by 2026-11-16** (database expiry); set a reminder for 2026-11-09. Until then,
take a `pg_dump` before any risky migration.

The web service can stay on Free for the pilot; the cold start is covered by Shopify's
retries and the polling fallback. Revisit if the pilot seller finds the first page load
too slow.
