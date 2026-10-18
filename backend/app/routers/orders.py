"""Order import endpoints. The order list/detail/status endpoints come with #10."""

from datetime import datetime

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query
from pydantic import BaseModel, ConfigDict
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db import get_db
from app.models import OrderImport, OrderImportRow
from app.services import order_ingestion
from app.services.shopify import ShopifyConfigError, ShopifyError

router = APIRouter(prefix="/orders", tags=["orders"])

READ_ALL_ORDERS_WARNING = (
    "The Shopify app lacks the read_all_orders scope, so only orders from the last "
    "60 days are imported. Add the scope in the Shopify Dev Dashboard to import older orders."
)


class ImportRun(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    order_import_id: int
    channel: str
    ingestion_method: str
    status: str
    total_records: int
    processed_records: int
    created_count: int
    updated_count: int
    unchanged_count: int
    skipped_count: int
    failed_count: int
    error_summary: str | None
    started_at: datetime | None
    completed_at: datetime | None
    created_at: datetime


class ImportStarted(ImportRun):
    warnings: list[str]


class ImportRowOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    order_import_row_id: int
    record_number: int
    external_order_id: str | None
    status: str
    action_taken: str | None
    error_messages: list[str] | None
    order_header_id: int | None


@router.post("/import", status_code=202, response_model=ImportStarted)
def import_orders(background_tasks: BackgroundTasks, db: Session = Depends(get_db)):
    """Start importing every Shopify order. Runs in the background: poll
    GET /orders/imports/{order_import_id} for progress."""
    try:
        with order_ingestion.shopify_client_factory() as shopify:
            scopes = shopify.access_scopes()
    except ShopifyConfigError as exc:
        raise HTTPException(503, f"Shopify is not configured: {exc}") from exc
    except ShopifyError as exc:
        raise HTTPException(502, f"Shopify rejected the request: {exc}") from exc
    if "read_orders" not in scopes:
        raise HTTPException(503, "The Shopify app lacks the read_orders scope.")
    warnings = [] if "read_all_orders" in scopes else [READ_ALL_ORDERS_WARNING]

    try:
        connection = order_ingestion.ensure_shopify_connection(db)
        run = order_ingestion.start_import(db, connection.channel_connection_id, "manual_sync")
        db.commit()
    except order_ingestion.ImportAlreadyRunning as exc:
        db.rollback()
        raise HTTPException(
            409,
            {
                "message": "An order import is already running for this store.",
                "order_import_id": exc.order_import_id,
            },
        ) from exc

    background_tasks.add_task(order_ingestion.run_shopify_order_import, run.order_import_id)
    return ImportStarted(**ImportRun.model_validate(run).model_dump(), warnings=warnings)


def get_run(db: Session, order_import_id: int) -> OrderImport:
    run = db.get(OrderImport, order_import_id)
    if run is None:
        raise HTTPException(404, "Import run not found")
    return run


@router.get("/imports/{order_import_id}", response_model=ImportRun)
def get_import(order_import_id: int, db: Session = Depends(get_db)):
    return get_run(db, order_import_id)


@router.get("/imports/{order_import_id}/rows", response_model=list[ImportRowOut])
def get_import_rows(
    order_import_id: int,
    status: str | None = Query(None, description="e.g. failed"),
    limit: int = Query(100, ge=1, le=1000),
    db: Session = Depends(get_db),
):
    get_run(db, order_import_id)
    query = select(OrderImportRow).where(OrderImportRow.order_import_id == order_import_id)
    if status:
        query = query.where(OrderImportRow.status == status)
    return db.scalars(query.order_by(OrderImportRow.record_number).limit(limit)).all()
