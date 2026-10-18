import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { useEffect } from "react";
import { MemoryRouter, useLocation } from "react-router-dom";
import { describe, expect, it, vi } from "vitest";
import App from "../App";

const PRODUCT = {
  article_id: 1, article_code: "classic-watch", name: "Classic Watch", brand: "Velocis", category: "Watches",
  status: "active", image_url: null, item_count: 4, low_stock_items: 1, total_stock: 19,
  price_min: "1499.00", price_max: "1999.00", created_at: "2026-10-18T10:00:00Z",
  channels: [{ listing_id: 1, channel_account_id: 1, account_name: "Shopify · watchstore", channel_code: "SHOPIFY", status: "live" }],
};
const DRAFT = { ...PRODUCT, article_id: 2, article_code: "TEE", name: "Basic Tee", status: "draft", channels: [],
  price_min: "599.00", price_max: "599.00", low_stock_items: 0 };

const respond = (body, status = 200) => Promise.resolve(new Response(JSON.stringify(body), { status }));
function mockApi(...responses) {
  const fetchMock = vi.spyOn(globalThis, "fetch");
  for (const response of responses) fetchMock.mockImplementationOnce(() => response);
  fetchMock.mockImplementation(() => respond({ total: 0, products: [] }));
  return fetchMock;
}
const lastUrl = (fetchMock) => new URL(fetchMock.mock.calls.at(-1)[0]);
const seen = { location: null };
function Spy() {
  const current = useLocation();
  useEffect(() => { seen.location = current; });
  return null;
}
const renderAt = (path) => render(<MemoryRouter initialEntries={[path]}><App /><Spy /></MemoryRouter>);

describe("Products list", () => {
  it("shows products with variants, stock, price range, channels and status", async () => {
    mockApi(respond({ total: 2, products: [PRODUCT, DRAFT] }));
    renderAt("/products");
    const row = await screen.findByRole("row", { name: /Classic Watch/ });
    expect(row).toHaveTextContent("classic-watch");
    expect(row).toHaveTextContent("1 low");
    expect(row).toHaveTextContent("₹1,499.00 – ₹1,999.00");
    expect(row).toHaveTextContent("Shopify · on sale");
    expect(screen.getByRole("row", { name: /Basic Tee/ })).toHaveTextContent("Not listed");
    expect(screen.getByText("2 products")).toBeInTheDocument();
    expect(screen.getByRole("navigation", { name: "Products views" })).toBeInTheDocument();
  });

  it("sends filters and paging to the API and keeps them in the URL", async () => {
    const fetchMock = mockApi(respond({ total: 120, products: [PRODUCT] }), respond({ total: 120, products: [PRODUCT] }));
    renderAt("/products?status=active&channel=live&low=true&q=watch&sort=stock");
    await screen.findByRole("row", { name: /Classic Watch/ });
    expect(Object.fromEntries(lastUrl(fetchMock).searchParams)).toEqual({
      status: "active", channel_status: "live", low_stock: "true", q: "watch", sort: "stock", limit: "50", offset: "0",
    });
    expect(screen.getByText("1–1 of 120")).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "Next" }));
    await waitFor(() => expect(lastUrl(fetchMock).searchParams.get("offset")).toBe("50"));
    expect(seen.location.search).toContain("page=2");
  });

  it("changing a filter goes back to page 1", async () => {
    mockApi(respond({ total: 120, products: [PRODUCT] }));
    renderAt("/products?page=3");
    await screen.findByRole("row", { name: /Classic Watch/ });
    await userEvent.selectOptions(screen.getByLabelText("Status"), "draft");
    await waitFor(() => expect(seen.location.search).toBe("?status=draft"));
  });

  it("takes selected products off sale after confirmation and reports each result", async () => {
    const fetchMock = mockApi(
      respond({ total: 2, products: [PRODUCT, DRAFT] }),
      respond([{ article_id: 1, ok: true, message: "Done" }, { article_id: 2, ok: false, message: "Not on any channel" }]),
    );
    renderAt("/products");
    await userEvent.click(await screen.findByRole("checkbox", { name: "Select all on this page" }));
    const bar = screen.getByRole("region", { name: "Bulk actions" });
    expect(bar).toHaveTextContent("2 selected");
    await userEvent.click(within(bar).getByRole("button", { name: "Take off sale" }));
    expect(bar).toHaveTextContent("Take 2 products off sale on Shopify?");
    await userEvent.click(within(bar).getByRole("button", { name: "Confirm" }));
    expect(await screen.findByText(/1 of 2 products taken off sale\. Not changed: Basic Tee \(Not on any channel\)/)).toBeInTheDocument();
    const [, options] = fetchMock.mock.calls.find(([url]) => String(url).endsWith("/products/status"));
    expect(JSON.parse(options.body)).toEqual({ article_ids: [1, 2], status: "inactive" });
  });

  it("offers the Shopify import on an empty catalogue and shows its summary", async () => {
    mockApi(
      respond({ total: 0, products: [] }),
      respond({ products: 35, articles_created: 35, articles_updated: 0, products_deactivated: 0, errors: [] }),
      respond({ total: 1, products: [PRODUCT] }),
    );
    renderAt("/products");
    const empty = await screen.findByText("No products found");
    await userEvent.click(within(empty.parentElement).getByRole("button", { name: "Import from Shopify" }));
    expect(await screen.findByText("Imported from Shopify: 35 products read, 35 new, 0 updated.")).toBeInTheDocument();
    expect(await screen.findByRole("row", { name: /Classic Watch/ })).toBeInTheDocument();
  });

  it("opens a product without reloading", async () => {
    mockApi(
      respond({ total: 1, products: [PRODUCT] }),
      respond({ ...PRODUCT, description: null, variation_axes: [], attributes: {}, images: [],
                updated_at: PRODUCT.created_at, items: [], listings: [] }),
    );
    renderAt("/products");
    await userEvent.click(await screen.findByText("Velocis"));
    expect(seen.location.pathname).toBe("/products/1");
    expect(await screen.findByRole("heading", { name: /Classic Watch/ })).toBeInTheDocument();
  });

  it("shows API errors with a retry", async () => {
    mockApi(respond({ detail: "Database unavailable" }, 503), respond({ total: 1, products: [PRODUCT] }));
    renderAt("/products");
    expect(await screen.findByRole("alert")).toHaveTextContent("Database unavailable");
    await userEvent.click(screen.getByRole("button", { name: "Try again" }));
    expect(await screen.findByRole("row", { name: /Classic Watch/ })).toBeInTheDocument();
  });
});
