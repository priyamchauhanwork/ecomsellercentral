"""Order ingestion: import runs and the shared processing step (docs/order-erd.md).

Every way an order arrives (import, scheduled or manual sync, webhook) records an
ecs_order_import run with one ecs_order_import_row per order received, then runs
process_import_row() on each row. Each order is saved in its own transaction.
"""

import hashlib
import json
import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import httpx
from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from app.config import get_settings
from app.db import SessionLocal
from app.models import (
    Channel,
    ChannelAccount,
    OrderHeader,
    OrderHistory,
    OrderImport,
    OrderImportRow,
    OrderLine,
    OrderLineHistory,
)
from app.services.order_mapping import (
    LINE_STATUS_RANK,
    MappedOrder,
    OrderMappingError,
    clip,
    map_shopify_order,
    order_status_from_lines,
    timestamp,
)
from app.services.shopify import (
    ShopifyClient,
    ShopifyConfigError,
    ShopifyError,
    get_shopify_client,
    normalize_store_domain,
)

logger = logging.getLogger(__name__)

RUNNING_STATUSES = ("pending", "validating", "processing")
STALE_RUN_AFTER = timedelta(minutes=30)
# Each sync re-reads this much before its cursor, so orders updated while the previous
# sync was paging aren't missed. Re-read orders are reported "unchanged".
SYNC_OVERLAP = timedelta(minutes=5)
SHOPIFY_CHANNEL_CODE = "SHOPIFY"  # ecs_channels.code
# Rows whose payload we are consistent with; the newest one's hash is "last applied".
CONSISTENT_ACTIONS = ("created", "updated", "unchanged")
ORDER_STATUS_RANK = {"pending": 0, "shipped": 1, "delivered": 2, "cancelled": 3}
ORDER_CREATE_RACE_CONSTRAINTS = {
    "uq_ecs_order_header_seller_id_channel_external_order_id",
    "uq_ecs_order_header_order_number",
}
COUNTER_FOR_ACTION = {
    "created": "created_count",
    "updated": "updated_count",
    "unchanged": "unchanged_count",
    "skipped_stale": "skipped_count",
    "skipped_duplicate": "skipped_count",
    "failed": "failed_count",
}
# Line fields never overwritten by a re-import: catalog links are set by #13.
LINE_FIELDS_KEPT = {"line_number", "product_id", "variant_id"}
# Line fields kept when Shopify's status is behind ours (e.g. a manual "Mark shipped").
LINE_SHIPPING_FIELDS = {
    "line_status",
    "shipped_qty",
    "shipped_at",
    "delivered_at",
    "courier_name",
    "tracking_number",
}

# Swapped out in tests.
shopify_client_factory: Callable[[], ShopifyClient] = get_shopify_client


class ImportAlreadyRunning(Exception):
    def __init__(self, order_import_id: int) -> None:
        super().__init__(f"Import {order_import_id} is already running")
        self.order_import_id = order_import_id


def utcnow() -> datetime:
    return datetime.now(UTC)


def clean_payload(value: Any) -> Any:
    """Postgres JSONB rejects the NUL character; strip it from every string."""
    if isinstance(value, str):
        return value.replace("\x00", "")
    if isinstance(value, dict):
        return {clean_payload(k): clean_payload(v) for k, v in value.items()}
    if isinstance(value, list):
        return [clean_payload(v) for v in value]
    return value


def payload_hash(payload: Any) -> str:
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(canonical.encode()).hexdigest()


def json_safe(value: Any) -> Any:
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, datetime):
        return value.isoformat()
    return value


# --- Channel account and runs ----------------------------------------------------------------


