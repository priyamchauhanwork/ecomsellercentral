// Stock helpers shared by the inventory pages (#22, #23) and the order page (#21).

export const STOCK_LABELS = { ok: "OK", low: "Low", out: "Out" };
export const MAX_CHANGE = 1_000_000;

export const MANUAL_REASONS = [
  { value: "restock", label: "Restock" },
  { value: "damage", label: "Damaged (moves to damaged)", removeOnly: true },
  { value: "correction", label: "Correction (count)" },
  { value: "return", label: "Customer return (no return record)" },
  { value: "other", label: "Other" },
];

const REASON_LABELS = {
  ...Object.fromEntries(MANUAL_REASONS.map((reason) => [reason.value, reason.label])),
  order_created: "Order placed",
  order_cancelled: "Order cancelled",
  order_returned: "Order returned (refund)",
  order_edited: "Order edited",
  order_allocated: "Order acknowledged",
  order_shipped: "Shipped",
  order_delivered: "Delivered",
  order_rto: "RTO",
  return_opened: "Return approved",
  return_accepted: "Return accepted (QC)",
  return_rejected: "Return rejected (QC)",
  return_cancelled: "Return cancelled",
  return_closed: "Return closed",
  damaged_repaired: "Repaired",
  damaged_written_off: "Written off",
  opening_stock: "Opening stock",
  migration: "Starting figure",
};

const TYPE_LABELS = {
  ONHAND: "On hand",
  RETURN_PENDING: "Returning",
  DAMAGED: "Damaged",
  OPEN: "Open orders",
  ALLOCATED: "Allocated",
  SHIPPED: "Shipped",
};

export function typeLabel(type) {
  return TYPE_LABELS[type] ?? "Stock";
}

export function reasonLabel(reason) {
  return REASON_LABELS[reason] ?? reason;
}

// "+3", "−2" (a real minus sign), "0".
export function signed(quantity) {
  if (quantity > 0) return `+${quantity}`;
  if (quantity < 0) return `−${-quantity}`;
  return "0";
}

export function variantLabel(values) {
  return Object.values(values ?? {}).join(" / ");
}

// The adjust form's checks, before anything is sent. Returns { field: message }.
// `mode` is "add" or "remove"; `quantity` is the text typed; removals are limited to what
// is on the shelf (on hand − shipped).
export function adjustmentErrors({ mode, quantity, reason }, onShelf) {
  const errors = {};
  const text = String(quantity ?? "").trim();
  if (!/^\d+$/.test(text) || Number(text) === 0) {
    errors.quantity = "Enter a whole number above 0.";
  } else if (Number(text) > MAX_CHANGE) {
    errors.quantity = `Enter at most ${MAX_CHANGE.toLocaleString("en-IN")}.`;
  } else if (mode === "remove" && Number(text) > onShelf) {
    errors.quantity = `Only ${Math.max(onShelf, 0)} on the shelf, so ${text} can't be removed.`;
  }
  if (!reason) errors.reason = "Choose a reason.";
  else if (reason === "damage" && mode === "add") errors.reason = "Damaged units are removed; use Repair to bring them back.";
  return errors;
}

// The signed change the form describes, or null while the quantity isn't valid.
export function quantityChange(mode, quantity) {
  const text = String(quantity ?? "").trim();
  if (!/^\d+$/.test(text) || Number(text) === 0) return null;
  return mode === "remove" ? -Number(text) : Number(text);
}

// What happened on Shopify after an adjustment, for the result message.
export function describeSync(result) {
  if (result.synced === false) {
    return { tone: "error", text: `Saved locally, Shopify sync pending: ${result.sync_error}` };
  }
  if (result.synced) return { tone: "success", text: "Shopify updated." };
  return { tone: "success", text: "Not sold on Shopify, so nothing to update there." };
}

// An order line's place in stock (docs/inventory-erd.md), for the order page: its demand
// (open, allocated, shipped) and the units that left on-hand stock (delivered, RTO).
export function describeLineStock(stock) {
  if (!stock) return null;
  if (stock.not_counted === "not_linked") return "Stock: not linked to a product";
  if (stock.not_counted === "before_starting_stock") return "Stock: not counted (placed before stock was imported)";
  const parts = [
    stock.open && `${stock.open} open`,
    stock.allocated && `${stock.allocated} allocated`,
    stock.shipped && `${stock.shipped} shipped`,
    stock.delivered && `${stock.delivered} delivered`,
    stock.rto && `${stock.rto} RTO`,
  ].filter(Boolean);
  return parts.length ? `Stock: ${parts.join(" · ")}` : "Stock: nothing held";
}

export function readInventoryFilters(searchParams) {
  const page = Number.parseInt(searchParams.get("page") ?? "1", 10);
  return {
    q: (searchParams.get("q") ?? "").trim(),
    low: searchParams.get("low") === "true",
    page: Number.isInteger(page) && page > 0 ? page : 1,
  };
}

export function inventoryFiltersToParams({ q, low, page }) {
  const params = {};
  if (q) params.q = q;
  if (low) params.low = "true";
  if (page > 1) params.page = String(page);
  return params;
}
