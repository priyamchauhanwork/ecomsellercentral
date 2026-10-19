import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { describe, expect, it, vi } from "vitest";
import App from "../App";

const ROW = {
  item_id: 1, article_id: 10, article_name: "Classic Watch", sku: "WATCH-BLK-40", barcode: "8901",
  variant_values: { Colour: "Black", Size: "40mm" }, image_url: null, stock_qty: 12, stock_status: "ok",
  status: "active", shopify: { listing_item_id: 5, status: "live", last_sync_error: null },
};
const LOW = { ...ROW, item_id: 2, sku: "WATCH-BLK-42", barcode: null, stock_qty: 3, stock_status: "low" };
const OUT = { ...ROW, item_id: 3, sku: "WATCH-SLV-40", barcode: null, stock_qty: -1, stock_status: "out",
  shopify: { listing_item_id: 7, status: "live", last_sync_error: "Access denied" } };
const LIST = { summary: { items: 3, low_stock: 1, out_of_stock: 1 }, total: 3, items: [ROW, LOW, OUT] };
const DRIFT = {
  checked: 3, mismatched: 1, accuracy_pct: 66.67,
  items: [
    { item_id: 1, article_id: 10, article_name: "Classic Watch", sku: "WATCH-BLK-40", listing_item_id: 5, stock_qty: 12,
      expected_on_shopify: 12, shopify_qty: 9, state: "drift" },
    { item_id: 2, article_id: 10, article_name: "Classic Watch", sku: "WATCH-BLK-42", listing_item_id: 6, stock_qty: 3,
      expected_on_shopify: 3, shopify_qty: 3, state: "ok" },
  ],
};
const HISTORY = [
  { inventory_log_id: 2, qty_change: -2, stock_after: 12, reason: "order_created", note: "Order #1001",
    order_header_id: 44, order_number: "#1001", created_at: "2026-10-19T06:00:00Z" },
  { inventory_log_id: 1, qty_change: 4, stock_after: 14, reason: "restock", note: "supplier",
    order_header_id: null, order_number: null, created_at: "2026-10-18T06:00:00Z" },
];

const json = (body, status = 200) => new Response(JSON.stringify(body), { status });

// Answers by method and path; `routes` maps "GET /inventory" → body or (url, init) → Response.
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