def ensure_shopify_account(session: Session) -> ChannelAccount:
    """Create or update the seller's Shopify account (ecs_channel_accounts) from settings,
    creating the SHOPIFY channel row if needed.

    One account per seller, channel and store domain. Phase 0 keeps credentials in env
    vars, so the *_ref columns name the variables.
    """
    settings = get_settings()
    domain = normalize_store_domain(settings.shopify_store_domain)
    if not domain:
        raise ShopifyConfigError("Missing in .env: SHOPIFY_STORE_DOMAIN")
    credential_ref = (
        "env:SHOPIFY_ADMIN_API_ACCESS_TOKEN"
        if settings.shopify_admin_api_access_token
        else "env:SHOPIFY_API_KEY,SHOPIFY_API_SECRET"
    )
    secret_env = (
        "SHOPIFY_WEBHOOK_SECRET" if settings.shopify_webhook_secret else "SHOPIFY_API_SECRET"
    )

    session.execute(
        pg_insert(Channel)
        .values(code=SHOPIFY_CHANNEL_CODE, name="Shopify")
        .on_conflict_do_nothing(index_elements=["code"])
    )
    channel_id = session.scalar(
        select(Channel.channel_id).where(Channel.code == SHOPIFY_CHANNEL_CODE)
    )
    session.execute(
        pg_insert(ChannelAccount)
        .values(
            seller_id=settings.seller_id,
            channel_id=channel_id,
            account_name=clip(f"Shopify · {domain}", 100),
            external_seller_id=domain,
            credential_ref=credential_ref,
            webhook_secret_ref=f"env:{secret_env}",
        )
        .on_conflict_do_update(
            index_elements=["seller_id", "channel_id", "external_seller_id"],
            set_={
                "credential_ref": credential_ref,
                "webhook_secret_ref": f"env:{secret_env}",
                "updated_at": utcnow(),
            },
        )
    )
    return session.scalars(
        select(ChannelAccount).where(
            ChannelAccount.seller_id == settings.seller_id,
            ChannelAccount.channel_id == channel_id,
            ChannelAccount.external_seller_id == domain,
        )
    ).one()


def start_import(session: Session, channel_account_id: int, ingestion_method: str) -> OrderImport:
    """Create a pending import or sync run. One at a time per account: raises
    ImportAlreadyRunning if another is active; a run with no progress for 30 minutes is
    marked failed. Webhook runs are independent and never block or are blocked."""
    session.execute(
        select(ChannelAccount)
        .where(ChannelAccount.channel_account_id == channel_account_id)
        .with_for_update()
    ).scalar_one()
    running = session.scalars(
        select(OrderImport).where(
            OrderImport.channel_account_id == channel_account_id,
            OrderImport.status.in_(RUNNING_STATUSES),
            OrderImport.ingestion_method != "webhook",
        )
    ).all()
    for run in running:
        if run.updated_at < utcnow() - STALE_RUN_AFTER:
            run.status = "failed"
            run.error_summary = (
                "Abandoned: no progress for 30 minutes (the server probably restarted)"
            )
            run.completed_at = utcnow()
        else:
            raise ImportAlreadyRunning(run.order_import_id)

    run = OrderImport(
        seller_id=get_settings().seller_id,
        channel_account_id=channel_account_id,
        channel="shopify",
        ingestion_method=ingestion_method,
    )
    session.add(run)
    session.flush()
    return run


def sync_start_time(session: Session, account: ChannelAccount) -> datetime | None:
    """Where a sync resumes: the saved cursor, else the newest order update we hold,
    minus SYNC_OVERLAP. None (fetch everything) for a account with no orders yet."""
    resume_from = timestamp(account.sync_cursor, [], "sync_cursor") or session.scalar(
        select(func.max(OrderHeader.external_updated_at)).where(
            OrderHeader.channel_account_id == account.channel_account_id
        )
    )
    return resume_from - SYNC_OVERLAP if resume_from else None


