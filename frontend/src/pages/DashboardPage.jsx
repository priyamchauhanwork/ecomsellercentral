import { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { api } from "../api";
import { EmptyState, ErrorMessage, Loading } from "../components/Feedback";
import ProductThumbnail from "../components/ProductThumbnail";
import { capitalize, formatDateTime, formatMoney } from "../format";
import { STOCK_LABELS, variantLabel } from "../inventory/stock";

function StatCard({ label, value, detail, to, tone }) {
  const body = (
    <>
      <span className="small">{label}</span>
      <strong>{value}</strong>
      {detail && <span className="muted small">{detail}</span>}
    </>
  );
  const className = `stat-card${tone ? ` ${tone}` : ""}`;
  return to ? (
    <Link to={to} className={`${className} link-card`}>
      {body}
    </Link>
  ) : (
    <div className={className}>{body}</div>
  );
}

function plural(count, one, many = `${one}s`) {
  return `${count.toLocaleString("en-IN")} ${count === 1 ? one : many}`;
}

// Home page (#24): today's and all-time orders and revenue, stock alerts, recent orders.
// Figures come from /reports/dashboard; "today" is the India (IST) calendar day.
export default function DashboardPage() {
  const [reloads, setReloads] = useState(0);
  const [result, setResult] = useState({ key: null, data: null, error: "" });
  const loading = result.key !== reloads;

  useEffect(() => {
    const controller = new AbortController();
    api("/reports/dashboard", { signal: controller.signal })
      .then((data) => !controller.signal.aborted && setResult({ key: reloads, data, error: "" }))
      .catch((error) => {
        // A failed refresh keeps the last figures and says so.
        if (!controller.signal.aborted) setResult((current) => ({ key: reloads, data: current.data, error: error.message }));
      });
    return () => controller.abort();
  }, [reloads]);

  const { data, error } = result;
  const refresh = (
    <button type="button" className="button secondary" onClick={() => setReloads((n) => n + 1)} disabled={loading}>
      {loading && data ? "Refreshing…" : "Refresh"}
    </button>
  );

  if (!data) {
    return (
      <section>
        <div className="page-header spread">
          <h1 className="page-title">Dashboard</h1>
        </div>
        {error && !loading ? (
          <ErrorMessage message={error} onRetry={() => setReloads((n) => n + 1)} />
        ) : (
          <Loading label="Loading dashboard…" />
        )}
      </section>
    );
  }

  const { orders, stock } = data;
  const money = (amount) => formatMoney(amount, data.currency);
  const lowOrOut = stock.low_stock + stock.out_of_stock;
  const revenueDetail = (refunded, extra) =>
    [Number(refunded) > 0 && `${money(refunded)} refunded`, extra].filter(Boolean).join(" · ") || "Excl. cancelled orders";

  return (
    <section className="dashboard">
      <div className="page-header spread">
        <div className="page-header">
          <h1 className="page-title">Dashboard</h1>
          <p className="muted" aria-live="polite">
            Last updated {formatDateTime(data.generated_at)} IST
          </p>
        </div>
        {refresh}
      </div>
      {error && !loading && (
        <p className="notice error" role="alert">
          Couldn&apos;t refresh: {error}. Showing the figures from {formatDateTime(data.generated_at)}.
        </p>
      )}

      <div className={`stat-grid${loading ? " is-updating" : ""}`} aria-label="Key figures" aria-busy={loading}>
        <StatCard label="Orders today" value={orders.orders_today.toLocaleString("en-IN")} detail={`India time, ${data.today}`} to="/orders" />
        <StatCard label="Revenue today" value={money(orders.revenue_today)} detail={revenueDetail(orders.refunded_today)} />
        <StatCard
          label="Total orders"
          value={orders.orders_total.toLocaleString("en-IN")}
          detail={orders.cancelled_total ? `${plural(orders.cancelled_total, "cancelled", "cancelled")}` : "All time"}
          to="/orders"
        />
        <StatCard
          label="Total revenue"
          value={money(orders.revenue_total)}
          detail={revenueDetail(
            orders.refunded_total,
            orders.other_currency_orders > 0 &&
              `${plural(orders.other_currency_orders, "order")} in other currencies not included`,
          )}
        />
        <StatCard
          label="Low stock items"
          value={lowOrOut.toLocaleString("en-IN")}
          detail={`${stock.low_stock} low · ${stock.out_of_stock} out (≤ ${data.low_stock_threshold})`}
          to="/inventory?low=true"
          tone={lowOrOut ? "warning" : undefined}
        />
        <StatCard label="Total products" value={plural(stock.skus, "SKU")} detail={plural(stock.products, "product")} to="/inventory" />
        <StatCard
          label="Needs attention"
          value={orders.needs_attention.toLocaleString("en-IN")}
          detail="Orders changed after acknowledgement"
          to="/orders?attention=true"
          tone={orders.needs_attention ? "danger" : undefined}
        />
        <StatCard
          label="Returns awaiting QC"
          value={data.returns_awaiting_qc.toLocaleString("en-IN")}
          detail="Open or received returns and RTOs"
          to="/returns?pending=true"
        />
      </div>

      <div className="dashboard-grid">
        <div className="card">
          <div className="card-header">
            <h2>Recent orders</h2>
            <Link to="/orders" className="link-button">
              View all
            </Link>
          </div>
          {data.recent_orders.length === 0 ? (
            <EmptyState title="No orders yet">
              <Link to="/orders" className="link-button">
                Go to orders
              </Link>
            </EmptyState>
          ) : (
            <ul className="dashboard-list" aria-label="Recent orders">
              {data.recent_orders.map((order) => (
                <li key={order.order_header_id}>
                  <div>
                    <Link to={`/orders/${order.order_header_id}`} className="order-link">
                      {order.order_number}
                    </Link>
                    <div className="muted small">
                      {order.customer_name || "Guest"} · {formatDateTime(order.order_date)} · {plural(order.line_count, "item")}
                    </div>
                  </div>
                  <div className="end">
                    <strong>{formatMoney(order.total_amount, order.currency)}</strong>
                    <div>
                      <span className={`badge badge-${order.order_status}`}>{capitalize(order.order_status)}</span>
                      {order.attention_reason && <span className="badge badge-attention">Needs attention</span>}
                    </div>
                  </div>
                </li>
              ))}
            </ul>
          )}
        </div>

        <div className="card">
          <div className="card-header">
            <h2>Low stock alert</h2>
            <Link to="/inventory?low=true" className="link-button">
              View all
            </Link>
          </div>
          {data.low_stock_items.length === 0 ? (
            <EmptyState title="All stocked up">
              <span className="muted">Every item has more than {data.low_stock_threshold} available.</span>
            </EmptyState>
          ) : (
            <ul className="dashboard-list" aria-label="Low stock items">
              {data.low_stock_items.map((item) => (
                <li key={item.item_id}>
                  <div className="product-cell">
                    <ProductThumbnail key={item.image_url} url={item.image_url} name={item.article_name} size={36} />
                    <div>
                      <Link to={`/inventory/${item.item_id}`} className="order-link">
                        {item.article_name}
                      </Link>
                      <div className="muted small">
                        {[variantLabel(item.variant_values), item.sku].filter(Boolean).join(" · ")}
                      </div>
                    </div>
                  </div>
                  <div className="end">
                    <strong>{item.available}</strong>
                    <span className={`badge stock-${item.stock_status}`}>{STOCK_LABELS[item.stock_status]}</span>
                  </div>
                </li>
              ))}
            </ul>
          )}
        </div>
      </div>
    </section>
  );
}
