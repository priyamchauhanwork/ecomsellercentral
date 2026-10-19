import { useEffect, useState } from "react";
import { Link, useSearchParams } from "react-router-dom";
import { api } from "../api";
import { EmptyState, ErrorMessage, Loading } from "../components/Feedback";
import { capitalize, formatDateTime } from "../format";
import { RETURN_STATUSES, readReturnFilters, returnFiltersToParams, returnTypeLabel } from "../returns/returns";

const PAGE_SIZE = 50;
const SEARCH_DELAY_MS = 300;

// Returns and RTOs (#54): customer returns come from Shopify, RTOs are entered on orders.
export default function ReturnsPage() {
  const [searchParams, setSearchParams] = useSearchParams();
  const { status, type, pending, q, page } = readReturnFilters(searchParams);
  const [search, setSearch] = useState(q);
  const [searchedQ, setSearchedQ] = useState(q);
  const [reloads, setReloads] = useState(0);
  const [result, setResult] = useState({ key: null, data: null, error: "" });
  const [syncing, setSyncing] = useState(false);
  const [notice, setNotice] = useState(null);
  const requestKey = JSON.stringify([status, type, pending, q, page, reloads]);
  const loading = result.key !== requestKey;

  if (q !== searchedQ) {
    setSearchedQ(q);
    if (search.trim() !== q) setSearch(q);
  }

  function updateFilters(changes, options) {
    setSearchParams(
      (current) => returnFiltersToParams({ ...readReturnFilters(current), page: 1, ...changes }),
      options,
    );
  }

  useEffect(() => {
    if (search.trim() === q) return undefined;
    const timer = setTimeout(() => updateFilters({ q: search.trim() }, { replace: true }), SEARCH_DELAY_MS);
    return () => clearTimeout(timer);
  }, [search]); // eslint-disable-line react-hooks/exhaustive-deps

  useEffect(() => {
    const controller = new AbortController();
    api("/returns", {
      params: { status, type, pending: pending || undefined, q, limit: PAGE_SIZE, offset: (page - 1) * PAGE_SIZE },
      signal: controller.signal,
    })
      .then((data) => !controller.signal.aborted && setResult({ key: requestKey, data, error: "" }))
      .catch((error) => !controller.signal.aborted && setResult({ key: requestKey, data: null, error: error.message }));
    return () => controller.abort();
  }, [status, type, pending, q, page, requestKey]);

  async function syncFromShopify() {
    setSyncing(true);
    setNotice(null);
    try {
      const outcome = await api("/returns/sync", { method: "POST" });
      setNotice({
        tone: outcome.errors.length ? "error" : "success",
        text:
          `Read returns of ${outcome.orders} shipped ${outcome.orders === 1 ? "order" : "orders"}; ${outcome.changed_orders} changed.` +
          (outcome.errors.length ? ` ${outcome.errors.length} failed: ${outcome.errors[0]}` : ""),
      });
      setReloads((n) => n + 1);
    } catch (error) {
      setNotice({ tone: "error", text: error.message });
    } finally {
      setSyncing(false);
    }
  }

  const { data } = result;
  const rows = data?.returns ?? [];
  const filtered = Boolean(status || type || pending || q);
  const first = data ? (page - 1) * PAGE_SIZE + 1 : 0;
  const last = data ? first + rows.length - 1 : 0;

  return (
    <section>
      <div className="page-header spread">
        <div className="page-header">
          <h1 className="page-title">{pending ? "Awaiting QC" : type ? returnTypeLabel(type) : "Returns"}</h1>
          {data && (
            <p className="muted" aria-live="polite">
              {data.total} {data.total === 1 ? "return" : "returns"}
            </p>
          )}
        </div>
        <button type="button" className="button secondary" onClick={syncFromShopify} disabled={syncing}>
          {syncing ? "Reading Shopify…" : "Sync from Shopify"}
        </button>
      </div>

      {notice && (
        <p className={`notice ${notice.tone}`} role="status">
          {notice.text}
        </p>
      )}

      <form className="filters" role="search" onSubmit={(event) => event.preventDefault()}>
        <label className="field">
          <span>Status</span>
          <select value={status} onChange={(event) => updateFilters({ status: event.target.value })}>
            {RETURN_STATUSES.map((option) => (
              <option key={option.value} value={option.value}>
                {option.label}
              </option>
            ))}
          </select>
        </label>
        <label className="field">
          <span>Type</span>
          <select value={type} onChange={(event) => updateFilters({ type: event.target.value })}>
            <option value="">All</option>
            <option value="customer_return">Customer returns</option>
            <option value="rto">RTO</option>
          </select>
        </label>
        <label className="field search">
          <span>Search</span>
          <input
            type="search"
            placeholder="Return or order number"
            value={search}
            maxLength={100}
            onChange={(event) => setSearch(event.target.value)}
          />
        </label>
        {filtered && (
          <button
            type="button"
            className="link-button"
            onClick={() => {
              setSearch("");
              setSearchParams({});
            }}
          >
            Clear filters
          </button>
        )}
      </form>

      {result.error && !loading ? (
        <ErrorMessage message={result.error} onRetry={() => setReloads((n) => n + 1)} />
      ) : !data ? (
        <Loading label="Loading returns…" />
      ) : rows.length === 0 ? (
        <EmptyState title={filtered ? "No returns match" : "No returns yet"}>
          <span className="muted">
            {filtered
              ? "Try other filters."
              : "Approved Shopify returns appear here; RTOs are marked from an order's page."}
          </span>
        </EmptyState>
      ) : (
        <>
          <div className="table-wrap">
            <table className={`data-table${loading ? " is-updating" : ""}`} aria-busy={loading}>
              <thead>
                <tr>
                  <th scope="col">Return</th>
                  <th scope="col">Order</th>
                  <th scope="col">Type</th>
                  <th scope="col" className="number">Units</th>
                  <th scope="col" className="number">Still to come</th>
                  <th scope="col">Expected</th>
                  <th scope="col">Status</th>
                </tr>
              </thead>
              <tbody>
                {rows.map((ret) => (
                  <tr key={ret.return_id}>
                    <td>
                      <Link to={`/returns/${ret.return_id}`} className="order-link">
                        {ret.return_number}
                      </Link>
                      <div className="muted small">{formatDateTime(ret.created_at)}</div>
                    </td>
                    <td>
                      <Link to={`/orders/${ret.order_header_id}`} className="order-link">
                        {ret.order_number}
                      </Link>
                    </td>
                    <td>{returnTypeLabel(ret.return_type)}</td>
                    <td className="number">{ret.return_qty}</td>
                    <td className="number">{ret.pending_qty || "—"}</td>
                    <td className="nowrap">{ret.pending_qty ? ret.expected_at ?? "—" : "—"}</td>
                    <td>
                      <span className={`badge return-${ret.status}`}>{capitalize(ret.status)}</span>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          {data.total > PAGE_SIZE && (
            <nav className="pagination" aria-label="Pages">
              <span className="muted">
                {first}–{last} of {data.total}
              </span>
              <button type="button" className="button secondary" disabled={page <= 1} onClick={() => updateFilters({ page: page - 1 })}>
                Previous
              </button>
              <button type="button" className="button secondary" disabled={last >= data.total} onClick={() => updateFilters({ page: page + 1 })}>
                Next
              </button>
            </nav>
          )}
        </>
      )}
    </section>
  );
}
