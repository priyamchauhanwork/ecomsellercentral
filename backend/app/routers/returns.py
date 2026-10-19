"""Returns and RTOs (#53–#55; docs/inventory-erd.md): list, detail, Shopify sync,
receive, QC (reported to Shopify), ETA, and cancelling or closing RTOs."""

from datetime import date, datetime
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field, StrictInt
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from app.auth import require_api_key
from app.config import get_settings
from app.db import SessionLocal, get_db
from app.models import OrderHeader, OrderLine, Return, ReturnLine
from app.services import product_import, returns, stock, stock_sync
from app.services.catalog import like_pattern
from app.services.shopify import ShopifyConfigError, ShopifyError

router = APIRouter(prefix="/returns", tags=["returns"], dependencies=[Depends(require_api_key)])

ReturnStatus = Literal[
    "requested", "open", "received", "qc_done", "closed", "declined", "cancelled"
]
Quantity = Annotated[StrictInt, Field(ge=0, le=100_000)]


class ReturnLineOut(BaseModel):
    return_line_id: int
    order_line_id: int
    item_id: int | None
    sku: str | None
    product_name: str
    variant_name: str | None
    return_qty: int
    received_qty: int
    accepted_qty: int
    rejected_qty: int
    missing_qty: int
    pending_qty: int  # still expected back
    return_reason: str | None
    customer_note: str | None
    qc_note: str | None
    status: str
    can_report_qc: bool  # Shopify has a reverse fulfilment line to report the QC to


class ReturnSummary(BaseModel):
    return_id: int
    return_number: str
    return_type: str
    status: str
    external_status: str | None
    order_header_id: int
    order_number: str
    channel: str
    expected_at: date | None
    return_qty: int
    pending_qty: int
    created_at: datetime


class ReturnDetail(ReturnSummary):
    external_return_id: str | None
    received_at: datetime | None
    qc_completed_at: datetime | None
    note: str | None
    updated_at: datetime
    lines: list[ReturnLineOut]


class ReturnList(BaseModel):
    total: int
    returns: list[ReturnSummary]


class ReceivedLine(BaseModel):
    return_line_id: int
    received_qty: Quantity


class Receive(BaseModel):
    lines: list[ReceivedLine] = Field(min_length=1)


class QcLine(BaseModel):
    return_line_id: int
    accepted: Quantity = 0
    rejected: Quantity = 0


class Qc(BaseModel):
    lines: list[QcLine] = Field(min_length=1)
    note: str | None = Field(None, max_length=500)


class EtaChange(BaseModel):
    expected_at: date


class SyncResult(BaseModel):
    orders: int
    changed_orders: int
    errors: list[str]


def seller_id() -> int:
    return get_settings().seller_id


def pending(line: ReturnLine, ret: Return) -> int:
    if ret.status not in ("open", "received") or line.status == "cancelled":
        return 0
    return max(line.return_qty - line.accepted_qty - line.rejected_qty - line.missing_qty, 0)


def summary(ret: Return, order_number: str) -> dict:
    return {
        "return_id": ret.return_id,
        "return_number": ret.return_number,
        "return_type": ret.return_type,
        "status": ret.status,
        "external_status": ret.external_status,
        "order_header_id": ret.order_header_id,
        "order_number": order_number,
        "channel": ret.channel,
        "expected_at": ret.expected_at,
        "return_qty": sum(line.return_qty for line in ret.lines),
        "pending_qty": sum(pending(line, ret) for line in ret.lines),
        "created_at": ret.created_at,
    }


def detail(db: Session, return_id: int) -> ReturnDetail:
    ret = db.get(Return, return_id)
    if ret is None or ret.seller_id != seller_id():
        raise HTTPException(404, "Return not found")
    order_number = db.scalar(
        select(OrderHeader.order_number).where(OrderHeader.order_header_id == ret.order_header_id)
    )
    order_lines = {
        line.order_line_id: line
        for line in db.scalars(
            select(OrderLine).where(
                OrderLine.order_line_id.in_([line.order_line_id for line in ret.lines])
            )
        )
    }
    lines = []
    for line in ret.lines:
        order_line = order_lines[line.order_line_id]
        lines.append(
            ReturnLineOut(
                return_line_id=line.return_line_id,
                order_line_id=line.order_line_id,
                item_id=line.item_id,
                sku=order_line.sku,
                product_name=order_line.product_name,
                variant_name=order_line.variant_name,
                return_qty=line.return_qty,
                received_qty=line.received_qty,
                accepted_qty=line.accepted_qty,
                rejected_qty=line.rejected_qty,
                missing_qty=line.missing_qty,
                pending_qty=pending(line, ret),
                return_reason=line.return_reason,
                customer_note=line.customer_note,
                qc_note=line.qc_note,
                status=line.status,
                can_report_qc=ret.return_type == "rto" or bool(line.external_disposal_ref),
            )
        )
    return ReturnDetail(
        **summary(ret, order_number),
        external_return_id=ret.external_return_id,
        received_at=ret.received_at,
        qc_completed_at=ret.qc_completed_at,
        note=ret.note,
        updated_at=ret.updated_at,
        lines=lines,
    )


