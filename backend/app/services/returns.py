"""Returns and RTOs (docs/inventory-erd.md, #53–#55).

Shopify returns are read from the GraphQL Admin API: always the current state, so
webhook order doesn't matter, and merged with the QC done in the app (totals only grow).
RTOs (shipped, undelivered, coming back) are entered in the app. Every change ends with
stock.reconcile_order(), which moves supply and demand to match.
"""

import logging
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import (
    Item,
    OrderHeader,
    OrderHistory,
    OrderLine,
    Return,
    ReturnLine,
    Seller,
)
from app.services import stock
from app.services.order_mapping import clip, timestamp
from app.services.shopify import ShopifyClient
from app.services.shopify_catalog import gid, mutate, numeric
from app.services.stock_sync import location

logger = logging.getLogger(__name__)

IST = ZoneInfo("Asia/Kolkata")
BATCH = 10  # orders per Shopify query (nested returns keep the query cost moderate)

RETURNS_QUERY = """
query ($ids: [ID!]!) {
  nodes(ids: $ids) {
    ... on Order {
      id
      returns(first: 20) {
        nodes {
          id name status createdAt closedAt
          returnLineItems(first: 50) {
            nodes {
              id quantity customerNote
              ... on ReturnLineItem { returnReason fulfillmentLineItem { lineItem { id } } }
            }
          }
          reverseFulfillmentOrders(first: 10) {
            nodes {
              id
              lineItems(first: 50) {
                nodes {
                  id totalQuantity dispositions { type quantity }
                  fulfillmentLineItem { lineItem { id } }
                }
              }
            }
          }
        }
      }
    }
  }
}"""
ORDER_OF = """
query ($id: ID!) {
  node(id: $id) {
    ... on Return { order { id } }
    ... on ReverseFulfillmentOrder { order { id } }
  }
}"""
DISPOSE = """
mutation ($inputs: [ReverseFulfillmentOrderDisposeInput!]!) {
  reverseFulfillmentOrderDispose(dispositionInputs: $inputs) {
    reverseFulfillmentOrderLineItems { id }
    userErrors { field message }
  }
}"""

SHOPIFY_STATUSES = {
    "REQUESTED": "requested",
    "OPEN": "open",
    "DECLINED": "declined",
    "CANCELED": "cancelled",
    "CANCELLED": "cancelled",
    "CLOSED": "closed",
}
# Statuses the app sets while Shopify's return is still OPEN.
APP_PROGRESS = ("received", "qc_done")


class ReturnInvalid(ValueError):
    """The change doesn't fit the return (too many units, wrong state, ...)."""


class ReturnConflict(Exception):
    """The return is in a state that doesn't allow this action."""


def today() -> date:
    return datetime.now(IST).date()


def eta_for(session: Session, seller_id: int) -> date:
    days = session.scalar(select(Seller.return_eta_days).where(Seller.seller_id == seller_id))
    return today() + timedelta(days=days or 10)


# --- Shopify returns ----------------------------------------------------------------------


@dataclass
class ShopifyLine:
    external_return_line_id: str
    line_item_id: str
    quantity: int
    reason: str | None
    customer_note: str | None
    accepted: int = 0
    rejected: int = 0
    missing: int = 0
    disposal_ref: str | None = None


def parse_return(node: dict[str, Any]) -> tuple[dict[str, Any], dict[str, ShopifyLine]]:
    """A Shopify Return node → return fields and its lines by order line item id."""
    lines: dict[str, ShopifyLine] = {}
    for item in node["returnLineItems"]["nodes"]:
        line_item = ((item.get("fulfillmentLineItem") or {}).get("lineItem") or {}).get("id")
        if not line_item:
            continue  # an unverified line (not tied to an order line): nothing to track
        line_item = numeric(line_item)
        if line_item in lines:
            lines[line_item].quantity += item["quantity"]
            continue
        lines[line_item] = ShopifyLine(
            external_return_line_id=numeric(item["id"]),
            line_item_id=line_item,
            quantity=item["quantity"],
            reason=clip(item.get("returnReason"), 50),
            customer_note=item.get("customerNote"),
        )
    for rfo in node["reverseFulfillmentOrders"]["nodes"]:
        for rfo_line in rfo["lineItems"]["nodes"]:
            line_item = numeric(rfo_line["fulfillmentLineItem"]["lineItem"]["id"])
            line = lines.get(line_item)
            if line is None:
                continue
            line.disposal_ref = line.disposal_ref or rfo_line["id"]
            for disposition in rfo_line.get("dispositions") or []:
                kind, qty = disposition["type"], disposition["quantity"]
                if kind == "RESTOCKED":
                    line.accepted += qty
                elif kind == "MISSING":
                    line.missing += qty
                elif kind != "PROCESSING_REQUIRED":  # that one stays pending (QC to do)
                    line.rejected += qty
    fields = {
        "external_return_id": numeric(node["id"]),
        "return_number": clip(node.get("name"), 50) or numeric(node["id"]),
        "external_status": node["status"],
        "status": SHOPIFY_STATUSES.get(node["status"], "open"),
        "external_created_at": timestamp(node.get("createdAt"), [], "createdAt"),
    }
    return fields, lines


