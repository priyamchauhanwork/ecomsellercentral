import { describe, expect, it } from "vitest";
import {
  dateRangeError,
  filtersToParams,
  hasFilters,
  quickRanges,
  readFilters,
  todayInIndia,
} from "./filters";

describe("readFilters", () => {
  it("reads valid filters from the URL", () => {
    const params = new URLSearchParams("status=processing&from=2026-10-01&to=2026-10-18&q=+asha+&attention=true");
    expect(readFilters(params)).toEqual({
      status: "processing",
      from: "2026-10-01",
      to: "2026-10-18",
      q: "asha",
      attention: "true",
    });
  });

  it("drops unknown statuses, malformed or impossible dates, and over-long search", () => {
    const params = new URLSearchParams(
      `status=fulfilled&from=18-10-2026&to=2026-02-30&q=${"x".repeat(150)}`,
    );
    expect(readFilters(params)).toEqual({ status: "", from: "", to: "", q: "x".repeat(100), attention: "" });
  });
});

describe("filter helpers", () => {
  it("leaves empty values out of the URL", () => {
    expect(filtersToParams({ status: "", from: "2026-10-01", to: "", q: "" })).toEqual({
      from: "2026-10-01",
    });
  });

  it("knows when any filter is set", () => {
    expect(hasFilters({ status: "", from: "", to: "", q: "" })).toBe(false);
    expect(hasFilters({ status: "", from: "", to: "", q: "a" })).toBe(true);
  });

  it("flags a start date after the end date", () => {
    expect(dateRangeError({ from: "2026-10-18", to: "2026-10-01" })).toMatch(/after/);
    expect(dateRangeError({ from: "2026-10-01", to: "2026-10-01" })).toBe("");
    expect(dateRangeError({ from: "2026-10-01", to: "" })).toBe("");
  });
});

describe("dates in India", () => {
  it("uses the Indian calendar day, not UTC", () => {
    // 18:45 UTC on 17 Oct is 00:15 on 18 Oct in India.
    expect(todayInIndia(new Date("2026-10-17T18:45:00Z"))).toBe("2026-10-18");
    expect(todayInIndia(new Date("2026-10-17T18:15:00Z"))).toBe("2026-10-17");
  });

  it("builds today, last 7 days and this month", () => {
    const ranges = quickRanges(new Date("2026-10-03T06:00:00Z"));
    expect(ranges).toEqual([
      { label: "Today", from: "2026-10-03", to: "2026-10-03" },
      { label: "Last 7 days", from: "2026-09-27", to: "2026-10-03" }, // across months
      { label: "This month", from: "2026-10-01", to: "2026-10-03" },
    ]);
  });
});
