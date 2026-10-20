import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { describe, expect, it, vi } from "vitest";
import App from "../App";

const DASHBOARD = {
  generated_at: "2026-10-20T04:30:00Z",
  today: "2026-10-20",
  currency: "INR",
  low_stock_threshold: 5,
  orders: {
    orders_today: 3, revenue_today: "5898.00", refunded_today: "0", orders_total: 1250,
    revenue_total: "1234567.50", refunded_total: "1500.00", cancelled_total: 12,
    other_currency_orders: 1, needs_attention: 2,
  },
  stock: { low_stock: 2, out_of_stock: 91, skus: 98, products: 17 },
  returns_awaiting_qc: 4,
  recent_orders: [
    { order_header_id: 44, order_number: "#1004", order_date: "2026-10-20T04:00:00Z", customer_name: "Asha Verma",
      total_amount: "960.40", currency: "INR", order_status: "pending", attention_reason: null, line_count: 2 },
    { order_header_id: 43, order_number: "#1003", order_date: "2026-10-19T10:00:00Z", customer_name: null,
      total_amount: "275.00", currency: "USD", order_status: "cancelled",
      attention_reason: "cancelled_after_acknowledgement", line_count: 1 },
  ],
  low_stock_items: [
    { item_id: 7, article_id: 1, article_name: "Classic Watch", sku: "WATCH-SLV-40", barcode: null,
      variant_values: { Colour: "Silver", Size: "40mm" }, image_url: null, available: -2, stock_status: "out" },
    { item_id: 8, article_id: 1, article_name: "Classic Watch", sku: "WATCH-BLK-40", barcode: null,
      variant_values: { Colour: "Black", Size: "40mm" }, image_url: null, available: 4, stock_status: "low" },
  ],
};
const EMPTY = {
  ...DASHBOARD,
  orders: Object.fromEntries(Object.keys(DASHBOARD.orders).map((key) => [key, key.includes("revenue") || key.includes("refunded") ? "0" : 0])),
  stock: { low_stock: 0, out_of_stock: 0, skus: 0, products: 0 },
  returns_awaiting_qc: 0,
  recent_orders: [],
  low_stock_items: [],
};

const json = (body, status = 200) => new Response(JSON.stringify(body), { status });
const renderAt = (path) => render(<MemoryRouter initialEntries={[path]}><App /></MemoryRouter>);

describe("Dashboard", () => {
  it("shows the key figures with links to their lists", async () => {
    vi.spyOn(globalThis, "fetch").mockImplementation(async () => json(DASHBOARD));
    renderAt("/");
    const figures = await screen.findByLabelText("Key figures");
    const card = (label) => within(figures).getByText(label).closest(".stat-card");
    expect(card("Orders today")).toHaveTextContent("3India time, 2026-10-20");
    expect(card("Revenue today")).toHaveTextContent("₹5,898.00Excl. cancelled orders");
    expect(card("Total orders")).toHaveTextContent("1,25012 cancelled");
    expect(card("Total revenue")).toHaveTextContent("₹12,34,567.50");
    expect(card("Total revenue")).toHaveTextContent("₹1,500.00 refunded · 1 order in other currencies not included");
    expect(card("Low stock items")).toHaveTextContent("932 low · 91 out (≤ 5)");
    expect(card("Low stock items")).toHaveAttribute("href", "/inventory?low=true");
    expect(card("Total products")).toHaveTextContent("98 SKUs17 products");
    expect(card("Needs attention")).toHaveAttribute("href", "/orders?attention=true");
    expect(card("Returns awaiting QC")).toHaveAttribute("href", "/returns?pending=true");
    expect(screen.getByText(/Last updated/)).toHaveTextContent("IST");
  });

  it("lists recent orders and the lowest stock, linking to each", async () => {
    vi.spyOn(globalThis, "fetch").mockImplementation(async () => json(DASHBOARD));
    renderAt("/");
    const recent = await screen.findByRole("list", { name: "Recent orders" });
    const rows = within(recent).getAllByRole("listitem");
    expect(within(rows[0]).getByRole("link", { name: "#1004" })).toHaveAttribute("href", "/orders/44");
    expect(rows[0]).toHaveTextContent("Asha Verma");
    expect(rows[0]).toHaveTextContent("2 items");
    expect(rows[0]).toHaveTextContent("₹960.40");
    expect(rows[1]).toHaveTextContent("Guest");
    expect(rows[1]).toHaveTextContent("$275.00");
    expect(rows[1]).toHaveTextContent("Needs attention");

    const low = within(screen.getByRole("list", { name: "Low stock items" })).getAllByRole("listitem");
    expect(within(low[0]).getByRole("link", { name: "Classic Watch" })).toHaveAttribute("href", "/inventory/7");
    expect(low[0]).toHaveTextContent("Silver / 40mm · WATCH-SLV-40");
    expect(low[0]).toHaveTextContent("-2Out");
    expect(low[1]).toHaveTextContent("4Low");
  });

  it("shows empty states", async () => {
    vi.spyOn(globalThis, "fetch").mockImplementation(async () => json(EMPTY));
    renderAt("/");
    expect(await screen.findByText("No orders yet")).toBeInTheDocument();
    expect(screen.getByText("All stocked up")).toBeInTheDocument();
    expect(screen.getByText("Every item has more than 5 available.")).toBeInTheDocument();
    expect(within(screen.getByLabelText("Key figures")).getByText("Revenue today").closest(".stat-card")).toHaveTextContent("₹0.00");
  });

  it("refreshes in place, keeping the figures while loading", async () => {
    let call = 0;
    const fetchMock = vi.spyOn(globalThis, "fetch").mockImplementation(async () => {
      call += 1;
      return json(call === 1 ? DASHBOARD : { ...DASHBOARD, orders: { ...DASHBOARD.orders, orders_today: 4 } });
    });
    renderAt("/");
    const figures = await screen.findByLabelText("Key figures");
    await userEvent.click(screen.getByRole("button", { name: "Refresh" }));
    await waitFor(() => expect(within(figures).getByText("Orders today").closest(".stat-card")).toHaveTextContent("4India"));
    expect(fetchMock).toHaveBeenCalledTimes(2);
    expect(screen.getByRole("button", { name: "Refresh" })).toBeEnabled();
  });

  it("keeps the last figures when a refresh fails", async () => {
    let call = 0;
    vi.spyOn(globalThis, "fetch").mockImplementation(async () => {
      call += 1;
      return call === 1 ? json(DASHBOARD) : json({ detail: "Database unavailable" }, 503);
    });
    renderAt("/");
    await screen.findByLabelText("Key figures");
    await userEvent.click(screen.getByRole("button", { name: "Refresh" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("Couldn't refresh: Database unavailable");
    expect(screen.getByLabelText("Key figures")).toHaveTextContent("₹5,898.00");
  });

  it("shows an error with a retry when the first load fails", async () => {
    let call = 0;
    vi.spyOn(globalThis, "fetch").mockImplementation(async () => {
      call += 1;
      return call === 1 ? json({ detail: "Database unavailable" }, 503) : json(DASHBOARD);
    });
    renderAt("/");
    expect(await screen.findByText("Database unavailable")).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "Try again" }));
    expect(await screen.findByLabelText("Key figures")).toBeInTheDocument();
  });
});
