import { act, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { useEffect } from "react";
import { MemoryRouter, useLocation } from "react-router-dom";
import { describe, expect, it, vi } from "vitest";
import App from "../App";

const ORDER = {
  order_header_id: 10,
  order_number: "#1001",
  channel: "shopify",
  order_date: "2026-10-17T18:00:00Z", // 23:30 on 17 Oct in India
  customer_name: "Asha Verma",
  customer_email: "asha@example.com",
  customer_phone: null,
  order_status: "pending",
  payment_status: "partially_paid",
  fulfillment_status: "unfulfilled",
  currency: "INR",
  total_amount: "2949.00",
  line_count: 2,
};

function respond(body, status = 200) {
  return Promise.resolve(new Response(JSON.stringify(body), { status }));
}

function mockApi(...responses) {
  const fetchMock = vi.spyOn(globalThis, "fetch");
  for (const response of responses) fetchMock.mockImplementationOnce(() => response);
  fetchMock.mockImplementation(() => respond({ total: 0, orders: [] }));
  return fetchMock;
}

function requestedParams(fetchMock, call = -1) {
  const url = fetchMock.mock.calls.at(call)[0];
  return Object.fromEntries(new URL(url).searchParams);
}

const seen = { location: null }; // the router location after each render
function LocationSpy() {
  const current = useLocation();
  useEffect(() => {
    seen.location = current;
  });
  return null;
}

function renderAt(path) {
  return render(
    <MemoryRouter initialEntries={[path]}>
      <App />
      <LocationSpy />
    </MemoryRouter>,
  );
}

describe("Orders list", () => {
  it("shows orders with the total, formatted in Indian time and the order currency", async () => {
    mockApi(respond({ total: 1, orders: [ORDER] }));
    renderAt("/orders");

    const row = await screen.findByRole("row", { name: /#1001/ });
    expect(within(row).getByText("Asha Verma")).toBeInTheDocument();
    expect(within(row).getByText("₹2,949.00")).toBeInTheDocument();
    expect(within(row).getByText(/17 Oct 2026/)).toBeInTheDocument();
    expect(within(row).getByText("Pending")).toBeInTheDocument();
    expect(within(row).getByText("Partially paid")).toBeInTheDocument();
    expect(screen.getByText("1 order")).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "All orders" })).toBeInTheDocument();
  });

  it("shows a non-INR order in its own currency and a guest customer", async () => {
    mockApi(
      respond({
        total: 1,
        orders: [{ ...ORDER, currency: "USD", total_amount: "275.00", customer_name: null }],
      }),
    );
    renderAt("/orders");
    expect(await screen.findByText(/^\$275\.00$/)).toBeInTheDocument();
    expect(screen.getByText("Guest")).toBeInTheDocument();
  });

  it("says when only the newest orders are shown", async () => {
    mockApi(respond({ total: 731, orders: [ORDER] }));
    renderAt("/orders");
    expect(await screen.findByText(/731 orders · showing the newest 1/)).toBeInTheDocument();
  });

  it("reads the filters from the URL and sends them to the API", async () => {
    const fetchMock = mockApi(respond({ total: 0, orders: [] }));
    renderAt("/orders?status=shipped&from=2026-10-01&to=2026-10-18&q=asha&bogus=1");
    await screen.findByText("No orders found");
    expect(requestedParams(fetchMock)).toEqual({
      status: "shipped",
      from: "2026-10-01",
      to: "2026-10-18",
      q: "asha",
    });
    expect(screen.getByRole("heading", { name: "Shipped orders" })).toBeInTheDocument();
    expect(screen.getByLabelText("Status")).toHaveValue("shipped");
  });

  it("puts a status change in the URL and refetches", async () => {
    const fetchMock = mockApi(respond({ total: 1, orders: [ORDER] }));
    renderAt("/orders");
    await screen.findByRole("row", { name: /#1001/ });

    await userEvent.selectOptions(screen.getByLabelText("Status"), "delivered");
    await waitFor(() => expect(seen.location.search).toBe("?status=delivered"));
    await waitFor(() => expect(requestedParams(fetchMock)).toEqual({ status: "delivered" }));
  });

  it("applies a quick date range", async () => {
    vi.useFakeTimers({ toFake: ["Date"], now: new Date("2026-10-18T06:00:00Z") });
    mockApi();
    renderAt("/orders");
    await userEvent.click(await screen.findByRole("button", { name: "Last 7 days" }));
    await waitFor(() => expect(seen.location.search).toBe("?from=2026-10-12&to=2026-10-18"));
    expect(screen.getByRole("button", { name: "Last 7 days" })).toHaveAttribute(
      "aria-pressed",
      "true",
    );
    vi.useRealTimers();
  });

  it("searches after the user pauses typing, not on every key", async () => {
    const fetchMock = mockApi();
    renderAt("/orders");
    await screen.findByText("No orders found");
    const callsBefore = fetchMock.mock.calls.length;

    await userEvent.type(screen.getByLabelText("Search"), "rahul");
    await waitFor(() => expect(seen.location.search).toBe("?q=rahul"));
    await waitFor(() => expect(requestedParams(fetchMock)).toEqual({ q: "rahul" }));
    expect(fetchMock.mock.calls.length - callsBefore).toBe(1);
  });

  it("does not call the API when the start date is after the end date", async () => {
    const fetchMock = mockApi();
    renderAt("/orders?from=2026-10-18&to=2026-10-01");
    expect(await screen.findByRole("alert")).toHaveTextContent(/start date is after/);
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it("offers to clear filters when nothing matches", async () => {
    mockApi();
    renderAt("/orders?status=cancelled&q=zzz");
    await userEvent.click(await screen.findByRole("button", { name: "Clear the filters" }));
    await waitFor(() => expect(seen.location.search).toBe(""));
    expect(screen.getByLabelText("Search")).toHaveValue("");
  });

  it("explains an empty store without filters", async () => {
    mockApi();
    renderAt("/orders");
    expect(await screen.findByText(/Orders imported from Shopify/)).toBeInTheDocument();
  });

  it("shows API errors and retries", async () => {
    mockApi(respond({ detail: "Database unavailable" }, 503), respond({ total: 1, orders: [ORDER] }));
    renderAt("/orders");
    expect(await screen.findByRole("alert")).toHaveTextContent("Database unavailable");
    await userEvent.click(screen.getByRole("button", { name: "Try again" }));
    expect(await screen.findByRole("row", { name: /#1001/ })).toBeInTheDocument();
  });

  it("explains when the backend can't be reached", async () => {
    mockApi(Promise.reject(new TypeError("Failed to fetch")));
    renderAt("/orders");
    expect(await screen.findByRole("alert")).toHaveTextContent(/Can't reach the server/);
  });

  it("opens the order when its row is clicked, without reloading the page", async () => {
    mockApi(
      respond({ total: 1, orders: [ORDER] }),
      respond({ ...ORDER, lines: [], history: [], bill_same_as_ship: true }),
    );
    renderAt("/orders");
    await userEvent.click(await screen.findByText("Asha Verma"));
    expect(seen.location.pathname).toBe("/orders/10");
    expect(await screen.findByRole("heading", { name: /Order #1001/ })).toBeInTheDocument();
  });

  it("filters by status from the side bar and highlights the view", async () => {
    const fetchMock = mockApi();
    renderAt("/orders?q=asha");
    await screen.findByText("No orders found");
    const sidebar = screen.getByRole("navigation", { name: "Orders views" });

    await userEvent.click(within(sidebar).getByRole("link", { name: "Pending" }));
    await waitFor(() => expect(requestedParams(fetchMock)).toEqual({ status: "pending" }));
    expect(within(sidebar).getByRole("link", { name: "Pending" })).toHaveAttribute(
      "aria-current",
      "page",
    );
    expect(screen.getByLabelText("Search")).toHaveValue(""); // views start unfiltered
  });

  it("ignores a response for filters that have since changed", async () => {
    let resolveSlow;
    const slow = new Promise((resolve) => {
      resolveSlow = resolve;
    });
    mockApi(slow, respond({ total: 1, orders: [{ ...ORDER, order_number: "#2002" }] }));
    renderAt("/orders");
    await userEvent.selectOptions(screen.getByLabelText("Status"), "shipped");
    expect(await screen.findByRole("row", { name: /#2002/ })).toBeInTheDocument();

    await act(async () => resolveSlow(new Response(JSON.stringify({ total: 1, orders: [ORDER] }))));
    expect(screen.queryByRole("row", { name: /#1001/ })).not.toBeInTheDocument();
  });
});

describe("Top navigation", () => {
  it("switches sections and their side bar", async () => {
    const emptyStock = { summary: { items: 0, low_stock: 0, out_of_stock: 0 }, total: 0, items: [] };
    mockApi().mockImplementation((input) =>
      respond(new URL(input).pathname === "/inventory" ? emptyStock : { total: 0, orders: [] }),
    );
    renderAt("/orders");
    await userEvent.click(screen.getByRole("link", { name: "Inventory" }));
    expect(seen.location.pathname).toBe("/inventory");
    expect(screen.getByRole("navigation", { name: "Inventory views" })).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Inventory" })).toHaveClass("active");
  });
});
