// Order list filters <-> URL query string, and the quick date ranges.
// Dates are calendar days in India (IST), matching the backend's day boundaries.

export const ORDER_STATUSES = [
  { value: "", label: "All" },
  { value: "pending", label: "Pending" },
  { value: "processing", label: "Processing" },
  { value: "shipped", label: "Shipped" },
  { value: "delivered", label: "Delivered" },
  { value: "cancelled", label: "Cancelled" },
];

const STATUS_VALUES = new Set(ORDER_STATUSES.map((status) => status.value));
const DATE_PATTERN = /^\d{4}-\d{2}-\d{2}$/;
export const SEARCH_MAX_LENGTH = 100; // the backend's limit

function validDate(value) {
  if (!DATE_PATTERN.test(value ?? "")) return "";
  const [year, month, day] = value.split("-").map(Number);
  const date = new Date(Date.UTC(year, month - 1, day));
  return date.getUTCMonth() === month - 1 && date.getUTCDate() === day ? value : "";
}

// Filters from the URL. Unknown or malformed values (e.g. a hand-edited link) are
// dropped instead of being sent to the API.
export function readFilters(searchParams) {
  const status = searchParams.get("status") ?? "";
  return {
    status: STATUS_VALUES.has(status) ? status : "",
    from: validDate(searchParams.get("from")),
    to: validDate(searchParams.get("to")),
    q: (searchParams.get("q") ?? "").trim().slice(0, SEARCH_MAX_LENGTH),
    attention: searchParams.get("attention") === "true" ? "true" : "",
  };
}

// URL query for the given filters; empty values are left out.
export function filtersToParams(filters) {
  return Object.fromEntries(Object.entries(filters).filter(([, value]) => value));
}

export function hasFilters(filters) {
  return Object.values(filters).some(Boolean);
}

export function dateRangeError({ from, to }) {
  return from && to && from > to ? "The start date is after the end date." : "";
}

// Today's date in India as YYYY-MM-DD ("en-CA" formats dates that way).
export function todayInIndia(now = new Date()) {
  return new Intl.DateTimeFormat("en-CA", { timeZone: "Asia/Kolkata" }).format(now);
}

function shiftDays(isoDate, days) {
  const [year, month, day] = isoDate.split("-").map(Number);
  return new Date(Date.UTC(year, month - 1, day + days)).toISOString().slice(0, 10);
}

export function quickRanges(now = new Date()) {
  const today = todayInIndia(now);
  return [
    { label: "Today", from: today, to: today },
    { label: "Last 7 days", from: shiftDays(today, -6), to: today },
    { label: "This month", from: `${today.slice(0, 8)}01`, to: today },
  ];
}
