import { describe, expect, it } from "vitest";
import { fieldErrors, suggestedSku, variantRows } from "./form";
import { priceRange, productApiParams, readProductFilters } from "./filters";

describe("variantRows", () => {
  it("makes every combination with stable keys", () => {
    const rows = variantRows([
      { name: "Colour", values: "Black, White" },
      { name: " Size ", values: "M,L" },
    ]);
    expect(rows.map((r) => r.values)).toEqual([
      { Colour: "Black", Size: "M" }, { Colour: "Black", Size: "L" },
      { Colour: "White", Size: "M" }, { Colour: "White", Size: "L" },
    ]);
    expect(rows[0].key).toBe("Colour=Black|Size=M");
  });

  it("ignores blank or repeated values and unnamed options; no options → one variant", () => {
    expect(variantRows([{ name: "Colour", values: "Black, , black, Black" }]).length).toBe(1);
    expect(variantRows([{ name: "", values: "A, B" }])).toEqual([{ key: "single", values: {} }]);
    expect(variantRows([])).toEqual([{ key: "single", values: {} }]);
  });
});

describe("suggestedSku", () => {
  it("joins the code and values in upper case", () => {
    expect(suggestedSku("tshirt basic", { Colour: "Black", Size: "XL / 2" })).toBe("TSHIRT-BASIC-BLACK-XL-2");
    expect(suggestedSku("", { Colour: "Red" })).toBe("RED");
  });
});

describe("fieldErrors", () => {
  it("puts item errors on their row and keeps several messages per field", () => {
    const detail = [
      { loc: ["body", "article_code"], msg: "Taken" },
      { loc: ["body", "items", 1, "sku"], msg: "Same SKU as variant 1." },
      { loc: ["body", "items", 1, "sku"], msg: "Already used." },
      { loc: ["body"], msg: "Something general" },
    ];
    expect(fieldErrors(detail, ["a", "b"])).toEqual({
      article_code: ["Taken"],
      "items.b.sku": ["Same SKU as variant 1.", "Already used."],
      "": ["Something general"],
    });
    expect(fieldErrors("Plain message", [])).toEqual({ "": ["Plain message"] });
  });
});

describe("product filters", () => {
  it("reads only known values and pages from the URL", () => {
    const filters = readProductFilters(new URLSearchParams("status=gone&channel=live&sort=stock&low=true&page=3&q=+tee+"));
    expect(filters).toEqual({ status: "", channel: "live", sort: "stock", low: true, q: "tee", page: 3 });
    expect(readProductFilters(new URLSearchParams("page=-2")).page).toBe(1);
  });

  it("turns filters into API parameters with offset paging", () => {
    expect(productApiParams({ status: "draft", channel: "", sort: "name", low: true, q: "x", page: 2 })).toEqual({
      status: "draft", channel_status: "", sort: "name", low_stock: "true", q: "x", limit: 50, offset: 50,
    });
  });

  it("formats a price range", () => {
    const format = (v) => `₹${v}`;
    expect(priceRange("10.00", "10.00", format)).toBe("₹10.00");
    expect(priceRange("10.00", "20.00", format)).toBe("₹10.00 – ₹20.00");
    expect(priceRange(null, null, format)).toBe("—");
  });
});
