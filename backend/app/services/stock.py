"""The supply & demand engine (docs/inventory-erd.md).

Every stock change goes through a StockChange: it locks the items (FOR NO KEY UPDATE, in
item-id order), changes supply and demand rows, writes one ecs_inventory_log row per
change with a shared transaction id, and finally refreshes ecs_items.stock_qty, the
cached `available`. Order and return events are reconciled from targets computed from
the order and return lines, so replaying any event changes nothing.
"""

import logging
import uuid
from collections import defaultdict
from datetime import date
from typing import Any

from sqlalchemy import func, select, text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from app.models import (
    InventoryDemand,
    InventoryLog,
    InventorySupply,
    Item,
    OrderHeader,
    OrderLine,
    Return,
    ReturnLine,
)

logger = logging.getLogger(__name__)

ACKNOWLEDGED_LINE_STATUSES = ("allocated", "packed")
# Customer returns that say the customer had the units (approved or further on).
APPROVED_RETURN_STATUSES = ("open", "received", "qc_done", "closed")
PENDING_RETURN_STATUSES = ("open", "received")


class StockInvalid(ValueError):
    """A manual change the stock doesn't allow (e.g. removing more than is on the shelf)."""


def item_stock(session: Session, item_ids) -> dict[int, dict[str, Any]]:
    """Every figure of these items from the ecs_item_stock view."""
    if not item_ids:
        return {}
    rows = session.execute(
        text("SELECT * FROM ecs_item_stock WHERE item_id = ANY(:ids)"), {"ids": list(item_ids)}
    ).mappings()
    return {row["item_id"]: dict(row) for row in rows}


def counts_for_stock(order_date, item: Item) -> bool:
    """Only orders placed after the item's starting stock was taken change it: that
    figure (copied from Shopify by #13) already reflects every older order."""
    return item.stock_baseline_at is None or order_date > item.stock_baseline_at


