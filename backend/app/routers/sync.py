"""Polling fallback (#12): POST /sync/orders, called by the scheduler every few minutes
(see docs/infra.md) and usable for a manual "Sync now"."""

from datetime import datetime
from typing import Literal

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Response
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.auth import require_api_key
from app.db import get_db
from app.routers.orders import ImportRun
from app.services import order_ingestion
from app.services.shopify import ShopifyConfigError

router = APIRouter(prefix="/sync", tags=["sync"], dependencies=[Depends(require_api_key)])


class SyncResult(BaseModel):
    result: Literal["started", "already_running", "not_due", "disabled"]
    run: ImportRun | None = None
    next_due_at: datetime | None = None


@router.post("/orders", response_model=SyncResult)
def sync_orders(
    response: Response,
    background_tasks: BackgroundTasks,
    trigger: Literal["scheduled", "manual"] = "scheduled",
    db: Session = Depends(get_db),
):
    """Fetch Shopify orders updated since the last sync and process them in the
    background. 202 when a run starts; 200 when nothing starts (a run is already in
    progress, the sync isn't due yet, or syncing is disabled), so schedulers don't fail."""
    try:
        start = order_ingestion.start_order_sync(db, trigger)
    except ShopifyConfigError as exc:
        raise HTTPException(503, f"Shopify is not configured: {exc}") from exc
    db.commit()

    if start.result == "started":
        background_tasks.add_task(
            order_ingestion.run_shopify_order_import,
            start.run.order_import_id,
            updated_at_min=start.updated_at_min,
        )
        response.status_code = 202
    return SyncResult(
        result=start.result,
        run=ImportRun.model_validate(start.run) if start.run else None,
        next_due_at=start.next_due_at,
    )
