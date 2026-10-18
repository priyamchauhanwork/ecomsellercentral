import { describe, expect, it } from "vitest";
import { SECTIONS, currentSection, isLinkActive } from "./navigation";

const orders = SECTIONS.find((section) => section.label === "Orders");
const at = (path) => {
  const url = new URL(path, "http://local");
  return { pathname: url.pathname, search: url.search };
};

describe("navigation", () => {
  it("picks the section for the current page", () => {
    expect(currentSection("/orders/12").label).toBe("Orders");
    expect(currentSection("/").label).toBe("Dashboard");
    expect(currentSection("/unknown").label).toBe("Dashboard");
  });

  it("highlights only the side link for the current view", () => {
    const [all, pending] = orders.links;
    expect(isLinkActive(all, at("/orders?from=2026-10-01"))).toBe(true);
    expect(isLinkActive(pending, at("/orders?from=2026-10-01"))).toBe(false);
    expect(isLinkActive(pending, at("/orders?status=pending&q=asha"))).toBe(true);
    expect(isLinkActive(all, at("/orders?status=pending"))).toBe(false);
    expect(isLinkActive(all, at("/orders/12"))).toBe(false);
  });
});
