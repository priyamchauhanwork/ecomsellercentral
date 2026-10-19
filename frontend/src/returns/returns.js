// Return helpers (#54, #55): filters, labels and the receive / QC form checks.

export const RETURN_STATUSES = [
  { value: "", label: "All" },
  { value: "requested", label: "Requested" },
  { value: "open", label: "Open" },
  { value: "received", label: "Received" },
  { value: "qc_done", label: "QC done" },
  { value: "closed", label: "Closed" },
  { value: "declined", label: "Declined" },
  { value: "cancelled", label: "Cancelled" },
];
const STATUS_VALUES = new Set(RETURN_STATUSES.map((status) => status.value));
const TYPES = new Set(["customer_return", "rto"]);

export function returnTypeLabel(type) {
  return type === "rto" ? "RTO" : "Customer return";
}

export function readReturnFilters(searchParams) {
  const page = Number.parseInt(searchParams.get("page") ?? "1", 10);
  const status = searchParams.get("status") ?? "";
  const type = searchParams.get("type") ?? "";
  return {
    status: STATUS_VALUES.has(status) ? status : "",
    type: TYPES.has(type) ? type : "",
    pending: searchParams.get("pending") === "true",
    q: (searchParams.get("q") ?? "").trim().slice(0, 100),
    page: Number.isInteger(page) && page > 0 ? page : 1,
  };
}

export function returnFiltersToParams({ status, type, pending, q, page }) {
  const params = {};
  if (status) params.status = status;
  if (type) params.type = type;
  if (pending) params.pending = "true";
  if (q) params.q = q;
  if (page > 1) params.page = String(page);
  return params;
}

// Units of a line still without a QC outcome.
export function outstanding(line) {
  return line.return_qty - line.accepted_qty - line.rejected_qty - line.missing_qty;
}

const WHOLE = /^\d+$/;

// QC form checks: whole numbers, at most what is outstanding per line, at least one unit.
// `values` maps return_line_id → { accepted, rejected } as typed. Returns { errors, lines }.
export function qcLines(returnLines, values) {
  const errors = {};
  const lines = [];
  for (const line of returnLines) {
    const typed = values[line.return_line_id] ?? {};
    const accepted = (typed.accepted ?? "").trim() || "0";
    const rejected = (typed.rejected ?? "").trim() || "0";
    if (!WHOLE.test(accepted) || !WHOLE.test(rejected)) {
      errors[line.return_line_id] = "Enter whole numbers.";
      continue;
    }
    const total = Number(accepted) + Number(rejected);
    if (total > outstanding(line)) {
      errors[line.return_line_id] = `Only ${outstanding(line)} still to check.`;
      continue;
    }
    if (total > 0) {
      lines.push({ return_line_id: line.return_line_id, accepted: Number(accepted), rejected: Number(rejected) });
    }
  }
  if (!Object.keys(errors).length && lines.length === 0) errors.form = "Enter at least one accepted or rejected unit.";
  return { errors, lines };
}
