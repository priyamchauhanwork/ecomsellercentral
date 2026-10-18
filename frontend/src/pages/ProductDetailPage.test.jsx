import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { describe, expect, it, vi } from "vitest";
import App from "../App";

const ITEM = (id, sku, colour, extra = {}) => ({
  item_id: id, sku, barcode: null, variant_values: { Colour: colour }, mrp: "1999.00", weight: null,
  stock_qty: 7, stock_status: "ok", stock_baseline_at: null, status: "active", image_url: null, ...extra,
});
const LISTING_ITEM = (id, itemId, extra = {}) => ({
  listing_item_id: id, item_id: itemId, channel_sku: `SKU-${itemId}`, channel_item_id: String(800 + itemId),
  selling_price: "1499.00", status: "live", last_sync_error: null, ...extra,
});
const LISTING = {
  listing_id: 5, channel_account_id: 1, account_name: "Shopify · watchstore", channel_code: "SHOPIFY",
  channel_name: "Shopify", split_key: {}, title: null, description: null, channel_parent_id: "7001", status: "live",
  items: [LISTING_ITEM(51, 1), LISTING_ITEM(52, 2)],
};
const PRODUCT = {
  article_id: 1, article_code: "classic-watch", name: "Classic Watch", brand: "Velocis", category: "Watches",
  description: '<p>Steel case</p><img src=x onerror="window.hacked=true"><script>window.hacked=true</script>',
  variation_axes: ["Colour"], attributes: { Strap: "Leather" }, status: "active", image_url: null,
  images: [{ url: "https://cdn.shopify.com/a.png", filter: {} }, { url: "https://cdn.shopify.com/b.png", filter: { Colour: "Black" } }],
  created_at: "2026-10-18T10:00:00Z", updated_at: "2026-10-18T10:00:00Z",
  items: [ITEM(1, "W-BLK", "Black"), ITEM(2, "W-SLV", "Silver", { stock_qty: -2, stock_status: "out" })],
  listings: [LISTING],
};

const respond = (body, status = 200) => Promise.resolve(new Response(JSON.stringify(body), { status }));
function mockApi(...responses) {
  const fetchMock = vi.spyOn(globalThis, "fetch");
  for (const response of responses) fetchMock.mockImplementationOnce(() => response);
  return fetchMock;
}
const posted = (fetchMock) => {
  const [url, options] = fetchMock.mock.calls.at(-1);
  return { path: new URL(url).pathname, body: JSON.parse(options.body) };
};
const renderAt = (path) => render(<MemoryRouter initialEntries={[path]}><App /></MemoryRouter>);