class StockChange:
    """One stock event, in the caller's transaction."""

    def __init__(self, session: Session, note: str | None = None) -> None:
        self.session = session
        self.note = note
        self.transaction_id = uuid.uuid4()
        self.items: dict[int, Item] = {}
        self.available_before: dict[int, int] = {}

    def lock(self, item_ids) -> dict[int, Item]:
        """Lock the items (once each, in id order, so concurrent events can't deadlock;
        FOR NO KEY UPDATE doesn't block the key-share locks of new order lines)."""
        new_ids = sorted(set(item_ids) - self.items.keys())
        if new_ids:
            for item in self.session.scalars(
                select(Item)
                .where(Item.item_id.in_(new_ids))
                .order_by(Item.item_id)
                .with_for_update(key_share=True)
            ):
                self.items[item.item_id] = item
                self.available_before[item.item_id] = item.stock_qty
            self.open_missing(new_ids)
        return {item_id: self.items[item_id] for item_id in item_ids if item_id in self.items}

    def open_missing(self, item_ids: list[int]) -> None:
        """Items without supply rows (made outside open_item_stock) get them now, from
        their cached stock, so `available` never jumps."""
        have = set(
            self.session.scalars(
                select(InventorySupply.item_id).where(
                    InventorySupply.item_id.in_(item_ids), InventorySupply.supply_type == "ONHAND"
                )
            )
        )
        for item_id in item_ids:
            if item_id in have or item_id not in self.items:
                continue
            item = self.items[item_id]
            committed = self.session.scalar(
                select(func.coalesce(func.sum(InventoryDemand.quantity), 0)).where(
                    InventoryDemand.item_id == item_id
                )
            )
            self.supply_row(item, "DAMAGED")
            self.change_supply(item, "ONHAND", item.stock_qty + committed, "opening_stock")

    # --- Rows -----------------------------------------------------------------------------

    def supply_row(self, item: Item, supply_type: str) -> InventorySupply:
        """The item's ONHAND or DAMAGED row, created at 0 if missing."""
        self.session.execute(
            pg_insert(InventorySupply)
            .values(seller_id=item.seller_id, item_id=item.item_id, supply_type=supply_type)
            .on_conflict_do_nothing()
        )
        return self.session.scalars(
            select(InventorySupply).where(
                InventorySupply.item_id == item.item_id,
                InventorySupply.supply_type == supply_type,
                InventorySupply.return_line_id.is_(None),
            )
        ).one()

    def change_supply(
        self,
        item: Item,
        supply_type: str,
        qty_change: int,
        reason: str,
        *,
        order_line_id: int | None = None,
        return_line_id: int | None = None,
        eta: date | None = None,
        note: str | None = None,
    ) -> None:
        if qty_change == 0:
            return
        if supply_type == "RETURN_PENDING":
            row = self.session.scalars(
                select(InventorySupply).where(InventorySupply.return_line_id == return_line_id)
            ).one_or_none()
            if row is None:
                row = InventorySupply(
                    seller_id=item.seller_id,
                    item_id=item.item_id,
                    supply_type="RETURN_PENDING",
                    quantity=0,
                    return_line_id=return_line_id,
                    eta=eta,
                )
                self.session.add(row)
            row.quantity += qty_change
            after = row.quantity
            if row.quantity == 0:
                self.session.delete(row)
        else:
            row = self.supply_row(item, supply_type)
            row.quantity += qty_change
            after = row.quantity
        self.log(
            item,
            "SUPPLY",
            supply_type,
            qty_change,
            after,
            reason,
            order_line_id,
            return_line_id,
            note,
        )

    def change_demand(
        self,
        item: Item,
        line: OrderLine,
        demand_type: str,
        qty_change: int,
        reason: str,
        rows: dict[tuple[int, str], InventoryDemand],
    ) -> None:
        if qty_change == 0:
            return
        key = (line.order_line_id, demand_type)
        row = rows.get(key)
        if row is None:
            row = InventoryDemand(
                seller_id=item.seller_id,
                item_id=item.item_id,
                order_header_id=line.order_header_id,
                order_line_id=line.order_line_id,
                demand_type=demand_type,
                quantity=0,
            )
            self.session.add(row)
            rows[key] = row
        row.quantity += qty_change
        after = row.quantity
        if row.quantity == 0:
            self.session.delete(row)
            del rows[key]
        self.log(item, "DEMAND", demand_type, qty_change, after, reason, line.order_line_id)

    def log(
        self,
        item: Item,
        side: str,
        inv_type: str,
        qty_change: int,
        after: int,
        reason: str,
        order_line_id: int | None = None,
        return_line_id: int | None = None,
        note: str | None = None,
    ) -> None:
        self.session.add(
            InventoryLog(
                item_id=item.item_id,
                side=side,
                inv_type=inv_type,
                qty_change=qty_change,
                stock_after=after,
                reason=reason,
                note=note or self.note,
                order_line_id=order_line_id,
                return_line_id=return_line_id,
                transaction_id=self.transaction_id,
            )
        )

    # --- Finish ---------------------------------------------------------------------------

    def finish(self) -> set[int]:
        """Refresh the stock_qty cache. Returns the items whose `available` changed (the
        ones to push to Shopify, when the change didn't come from Shopify)."""
        self.session.flush()
        changed = set()
        for item_id, figures in item_stock(self.session, self.items).items():
            item = self.items[item_id]
            item.stock_qty = figures["available"]
            if figures["available"] != self.available_before[item_id]:
                changed.add(item_id)
        self.session.flush()
        return changed


# --- New items ----------------------------------------------------------------------------


def open_item_stock(session: Session, items: list[Item]) -> None:
    """Supply rows for new items: ONHAND = their opening stock_qty, DAMAGED = 0."""
    change = StockChange(session, "Opening stock")
    for item in items:
        change.items[item.item_id] = item
        change.available_before[item.item_id] = item.stock_qty
    change.open_missing([item.item_id for item in items])
    session.flush()


