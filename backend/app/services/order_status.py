"""Manual order status changes from the admin UI (#10, #18).

The status is set on the lines and the header is recalculated from them, as the ERD's
design rules require. Only forward moves are allowed, matching the Shopify sync (#9).
"""

from datetime import UTC, datetime
from typing import Literal

from sqlalchemy.orm import Session

from app.models import OrderHeader, OrderHistory, OrderLineHistory
from app.services.order_ingestion import line_action
from app.services.order_mapping import order_status_from_lines

OrderStatus = Literal["pending", "shipped", "delivered", "cancelled"]

# Allowed manual moves. Delivered and cancelled are final; un-shipping is not supported.
ALLOWED_CHANGES: dict[str, set[str]] = {
    "pending": {"shipped", "delivered", "cancelled"},
    "shipped": {"delivered"},
    "delivered": set(),
    "cancelled": set(),
}
# Lines a manual change never touches.
FINAL_LINE_STATUSES = {"cancelled", "returned"}


class StatusChangeNotAllowed(Exception):
    pass


def change_order_status(
    session: Session, header: OrderHeader, new_status: OrderStatus, remarks: str | None
) -> bool:
    """Apply a manual status change to a header locked by the caller.

    Returns False when the order already has that status (a no-op, e.g. a double click).
    Raises StatusChangeNotAllowed for moves outside ALLOWED_CHANGES.
    """
    old_status = header.order_status
    if new_status == old_status:
        return False
    if new_status not in ALLOWED_CHANGES.get(old_status, set()):
        raise StatusChangeNotAllowed(f"An order that is {old_status} can't be marked {new_status}.")

    now = datetime.now(UTC)
    changed_lines = []
    for line in header.lines:
        if line.line_status in FINAL_LINE_STATUSES:
            continue
        line_old_status = line.line_status
        live_qty = line.ordered_qty - line.cancelled_qty
        if new_status == "cancelled":
            line.cancelled_qty = line.ordered_qty - line.shipped_qty
        else:  # shipped or delivered: everything not cancelled has left the warehouse
            line.shipped_qty = live_qty
            line.shipped_at = line.shipped_at or now
            if new_status == "delivered":
                line.delivered_at = line.delivered_at or now
        line.line_status = new_status
        line.version_no += 1
        changed_lines.append((line, line_old_status))

    header.order_status = order_status_from_lines([line.line_status for line in header.lines])
    if header.order_status == "cancelled":
        header.cancelled_at = now
        header.cancel_reason = remarks or header.cancel_reason
    header.version_no += 1

    history_source = {"change_source": "admin_ui", "remarks": remarks}
    session.add(
        OrderHistory(
            order_header_id=header.order_header_id,
            version_no=header.version_no,
            action_type="cancelled" if header.order_status == "cancelled" else "status_change",
            old_status=old_status,
            new_status=header.order_status,
            changed_fields={"order_status": [old_status, header.order_status]},
            **history_source,
        )
    )
    for line, line_old_status in changed_lines:
        session.add(
            OrderLineHistory(
                order_line_id=line.order_line_id,
                order_header_id=header.order_header_id,
                version_no=line.version_no,
                action_type=line_action(line_old_status, line.line_status, {}),
                old_status=line_old_status,
                new_status=line.line_status,
                **history_source,
            )
        )
    return True
