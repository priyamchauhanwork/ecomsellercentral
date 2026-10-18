import { Route, Routes, useParams } from "react-router-dom";
import AppLayout from "./layout/AppLayout";
import OrdersPage from "./pages/OrdersPage";
import Placeholder from "./pages/Placeholder";

function OrderDetailPlaceholder() {
  const { orderId } = useParams();
  return (
    <Placeholder
      title={`Order ${orderId}`}
      ticket="#17 (order detail)"
      backTo={{ to: "/orders", label: "Back to orders" }}
    />
  );
}

export default function App() {
  return (
    <Routes>
      <Route element={<AppLayout />}>
        <Route index element={<Placeholder title="Dashboard" ticket="#24 (dashboard)" />} />
        <Route path="orders" element={<OrdersPage />} />
        <Route path="orders/:orderId" element={<OrderDetailPlaceholder />} />
        <Route path="inventory" element={<Placeholder title="Inventory" ticket="#22 (inventory list)" />} />
        <Route
          path="*"
          element={<Placeholder title="Page not found" ticket="no ticket" backTo={{ to: "/", label: "Home" }} />}
        />
      </Route>
    </Routes>
  );
}