# --- Manual changes -----------------------------------------------------------------------


def figures(session: Session, item_id: int) -> dict[str, Any]:
    return item_stock(session, [item_id])[item_id]


def adjust_onhand(
    session: Session, item: Item, quantity_change: int, reason: str, note: str | None
) -> StockChange:
    """A manual change to ONHAND. Removing may not exceed what is on the shelf
    (ONHAND − SHIPPED). Reason `damage` moves units from the shelf to DAMAGED."""
    change = StockChange(session, note)
    change.lock([item.item_id])
    current = figures(session, item.item_id)
    if reason == "damage" and quantity_change > 0:
        raise StockInvalid("Damaged units are removed from stock; use Repair to bring them back.")
    if quantity_change < 0 and -quantity_change > current["on_shelf"]:
        raise StockInvalid(
            f"Only {max(current['on_shelf'], 0)} on the shelf, so {-quantity_change} "
            "can't be removed."
        )
    change.change_supply(item, "ONHAND", quantity_change, reason)
    if reason == "damage":
        change.change_supply(item, "DAMAGED", -quantity_change, reason)
    return change


def change_damaged(
    session: Session, item: Item, action: str, quantity: int, note: str | None
) -> StockChange:
    """Repair (DAMAGED → ONHAND) or write off (DAMAGED −) damaged units."""
    change = StockChange(session, note)
    change.lock([item.item_id])
    damaged = figures(session, item.item_id)["damaged"]
    if quantity > damaged:
        raise StockInvalid(
            f"Only {damaged} damaged, so {quantity} can't be {action.replace('_', ' ')}ed."
        )
    if action == "repair":
        change.change_supply(item, "DAMAGED", -quantity, "damaged_repaired")
        change.change_supply(item, "ONHAND", quantity, "damaged_repaired")
    else:
        change.change_supply(item, "DAMAGED", -quantity, "damaged_written_off")
    return change


# --- Orders and their returns -------------------------------------------------------------


def reconcile_order(
    session: Session,
    header: OrderHeader,
    *,
    new_order: bool = False,
    change: StockChange | None = None,
) -> StockChange:
    """Bring supply and demand in line with an order and its returns. Safe to call any
    number of times: only differences from the current rows and logs are written."""
    change = change or StockChange(session, f"Order {header.order_number}")
    lines = list(header.lines)
    returns = list(
        session.scalars(select(Return).where(Return.order_header_id == header.order_header_id))
    )
    return_lines = [rline for ret in returns for rline in ret.lines]
    change.lock(
        {line.variant_id for line in lines if line.variant_id}
        | {rline.item_id for rline in return_lines if rline.item_id}
    )
    session.flush()  # pending line changes, so the queries below see them

    rto_qty: dict[int, int] = defaultdict(int)
    returned_qty: dict[int, int] = defaultdict(int)
    for ret in returns:
        for rline in ret.lines:
            if ret.return_type == "rto" and ret.status != "cancelled":
                rto_qty[rline.order_line_id] += rline.return_qty
            elif ret.return_type == "customer_return" and ret.status in APPROVED_RETURN_STATUSES:
                returned_qty[rline.order_line_id] += rline.return_qty

    line_ids = [line.order_line_id for line in lines]
    demand = {
        (row.order_line_id, row.demand_type): row
        for row in session.scalars(
            select(InventoryDemand).where(InventoryDemand.order_line_id.in_(line_ids))
        )
    }
    onhand_logged = logged_by(session, InventoryLog.order_line_id, line_ids, "ONHAND")

    for line in lines:
        item = change.items.get(line.variant_id) if line.variant_id else None
        if item is None or not counts_for_stock(header.order_date, item):
            continue  # unlinked, or placed before the starting stock: never counted
        reconcile_line(change, item, line, demand, onhand_logged, rto_qty, returned_qty, new_order)

    for ret in returns:
        for rline in ret.lines:
            item = change.items.get(rline.item_id) if rline.item_id else None
            if item is not None:
                reconcile_return_line(session, change, item, ret, rline)
    return change


