import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { describe, expect, it, vi } from "vitest";
import App from "../App";

const STATUS = {
  configured: true, config_error: null, environment: "sandbox",
  base_url: "https://api-sandbox.shiprocket.in/v1/external", api_user: "api+oms@example.com",
  webhook_token_set: false, token_expires_at: null, connection_status: null, last_connected_at: null, last_error: null,
};
const TEST = {
  pickup_locations: [
    { name: "Primary", address: "12 MG Road", city: "Bengaluru", state: "Karnataka", pincode: "560001", phone_verified: true, is_primary: true },
    { name: "Warehouse 2", address: "", city: null, state: null, pincode: "110001", phone_verified: false, is_primary: false },
  ],
  wallet_balance: "1520.50", wallet_error: null, token_expires_at: "2026-11-01T04:30:00Z",
  status: { ...STATUS, connection_status: "active", last_connected_at: "2026-10-22T04:30:00Z", token_expires_at: "2026-11-01T04:30:00Z" },
};

const json = (body, status = 200) => new Response(JSON.stringify(body), { status });
function mockApi(routes) {
  return vi.spyOn(globalThis, "fetch").mockImplementation(async (input, init = {}) => {
    const url = new URL(input);
    const handler = routes[`${init.method ?? "GET"} ${url.pathname}`];
    if (handler === undefined) return json({ detail: "Not found" }, 404);
    return typeof handler === "function" ? handler() : json(handler);
  });
}
const renderAt = (path) => render(<MemoryRouter initialEntries={[path]}><App /></MemoryRouter>);

describe("Shiprocket settings", () => {
  it("shows the connection and the setup steps", async () => {
    mockApi({ "GET /shiprocket/status": STATUS });
    renderAt("/settings");
    expect(await screen.findByRole("heading", { name: "Shiprocket" })).toBeInTheDocument();
    expect(screen.getByText("Not tested yet")).toBeInTheDocument();
    expect(screen.getByText("Sandbox")).toBeInTheDocument();
    expect(screen.getByText("api+oms@example.com")).toBeInTheDocument();
    expect(screen.getByText("Not set (needed for tracking updates)")).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "How to connect" })).toBeInTheDocument();
    expect(screen.getByRole("navigation", { name: "Settings views" })).toBeInTheDocument();
  });

  it("tests the connection and lists pickup locations and the wallet", async () => {
    mockApi({ "GET /shiprocket/status": STATUS, "POST /shiprocket/test": TEST });
    renderAt("/settings/shiprocket");
    await userEvent.click(await screen.findByRole("button", { name: "Test connection" }));
    expect(await screen.findByText("Connected to Shiprocket as api+oms@example.com.")).toBeInTheDocument();
    expect(screen.getByText("Connected")).toBeInTheDocument();
    expect(screen.getByText("₹1,520.50")).toBeInTheDocument();
    const rows = within(screen.getByRole("list", { name: "Pickup locations" })).getAllByRole("listitem");
    expect(rows[0]).toHaveTextContent("PrimaryPrimary");
    expect(rows[0]).toHaveTextContent("12 MG Road, Bengaluru, Karnataka, 560001");
    expect(rows[0]).toHaveTextContent("Verified");
    expect(rows[1]).toHaveTextContent("Not verified");
  });

  it("shows why the test failed and the recorded status", async () => {
    let tested = false;
    mockApi({
      "GET /shiprocket/status": () =>
        json(tested ? { ...STATUS, connection_status: "auth_expired", last_error: "Shiprocket rejected the API user" } : STATUS),
      "POST /shiprocket/test": () => {
        tested = true;
        return json({ detail: "Shiprocket rejected the API user: check SHIPROCKET_EMAIL" }, 502);
      },
    });
    renderAt("/settings/shiprocket");
    await userEvent.click(await screen.findByRole("button", { name: "Test connection" }));
    expect(await screen.findByText(/Connection test failed: Shiprocket rejected the API user/)).toBeInTheDocument();
    expect(await screen.findByText("Credentials rejected")).toBeInTheDocument();
  });

  it("explains a missing configuration and disables the test", async () => {
    mockApi({
      "GET /shiprocket/status": { ...STATUS, configured: false, config_error: "Missing in .env: SHIPROCKET_EMAIL, SHIPROCKET_PASSWORD", api_user: null, environment: "production" },
    });
    renderAt("/settings/shiprocket");
    expect(await screen.findByText("Not configured")).toBeInTheDocument();
    expect(screen.getByRole("alert")).toHaveTextContent("Missing in .env: SHIPROCKET_EMAIL");
    expect(screen.getByRole("button", { name: "Test connection" })).toBeDisabled();
  });

  it("warns when no pickup location is verified", async () => {
    const unverified = { ...TEST, pickup_locations: [{ ...TEST.pickup_locations[1] }] };
    mockApi({ "GET /shiprocket/status": STATUS, "POST /shiprocket/test": unverified });
    renderAt("/settings/shiprocket");
    await userEvent.click(await screen.findByRole("button", { name: "Test connection" }));
    expect(await screen.findByText(/None of the pickup locations is phone-verified/)).toBeInTheDocument();
  });
});
