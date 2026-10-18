import { Route, Routes } from "react-router-dom";
import AppLayout from "./layout/AppLayout";
import OrderDetailPage from "./pages/OrderDetailPage";
import OrdersPage from "./pages/OrdersPage";
import Placeholder from "./pages/Placeholder";

export default function App() {
  return (
    <Routes>
      <Route element={<AppLayout />}>
        <Route index element={<Placeholder title="Dashboard" ticket="#24 (dashboard)" />} />
        <Route path="orders" element={<OrdersPage />} />
        <Route path="orders/:orderId" element={<OrderDetailPage />} />
        <Route path="inventory" element={<Placeholder title="Inventory" ticket="#22 (inventory list)" />} />
        <Route
          path="*"
          element={<Placeholder title="Page not found" ticket="no ticket" backTo={{ to: "/", label: "Home" }} />}
        />
      </Route>
    </Routes>
  );
}