def fetch_returns(shopify: ShopifyClient, external_order_ids: list[str]) -> dict[str, list]:
    """Shopify's returns of these orders, by order id."""
    found: dict[str, list] = {}
    for start in range(0, len(external_order_ids), BATCH):
        ids = [gid("Order", order_id) for order_id in external_order_ids[start : start + BATCH]]
        for node in shopify.graphql(RETURNS_QUERY, {"ids": ids})["nodes"]:
            if node:
                found[numeric(node["id"])] = node["returns"]["nodes"]
    return found


def order_id_from_payload(shopify: ShopifyClient, payload: dict[str, Any]) -> str | None:
    """The Shopify order id a return webhook is about."""
    order = payload.get("order")
    if isinstance(order, dict) and (order.get("id") or order.get("admin_graphql_api_id")):
        return numeric(str(order.get("admin_graphql_api_id") or order["id"]))
    if payload.get("order_id"):
        return numeric(str(payload["order_id"]))
    node_id = payload.get("admin_graphql_api_id")
    if node_id:
        node = shopify.graphql(ORDER_OF, {"id": node_id})["node"]
        if node and node.get("order"):
            return numeric(node["order"]["id"])
    return None


def apply_shopify_returns(
    session: Session, header: OrderHeader, nodes: list[dict[str, Any]], import_id: int | None
) -> bool:
    """Upsert the order's Shopify returns and reconcile its stock. Returns whether
    anything changed."""
    lines_by_external = {line.external_line_id: line for line in header.lines}
    changed = False
    for node in nodes:
        fields, shopify_lines = parse_return(node)
        ret = session.scalars(
            select(Return)
            .where(
                Return.channel_account_id == header.channel_account_id,
                Return.external_return_id == fields["external_return_id"],
            )
            .with_for_update()
        ).one_or_none()
        if ret is None:
            ret = Return(
                seller_id=header.seller_id,
                order_header_id=header.order_header_id,
                channel=header.channel,
                channel_account_id=header.channel_account_id,
                return_type="customer_return",
                created_by_import_id=import_id,
                version_no=1,
                **fields,
            )
            session.add(ret)
            session.flush()
            changed = True
        changed |= merge_return(session, ret, fields, shopify_lines, lines_by_external, header)
        if changed:
            ret.last_import_id = import_id or ret.last_import_id
            ret.external_updated_at = datetime.now(UTC)
    if changed:
        session.flush()
        session.expire(header, ["lines"])
    return changed