def run_shopify_order_import(
    order_import_id: int,
    *,
    updated_at_min: datetime | None = None,
    client_factory: Callable[[], ShopifyClient] | None = None,
    session_factory: sessionmaker[Session] = SessionLocal,
) -> None:
    """Fetch Shopify orders (all of them, or those updated since `updated_at_min`) into
    the run and process each one, then move the account's sync cursor. Never raises:
    any failure ends up in the run's status and error_summary."""
    mark_processing(session_factory, order_import_id)
    params = {"status": "any"}
    if updated_at_min:
        params["updated_at_min"] = updated_at_min.astimezone(UTC).isoformat()
    newest_seen: datetime | None = None
    fetch_error: str | None = None
    try:
        with (client_factory or shopify_client_factory)() as shopify:
            orders = shopify.paginate("/orders.json", "orders", params)
            for record_number, payload in enumerate(orders, 1):
                with session_factory.begin() as session:
                    row_id = record_import_row(session, order_import_id, record_number, payload)
                process_import_row(row_id, session_factory=session_factory)
                order = payload if isinstance(payload, dict) else {}
                updated = timestamp(order.get("updated_at"), [], "updated_at")
                if updated and (newest_seen is None or updated > newest_seen):
                    newest_seen = updated
    except ShopifyError as exc:
        fetch_error = f"Shopify request failed: {exc}. {exc.body[:300]}"
        if "protected customer data" in exc.body:
            fetch_error += (
                " Fix: in the Shopify Dev Dashboard, request protected customer data access "
                "(name, email, phone, address) for the app."
            )
    except (ShopifyConfigError, httpx.HTTPError) as exc:
        fetch_error = f"Shopify request failed: {exc}"
    except Exception as exc:
        logger.exception("Order import %s crashed", order_import_id)
        fetch_error = f"Unexpected error: {type(exc).__name__}: {exc}"
    finish_import(session_factory, order_import_id, fetch_error)
    save_sync_progress(session_factory, order_import_id, newest_seen)


def save_sync_progress(
    session_factory: sessionmaker[Session], order_import_id: int, newest_seen: datetime | None
) -> None:
    """Record the outcome on the account. A completed or partially completed run moves
    the cursor (never backwards) and last_synced_at; its failed rows carry their errors."""
    with session_factory.begin() as session:
        run = session.get(OrderImport, order_import_id)
        account = session.get(ChannelAccount, run.channel_account_id, with_for_update=True)
        account.last_sync_status = run.status
        if run.status == "failed":  # fetch error, or every order failed: retry next time
            return
        current = timestamp(account.sync_cursor, [], "sync_cursor")
        if newest_seen and (current is None or newest_seen > current):
            account.sync_cursor = newest_seen.astimezone(UTC).isoformat()  # always UTC
        run.cursor_end = account.sync_cursor
        account.last_synced_at = run.completed_at


def record_webhook_delivery(
    session: Session, topic: str, delivery_id: str | None, payload: Any
) -> tuple[int, int] | None:
    """Record a webhook delivery as a run with one row, in the caller's transaction.

    Returns (order_import_id, order_import_row_id), or None when this delivery id was
    already recorded (Shopify retried, or two deliveries raced): the unique
    (channel_account_id, external_event_id) constraint decides.
    """
    account = ensure_shopify_account(session)
    run = OrderImport(
        seller_id=account.seller_id,
        channel_account_id=account.channel_account_id,
        channel="shopify",
        ingestion_method="webhook",
        external_event_id=clip(delivery_id, 150),
        event_topic=clip(topic, 50),
    )
    try:
        with session.begin_nested():
            session.add(run)
            session.flush()
    except IntegrityError as exc:
        constraint = getattr(getattr(exc.orig, "diag", None), "constraint_name", "")
        if constraint == "uq_ecs_order_import_channel_account_id_external_event_id":
            return None
        raise
    return run.order_import_id, record_import_row(session, run.order_import_id, 1, payload)


def run_webhook_import(
    order_import_id: int, row_id: int, *, session_factory: sessionmaker[Session] = SessionLocal
) -> None:
    """Process a recorded webhook delivery (one row) and close its run."""
    mark_processing(session_factory, order_import_id)
    process_import_row(row_id, session_factory=session_factory)
    finish_import(session_factory, order_import_id, None)


def mark_processing(session_factory: sessionmaker[Session], order_import_id: int) -> None:
    with session_factory.begin() as session:
        run = session.get(OrderImport, order_import_id)
        run.status = "processing"
        run.started_at = utcnow()


def record_import_row(
    session: Session, order_import_id: int, record_number: int, payload: Any
) -> int:
    """Store one received order exactly as received, before any processing. Runs in the
    caller's transaction."""
    payload = clean_payload(payload)
    order = payload if isinstance(payload, dict) else {}
    row = OrderImportRow(
        order_import_id=order_import_id,
        record_number=record_number,
        external_order_id=clip(order.get("id"), 100),
        external_updated_at=timestamp(order.get("updated_at"), [], "updated_at"),
        raw_payload=payload,
        payload_hash=payload_hash(payload),
    )
    session.add(row)
    session.flush()
    session.execute(
        update(OrderImport)
        .where(OrderImport.order_import_id == order_import_id)
        .values(total_records=OrderImport.total_records + 1)
    )
    return row.order_import_row_id