describe("Product detail", () => {
  it("shows details, images, variants with stock badges and channel listings", async () => {
    mockApi(respond(PRODUCT));
    renderAt("/products/1");
    expect(await screen.findByRole("heading", { name: /Classic Watch/ })).toHaveTextContent("Active");
    expect(screen.getByText(/classic-watch · Velocis · Watches/)).toBeInTheDocument();
    expect(screen.getByText("Colour: Black", { selector: ".gallery span" })).toBeInTheDocument();
    expect(screen.getByText("Leather")).toBeInTheDocument();
    const variants = screen.getByRole("heading", { name: "Variants (2)" }).parentElement;
    expect(within(variants).getByRole("row", { name: /W-SLV/ })).toHaveTextContent("-2 Out");
    const channel = screen.getByRole("heading", { name: "Shopify · Shopify · watchstore" }).closest(".card");
    expect(channel).toHaveTextContent("Shopify product 7001");
    expect(within(channel).getByRole("switch", { name: "Classic Watch (Shopify · watchstore) on sale" })).toBeChecked();
    expect(screen.queryByRole("button", { name: "Publish to Shopify" })).not.toBeInTheDocument();
  });

  it("shows the Shopify description as text without running it", async () => {
    mockApi(respond(PRODUCT));
    renderAt("/products/1");
    const description = await screen.findByText(/Steel case/);
    expect(description).toHaveTextContent(/^Steel case$/); // no script source shown either
    expect(document.querySelector(".notes script, .notes img")).toBeNull();
    expect(window.hacked).toBeUndefined();
  });

  it("takes the product off sale only after confirmation", async () => {
    const off = { ...PRODUCT, listings: [{ ...LISTING, status: "inactive" }] };
    const fetchMock = mockApi(respond(PRODUCT), respond(off));
    renderAt("/products/1");
    await userEvent.click(await screen.findByRole("switch", { name: /Classic Watch .* on sale/ }));
    const dialog = screen.getByRole("alertdialog");
    expect(dialog).toHaveTextContent("becomes a draft on Shopify");
    expect(fetchMock).toHaveBeenCalledTimes(1);
    await userEvent.click(within(dialog).getByRole("button", { name: "Take off sale" }));
    expect(await screen.findByText(/is off sale on Shopify/)).toBeInTheDocument();
    expect(posted(fetchMock)).toEqual({ path: "/products/1/listings/5/status", body: { status: "inactive" } });
    expect(screen.getByRole("switch", { name: /Classic Watch .* on sale/ })).not.toBeChecked();
  });

  it("puts a SKU back on sale without asking", async () => {
    const product = { ...PRODUCT, listings: [{ ...LISTING, items: [LISTING_ITEM(51, 1, { status: "inactive" }), LISTING_ITEM(52, 2)] }] };
    const fetchMock = mockApi(respond(product), respond(PRODUCT));
    renderAt("/products/1");
    await userEvent.click(await screen.findByRole("switch", { name: "W-BLK on sale" }));
    expect(posted(fetchMock)).toEqual({ path: "/products/1/listing-items/51/status", body: { status: "live" } });
    expect(await screen.findByRole("switch", { name: "W-BLK on sale" })).toBeChecked();
  });

  it("explains a SKU can only look sold out, and shows Shopify failures with the stored error", async () => {
    const failed = { ...PRODUCT, listings: [{ ...LISTING, items: [LISTING_ITEM(51, 1, { last_sync_error: "Access denied" }), LISTING_ITEM(52, 2)] }] };
    mockApi(respond(PRODUCT), respond({ detail: "Shopify refused the change: Access denied" }, 502), respond(failed));
    renderAt("/products/1");
    await userEvent.click(await screen.findByRole("switch", { name: "W-BLK on sale" }));
    expect(screen.getByRole("alertdialog")).toHaveTextContent("show as sold out");
    await userEvent.click(screen.getByRole("button", { name: "Take off sale" }));
    expect(await screen.findByText(/W-BLK: Shopify refused the change: Access denied Nothing was changed/)).toBeInTheDocument();
    expect(await screen.findByText("Last sync failed: Access denied")).toBeInTheDocument();
  });

  it("publishes a product that isn't on Shopify, with selling prices", async () => {
    const draft = { ...PRODUCT, status: "draft", listings: [] };
    const fetchMock = mockApi(
      respond(draft),
      respond({ listing_id: 9, shopify_product_id: "9001", variants: 2, enabled: true, warnings: [], product: PRODUCT }),
    );
    renderAt("/products/1");
    expect(await screen.findByText(/Not listed on any channel yet/)).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "Publish to Shopify" }));
    const panel = screen.getByRole("form", { name: "Publish to Shopify" });
    const price = within(panel).getByLabelText("Selling price for W-BLK");
    expect(price).toHaveValue(1999);
    await userEvent.clear(price);
    await userEvent.type(price, "1499");
    await userEvent.click(within(panel).getByRole("button", { name: "Publish" }));
    expect(await screen.findByText("Published to Shopify with 2 variants, on sale.")).toBeInTheDocument();
    expect(posted(fetchMock)).toEqual({
      path: "/products/1/publish", body: { enable: true, prices: { 1: "1499", 2: "1999.00" } },
    });
  });

  it("keeps the publish panel open with Shopify's error", async () => {
    const draft = { ...PRODUCT, listings: [] };
    mockApi(respond(draft), respond({ detail: "Set a selling price for: W-SLV." }, 422));
    renderAt("/products/1");
    await userEvent.click(await screen.findByRole("button", { name: "Publish to Shopify" }));
    await userEvent.click(screen.getByRole("button", { name: "Publish" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("Set a selling price for: W-SLV.");
    expect(screen.getByRole("form", { name: "Publish to Shopify" })).toBeInTheDocument();
  });

  it("archived products can't be published; unknown ids show not found", async () => {
    mockApi(respond({ ...PRODUCT, status: "archived", listings: [] }), respond({ detail: "Product not found" }, 404));
    const { unmount } = renderAt("/products/1");
    await screen.findByRole("heading", { name: /Classic Watch/ });
    expect(screen.queryByRole("button", { name: "Publish to Shopify" })).not.toBeInTheDocument();
    unmount();
    renderAt("/products/999");
    expect(await screen.findByText("Product not found")).toBeInTheDocument();
  });
});
