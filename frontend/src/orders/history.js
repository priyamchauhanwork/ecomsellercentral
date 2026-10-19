// Turns an ecs_order_history entry into timeline text.
import { capitalize } from "../format";

const SOURCES = {
  webhook: "Shopify webhook",
  scheduled_sync: "Scheduled sync",
  manual_sync: "Shopify import",
  csv_upload: "CSV upload",
  admin_ui: "Admin",
  customer: "Customer",
  system: "System",
};

// Friendly names for fields a sync can change; anything else is shown as-is.
const FIELDS = {
  lines: "items",
  ship_address_line1: "shipping address",
  customer_notes: "notes",
  payment_status: "payment status",
  fulfillment_status: "fulfilment status",
  total_amount: "total",
};

export function describeSource(source) {
  return SOURCES[source] ?? capitalize(source);
}

export function describeChange(entry) {
  const from = capitalize(entry.old_status);
  const to = capitalize(entry.new_status);
  switch (entry.action_type) {
    case "created":
      return to ? `Order created · ${to}` : "Order created";
    case "cancelled":
      return "Order cancelled";
    case "acknowledged":
      return "Acknowledged: being processed";
    case "rto": {
      const units = Object.entries(entry.changed_fields?.rto ?? {}).map(([sku, qty]) => `${qty} × ${sku}`);
      return units.length ? `Marked RTO: ${units.join(", ")}` : "Marked RTO";
    }
    case "attention_cleared":
      return "Attention flag dismissed";
    case "status_change":
      return from ? `Status changed from ${from} to ${to}` : `Status set to ${to}`;
    case "synced_update": {
      const fields = Object.keys(entry.changed_fields ?? {}).map(
        (field) => FIELDS[field] ?? field.replaceAll("_", " "),
      );
      return fields.length ? `Updated: ${[...new Set(fields)].join(", ")}` : "Updated";
    }
    default:
      return capitalize(entry.action_type);
  }
}
