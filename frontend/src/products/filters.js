// Product list filters <-> URL query string <-> API parameters (#43).

export const PRODUCT_STATUSES = [
  { value: "", label: "All statuses" },
  { value: "active", label: "Active" },
  { value: "draft", label: "Draft" },
  { value: "archived", label: "Archived" },
];
export const CHANNEL_STATUSES = [
  { value: "", label: "Any channel status" },
  { value: "live", label: "On sale" },
  { value: "inactive", label: "Listed, not on sale" },
  { value: "unlisted", label: "Not listed" },
];
export const SORTS = [
  { value: "newest", label: "Newest" },
  { value: "name", label: "Name" },
  { value: "stock", label: "Lowest stock" },
];
export const PAGE_SIZE = 50;
export const SEARCH_MAX_LENGTH = 100;

const allowed = (options) => new Set(options.map((option) => option.value));
const STATUS_VALUES = allowed(PRODUCT_STATUSES);
const CHANNEL_VALUES = allowed(CHANNEL_STATUSES);
const SORT_VALUES = allowed(SORTS);

// Unknown values from a hand-edited URL are dropped, not sent to the API.
export function readProductFilters(searchParams) {
  const pick = (key, values, fallback = "") =>
    values.has(searchParams.get(key) ?? "") ? searchParams.get(key) ?? "" : fallback;
  const page = Number.parseInt(searchParams.get("page") ?? "1", 10);
  return {
    status: pick("status", STATUS_VALUES),
    channel: pick("channel", CHANNEL_VALUES),
    sort: pick("sort", SORT_VALUES, "newest"),
    low: searchParams.get("low") === "true",
    q: (searchParams.get("q") ?? "").trim().slice(0, SEARCH_MAX_LENGTH),
    page: Number.isFinite(page) && page > 0 ? page : 1,
  };
}

// URL query: defaults and empty values are left out.
export function productFiltersToParams({ status, channel, sort, low, q, page }) {
  const params = {};
  if (status) params.status = status;
  if (channel) params.channel = channel;
  if (sort && sort !== "newest") params.sort = sort;
  if (low) params.low = "true";
  if (q) params.q = q;
  if (page > 1) params.page = String(page);
  return params;
}

export function productApiParams({ status, channel, sort, low, q, page }) {
  return {
    status,
    channel_status: channel,
    sort,
    low_stock: low ? "true" : "",
    q,
    limit: PAGE_SIZE,
    offset: (page - 1) * PAGE_SIZE,
  };
}

export function hasProductFilters({ status, channel, low, q }) {
  return Boolean(status || channel || low || q);
}

// "₹349.00" or "₹349.00 – ₹1,499.00" (amounts arrive as strings).
export function priceRange(low, high, format) {
  if (low == null) return "—";
  return low === high || high == null ? format(low) : `${format(low)} – ${format(high)}`;
}
