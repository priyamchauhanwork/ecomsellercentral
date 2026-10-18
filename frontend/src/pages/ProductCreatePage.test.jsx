import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { useEffect } from "react";
import { MemoryRouter, useLocation } from "react-router-dom";
import { describe, expect, it, vi } from "vitest";
import App from "../App";

const respond = (body, status = 200) => Promise.resolve(new Response(JSON.stringify(body), { status }));
const seen = { location: null };
function Spy() {
  const current = useLocation();
  useEffect(() => { seen.location = current; });
  return null;
}
const renderForm = () => render(<MemoryRouter initialEntries={["/products/new"]}><App /><Spy /></MemoryRouter>);

async function fillTshirt() {
  await userEvent.type(screen.getByLabelText("Product name *"), "Basic Tee");
  await userEvent.type(screen.getByLabelText("Product code *"), "tee");
  for (const [name, values] of [["Colour", "Black, White"], ["Size", "M"]]) {
    await userEvent.click(screen.getByRole("button", { name: "Add option" }));
    const names = screen.getAllByLabelText("Option name");
    const valueBoxes = screen.getAllByLabelText("Values, separated by commas");
    await userEvent.type(names.at(-1), name);
    await userEvent.type(valueBoxes.at(-1), values);
  }
}

describe("New product", () => {
  it("builds the variant grid with suggested SKUs and keeps edits", async () => {
    vi.spyOn(globalThis, "fetch");
    renderForm();
    await fillTshirt();
    expect(screen.getByRole("heading", { name: "Variants (2)" })).toBeInTheDocument();
    expect(screen.getByLabelText("SKU for Black / M")).toHaveValue("TEE-BLACK-M");
    await userEvent.clear(screen.getByLabelText("SKU for White / M"));
    await userEvent.type(screen.getByLabelText("SKU for White / M"), "MY-SKU");
    await userEvent.type(screen.getAllByLabelText("Values, separated by commas")[1], ", L");
    expect(screen.getByRole("heading", { name: "Variants (4)" })).toBeInTheDocument();
    expect(screen.getByLabelText("SKU for White / M")).toHaveValue("MY-SKU"); // kept
    expect(screen.getByLabelText("SKU for White / L")).toHaveValue("TEE-WHITE-L");
  });

  it("saves the product and opens it", async () => {
    const fetchMock = vi.spyOn(globalThis, "fetch").mockImplementationOnce(() =>
      respond({ article_id: 42 }, 201),
    ).mockImplementation(() => new Promise(() => {}));
    renderForm();
    await fillTshirt();
    await userEvent.type(screen.getByLabelText("MRP for Black / M"), "599");
    const whiteRow = screen.getByLabelText("SKU for White / M").closest("tr");
    await userEvent.click(within(whiteRow).getByRole("button", { name: "Remove" }));
    expect(screen.getByRole("heading", { name: "Variants (1)" })).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "Save product" }));
    const [url, options] = fetchMock.mock.calls[0];
    expect(new URL(url).pathname).toBe("/products");
    const body = JSON.parse(options.body);
    expect(body).toMatchObject({ article_code: "tee", name: "Basic Tee", variation_axes: ["Colour", "Size"], images: [] });
    expect(body.items).toEqual([
      { sku: "TEE-BLACK-M", variant_values: { Colour: "Black", Size: "M" }, barcode: null, mrp: "599", weight: null, stock_qty: 0 },
    ]);
    await vi.waitFor(() => expect(seen.location.pathname).toBe("/products/42"));
  });

  it("shows each API error next to its field and keeps what was typed", async () => {
    vi.spyOn(globalThis, "fetch").mockImplementationOnce(() =>
      respond({ detail: [
        { loc: ["body", "article_code"], msg: "Another product already uses this code." },
        { loc: ["body", "items", 1, "sku"], msg: "Another product already uses this SKU." },
      ] }, 422),
    );
    renderForm();
    await fillTshirt();
    await userEvent.click(screen.getByRole("button", { name: "Save product" }));
    expect(await screen.findByText("Please fix the highlighted fields.")).toBeInTheDocument();
    expect(screen.getByText("Another product already uses this code.")).toBeInTheDocument();
    const whiteRow = screen.getByLabelText("SKU for White / M").closest("tr");
    expect(within(whiteRow).getByText("Another product already uses this SKU.")).toBeInTheDocument();
    expect(screen.getByLabelText("Product name *")).toHaveValue("Basic Tee");
  });

  it("warns that more than 3 options can't go to Shopify", async () => {
    renderForm();
    for (let i = 0; i < 4; i += 1) await userEvent.click(screen.getByRole("button", { name: "Add option" }));
    expect(screen.getByText(/Shopify allows at most 3 options/)).toBeInTheDocument();
  });
});