def merge_return(
    session: Session,
    ret: Return,
    fields: dict[str, Any],
    shopify_lines: dict[str, ShopifyLine],
    lines_by_external: dict[str | None, OrderLine],
    header: OrderHeader,
) -> bool:
    changed = False
    existing = {line.order_line_id: line for line in ret.lines}
    for line_item_id, shopify_line in shopify_lines.items():
        order_line = lines_by_external.get(line_item_id)
        if order_line is None:
            logger.warning(
                "Return %s: line item %s isn't in the order", ret.return_number, line_item_id
            )
            continue
        rline = existing.get(order_line.order_line_id)
        if rline is None:
            rline = ReturnLine(
                return_id=ret.return_id,
                order_line_id=order_line.order_line_id,
                item_id=order_line.variant_id,
                external_return_line_id=shopify_line.external_return_line_id,
                return_qty=shopify_line.quantity,
                received_qty=0,
                accepted_qty=0,
                rejected_qty=0,
                missing_qty=0,
                opening_accepted_qty=0,
                opening_rejected_qty=0,
                return_reason=shopify_line.reason,
                customer_note=shopify_line.customer_note,
            )
            ret.lines.append(rline)
            changed = True
            before_baseline = before_starting_stock(session, rline, fields)
        else:
            before_baseline = False
        changed |= set_if_changed(
            rline,
            "return_qty",
            max(shopify_line.quantity, rline.accepted_qty + rline.rejected_qty + rline.missing_qty),
        )
        changed |= set_if_changed(
            rline, "external_disposal_ref", shopify_line.disposal_ref or rline.external_disposal_ref
        )
        # Dispositions are final on Shopify and QC in the app reports them there: the
        # larger of the two is the truth.
        accepted = max(rline.accepted_qty, shopify_line.accepted)
        rejected = max(rline.rejected_qty, shopify_line.rejected)
        missing = min(
            max(rline.missing_qty, shopify_line.missing), rline.return_qty - accepted - rejected
        )
        changed |= set_if_changed(
            rline, "received_qty", max(rline.received_qty, accepted + rejected)
        )
        changed |= set_if_changed(rline, "accepted_qty", accepted)
        changed |= set_if_changed(rline, "rejected_qty", rejected)
        changed |= set_if_changed(rline, "missing_qty", max(missing, 0))
        if before_baseline:
            # Outcomes from before the starting stock are already in it (R14).
            rline.opening_accepted_qty = rline.accepted_qty
            rline.opening_rejected_qty = rline.rejected_qty

    status = fields["status"]
    if status == "open" and ret.status in APP_PROGRESS:
        status = ret.status
    if (
        status in ("open", "received", "closed")
        and ret.lines
        and all(outcome_complete(line) for line in ret.lines)
    ):
        status = "qc_done"
    changed |= set_if_changed(ret, "external_status", fields["external_status"])
    changed |= set_if_changed(ret, "return_number", fields["return_number"])
    changed |= set_if_changed(ret, "external_created_at", fields["external_created_at"])
    changed |= update_status(session, ret, status, header.seller_id)
    return changed


def before_starting_stock(session: Session, rline: ReturnLine, fields: dict[str, Any]) -> bool:
    if rline.item_id is None or fields["external_created_at"] is None:
        return False
    baseline = session.scalar(select(Item.stock_baseline_at).where(Item.item_id == rline.item_id))
    return baseline is not None and fields["external_created_at"] <= baseline


def fully_received(line: ReturnLine) -> bool:
    return line.received_qty >= line.return_qty - line.missing_qty


def outcome_complete(line: ReturnLine) -> bool:
    return line.accepted_qty + line.rejected_qty + line.missing_qty >= line.return_qty


def set_if_changed(row: Any, attribute: str, value: Any) -> bool:
    if getattr(row, attribute) == value:
        return False
    setattr(row, attribute, value)
    return True


def update_status(session: Session, ret: Return, status: str, seller_id: int) -> bool:
    """Set the return's status, its lines' statuses, and the ETA when it first opens."""
    changed = set_if_changed(ret, "status", status)
    if status in ("open", "received") and ret.expected_at is None:
        ret.expected_at = eta_for(session, seller_id)
        changed = True
    if status == "qc_done" and ret.qc_completed_at is None:
        ret.qc_completed_at = datetime.now(UTC)
    for line in ret.lines:
        if status in ("declined", "cancelled"):
            line_status = "cancelled"
        elif outcome_complete(line):
            line_status = "qc_done"
        elif line.received_qty >= line.return_qty:
            line_status = "received"
        else:
            line_status = "pending"
        changed |= set_if_changed(line, "status", line_status)
    if changed:
        ret.version_no += 1
    return changed


def sync_order_returns(
    session: Session, shopify: ShopifyClient, header: OrderHeader, import_id: int | None = None
) -> set[int]:
    """Read one order's returns from Shopify and apply them. Returns the items whose
    `available` changed."""
    nodes = fetch_returns(shopify, [header.external_order_id]).get(header.external_order_id, [])
    apply_shopify_returns(session, header, nodes, import_id)
    return stock.reconcile_order(session, header).finish()


def sync_returns(session_factory, shopify: ShopifyClient, header_ids: list[int]) -> dict[str, Any]:
    """Read the returns of these orders (in batches) and apply them, one transaction per
    order. Returns counts and the items whose `available` changed."""
    result = {"orders": 0, "changed_orders": 0, "errors": [], "items": set()}
    with session_factory() as session:
        external = dict(
            session.execute(
                select(OrderHeader.order_header_id, OrderHeader.external_order_id).where(
                    OrderHeader.order_header_id.in_(header_ids),
                    OrderHeader.external_order_id.is_not(None),
                )
            ).all()
        )
    nodes = fetch_returns(shopify, list(external.values()))
    for header_id, external_id in external.items():
        result["orders"] += 1
        try:
            with session_factory.begin() as session:
                header = session.get(OrderHeader, header_id, with_for_update=True)
                if apply_shopify_returns(session, header, nodes.get(external_id, []), None):
                    result["changed_orders"] += 1
                result["items"] |= stock.reconcile_order(session, header).finish()
        except Exception as exc:  # one bad order mustn't stop the others
            logger.exception("Returns of order %s failed", header_id)
            result["errors"].append(f"Order {header_id}: {type(exc).__name__}: {exc}")
    return result