def change_return(db: Session, return_id: int, action) -> ReturnDetail:
    """Lock the return, apply `action`, reconcile the order's stock, commit, then push
    the items whose `available` changed (QC) to Shopify."""
    try:
        ret = returns.load_return(db, seller_id(), return_id)
        action(ret)
        header = db.get(OrderHeader, ret.order_header_id, with_for_update=True)
        changed = stock.reconcile_order(db, header).finish()
    except LookupError as exc:
        db.rollback()
        raise HTTPException(404, "Return not found") from exc
    except returns.ReturnInvalid as exc:
        db.rollback()
        raise HTTPException(422, str(exc)) from exc
    except returns.ReturnConflict as exc:
        db.rollback()
        raise HTTPException(409, str(exc)) from exc
    except ShopifyConfigError as exc:
        db.rollback()
        raise HTTPException(503, f"Shopify is not configured: {exc}") from exc
    except ShopifyError as exc:
        db.rollback()
        raise HTTPException(
            502, f"Shopify didn't accept the QC result, so nothing was saved: {exc}"
        ) from exc
    db.commit()
    if changed:
        stock_sync.sync_items(changed)
    db.expire_all()
    return detail(db, return_id)


@router.get("", response_model=ReturnList)
def list_returns(
    status: ReturnStatus | None = None,
    return_type: Literal["customer_return", "rto"] | None = Query(None, alias="type"),
    pending_only: bool = Query(False, alias="pending"),
    q: str | None = Query(None, max_length=100, description="Return or order number"),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
):
    """Returns and RTOs, newest first."""
    filters = [Return.seller_id == seller_id()]
    if status:
        filters.append(Return.status == status)
    if pending_only:
        filters.append(Return.status.in_(("open", "received")))
    if return_type:
        filters.append(Return.return_type == return_type)
    if q and q.strip():
        pattern = like_pattern(q.strip())
        filters.append(
            or_(
                Return.return_number.ilike(pattern, escape="\\"),
                OrderHeader.order_number.ilike(pattern, escape="\\"),
            )
        )
    base = select(Return, OrderHeader.order_number).join(
        OrderHeader, OrderHeader.order_header_id == Return.order_header_id
    )
    rows = db.execute(
        base.where(*filters)
        .order_by(Return.created_at.desc(), Return.return_id.desc())
        .limit(limit)
        .offset(offset)
    ).all()
    total = db.scalar(select(func.count()).select_from(base.where(*filters).subquery()))
    return ReturnList(total=total, returns=[summary(ret, number) for ret, number in rows])


@router.post("/sync", response_model=SyncResult)
def sync(order_id: int | None = None, db: Session = Depends(get_db)):
    """Read Shopify's returns of every shipped order (or of one order) and apply them:
    the cutover, and a manual catch-up after missed webhooks."""
    header_ids = returns.fulfilled_order_ids(db, seller_id(), [order_id] if order_id else None)
    db.close()
    try:
        with product_import.shopify_client_factory() as shopify:
            result = returns.sync_returns(SessionLocal, shopify, header_ids)
    except ShopifyConfigError as exc:
        raise HTTPException(503, f"Shopify is not configured: {exc}") from exc
    except ShopifyError as exc:
        raise HTTPException(502, str(exc)) from exc
    if result["items"]:
        stock_sync.sync_items(result["items"])
    return SyncResult(
        orders=result["orders"], changed_orders=result["changed_orders"], errors=result["errors"]
    )


@router.get("/{return_id}", response_model=ReturnDetail)
def get_return(return_id: int, db: Session = Depends(get_db)):
    return detail(db, return_id)


@router.post("/{return_id}/receive", response_model=ReturnDetail)
def receive(return_id: int, body: Receive, db: Session = Depends(get_db)):
    """Record the units that arrived, per line (stock moves at QC)."""
    received = {line.return_line_id: line.received_qty for line in body.lines}
    return change_return(db, return_id, lambda ret: returns.receive(db, ret, received))


@router.post("/{return_id}/qc", response_model=ReturnDetail)
def qc(return_id: int, body: Qc, db: Session = Depends(get_db)):
    """Add QC outcomes: accepted units go to on-hand stock, rejected ones to damaged.
    For a Shopify return they're reported to Shopify first; final once saved."""
    outcomes = {
        line.return_line_id: (line.accepted, line.rejected)
        for line in body.lines
        if line.accepted or line.rejected
    }
    if not outcomes:
        raise HTTPException(422, "Enter at least one accepted or rejected unit.")
    return change_return(
        db,
        return_id,
        lambda ret: returns.record_qc(
            db, product_import.shopify_client_factory, ret, outcomes, body.note
        ),
    )


@router.patch("/{return_id}", response_model=ReturnDetail)
def set_eta(return_id: int, body: EtaChange, db: Session = Depends(get_db)):
    """Correct when the units are expected back."""
    return change_return(db, return_id, lambda ret: returns.set_eta(db, ret, body.expected_at))


@router.post("/{return_id}/cancel", response_model=ReturnDetail)
def cancel(return_id: int, db: Session = Depends(get_db)):
    """Cancel an RTO entered by mistake: its units are shipped again."""
    return change_return(db, return_id, lambda ret: returns.cancel_rto(db, ret))


@router.post("/{return_id}/close", response_model=ReturnDetail)
def close(return_id: int, db: Session = Depends(get_db)):
    """Close an RTO whose remaining units never came back."""
    return change_return(db, return_id, lambda ret: returns.close_rto(db, ret))
