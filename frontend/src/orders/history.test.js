import { describe, expect, it } from "vitest";
import { describeChange, describeSource } from "./history";

const entry = (fields) => ({ old_status: null, new_status: null, changed_fields: null, ...fields });

describe("timeline text", () => {
  it("describes each kind of change", () => {
    expect(describeChange(entry({ action_type: "created", new_status: "pending" }))).toBe(
      "Order created · Pending",
    );
    expect(
      describeChange(entry({ action_type: "status_change", old_status: "pending", new_status: "shipped" })),
    ).toBe("Status changed from Pending to Shipped");
    expect(describeChange(entry({ action_type: "cancelled", new_status: "cancelled" }))).toBe(
      "Order cancelled",
    );
  });

  it("names the fields a sync changed, once each", () => {
    const changed = { customer_notes: ["a", "b"], lines: {}, ship_city: ["x", "y"] };
    expect(describeChange(entry({ action_type: "synced_update", changed_fields: changed }))).toBe(
      "Updated: notes, items, ship city",
    );
    expect(describeChange(entry({ action_type: "synced_update" }))).toBe("Updated");
  });

  it("names where a change came from", () => {
    expect(describeSource("webhook")).toBe("Shopify webhook");
    expect(describeSource("admin_ui")).toBe("Admin");
    expect(describeSource("brand_new_source")).toBe("Brand new source");
  });
});
