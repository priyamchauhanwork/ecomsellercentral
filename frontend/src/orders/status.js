// The next manual status for each order status, matching the backend's allowed moves
// (pending → shipped → delivered). Delivered and cancelled orders have none.
export const NEXT_STATUS = {
  pending: { status: "shipped", label: "Mark shipped" },
  shipped: { status: "delivered", label: "Mark delivered" },
};
