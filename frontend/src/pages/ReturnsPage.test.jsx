import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { describe, expect, it, vi } from "vitest";
import App from "../App";

const SUMMARY = {
  return_id: 4, return_number: "#1001-R1", return_type: "customer_return", status: "open", external_status: "OPEN",
  order_header_id: 10, order_number: "#1001", channel: "shopify", expected_at: "2026-10-29", return_qty: 2,
  pending_qty: 2, created_at: "2026-10-19T06:00:00Z",
};
const RTO = { ...SUMMARY, return_id: 5, return_number: "#1001-RTO1", return_type: "rto", external_status: null };
const LINE = {
  return_line_id: 41, order_line_id: 1, item_id: 7, sku: "WATCH-BLK-40", product_name: "Classic Watch",
  variant_name: "Black / 40mm", return_qty: 2, received_qty: 0, accepted_qty: 0, rejected_qty: 0, missing_qty: 0,
  pending_qty: 2, return_reason: "SIZE_TOO_LARGE", customer_note: "Too big", qc_note: null, status: "pending",
  can_report_qc: true,
};
const DETAIL = {
  ...SUMMARY, external_return_id: "601", received_at: null, qc_completed_at: null, note: null,
  updated_at: "2026-10-19T06:00:00Z", lines: [LINE],
};

const json = (body, status = 200) => new Response(JSON.stringify(body), { status });
function mockApi(routes) {
  return vi.spyOn(globalThis, "fetch").mockImplementation(async (input, init = {}) => {
    const url = new URL(input);
    const handler = routes[`${init.method ?? "GET"} ${url.pathname}`];
    if (handler === undefined) return json({ detail: "Not found" }, 404);
    return typeof handler === "function" ? handler(url, init) : json(handler);
  });
}
const calls = (fetchMock, method, path) =>
  fetchMock.mock.calls.filter(([input, init = {}]) => (init.method ?? "GET") === method && new URL(input).pathname === path);
const renderAt = (path) => render(<MemoryRouter initialEntries={[path]}><App /></MemoryRouter>);