def logged_by(session: Session, column, ids, inv_type: str) -> dict[tuple[int, str], int]:
    """Σ qty_change of the logged changes to `inv_type`, per id and reason."""
    if not ids:
        return {}
    rows = session.execute(
        select(column, InventoryLog.reason, func.sum(InventoryLog.qty_change))
        .where(column.in_(ids), InventoryLog.side == "SUPPLY", InventoryLog.inv_type == inv_type)
        .group_by(column, InventoryLog.reason)
    )
    return {(row_id, reason): total for row_id, reason, total in rows}


def reconcile_line(
    change: StockChange,
    item: Item,
    line: OrderLine,
    demand: dict[tuple[int, str], InventoryDemand],
    onhand_logged: dict[tuple[int, str], int],
    rto_qty: dict[int, int],
    returned_qty: dict[int, int],
    new_order: bool,
) -> None:
    shipped = line.shipped_qty
    rto = min(rto_qty[line.order_line_id], shipped)
    # A customer return says the customer had the units; delivery never exceeds what
    # was shipped and not sent back undelivered.
    delivered = max(
        min(max(line.delivered_qty, returned_qty[line.order_line_id]), shipped - rto), 0
    )
    unshipped = max(line.ordered_qty - line.cancelled_qty - shipped, 0)
    acknowledged = line.line_status in ACKNOWLEDGED_LINE_STATUSES
    target = {
        "OPEN": 0 if acknowledged else unshipped,
        "ALLOCATED": unshipped if acknowledged else 0,
        "SHIPPED": max(shipped - delivered - rto, 0),
    }
    current = {
        t: (demand[(line.order_line_id, t)].quantity if (line.order_line_id, t) in demand else 0)
        for t in target
    }
    diff = {t: target[t] - current[t] for t in target}
    # Units accounted as shipped so far: still in SHIPPED, delivered, or sent back.
    shipped_before = (
        current["SHIPPED"]
        - onhand_logged.get((line.order_line_id, "order_delivered"), 0)
        - onhand_logged.get((line.order_line_id, "order_rto"), 0)
    )
    shipping = shipped > shipped_before
    moved_to_allocated = diff["ALLOCATED"] > 0 and diff["OPEN"] < 0

    rto_applied = -onhand_logged.get((line.order_line_id, "order_rto"), 0)

    def reason_for(demand_type: str, qty: int) -> str:
        if demand_type == "SHIPPED":
            if rto != rto_applied:
                return "order_rto"  # RTO marked, or cancelled (units shipped again)
            return "order_shipped" if qty > 0 else "order_delivered"
        if qty > 0:
            if moved_to_allocated and demand_type == "ALLOCATED":
                return "order_allocated"
            return "order_created" if new_order or not any(current.values()) else "order_edited"
        if moved_to_allocated and demand_type == "OPEN":
            return "order_allocated"
        return "order_shipped" if shipping else "order_cancelled"

    for demand_type in ("OPEN", "ALLOCATED", "SHIPPED"):
        qty = diff[demand_type]
        change.change_demand(item, line, demand_type, qty, reason_for(demand_type, qty), demand)

    # Delivered and RTO'd units leave ONHAND (RTO'd ones come back as RETURN_PENDING).
    for reason, units in (("order_delivered", delivered), ("order_rto", rto)):
        applied = -onhand_logged.get((line.order_line_id, reason), 0)
        change.change_supply(
            item, "ONHAND", applied - units, reason, order_line_id=line.order_line_id
        )