def fulfilled_order_ids(
    session: Session, seller_id: int, header_ids: list[int] | None = None
) -> list[int]:
    """Orders that can have returns: some units shipped."""
    query = (
        select(OrderHeader.order_header_id)
        .join(OrderLine, OrderLine.order_header_id == OrderHeader.order_header_id)
        .where(
            OrderHeader.seller_id == seller_id,
            OrderHeader.channel == "shopify",
            OrderHeader.is_deleted.is_(False),
            OrderLine.shipped_qty > 0,
        )
        .distinct()
    )
    if header_ids is not None:
        query = query.where(OrderHeader.order_header_id.in_(header_ids))
    return list(session.scalars(query))


# --- In the app: receive, QC, ETA (#54) ---------------------------------------------------


def load_return(session: Session, seller_id: int, return_id: int) -> Return:
    ret = session.get(Return, return_id, with_for_update=True)
    if ret is None or ret.seller_id != seller_id:
        raise LookupError(return_id)
    return ret


def lines_by_id(ret: Return, wanted: list[int]) -> dict[int, ReturnLine]:
    lines = {line.return_line_id: line for line in ret.lines}
    unknown = [line_id for line_id in wanted if line_id not in lines]
    if unknown:
        raise ReturnInvalid(f"Lines {unknown} aren't on this return.")
    return lines


def receive(session: Session, ret: Return, received: dict[int, int]) -> None:
    """Record how many units of each line arrived (no stock moves until QC)."""
    if ret.status not in ("open", "received"):
        raise ReturnConflict(f"A {ret.status} return can't be received.")
    lines = lines_by_id(ret, list(received))
    for line_id, qty in received.items():
        line = lines[line_id]
        if qty > line.return_qty:
            raise ReturnInvalid(f"Line {line_id}: only {line.return_qty} on the return.")
        if qty < line.accepted_qty + line.rejected_qty:
            raise ReturnInvalid(
                f"Line {line_id}: {line.accepted_qty + line.rejected_qty} already QC'd."
            )
        line.received_qty = qty
    all_received = all(
        line.received_qty >= line.return_qty - line.missing_qty for line in ret.lines
    )
    if all_received and ret.received_at is None:
        ret.received_at = datetime.now(UTC)
    update_status(session, ret, "received" if all_received else "open", ret.seller_id)


def record_qc(
    session: Session,
    shopify_factory,
    ret: Return,
    outcomes: dict[int, tuple[int, int]],
    note: str | None,
) -> None:
    """Add QC outcomes (accepted, rejected) per line. For a Shopify return they are
    reported to Shopify first; if Shopify refuses, nothing is saved."""
    if ret.status not in ("open", "received"):
        raise ReturnConflict(f"A {ret.status} return can't be QC'd.")
    lines = lines_by_id(ret, list(outcomes))
    for line_id, (accepted, rejected) in outcomes.items():
        line = lines[line_id]
        outstanding = line.return_qty - line.accepted_qty - line.rejected_qty - line.missing_qty
        if accepted + rejected > outstanding:
            raise ReturnInvalid(f"Line {line_id}: only {outstanding} units still to QC.")
    if ret.return_type == "customer_return" and ret.external_return_id:
        report_to_shopify(shopify_factory, ret, lines, outcomes)
    for line_id, (accepted, rejected) in outcomes.items():
        line = lines[line_id]
        line.accepted_qty += accepted
        line.rejected_qty += rejected
        line.received_qty = max(line.received_qty, line.accepted_qty + line.rejected_qty)
        if note:
            line.qc_note = note
    status = "qc_done" if all(outcome_complete(line) for line in ret.lines) else ret.status
    if status == "open" and all(fully_received(line) for line in ret.lines):
        status = "received"
    update_status(session, ret, status, ret.seller_id)