describe("Returns list", () => {
  it("lists returns and RTOs with their order, units and status", async () => {
    mockApi({ "GET /returns": { total: 2, returns: [SUMMARY, RTO] } });
    renderAt("/returns");
    const row = await screen.findByRole("row", { name: /#1001-R1/ });
    expect(within(row).getByRole("link", { name: "#1001" })).toHaveAttribute("href", "/orders/10");
    expect(row).toHaveTextContent("Customer return");
    expect(row).toHaveTextContent("2026-10-29");
    expect(screen.getByRole("row", { name: /#1001-RTO1/ })).toHaveTextContent("RTO");
    expect(screen.getByRole("navigation", { name: "Returns views" })).toBeInTheDocument();
  });

  it("filters through the API and syncs from Shopify", async () => {
    const fetchMock = mockApi({
      "GET /returns": { total: 1, returns: [SUMMARY] },
      "POST /returns/sync": { orders: 3, changed_orders: 1, errors: [] },
    });
    renderAt("/returns?pending=true&type=customer_return");
    await screen.findByRole("row", { name: /#1001-R1/ });
    const params = new URL(calls(fetchMock, "GET", "/returns")[0][0]).searchParams;
    expect(params.get("pending")).toBe("true");
    expect(params.get("type")).toBe("customer_return");
    expect(screen.getByRole("heading", { name: "Awaiting QC" })).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "Sync from Shopify" }));
    expect(await screen.findByText("Read returns of 3 shipped orders; 1 changed.")).toBeInTheDocument();
  });

  it("shows an empty state", async () => {
    mockApi({ "GET /returns": { total: 0, returns: [] } });
    renderAt("/returns");
    expect(await screen.findByText("No returns yet")).toBeInTheDocument();
  });
});

describe("Return page", () => {
  it("shows the lines and records QC, reported to Shopify", async () => {
    const done = { ...DETAIL, status: "qc_done", lines: [{ ...LINE, received_qty: 2, accepted_qty: 1, rejected_qty: 1, pending_qty: 0, status: "qc_done" }] };
    const fetchMock = mockApi({ "GET /returns/4": DETAIL, "POST /returns/4/qc": done });
    renderAt("/returns/4");
    expect(await screen.findByRole("heading", { name: /Customer return #1001-R1/ })).toBeInTheDocument();
    expect(screen.getByText("“Too big”")).toBeInTheDocument();
    await userEvent.type(screen.getByLabelText("Accepted for WATCH-BLK-40"), "2");
    await userEvent.type(screen.getByLabelText("Rejected for WATCH-BLK-40"), "1");
    await userEvent.click(screen.getByRole("button", { name: "Save QC" }));
    expect(screen.getByRole("alert")).toHaveTextContent("Only 2 still to check.");
    expect(calls(fetchMock, "POST", "/returns/4/qc")).toHaveLength(0);

    await userEvent.clear(screen.getByLabelText("Accepted for WATCH-BLK-40"));
    await userEvent.type(screen.getByLabelText("Accepted for WATCH-BLK-40"), "1");
    await userEvent.click(screen.getByRole("button", { name: "Save QC" }));
    expect(await screen.findByText("QC saved: 1 back in stock, 1 to damaged. Reported to Shopify.")).toBeInTheDocument();
    expect(JSON.parse(calls(fetchMock, "POST", "/returns/4/qc")[0][1].body)).toEqual({
      lines: [{ return_line_id: 41, accepted: 1, rejected: 1 }],
      note: null,
    });
    expect(screen.queryByRole("button", { name: "Save QC" })).not.toBeInTheDocument();
  });

  it("shows Shopify's refusal and keeps the form", async () => {
    mockApi({
      "GET /returns/4": DETAIL,
      "POST /returns/4/qc": () => json({ detail: "Shopify didn't accept the QC result, so nothing was saved: Access denied" }, 502),
    });
    renderAt("/returns/4");
    await userEvent.type(await screen.findByLabelText("Accepted for WATCH-BLK-40"), "1");
    await userEvent.click(screen.getByRole("button", { name: "Save QC" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("nothing was saved: Access denied");
    expect(screen.getByRole("button", { name: "Save QC" })).toBeEnabled();
  });

  it("marks everything received and corrects the ETA", async () => {
    const fetchMock = mockApi({
      "GET /returns/4": DETAIL,
      "POST /returns/4/receive": { ...DETAIL, status: "received", lines: [{ ...LINE, received_qty: 2, status: "received" }] },
      "PATCH /returns/4": { ...DETAIL, expected_at: "2026-10-25" },
    });
    renderAt("/returns/4");
    await userEvent.click(await screen.findByRole("button", { name: "Mark all received" }));
    expect(await screen.findByText("Marked as received. Check the units next.")).toBeInTheDocument();
    expect(JSON.parse(calls(fetchMock, "POST", "/returns/4/receive")[0][1].body)).toEqual({
      lines: [{ return_line_id: 41, received_qty: 2 }],
    });
    await userEvent.type(screen.getByLabelText("Change to"), "2026-10-25");
    await userEvent.click(screen.getByRole("button", { name: "Save date" }));
    await waitFor(() => expect(screen.getByText("2026-10-25")).toBeInTheDocument());
  });

  it("cancels or closes an RTO after confirming", async () => {
    const rtoDetail = { ...DETAIL, ...RTO, lines: [LINE] };
    const fetchMock = mockApi({
      "GET /returns/5": rtoDetail,
      "POST /returns/5/close": { ...rtoDetail, status: "closed", lines: [{ ...LINE, pending_qty: 0 }] },
    });
    renderAt("/returns/5");
    expect(await screen.findByRole("button", { name: "Cancel RTO" })).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "Close (not received)" }));
    expect(screen.getByRole("group", { name: "Confirm" })).toHaveTextContent("treated as lost in transit");
    await userEvent.click(screen.getByRole("button", { name: "Yes, close it" }));
    expect(await screen.findByText("RTO closed.")).toBeInTheDocument();
    expect(calls(fetchMock, "POST", "/returns/5/close")).toHaveLength(1);
    expect(screen.queryByRole("button", { name: "Cancel RTO" })).not.toBeInTheDocument();
  });

  it("says when the return doesn't exist", async () => {
    mockApi({});
    renderAt("/returns/99");
    expect(await screen.findByText("Return not found")).toBeInTheDocument();
  });
});
