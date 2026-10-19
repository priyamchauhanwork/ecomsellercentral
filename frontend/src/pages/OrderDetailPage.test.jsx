import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { useEffect } from "react";
import { MemoryRouter, useLocation } from "react-router-dom";
import { describe, expect, it, vi } from "vitest";
import App from "../App";

const ADDRESS = {
  ship_name: "Asha Verma",
  ship_phone: "+91 98765 43210",
  ship_address_line1: "12 MG Road",
  ship_address_line2: "Flat 4B",
  ship_landmark: null,
  ship_city: "Bengaluru",
  ship_state: "Karnataka",
  ship_state_code: "KA",
  ship_pincode: "560001",
  ship_country: "India",
};

const LINE = {
  order_line_id: 1,
  line_number: 1,
  external_line_id: "9001",
  product_id: null,
  variant_id: null,
  sku: "WATCH-BLK-42",
  product_name: "Classic Watch",
  variant_name: "Black / 42mm",
  hsn_code: null,
  ordered_qty: 2,
  shipped_qty: 0,
  cancelled_qty: 0,
  returned_qty: 0,
  unit_price: "1499.50",
  line_discount_amount: "100.00",
  tax_rate: "18.00",
  cgst_amount: "125.96",
  sgst_amount: "125.96",
  igst_amount: "0.00",
  line_total_amount: "2899.00",
  line_refunded_amount: "0.00",
  courier_name: null,
  tracking_number: null,
  shipped_at: null,
  delivered_at: null,
  line_status: "pending",
  version_no: 1,
};

const ORDER = {
  order_header_id: 10,
  order_number: "#1001",
  channel: "shopify",
  order_date: "2026-10-17T18:00:00Z",
  customer_name: "Asha Verma",
  customer_email: "asha@example.com",
  customer_phone: "+91 98765 43210",
  order_status: "pending",
  payment_status: "paid",
  fulfillment_status: "unfulfilled",
  currency: "INR",
  total_amount: "2949.00",
  line_count: 2,
  external_order_id: "5001",
  ingestion_method: "webhook",
  last_ingestion_method: "webhook",
  last_synced_at: "2026-10-17T18:01:00Z",
  ...ADDRESS,
  bill_same_as_ship: true,
  bill_name: null,
  bill_phone: null,
  bill_address_line1: null,
  bill_address_line2: null,
  bill_city: null,
  bill_state: null,
  bill_state_code: null,
  bill_pincode: null,
  bill_country: null,
  bill_gstin: null,
  subtotal_amount: "2899.00",
  discount_amount: "100.00",
  coupon_code: "DIWALI100",
  shipping_amount: "50.00",
  tax_amount: "251.92",
  paid_amount: "2949.00",
  refunded_amount: "0.00",
  payment_gateway: "razorpay",
  paid_at: "2026-10-17T18:00:05Z",
  customer_notes: "Gift wrap please",
  cancel_reason: null,
  cancelled_at: null,
  version_no: 2,
  updated_at: "2026-10-18T05:00:00Z",
  lines: [
    // Out of order on purpose: the page must show line 1 first.
    { ...LINE, order_line_id: 2, line_number: 2, sku: "STRAP-01", product_name: "Strap",
      variant_name: null, ordered_qty: 3, shipped_qty: 1, cancelled_qty: 1,
      unit_price: "199.00", line_discount_amount: "0.00", line_total_amount: "597.00",
      line_status: "pending" },
    LINE,
  ],
  history: [
    { order_history_id: 1, version_no: 1, action_type: "created", old_status: null,
      new_status: "pending", changed_fields: null, change_source: "webhook",
      order_import_id: 3, remarks: null, changed_at: "2026-10-17T18:01:00Z" },
    { order_history_id: 2, version_no: 2, action_type: "status_change", old_status: "pending",
      new_status: "shipped", changed_fields: {}, change_source: "admin_ui",
      order_import_id: null, remarks: "Packed by Ravi", changed_at: "2026-10-18T05:00:00Z" },
  ],
};

function respond(body, status = 200) {
  return Promise.resolve(new Response(JSON.stringify(body), { status }));
}

