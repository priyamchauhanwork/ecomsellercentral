import { Route, Routes } from "react-router-dom";
import AppLayout from "./layout/AppLayout";
import DashboardPage from "./pages/DashboardPage";
import InventoryAdjustPage from "./pages/InventoryAdjustPage";
import InventoryItemPage from "./pages/InventoryItemPage";
import InventoryPage from "./pages/InventoryPage";
import OrderDetailPage from "./pages/OrderDetailPage";
import OrdersPage from "./pages/OrdersPage";
import ProductCreatePage from "./pages/ProductCreatePage";
import ProductDetailPage from "./pages/ProductDetailPage";
import ProductsPage from "./pages/ProductsPage";
import ReturnDetailPage from "./pages/ReturnDetailPage";
import ReturnsPage from "./pages/ReturnsPage";
import Placeholder from "./pages/Placeholder";

export default function App() {
  return (
    <Routes>
      <Route element={<AppLayout />}>
        <Route index element={<DashboardPage />} />
        <Route path="orders" element={<OrdersPage />} />
        <Route path="orders/:orderId" element={<OrderDetailPage />} />
        <Route path="products" element={<ProductsPage />} />
        <Route path="products/new" element={<ProductCreatePage />} />
        <Route path="products/:productId" element={<ProductDetailPage />} />
        <Route path="inventory" element={<InventoryPage />} />
        <Route path="inventory/adjust" element={<InventoryAdjustPage />} />
        <Route path="inventory/:itemId" element={<InventoryItemPage />} />
        <Route path="returns" element={<ReturnsPage />} />
        <Route path="returns/:returnId" element={<ReturnDetailPage />} />
        <Route
          path="*"
          element={<Placeholder title="Page not found" ticket="no ticket" backTo={{ to: "/", label: "Home" }} />}
        />
      </Route>
    </Routes>
  );
}
