import { useEffect, useState } from "react";
import { Link, useNavigate, useSearchParams } from "react-router-dom";
import { api } from "../api";
import { EmptyState, ErrorMessage, Loading } from "../components/Feedback";
import { capitalize, formatDateTime, formatMoney } from "../format";
import {
  ORDER_STATUSES,
  SEARCH_MAX_LENGTH,
  dateRangeError,
  filtersToParams,
  hasFilters,
  quickRanges,
  readFilters,
} from "../orders/filters";

const SEARCH_DELAY_MS = 300;

export default function OrdersPage() {
  const [searchParams, setSearchParams] = useSearchParams();
  const filters = readFilters(searchParams);
  const { status, from, to, q } = filters;
  const rangeError = dateRangeError(filters);
  const [search, setSearch] = useState(q);
  const [searchedQ, setSearchedQ] = useState(q);
  const [reloads, setReloads] = useState(0);
  // The last finished request: its key, its data or its error.
  const [result, setResult] = useState({ key: null, data: null, error: "" });
  const requestKey = JSON.stringify([status, from, to, q, reloads]);
  const loading = result.key !== requestKey;
  const navigate = useNavigate();

  // Follow the URL when q changes from outside the box (side bar, Back, Clear), without
  // overwriting what the user is still typing.
  if (q !== searchedQ) {
    setSearchedQ(q);
    if (search.trim() !== q) setSearch(q);
  }

  // Filters live in the URL, so a view can be bookmarked, refreshed or shared.
  function updateFilters(changes, options) {
    setSearchParams((current) => filtersToParams({ ...readFilters(current), ...changes }), options);
  }

  function clearFilters() {
    setSearch("");
    setSearchParams({});
  }

  // Search is typed into local state and written to the URL after a pause, replacing
  // the history entry so Back doesn't step through every keystroke.
  useEffect(() => {
    if (search.trim() === q) return undefined;
    const timer = setTimeout(
      () => updateFilters({ q: search.trim() }, { replace: true }),
      SEARCH_DELAY_MS,
    );
    return () => clearTimeout(timer);
  }, [search]); // eslint-disable-line react-hooks/exhaustive-deps

  useEffect(() => {
    if (rangeError) return undefined;
    const controller = new AbortController(); // drop responses for outdated filters
    api("/orders", { params: { status, from, to, q }, signal: controller.signal })
      .then((data) => {
        if (!controller.signal.aborted) setResult({ key: requestKey, data, error: "" });
      })
      .catch((error) => {
        if (!controller.signal.aborted) {
          setResult({ key: requestKey, data: null, error: error.message });
        }
      });
    return () => controller.abort();
  }, [status, from, to, q, reloads, rangeError, requestKey]);

  // While new results load, the previous ones stay visible (dimmed).
  const { data } = result;
  const title = status ? `${capitalize(status)} orders` : "All orders";

  return (
    <section>
      <div className="page-header">
        <h1 className="page-title">{title}</h1>
        {data && !rangeError && (
          <p className="muted" aria-live="polite">
            {data.total} {data.total === 1 ? "order" : "orders"}
            {data.orders.length < data.total && ` · showing the newest ${data.orders.length}`}
          </p>
        )}
      </div>

      <form className="filters" role="search" onSubmit={(event) => event.preventDefault()}>
        <label className="field">
          <span>Status</span>
          <select value={status} onChange={(event) => updateFilters({ status: event.target.value })}>
            {ORDER_STATUSES.map((option) => (
              <option key={option.value} value={option.value}>
                {option.label}
              </option>
            ))}
          </select>
        </label>
        <label className="field">
          <span>From</span>
          <input
            type="date"
            value={from}
            max={to || undefined}
            onChange={(event) => updateFilters({ from: event.target.value })}
          />
        </label>
        <label className="field">
          <span>To</span>
          <input
            type="date"
            value={to}
            min={from || undefined}
            onChange={(event) => updateFilters({ to: event.target.value })}
          />
        </label>
        <div className="quick-ranges" role="group" aria-label="Quick date ranges">
          {quickRanges().map((range) => {
            const active = range.from === from && range.to === to;
            return (
              <button
                key={range.label}
                type="button"
                className={`chip${active ? " active" : ""}`}
                aria-pressed={active}
                onClick={() => updateFilters({ from: range.from, to: range.to })}
              >
                {range.label}
              </button>
            );
          })}
        </div>
        <label className="field search">
          <span>Search</span>
          <input
            type="search"
            placeholder="Order #, customer, email or phone"
            value={search}
            maxLength={SEARCH_MAX_LENGTH}
            onChange={(event) => setSearch(event.target.value)}
          />
        </label>
        {hasFilters(filters) && (
          <button type="button" className="link-button" onClick={clearFilters}>
            Clear filters
          </button>
        )}
      </form>

      {rangeError ? (
        <ErrorMessage message={rangeError} />
      ) : result.error && !loading ? (
        <ErrorMessage message={result.error} onRetry={() => setReloads((count) => count + 1)} />
      ) : !data ? (
        <Loading label="Loading orders…" />
      ) : data.orders.length === 0 ? (
        <EmptyState title="No orders found">
          {hasFilters(filters) ? (
            <button type="button" className="link-button" onClick={clearFilters}>
              Clear the filters
            </button>
          ) : (
            <span className="muted">Orders imported from Shopify will appear here.</span>
          )}
        </EmptyState>
      ) : (
        <div className="table-wrap">
          <table
            className={`orders-table${loading ? " is-updating" : ""}`}
            aria-busy={loading}
          >
            <thead>
              <tr>
                <th scope="col">Order</th>
                <th scope="col">Date</th>
                <th scope="col">Customer</th>
                <th scope="col" className="number">Items</th>
                <th scope="col" className="number">Total</th>
                <th scope="col">Status</th>
                <th scope="col">Payment</th>
              </tr>
            </thead>
            <tbody>
              {data.orders.map((order) => (
                <tr
                  key={order.order_header_id}
                  onClick={(event) => {
                    if (!event.target.closest("a")) navigate(`/orders/${order.order_header_id}`);
                  }}
                >
                  <td>
                    <Link to={`/orders/${order.order_header_id}`} className="order-link">
                      {order.order_number}
                    </Link>
                  </td>
                  <td className="nowrap">{formatDateTime(order.order_date)}</td>
                  <td>
                    <div>{order.customer_name || "Guest"}</div>
                    {order.customer_email && <div className="muted small">{order.customer_email}</div>}
                  </td>
                  <td className="number">{order.line_count}</td>
                  <td className="number nowrap">{formatMoney(order.total_amount, order.currency)}</td>
                  <td>
                    <span className={`badge badge-${order.order_status}`}>
                      {capitalize(order.order_status)}
                    </span>
                  </td>
                  <td className="muted">{capitalize(order.payment_status)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </section>
  );
}
