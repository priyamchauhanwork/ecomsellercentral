import { describe, expect, it } from "vitest";
import { outstanding, qcLines, readReturnFilters, returnFiltersToParams } from "./returns";

const LINE = { return_line_id: 1, return_qty: 3, accepted_qty: 1, rejected_qty: 0, missing_qty: 0 };

describe("returns helpers", () => {
  it("counts the units still to check", () => {
    expect(outstanding(LINE)).toBe(2);
    expect(outstanding({ ...LINE, missing_qty: 2 })).toBe(0);
  });

  it("checks the QC form", () => {
    expect(qcLines([LINE], { 1: { accepted: "1", rejected: "1" } })).toEqual({
      errors: {},
      lines: [{ return_line_id: 1, accepted: 1, rejected: 1 }],
    });
    expect(qcLines([LINE], { 1: { accepted: "2", rejected: "1" } }).errors[1]).toBe("Only 2 still to check.");
    expect(qcLines([LINE], { 1: { accepted: "-1" } }).errors[1]).toBe("Enter whole numbers.");
    expect(qcLines([LINE], { 1: { accepted: "1.5" } }).errors[1]).toBe("Enter whole numbers.");
    expect(qcLines([LINE], {}).errors.form).toBe("Enter at least one accepted or rejected unit.");
  });

  it("reads and writes the list filters", () => {
    const filters = readReturnFilters(new URLSearchParams("status=open&type=rto&pending=true&q=%20R1%20&page=2"));
    expect(filters).toEqual({ status: "open", type: "rto", pending: true, q: "R1", page: 2 });
    expect(returnFiltersToParams(filters)).toEqual({ status: "open", type: "rto", pending: "true", q: "R1", page: "2" });
    expect(readReturnFilters(new URLSearchParams("status=lost&type=x"))).toMatchObject({ status: "", type: "" });
  });
});