describe("Inventory list", () => {
  it("shows the summary, stock badges, variants and Shopify state", async () => {
    mockApi({ "GET /inventory": LIST, "GET /inventory/drift": DRIFT });
    renderAt("/inventory");
    const row = await screen.findByRole("row", { name: /WATCH-BLK-40/ });
    expect(row).toHaveTextContent("Black / 40mm");
    expect(row).toHaveTextContent("8901");
    expect(row).toHaveTextContent("OK");
    expect(screen.getByRole("row", { name: /WATCH-BLK-42/ })).toHaveTextContent("Low");
    expect(screen.getByRole("row", { name: /WATCH-SLV-40/ })).toHaveTextContent("Out");
    const summary = screen.getByLabelText("Stock summary");
    expect(summary).toHaveTextContent("3Items (variants)");
    expect(summary).toHaveTextContent("1Low stock");
    expect(summary).toHaveTextContent("1Out of stock");
    expect(within(row).getByRole("link", { name: "Adjust" })).toHaveAttribute("href", "/inventory/1");
  });

  it("flags rows that differ from Shopify and pushes the app's figure", async () => {
    const fetchMock = mockApi({
      "GET /inventory": LIST,
      "GET /inventory/drift": DRIFT,
      "POST /inventory/1/sync": { item: ROW, synced: true, sync_error: null },
    });
    renderAt("/inventory");
    const row = await screen.findByRole("row", { name: /WATCH-BLK-40/ });
    await within(row).findByText("Shopify shows 9");
    expect(row).toHaveClass("drift-row");
    expect(screen.getByLabelText("Stock summary")).toHaveTextContent("1Differ from Shopify");
    expect(screen.getByRole("row", { name: /WATCH-BLK-42/ })).toHaveTextContent("In sync");
    await userEvent.click(within(row).getByRole("button", { name: "Set Shopify to 12" }));
    expect(await screen.findByText("WATCH-BLK-40: Shopify now shows 12.")).toBeInTheDocument();
    expect(calls(fetchMock, "POST", "/inventory/1/sync")).toHaveLength(1);
    await waitFor(() => expect(calls(fetchMock, "GET", "/inventory/drift")).toHaveLength(2));
  });

  it("lists failed syncs and retries them", async () => {
    const fetchMock = mockApi({
      "GET /inventory": LIST,
      "GET /inventory/drift": DRIFT,
      "POST /inventory/sync-pending": { retried: 1, synced: 1, failed: [] },
    });
    renderAt("/inventory");
    expect(await screen.findByRole("row", { name: /WATCH-SLV-40/ })).toHaveTextContent("Sync failed");
    expect(screen.getByRole("alert")).toHaveTextContent("1 SKU's stock didn't reach Shopify.");
    await userEvent.click(screen.getByRole("button", { name: "Retry Shopify sync" }));
    expect(await screen.findByText("1 SKU updated on Shopify.")).toBeInTheDocument();
    expect(calls(fetchMock, "POST", "/inventory/sync-pending")).toHaveLength(1);
  });

  it("searches and filters low stock through the API, and copes with Shopify being down", async () => {
    const fetchMock = mockApi({
      "GET /inventory": LIST,
      "GET /inventory/drift": () => json({ detail: "Couldn't reach Shopify" }, 502),
    });
    renderAt("/inventory");
    await screen.findByRole("row", { name: /WATCH-BLK-40/ });
    expect(await screen.findByText(/Shopify comparison unavailable: Couldn't reach Shopify/)).toBeInTheDocument();
    await userEvent.type(screen.getByRole("searchbox"), "blk-42");
    await waitFor(() => expect(new URL(calls(fetchMock, "GET", "/inventory").at(-1)[0]).searchParams.get("q")).toBe("blk-42"));
    await userEvent.click(screen.getByRole("button", { name: "Show low stock only" }));
    await waitFor(() => expect(new URL(calls(fetchMock, "GET", "/inventory").at(-1)[0]).searchParams.get("low")).toBe("true"));
  });

  it("shows an empty state", async () => {
    mockApi({ "GET /inventory": { summary: { items: 0, low_stock: 0, out_of_stock: 0 }, total: 0, items: [] }, "GET /inventory/drift": DRIFT });
    renderAt("/inventory");
    expect(await screen.findByText("No stock yet")).toBeInTheDocument();
  });
});

describe("Adjust stock", () => {
  it("validates on the client before sending anything", async () => {
    const fetchMock = mockApi({ "GET /inventory/1": ROW, "GET /inventory/1/history": HISTORY });
    renderAt("/inventory/1");
    await screen.findByRole("heading", { name: /Classic Watch/ });
    await userEvent.click(screen.getByRole("button", { name: "Save adjustment" }));
    expect(screen.getByText("Enter a whole number above 0.")).toBeInTheDocument();
    expect(screen.getByText("Choose a reason.")).toBeInTheDocument();
    await userEvent.click(screen.getByRole("radio", { name: "Remove" }));
    await userEvent.type(screen.getByLabelText("Quantity *"), "13");
    await userEvent.selectOptions(screen.getByLabelText("Reason *"), "damage");
    await userEvent.click(screen.getByRole("button", { name: "Save adjustment" }));
    expect(screen.getByText("Only 12 in stock, so 13 can't be removed.")).toBeInTheDocument();
    expect(calls(fetchMock, "POST", "/inventory/adjust")).toHaveLength(0);
  });

  it("previews, saves, reports the Shopify result and refreshes the history", async () => {
    const after = { ...ROW, stock_qty: 9 };
    let saved = false;
    const fetchMock = mockApi({
      "GET /inventory/1": () => json(saved ? after : ROW),
      "GET /inventory/1/history": () => json(saved ? [{ ...HISTORY[1], inventory_log_id: 3, qty_change: -3, stock_after: 9, reason: "damage", note: "dropped" }, ...HISTORY] : HISTORY),
      "POST /inventory/adjust": () => {
        saved = true;
        return json({ item: after, entry: {}, synced: false, sync_error: "Access denied" });
      },
    });
    renderAt("/inventory/1");
    await screen.findByRole("heading", { name: /Classic Watch/ });
    await userEvent.click(screen.getByRole("radio", { name: "Remove" }));
    await userEvent.type(screen.getByLabelText("Quantity *"), "3");
    expect(screen.getByText(/Current/)).toHaveTextContent("Current 12 → New 9");
    await userEvent.selectOptions(screen.getByLabelText("Reason *"), "damage");
    await userEvent.type(screen.getByLabelText("Note"), "dropped");
    await userEvent.click(screen.getByRole("button", { name: "Save adjustment" }));

    expect(await screen.findByText("Stock updated: 12 → 9. Saved locally, Shopify sync pending: Access denied")).toBeInTheDocument();
    expect(JSON.parse(calls(fetchMock, "POST", "/inventory/adjust")[0][1].body)).toEqual({
      item_id: 1, quantity_change: -3, reason: "damage", note: "dropped",
    });
    const rows = await screen.findAllByRole("row");
    await waitFor(() => expect(screen.getAllByRole("row")[1]).toHaveTextContent("−3"));
    expect(rows.length).toBeGreaterThan(1);
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
  });

  it("shows manual and order changes newest first, linking orders", async () => {
    mockApi({ "GET /inventory/1": ROW, "GET /inventory/1/history": HISTORY });
    renderAt("/inventory/1");
    const rows = await screen.findAllByRole("row");
    expect(rows[1]).toHaveTextContent("−2");
    expect(within(rows[1]).getByRole("link", { name: "Order #1001" })).toHaveAttribute("href", "/orders/44");
    expect(rows[2]).toHaveTextContent("+4");
    expect(rows[2]).toHaveTextContent("Manual");
    expect(rows[2]).toHaveTextContent("supplier");
  });

  it("picks an item by search when not coming from a row", async () => {
    mockApi({ "GET /inventory": LIST });
    renderAt("/inventory/adjust");
    const link = await screen.findByRole("link", { name: /WATCH-BLK-42/ });
    expect(link).toHaveAttribute("href", "/inventory/2");
  });

  it("says when the item doesn't exist", async () => {
    mockApi({});
    renderAt("/inventory/99");
    expect(await screen.findByText("Item not found")).toBeInTheDocument();
  });
});
