// Stock helpers shared by the inventory pages (#22, #23) and the order page (#21).

export const STOCK_LABELS = { ok: "OK", low: "Low", out: "Out" };
export const MAX_CHANGE = 1_000_000;

export const MANUAL_REASONS = [
  { value: "restock", label: "Restock" },
  { value: "damage", label: "Damaged" },
  { value: "correction", label: "Correction (count)" },
  { value: "return", label: "Customer return" },
  { value: "other", label: "Other" },
];

const REASON_LABELS = {
  ...Object.fromEntries(MANUAL_REASONS.map((reason) => [reason.value, reason.label])),
  order_created: "Order placed",
  order_cancelled: "Order cancelled",
  order_returned: "Order returned",
  order_edited: "Order edited",
};

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
// `mode` is "add" or "remove"; `quantity` is the text typed.
export function adjustmentErrors({ mode, quantity, reason }, currentStock) {
  const errors = {};
  const text = String(quantity ?? "").trim();
  if (!/^\d+$/.test(text) || Number(text) === 0) {
    errors.quantity = "Enter a whole number above 0.";
  } else if (Number(text) > MAX_CHANGE) {
    errors.quantity = `Enter at most ${MAX_CHANGE.toLocaleString("en-IN")}.`;
  } else if (mode === "remove" && Number(text) > currentStock) {
    errors.quantity = `Only ${Math.max(currentStock, 0)} in stock, so ${text} can't be removed.`;
  }
  if (!reason) errors.reason = "Choose a reason.";
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

// An order line's effect on stock (#21), for the order page.
export function describeLineStock(stock) {
  if (!stock) return null;
  if (stock.not_counted === "not_linked") return "Stock: not linked to a product";
  if (stock.not_counted === "before_starting_stock") return "Stock: not counted (placed before stock was imported)";
  if (stock.change === 0) return "Stock: no change";
  return stock.change < 0 ? `Stock ${signed(stock.change)}` : `Stock ${signed(stock.change)} restocked`;
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
