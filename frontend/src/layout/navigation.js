// Top bar sections and, for each, the side bar links. Side bar links are views of
// the section; VIEW_PARAMS are the query parameters a view sets.

export const VIEW_PARAMS = ["status", "low", "attention", "type", "pending"];

export const SECTIONS = [
  {
    label: "Dashboard",
    path: "/",
    links: [{ label: "Overview", to: "/" }],
  },
  {
    label: "Orders",
    path: "/orders",
    links: [
      { label: "All orders", to: "/orders" },
      { label: "Pending", to: "/orders?status=pending" },
      { label: "Processing", to: "/orders?status=processing" },
      { label: "Shipped", to: "/orders?status=shipped" },
      { label: "Delivered", to: "/orders?status=delivered" },
      { label: "Cancelled", to: "/orders?status=cancelled" },
      { label: "Needs attention", to: "/orders?attention=true" },
    ],
  },
  {
    label: "Returns",
    path: "/returns",
    links: [
      { label: "All returns", to: "/returns" },
      { label: "Awaiting QC", to: "/returns?pending=true" },
      { label: "Customer returns", to: "/returns?type=customer_return" },
      { label: "RTO", to: "/returns?type=rto" },
    ],
  },
  {
    label: "Products",
    path: "/products",
    links: [
      { label: "All products", to: "/products" },
      { label: "Active", to: "/products?status=active" },
      { label: "Draft", to: "/products?status=draft" },
      { label: "Archived", to: "/products?status=archived" },
      { label: "Low stock", to: "/products?low=true" },
      { label: "New product", to: "/products/new" },
    ],
  },
  {
    label: "Inventory",
    path: "/inventory",
    links: [
      { label: "All stock", to: "/inventory" },
      { label: "Low stock", to: "/inventory?low=true" },
      { label: "Adjust stock", to: "/inventory/adjust" },
    ],
  },
  {
    label: "Settings",
    path: "/settings",
    links: [{ label: "Shiprocket", to: "/settings/shiprocket" }],
  },
];

export function currentSection(pathname) {
  return (
    SECTIONS.find((section) => section.path !== "/" && pathname.startsWith(section.path)) ??
    SECTIONS[0]
  );
}

// A side bar link is active when its path matches and the view parameters agree, so
// "All orders" isn't highlighted while the Pending view is open.
export function isLinkActive(link, location) {
  const target = new URL(link.to, "http://local");
  if (target.pathname !== location.pathname) return false;
  const current = new URLSearchParams(location.search);
  return VIEW_PARAMS.every((key) => target.searchParams.get(key) === current.get(key));
}