def finish_import(
    session_factory: sessionmaker[Session], order_import_id: int, fetch_error: str | None
) -> None:
    with session_factory.begin() as session:
        run = session.get(OrderImport, order_import_id, with_for_update=True)
        if fetch_error:
            run.status = "failed"
            run.error_summary = (
                f"{fetch_error} Stopped after {run.total_records} orders; "
                "orders already processed are kept."
            )
        elif run.failed_count == 0:
            run.status = "completed"
        elif run.failed_count >= run.total_records:
            run.status = "failed"
            run.error_summary = "Every order failed; see the failed import rows."
        else:
            run.status = "partially_completed"
            run.error_summary = f"{run.failed_count} of {run.total_records} orders failed."
        run.completed_at = utcnow()


# --- Shared processing step ---------------------------------------------------------------


def process_import_row(
    row_id: int, *, session_factory: sessionmaker[Session] = SessionLocal
) -> str:
    """The ERD's shared processing step for one import row, in one transaction.

    Returns the action taken. Safe to call twice: a processed row is left alone.
    """
    errors: list[str] = []
    for attempt in (1, 2):
        try:
            with session_factory.begin() as session:
                row = session.get(OrderImportRow, row_id, with_for_update=True)
                if row.status in ("processed", "failed"):
                    return row.action_taken
                run = session.get(OrderImport, row.order_import_id)
                action, header_id = apply_order_row(session, run, row)
                row.status = "processed"
                row.action_taken = action
                row.order_header_id = header_id
                row.error_messages = None
                row.processed_at = utcnow()
                bump_counters(session, run.order_import_id, action)
                return action
        except OrderMappingError as exc:
            errors = exc.messages
        except IntegrityError as exc:
            constraint = getattr(getattr(exc.orig, "diag", None), "constraint_name", "") or ""
            if attempt == 1 and constraint in ORDER_CREATE_RACE_CONSTRAINTS:
                # Most likely created concurrently (e.g. by a webhook); Postgres may report
                # either unique constraint. The retry finds the order and updates it; a
                # genuine order-number clash with a different order fails again below.
                continue
            if constraint == "uq_ecs_order_header_order_number":
                errors = ["Order number is already used by a different order"]
            else:
                errors = [f"Database rejected the order ({constraint or 'integrity error'})"]
        except Exception as exc:
            logger.exception("Processing import row %s failed", row_id)
            errors = [f"{type(exc).__name__}: {exc}"]
        break

    with session_factory.begin() as session:
        row = session.get(OrderImportRow, row_id, with_for_update=True)
        row.status = "failed"
        row.action_taken = "failed"
        row.error_messages = errors
        row.processed_at = utcnow()
        bump_counters(session, row.order_import_id, "failed")
    return "failed"


def bump_counters(session: Session, order_import_id: int, action: str) -> None:
    counter = COUNTER_FOR_ACTION[action]
    session.execute(
        update(OrderImport)
        .where(OrderImport.order_import_id == order_import_id)
        .values(
            {
                "processed_records": OrderImport.processed_records + 1,
                counter: getattr(OrderImport, counter) + 1,
            }
        )
    )


def apply_order_row(session: Session, run: OrderImport, row: OrderImportRow) -> tuple[str, int]:
    mapped = map_shopify_order(row.raw_payload)
    header = session.scalars(
        select(OrderHeader)
        .where(
            OrderHeader.seller_id == run.seller_id,
            OrderHeader.channel == run.channel,
            OrderHeader.external_order_id == mapped.external_order_id,
        )
        .with_for_update()
    ).one_or_none()

    if header is None:
        header = create_order(session, run, row, mapped)
        return "created", header.order_header_id

    last_hash = session.scalar(
        select(OrderImportRow.payload_hash)
        .where(
            OrderImportRow.order_header_id == header.order_header_id,
            OrderImportRow.action_taken.in_(CONSISTENT_ACTIONS),
            OrderImportRow.order_import_row_id != row.order_import_row_id,
        )
        .order_by(OrderImportRow.order_import_row_id.desc())
        .limit(1)
    )
    if last_hash == row.payload_hash:
        return "unchanged", header.order_header_id

    if (
        mapped.external_updated_at
        and header.external_updated_at
        and mapped.external_updated_at <= header.external_updated_at
    ):
        return "skipped_stale", header.order_header_id

    changed = update_order(session, run, row, header, mapped)
    return ("updated" if changed else "unchanged"), header.order_header_id


