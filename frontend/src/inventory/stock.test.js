import { describe, expect, it } from "vitest";
import {
  adjustmentErrors,
  describeLineStock,
  describeSync,
  inventoryFiltersToParams,
  quantityChange,
  readInventoryFilters,
  signed,
} from "./stock";

describe("adjustment checks", () => {
  const form = (quantity, mode = "add", reason = "restock") => ({ quantity, mode, reason });

  it.each(["", "0", "-3", "2.5", "abc", "1e3", " "])("blocks quantity %j", (quantity) => {
    expect(adjustmentErrors(form(quantity), 10).quantity).toBe("Enter a whole number above 0.");
  });

  it("needs a reason", () => {
    expect(adjustmentErrors(form("3", "add", ""), 10)).toEqual({ reason: "Choose a reason." });
  });

  it("can't remove more than is on the shelf, but can always add", () => {
    expect(adjustmentErrors(form("11", "remove"), 10).quantity).toBe("Only 10 on the shelf, so 11 can't be removed.");
    expect(adjustmentErrors(form("1", "remove"), -2).quantity).toBe("Only 0 on the shelf, so 1 can't be removed.");
    expect(adjustmentErrors(form("10", "remove"), 10)).toEqual({});
    expect(adjustmentErrors(form("3", "add"), -2)).toEqual({});
  });

  it("damaged units are only removed", () => {
    expect(adjustmentErrors(form("1", "add", "damage"), 5).reason).toMatch(/use Repair/);
    expect(adjustmentErrors(form("1", "remove", "damage"), 5)).toEqual({});
  });

  it("caps very large changes", () => {
    expect(adjustmentErrors(form("1000001"), 0).quantity).toMatch(/at most 10,00,000/);
  });

  it("turns the form into a signed change", () => {
    expect(quantityChange("add", " 3 ")).toBe(3);
    expect(quantityChange("remove", "3")).toBe(-3);
    expect(quantityChange("add", "x")).toBeNull();
  });
});

describe("messages", () => {
  it("signs changes with a real minus", () => {
    expect([signed(3), signed(-2), signed(0)]).toEqual(["+3", "−2", "0"]);
  });

  it("describes the Shopify sync result", () => {
    expect(describeSync({ synced: false, sync_error: "Access denied" })).toEqual({
      tone: "error",
      text: "Saved locally, Shopify sync pending: Access denied",
    });
    expect(describeSync({ synced: true }).text).toBe("Shopify updated.");
    expect(describeSync({ synced: null }).text).toMatch(/Not sold on Shopify/);
  });

  it("describes an order line's place in stock", () => {
    const line = { item_id: 1, open: 0, allocated: 0, shipped: 0, delivered: 0, rto: 0, not_counted: null };
    expect(describeLineStock({ ...line, open: 2 })).toBe("Stock: 2 open");
    expect(describeLineStock({ ...line, allocated: 1, shipped: 1 })).toBe("Stock: 1 allocated · 1 shipped");
    expect(describeLineStock({ ...line, delivered: 1, rto: 1 })).toBe("Stock: 1 delivered · 1 RTO");
    expect(describeLineStock(line)).toBe("Stock: nothing held");
    expect(describeLineStock({ ...line, not_counted: "before_starting_stock" })).toMatch(/before stock was imported/);
    expect(describeLineStock({ ...line, item_id: null, not_counted: "not_linked" })).toBe("Stock: not linked to a product");
    expect(describeLineStock(null)).toBeNull();
  });
});

describe("inventory filters", () => {
  it("reads and writes the URL", () => {
    const filters = readInventoryFilters(new URLSearchParams("q=%20watch%20&low=true&page=x"));
    expect(filters).toEqual({ q: "watch", low: true, page: 1 });
    expect(inventoryFiltersToParams({ ...filters, page: 3 })).toEqual({ q: "watch", low: "true", page: "3" });
    expect(inventoryFiltersToParams({ q: "", low: false, page: 1 })).toEqual({});
  });
});