def report_to_shopify(shopify_factory, ret: Return, lines: dict[int, ReturnLine], outcomes) -> None:
    inputs = []
    with shopify_factory() as shopify:
        for line_id, (accepted, rejected) in outcomes.items():
            ref = lines[line_id].external_disposal_ref
            if (accepted or rejected) and not ref:
                raise ReturnConflict(
                    "Shopify has no reverse fulfilment for this return yet (approve it in "
                    "Shopify first), so the QC result can't be reported."
                )
            for qty, kind in ((accepted, "RESTOCKED"), (rejected, "NOT_RESTOCKED")):
                if qty:
                    inputs.append(
                        {
                            "reverseFulfillmentOrderLineItemId": ref,
                            "quantity": qty,
                            "dispositionType": kind,
                            "locationId": location(shopify),
                        }
                    )
        if inputs:
            mutate(shopify, DISPOSE, {"inputs": inputs}, "reverseFulfillmentOrderDispose")


def set_eta(session: Session, ret: Return, expected_at: date) -> None:
    ret.expected_at = expected_at
    ret.version_no += 1


# --- RTO (#55) ----------------------------------------------------------------------------


def rto_open_qty(session: Session, line: OrderLine) -> int:
    """Units of the line already on an RTO that isn't cancelled."""
    return session.scalar(
        select(func.coalesce(func.sum(ReturnLine.return_qty), 0))
        .join(Return, Return.return_id == ReturnLine.return_id)
        .where(
            ReturnLine.order_line_id == line.order_line_id,
            Return.return_type == "rto",
            Return.status != "cancelled",
        )
    )


def create_rto(
    session: Session, header: OrderHeader, quantities: dict[int, int], note: str | None
) -> Return:
    """Shipped, undelivered units coming back to the seller."""
    lines = {line.order_line_id: line for line in header.lines}
    for line_id, qty in quantities.items():
        line = lines.get(line_id)
        if line is None:
            raise ReturnInvalid(f"Line {line_id} isn't on this order.")
        can = line.shipped_qty - line.delivered_qty - rto_open_qty(session, line)
        if qty > can:
            raise ReturnInvalid(
                f"{line.sku or line.product_name}: only {max(can, 0)} shipped and undelivered "
                "units can be marked RTO."
            )
    count = session.scalar(
        select(func.count()).where(
            Return.order_header_id == header.order_header_id, Return.return_type == "rto"
        )
    )
    ret = Return(
        seller_id=header.seller_id,
        order_header_id=header.order_header_id,
        channel=header.channel,
        channel_account_id=header.channel_account_id,
        return_number=clip(f"{header.order_number}-RTO{count + 1}", 50),
        return_type="rto",
        status="open",
        expected_at=eta_for(session, header.seller_id),
        note=note,
        version_no=1,
    )
    ret.lines = [
        ReturnLine(
            order_line_id=line_id,
            item_id=lines[line_id].variant_id,
            return_qty=qty,
            received_qty=0,
            accepted_qty=0,
            rejected_qty=0,
            missing_qty=0,
            opening_accepted_qty=0,
            opening_rejected_qty=0,
            status="pending",
        )
        for line_id, qty in quantities.items()
    ]
    session.add(ret)
    header.version_no += 1
    session.add(
        OrderHistory(
            order_header_id=header.order_header_id,
            version_no=header.version_no,
            action_type="rto",
            change_source="admin_ui",
            remarks=note,
            changed_fields={
                "rto": {
                    lines[line_id].sku or lines[line_id].product_name: qty
                    for line_id, qty in quantities.items()
                }
            },
        )
    )
    session.flush()
    return ret


def cancel_rto(session: Session, ret: Return) -> None:
    """An RTO entered by mistake: its units go back to shipped."""
    if ret.return_type != "rto":
        raise ReturnConflict(
            "Only RTOs can be cancelled in the app; customer returns are managed in Shopify."
        )
    if any(line.received_qty or line.accepted_qty or line.rejected_qty for line in ret.lines):
        raise ReturnConflict("Units of this RTO were already received or QC'd.")
    if ret.status != "open":
        raise ReturnConflict(f"A {ret.status} RTO can't be cancelled.")
    update_status(session, ret, "cancelled", ret.seller_id)


def close_rto(session: Session, ret: Return) -> None:
    """The remaining units of an RTO never came back (lost in transit)."""
    if ret.return_type != "rto":
        raise ReturnConflict(
            "Only RTOs can be closed in the app; customer returns are closed in Shopify."
        )
    if ret.status not in ("open", "received"):
        raise ReturnConflict(f"A {ret.status} RTO can't be closed.")
    update_status(session, ret, "closed", ret.seller_id)