def history_ids(run: OrderImport, row: OrderImportRow) -> dict[str, Any]:
    return {
        "change_source": run.ingestion_method,
        "order_import_id": run.order_import_id,
        "order_import_row_id": row.order_import_row_id,
    }


def create_order(
    session: Session, run: OrderImport, row: OrderImportRow, mapped: MappedOrder
) -> OrderHeader:
    header = OrderHeader(
        seller_id=run.seller_id,
        channel=run.channel,
        channel_account_id=run.channel_account_id,
        external_order_id=mapped.external_order_id,
        ingestion_method=run.ingestion_method,
        created_by_import_id=run.order_import_id,
        last_import_id=run.order_import_id,
        last_ingestion_method=run.ingestion_method,
        external_updated_at=mapped.external_updated_at,
        last_synced_at=utcnow(),
        version_no=1,
        **mapped.header,
    )
    header.lines = [OrderLine(version_no=1, **line) for line in mapped.lines]
    session.add(header)
    session.flush()

    session.add(
        OrderHistory(
            order_header_id=header.order_header_id,
            version_no=1,
            action_type="created",
            new_status=header.order_status,
            **history_ids(run, row),
        )
    )
    for line in header.lines:
        session.add(
            OrderLineHistory(
                order_line_id=line.order_line_id,
                order_header_id=header.order_header_id,
                version_no=1,
                action_type="created",
                new_status=line.line_status,
                **history_ids(run, row),
            )
        )
    return header


def line_action(old_status: str, new_status: str, diff: dict[str, Any]) -> str | None:
    """The line history action_type for a change, or None for changes recorded only
    in the header history (e.g. a renamed product)."""
    if old_status != new_status:
        return {"cancelled": "cancelled", "shipped": "shipped", "returned": "returned"}.get(
            new_status, "status_change"
        )
    if {"ordered_qty", "cancelled_qty", "returned_qty"} & diff.keys():
        return "qty_change"
    if "line_refunded_amount" in diff:
        return "refunded"
    return None


def apply_line_values(line: OrderLine, values: dict[str, Any]) -> dict[str, Any]:
    """Apply mapped values, never moving the status backwards. Returns the diff."""
    values = {k: v for k, v in values.items() if k not in LINE_FIELDS_KEPT}
    old_status = line.line_status
    new_status = values["line_status"]
    if old_status != new_status and (
        old_status in ("cancelled", "returned")
        or LINE_STATUS_RANK[new_status] < LINE_STATUS_RANK[old_status]
    ):
        values = {k: v for k, v in values.items() if k not in LINE_SHIPPING_FIELDS}
    diff = {}
    for key, value in values.items():
        old = getattr(line, key)
        if old != value:
            diff[key] = [json_safe(old), json_safe(value)]
            setattr(line, key, value)
    return diff