def reconcile_return_line(
    session: Session, change: StockChange, item: Item, ret: Return, rline: ReturnLine
) -> None:
    """RETURN_PENDING for what is still coming back; accepted units to ONHAND, rejected
    ones to DAMAGED (outcomes already in the starting stock excepted)."""
    outstanding = rline.return_qty - rline.accepted_qty - rline.rejected_qty - rline.missing_qty
    pending_target = (
        max(outstanding, 0)
        if ret.status in PENDING_RETURN_STATUSES and rline.status != "cancelled"
        else 0
    )
    row = session.scalars(
        select(InventorySupply).where(InventorySupply.return_line_id == rline.return_line_id)
    ).one_or_none()
    pending = row.quantity if row else 0
    if row is not None and row.eta != ret.expected_at:
        row.eta = ret.expected_at

    logged = logged_by(session, InventoryLog.return_line_id, [rline.return_line_id], "ONHAND")
    logged_damaged = logged_by(
        session, InventoryLog.return_line_id, [rline.return_line_id], "DAMAGED"
    )
    accepted = (
        rline.accepted_qty
        - rline.opening_accepted_qty
        - logged.get((rline.return_line_id, "return_accepted"), 0)
    )
    rejected = (
        rline.rejected_qty
        - rline.opening_rejected_qty
        - logged_damaged.get((rline.return_line_id, "return_rejected"), 0)
    )

    pending_change = pending_target - pending
    ids = {"return_line_id": rline.return_line_id, "order_line_id": rline.order_line_id}
    if pending_change > 0:
        change.change_supply(
            item, "RETURN_PENDING", pending_change, "return_opened", eta=ret.expected_at, **ids
        )
    else:
        remaining = -pending_change
        for reason, units in (("return_accepted", accepted), ("return_rejected", rejected)):
            take = min(max(units, 0), remaining)
            change.change_supply(item, "RETURN_PENDING", -take, reason, **ids)
            remaining -= take
        rest_reason = "return_closed" if ret.status in ("closed", "qc_done") else "return_cancelled"
        change.change_supply(item, "RETURN_PENDING", -remaining, rest_reason, **ids)
    change.change_supply(item, "ONHAND", accepted, "return_accepted", **ids)
    change.change_supply(item, "DAMAGED", rejected, "return_rejected", **ids)


def line_demand(session: Session, header: OrderHeader) -> dict[int, dict[str, Any]]:
    """Each line's place in stock, for the order page: its demand by type, delivered and
    RTO'd units, or why it doesn't count."""
    line_ids = [line.order_line_id for line in header.lines]
    rows = session.execute(
        select(
            InventoryDemand.order_line_id, InventoryDemand.demand_type, InventoryDemand.quantity
        ).where(InventoryDemand.order_line_id.in_(line_ids))
    ).all()
    demand: dict[int, dict[str, int]] = defaultdict(dict)
    for line_id, demand_type, quantity in rows:
        demand[line_id][demand_type.lower()] = quantity
    onhand = logged_by(session, InventoryLog.order_line_id, line_ids, "ONHAND")
    items = {
        item.item_id: item
        for item in session.scalars(
            select(Item).where(
                Item.item_id.in_({line.variant_id for line in header.lines if line.variant_id})
            )
        )
    }
    result = {}
    for line in header.lines:
        item = items.get(line.variant_id)
        if item is None:
            skipped = "not_linked"
        elif not counts_for_stock(header.order_date, item):
            skipped = "before_starting_stock"
        else:
            skipped = None
        result[line.order_line_id] = {
            "item_id": item.item_id if item else None,
            "open": demand[line.order_line_id].get("open", 0),
            "allocated": demand[line.order_line_id].get("allocated", 0),
            "shipped": demand[line.order_line_id].get("shipped", 0),
            "delivered": -onhand.get((line.order_line_id, "order_delivered"), 0),
            "rto": -onhand.get((line.order_line_id, "order_rto"), 0),
            "not_counted": skipped,
        }
    return result