function mockApi(...responses) {
  const fetchMock = vi.spyOn(globalThis, "fetch");
  for (const response of responses) fetchMock.mockImplementationOnce(() => response);
  return fetchMock;
}

const seen = { location: null };
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

describe("Order detail", () => {
  it("shows the order header, customer and addresses", async () => {
    mockApi(respond(ORDER));
    renderAt("/orders/10");

    expect(await screen.findByRole("heading", { name: /Order #1001/ })).toBeInTheDocument();
    expect(screen.getByText(/Placed 17 Oct 2026/)).toHaveTextContent("Shopify");
    expect(screen.getByRole("link", { name: "asha@example.com" })).toHaveAttribute(
      "href",
      "mailto:asha@example.com",
    );
    const shipping = screen.getByRole("heading", { name: "Shipping address" }).parentElement;
    expect(shipping).toHaveTextContent("12 MG Road");
    expect(shipping).toHaveTextContent("Bengaluru, Karnataka 560001");
    expect(screen.getByText("Same as shipping address")).toBeInTheDocument();
    expect(screen.getByText("Gift wrap please")).toBeInTheDocument();
  });

  it("shows every line in line-number order with quantities, prices and totals", async () => {
    mockApi(respond(ORDER));
    renderAt("/orders/10");
    const rows = (await screen.findAllByRole("row")).slice(1); // skip the header row

    expect(rows.map((row) => within(row).getAllByRole("cell")[0].textContent)).toEqual([
      "Classic WatchBlack / 42mm",
      "Strap",
    ]);
    expect(rows[0]).toHaveTextContent("₹1,499.50");
    expect(rows[0]).toHaveTextContent("−₹100.00 discount");
    expect(rows[0]).toHaveTextContent("₹2,899.00");
    expect(rows[1]).toHaveTextContent("1 shipped");
    expect(rows[1]).toHaveTextContent("1 cancelled");

    const totals = screen.getByText("Subtotal").closest("dl");
    expect(totals).toHaveTextContent("Subtotal₹2,899.00");
    expect(totals).toHaveTextContent("Discount (DIWALI100)−₹100.00");
    expect(totals).toHaveTextContent("Shipping₹50.00");
    expect(totals).toHaveTextContent("Tax₹251.92");
    expect(totals).toHaveTextContent("Total₹2,949.00");
    expect(totals).not.toHaveTextContent("Refunded");
  });

  it("shows each line's effect on stock, linking to the stock history", async () => {
    mockApi(
      respond({
        ...ORDER,
        lines: ORDER.lines.map((line) => ({
          ...line,
          stock:
            line.line_number === 1
              ? { item_id: 7, change: -2, not_counted: null }
              : { item_id: null, change: 0, not_counted: "not_linked" },
        })),
      }),
    );
    renderAt("/orders/10");
    const rows = (await screen.findAllByRole("row")).slice(1);
    expect(within(rows[0]).getByRole("link", { name: "Stock −2" })).toHaveAttribute("href", "/inventory/7");
    expect(rows[1]).toHaveTextContent("Stock: not linked to a product");
  });

  it("shows the timeline with the source of each change", async () => {
    mockApi(respond(ORDER));
    renderAt("/orders/10");
    const timeline = (await screen.findByRole("heading", { name: "Timeline" })).parentElement;
    const items = within(timeline).getAllByRole("listitem");
    expect(items[0]).toHaveTextContent("Order created · Pending");
    expect(items[0]).toHaveTextContent("via Shopify webhook");
    expect(items[1]).toHaveTextContent("Status changed from Pending to Shipped");
    expect(items[1]).toHaveTextContent("via Admin");
    expect(items[1]).toHaveTextContent("“Packed by Ravi”");
  });

  it("shows a separate billing address, GSTIN, tracking and refunds", async () => {
    mockApi(
      respond({
        ...ORDER,
        bill_same_as_ship: false,
        bill_name: "Verma Traders",
        bill_city: "Mumbai",
        bill_gstin: "29ABCDE1234F1Z5",
        refunded_amount: "597.00",
        lines: [{ ...LINE, line_status: "shipped", shipped_qty: 2, courier_name: "Delhivery",
                  tracking_number: "DL123" }],
      }),
    );
    renderAt("/orders/10");
    const billing = (await screen.findByRole("heading", { name: "Billing address" })).parentElement;
    expect(billing).toHaveTextContent("Verma Traders");
    expect(billing).toHaveTextContent("GSTIN: 29ABCDE1234F1Z5");
    expect(screen.getByText("Delhivery · DL123")).toBeInTheDocument();
    expect(screen.getByText("Subtotal").closest("dl")).toHaveTextContent("Refunded−₹597.00");
  });

  it("handles a guest order with no contact details or address", async () => {
    mockApi(
      respond({
        ...ORDER,
        customer_name: null,
        customer_email: null,
        customer_phone: null,
        ...Object.fromEntries(Object.keys(ADDRESS).map((key) => [key, null])),
        customer_notes: null,
        currency: "USD",
      }),
    );
    renderAt("/orders/10");
    expect(await screen.findByText("Guest")).toBeInTheDocument();
    expect(screen.getByText("No address")).toBeInTheDocument();
    expect(screen.queryByText("Customer notes")).not.toBeInTheDocument();
    expect(screen.getByText("Subtotal").closest("dl")).toHaveTextContent("Total$2,949.00");
  });

  it("shows the document buttons, disabled until their tickets", async () => {
    mockApi(respond(ORDER));
    renderAt("/orders/10");
    const actions = await screen.findByRole("group", { name: "Order actions" });
    for (const name of ["Invoice", "Packing slip", "Label"]) {
      expect(within(actions).getByRole("button", { name })).toBeDisabled();
    }
    expect(within(actions).getByRole("button", { name: "Mark shipped" })).toBeEnabled();
  });

  it("says when the order doesn't exist", async () => {
    mockApi(respond({ detail: "Order not found" }, 404));
    renderAt("/orders/999999");
    expect(await screen.findByText("Order not found")).toBeInTheDocument();
  });

  it("treats a non-numeric id as not found without calling the API", async () => {
    const fetchMock = mockApi();
    renderAt("/orders/abc");
    expect(screen.getByText("Order not found")).toBeInTheDocument();
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it("shows other errors with a retry", async () => {
    mockApi(respond({ detail: "Database unavailable" }, 503), respond(ORDER));
    renderAt("/orders/10");
    expect(await screen.findByRole("alert")).toHaveTextContent("Database unavailable");
    await userEvent.click(screen.getByRole("button", { name: "Try again" }));
    expect(await screen.findByRole("heading", { name: /Order #1001/ })).toBeInTheDocument();
  });

  it("goes back to the list with the filters it came from", async () => {
    const list = { total: 1, orders: [{ ...ORDER, line_count: 2 }] };
    mockApi(respond(list), respond(ORDER), respond(list));
    renderAt("/orders?status=pending&q=asha");

    await userEvent.click(await screen.findByRole("link", { name: "#1001" }));
    await userEvent.click(await screen.findByRole("link", { name: "← Back to orders" }));
    await waitFor(() => expect(seen.location.pathname).toBe("/orders"));
    expect(seen.location.search).toBe("?status=pending&q=asha");
  });

  it("goes back to the unfiltered list when opened directly", async () => {
    mockApi(respond(ORDER));
    renderAt("/orders/10");
    expect(await screen.findByRole("link", { name: "← Back to orders" })).toHaveAttribute(
      "href",
      "/orders",
    );
  });
});

// --- Mark shipped / delivered (#18) ------------------------------------------------------

const SHIPPED = {
  ...ORDER,
  order_status: "shipped",
  version_no: 3,
  lines: ORDER.lines.map((line) => ({ ...line, line_status: "shipped" })),
  history: [
    ...ORDER.history.slice(0, 1),
    { order_history_id: 3, version_no: 3, action_type: "status_change", old_status: "pending",
      new_status: "shipped", changed_fields: {}, change_source: "admin_ui",
      order_import_id: null, remarks: "Courier at 4 pm", changed_at: "2026-10-18T09:00:00Z" },
  ],
};
const PENDING = { ...ORDER, history: ORDER.history.slice(0, 1) };

function postedBody(fetchMock) {
  const [url, options] = fetchMock.mock.calls.at(-1);
  expect(new URL(url).pathname).toBe("/orders/10/status");
  expect(options.method).toBe("POST");
  return JSON.parse(options.body);
}

describe("Mark shipped", () => {
  it("confirms in the page, saves, and refreshes status, items and timeline in place", async () => {
    const fetchMock = mockApi(respond(PENDING), respond(SHIPPED));
    renderAt("/orders/10");
    await userEvent.click(await screen.findByRole("button", { name: "Mark shipped" }));

    const panel = screen.getByRole("form", { name: "Mark shipped confirmation" });
    expect(panel).toHaveTextContent("2 items will be marked shipped");
    expect(panel).toHaveTextContent("Shopify isn't changed");
    expect(within(panel).getByRole("button", { name: "Yes, mark shipped" })).toHaveFocus();

    await userEvent.type(within(panel).getByLabelText(/Note for the timeline/), "Courier at 4 pm");
    await userEvent.click(within(panel).getByRole("button", { name: "Yes, mark shipped" }));

    expect(await screen.findByText("Order marked as shipped.")).toBeInTheDocument();
    expect(postedBody(fetchMock)).toEqual({ status: "shipped", remarks: "Courier at 4 pm" });
    expect(fetchMock).toHaveBeenCalledTimes(2); // no reload needed
    expect(screen.queryByRole("form")).not.toBeInTheDocument();
    expect(screen.getByRole("heading", { name: /Order #1001/ })).toHaveTextContent("Shipped");
    const timeline = screen.getByRole("heading", { name: "Timeline" }).parentElement;
    expect(within(timeline).getAllByRole("listitem").at(-1)).toHaveTextContent(
      "Status changed from Pending to Shipped",
    );
    // Once shipped, the next step is delivery.
    expect(screen.queryByRole("button", { name: "Mark shipped" })).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Mark delivered" })).toBeEnabled();
  });

  it("sends no note when the note is blank", async () => {
    const fetchMock = mockApi(respond(PENDING), respond(SHIPPED));
    renderAt("/orders/10");
    await userEvent.click(await screen.findByRole("button", { name: "Mark shipped" }));
    await userEvent.type(screen.getByLabelText(/Note for the timeline/), "   ");
    await userEvent.click(screen.getByRole("button", { name: "Yes, mark shipped" }));
    await screen.findByText("Order marked as shipped.");
    expect(postedBody(fetchMock)).toEqual({ status: "shipped", remarks: null });
  });

  it("can be cancelled with the button or Escape, without saving", async () => {
    const fetchMock = mockApi(respond(PENDING));
    renderAt("/orders/10");
    await userEvent.click(await screen.findByRole("button", { name: "Mark shipped" }));
    expect(screen.getByRole("button", { name: "Mark shipped" })).toBeDisabled();
    await userEvent.click(screen.getByRole("button", { name: "Cancel" }));
    expect(screen.queryByRole("form")).not.toBeInTheDocument();

    await userEvent.click(screen.getByRole("button", { name: "Mark shipped" }));
    await userEvent.keyboard("{Escape}");
    expect(screen.queryByRole("form")).not.toBeInTheDocument();
    expect(fetchMock).toHaveBeenCalledTimes(1); // only the page load
  });

  it("disables the panel while saving, so a double click can't send twice", async () => {
    let finish;
    const fetchMock = mockApi(
      respond(PENDING),
      new Promise((resolve) => {
        finish = resolve;
      }),
    );
    renderAt("/orders/10");
    await userEvent.click(await screen.findByRole("button", { name: "Mark shipped" }));
    await userEvent.click(screen.getByRole("button", { name: "Yes, mark shipped" }));

    expect(screen.getByRole("button", { name: "Saving…" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "Cancel" })).toBeDisabled();
    await userEvent.click(screen.getByRole("button", { name: "Saving…" }));
    expect(fetchMock).toHaveBeenCalledTimes(2);

    finish(new Response(JSON.stringify(SHIPPED)));
    expect(await screen.findByText("Order marked as shipped.")).toBeInTheDocument();
  });

  it("keeps the panel open with the error when saving fails, and allows another try", async () => {
    mockApi(
      respond(PENDING),
      respond({ detail: "Database unavailable" }, 503),
      respond(SHIPPED),
    );
    renderAt("/orders/10");
    await userEvent.click(await screen.findByRole("button", { name: "Mark shipped" }));
    await userEvent.click(screen.getByRole("button", { name: "Yes, mark shipped" }));

    expect(await screen.findByRole("alert")).toHaveTextContent("Database unavailable");
    await userEvent.click(screen.getByRole("button", { name: "Yes, mark shipped" }));
    expect(await screen.findByText("Order marked as shipped.")).toBeInTheDocument();
  });

  it("reloads the order when it changed meanwhile (409)", async () => {
    const cancelled = { ...PENDING, order_status: "cancelled" };
    const fetchMock = mockApi(
      respond(PENDING),
      respond({ detail: "An order that is cancelled can't be marked shipped." }, 409),
      respond(cancelled),
    );
    renderAt("/orders/10");
    await userEvent.click(await screen.findByRole("button", { name: "Mark shipped" }));
    await userEvent.click(screen.getByRole("button", { name: "Yes, mark shipped" }));

    expect(await screen.findByText(/can't be marked shipped. The order has been reloaded./))
      .toBeInTheDocument();
    expect(fetchMock).toHaveBeenCalledTimes(3);
    expect(screen.getByRole("heading", { name: /Order #1001/ })).toHaveTextContent("Cancelled");
    expect(screen.queryByRole("button", { name: /^Mark/ })).not.toBeInTheDocument();
  });

  it("doesn't count cancelled items as being shipped", async () => {
    const lines = [{ ...LINE, line_status: "cancelled" }, { ...LINE, order_line_id: 2, line_number: 2 }];
    mockApi(respond({ ...PENDING, lines }));
    renderAt("/orders/10");
    await userEvent.click(await screen.findByRole("button", { name: "Mark shipped" }));
    expect(screen.getByRole("form")).toHaveTextContent("1 item will be marked shipped");
  });

  it.each(["delivered", "cancelled"])("offers no status change for a %s order", async (status) => {
    mockApi(respond({ ...PENDING, order_status: status }));
    renderAt("/orders/10");
    await screen.findByRole("heading", { name: /Order #1001/ });
    expect(screen.queryByRole("button", { name: /^Mark/ })).not.toBeInTheDocument();
  });
});

describe("Product images", () => {
  const IMAGE = "https://cdn.shopify.com/s/files/1/0787/files/watch.png?v=1";

  it("shows each line's product image, resized and lazy-loaded", async () => {
    mockApi(respond({ ...ORDER, lines: [{ ...LINE, image_url: IMAGE }] }));
    renderAt("/orders/10");
    const image = await screen.findByRole("img", { name: "Classic Watch" });
    expect(image).toHaveAttribute("src", `${IMAGE}&width=96`);
    expect(image).toHaveAttribute("loading", "lazy");
    expect(image).toHaveAttribute("width", "48");
  });

  it("shows a placeholder when there is no image", async () => {
    mockApi(respond({ ...ORDER, lines: [{ ...LINE, image_url: null }] }));
    renderAt("/orders/10");
    expect(await screen.findByRole("img", { name: "No image for Classic Watch" })).toBeInTheDocument();
  });

  it("falls back to the placeholder when the image fails to load", async () => {
    mockApi(respond({ ...ORDER, lines: [{ ...LINE, image_url: IMAGE }] }));
    renderAt("/orders/10");
    fireEvent.error(await screen.findByRole("img", { name: "Classic Watch" }));
    expect(screen.getByRole("img", { name: "No image for Classic Watch" })).toBeInTheDocument();
    expect(screen.queryByRole("img", { name: "Classic Watch" })).not.toBeInTheDocument();
  });

  it("keeps each line's image with its own line", async () => {
    const lines = [
      { ...LINE, image_url: "https://cdn.shopify.com/a.png" },
      { ...LINE, order_line_id: 2, line_number: 2, product_name: "Strap", image_url: null },
    ];
    mockApi(respond({ ...ORDER, lines }));
    renderAt("/orders/10");
    const rows = (await screen.findAllByRole("row")).slice(1);
    expect(within(rows[0]).getByRole("img", { name: "Classic Watch" })).toBeInTheDocument();
    expect(within(rows[1]).getByRole("img", { name: "No image for Strap" })).toBeInTheDocument();
  });
});