def update_order(
    session: Session,
    run: OrderImport,
    row: OrderImportRow,
    header: OrderHeader,
    mapped: MappedOrder,
) -> bool:
    """Apply a newer version of an existing order. Returns False if nothing changed."""
    line_diffs: dict[str, Any] = {}
    line_history: list[tuple[OrderLine, str, str, str]] = []
    existing = {line.external_line_id: line for line in header.lines}
    next_number = max((line.line_number for line in header.lines), default=0) + 1
    new_lines: list[OrderLine] = []

    for values in mapped.lines:
        line = existing.pop(values["external_line_id"], None)
        if line is None:  # item added by an order edit
            line = OrderLine(version_no=1, **{**values, "line_number": next_number})
            next_number += 1
            header.lines.append(line)
            new_lines.append(line)
            line_diffs[f"line {line.line_number}"] = "added"
            continue
        old_status = line.line_status
        diff = apply_line_values(line, values)
        if diff:
            line.version_no += 1
            line_diffs[f"line {line.line_number}"] = diff
            action = line_action(old_status, line.line_status, diff)
            if action:
                line_history.append((line, action, old_status, line.line_status))

    for line in existing.values():  # no longer in the Shopify order
        if line.line_status in ("cancelled", "returned"):
            continue
        old_status = line.line_status
        line.cancelled_qty = max(line.ordered_qty - line.shipped_qty, 0)
        line.line_status = "cancelled"
        line.version_no += 1
        line_diffs[f"line {line.line_number}"] = {"line_status": [old_status, "cancelled"]}
        line_history.append((line, "cancelled", old_status, "cancelled"))

    header_values = dict(mapped.header)
    new_status = order_status_from_lines([line.line_status for line in header.lines])
    if ORDER_STATUS_RANK[new_status] < ORDER_STATUS_RANK.get(header.order_status, 0):
        new_status = header.order_status  # never backwards
    header_values["order_status"] = new_status

    old_order_status = header.order_status
    changes: dict[str, Any] = {}
    for key, value in header_values.items():
        old = getattr(header, key)
        if old != value:
            changes[key] = [json_safe(old), json_safe(value)]
            setattr(header, key, value)

    header.external_updated_at = mapped.external_updated_at or header.external_updated_at
    header.last_synced_at = utcnow()
    if not changes and not line_diffs:
        return False

    header.version_no += 1
    header.last_import_id = run.order_import_id
    header.last_ingestion_method = run.ingestion_method
    session.flush()  # ids for new lines

    if header.order_status != old_order_status:
        action = "cancelled" if header.order_status == "cancelled" else "status_change"
    else:
        action = "synced_update"
    if line_diffs:
        changes["lines"] = line_diffs
    session.add(
        OrderHistory(
            order_header_id=header.order_header_id,
            version_no=header.version_no,
            action_type=action,
            old_status=old_order_status,
            new_status=header.order_status,
            changed_fields=changes,
            **history_ids(run, row),
        )
    )
    for line in new_lines:
        line_history.append((line, "created", None, line.line_status))
    for line, line_action_type, old, new in line_history:
        session.add(
            OrderLineHistory(
                order_line_id=line.order_line_id,
                order_header_id=header.order_header_id,
                version_no=line.version_no,
                action_type=line_action_type,
                old_status=old,
                new_status=new,
                **history_ids(run, row),
            )
        )
    return True


# --- Scheduled and manual sync (#12) ------------------------------------------------------


@dataclass
class SyncStart:
    result: str  # started | already_running | not_due | disabled
    run: OrderImport | None = None
    updated_at_min: datetime | None = None
    next_due_at: datetime | None = None


def start_order_sync(session: Session, trigger: str) -> SyncStart:
    """Start a sync run for the Shopify account, in the caller's transaction.

    `trigger` is "scheduled" or "manual". Scheduled calls are skipped until
    sync_interval_minutes have passed since the last successful sync, so the external
    scheduler can call more often than the interval; manual calls always run.
    """
    account = ensure_shopify_account(session)
    if not account.sync_enabled:
        return SyncStart("disabled")
    if trigger == "scheduled" and account.last_synced_at:
        # A minute's grace, so a scheduler running exactly every interval isn't skipped.
        next_due_at = account.last_synced_at + timedelta(minutes=account.sync_interval_minutes - 1)
        if utcnow() < next_due_at:
            return SyncStart("not_due", next_due_at=next_due_at)
    try:
        run = start_import(
            session,
            account.channel_account_id,
            "scheduled_sync" if trigger == "scheduled" else "manual_sync",
        )
    except ImportAlreadyRunning as exc:
        return SyncStart("already_running", run=session.get(OrderImport, exc.order_import_id))
    updated_at_min = sync_start_time(session, account)
    run.cursor_start = account.sync_cursor
    run.sync_from = updated_at_min
    run.sync_to = utcnow()
    return SyncStart("started", run=run, updated_at_min=updated_at_min)
