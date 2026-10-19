// The manual status changes offered for each order status, matching the backend's
// allowed moves: Acknowledge (pending → processing), then shipped, then delivered.
// Delivered and cancelled orders have none.
const ACKNOWLEDGE = { status: "processing", label: "Acknowledge" };
const SHIP = { status: "shipped", label: "Mark shipped" };
const DELIVER = { status: "delivered", label: "Mark delivered" };

export const NEXT_STATUSES = {
  pending: [ACKNOWLEDGE, SHIP],
  processing: [SHIP],
  shipped: [DELIVER],
};

const ATTENTION = {
  cancelled_after_acknowledgement:
    "Shopify cancelled this order after the team acknowledged it. Stop packing it; its stock is available again.",
  edited_after_acknowledgement:
    "Shopify removed items from this order after the team acknowledged it. Check what is being packed.",
};

export function attentionText(reason) {
  return ATTENTION[reason] ?? reason;
}
