import { useEffect, useState } from "react";
import { Link, useSearchParams } from "react-router-dom";
import { api } from "../api";
import { EmptyState, ErrorMessage, Loading } from "../components/Feedback";
import ProductThumbnail from "../components/ProductThumbnail";
import {
  STOCK_LABELS,
  inventoryFiltersToParams,
  readInventoryFilters,
  variantLabel,
} from "../inventory/stock";

const PAGE_SIZE = 100;
const SEARCH_DELAY_MS = 300;

// Shopify's side of a row: sync error, drift, or nothing to say.
function ShopifyCell({ row, drift, onPush, pushing }) {
  if (!row.shopify) return <span className="muted small">Not on Shopify</span>;
  if (row.shopify.status !== "live") return <span className="muted small">Disabled on Shopify</span>;
  if (row.shopify.last_sync_error) {
    return (
      <span className="form-error small" title={row.shopify.last_sync_error}>
        Sync failed
      </span>
    );
  }
  if (drift?.state === "drift") {
    return (
      <div>
        <span className="drift-flag">Shopify shows {drift.shopify_qty}</span>
        <button type="button" className="link-button small" disabled={pushing} onClick={() => onPush(row)}>
          {pushing ? "Updating…" : `Set Shopify to ${drift.expected_on_shopify}`}
        </button>
      </div>
    );
  }
  if (drift?.state === "untracked") return <span className="muted small">Not tracked on Shopify</span>;
  if (drift?.state === "missing") return <span className="form-error small">Variant missing on Shopify</span>;
  return <span className="muted small">{drift ? "In sync" : "On Shopify"}</span>;
}

export default function InventoryPage() {
  const [searchParams, setSearchParams] = useSearchParams();
  const { q, low, page } = readInventoryFilters(searchParams);
  const [search, setSearch] = useState(q);
  const [searchedQ, setSearchedQ] = useState(q);
  const [reloads, setReloads] = useState(0);
  const [result, setResult] = useState({ key: null, data: null, error: "" });
  const [driftChecks, setDriftChecks] = useState(0);
  const [drift, setDrift] = useState({ key: null, report: null, items: null, error: "" });
  const driftLoading = drift.key !== driftChecks;
  const [busy, setBusy] = useState(""); // "retry" | "push:<item_id>"
  const [notice, setNotice] = useState(null);
  const requestKey = JSON.stringify([q, low, page, reloads]);
  const loading = result.key !== requestKey;

  if (q !== searchedQ) {
    setSearchedQ(q);
    if (search.trim() !== q) setSearch(q);
  }

  function updateFilters(changes, options) {
    setSearchParams(
      (current) => inventoryFiltersToParams({ ...readInventoryFilters(current), page: 1, ...changes }),
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
    api("/inventory", {
      params: { q, low: low || undefined, limit: PAGE_SIZE, offset: (page - 1) * PAGE_SIZE },
      signal: controller.signal,
    })
      .then((data) => !controller.signal.aborted && setResult({ key: requestKey, data, error: "" }))
      .catch((error) => {
        if (!controller.signal.aborted) setResult({ key: requestKey, data: null, error: error.message });
      });
    return () => controller.abort();
  }, [q, low, page, requestKey]);

  // Shopify's figures are read live, so they load after the list and don't hold it up.
  useEffect(() => {
    const controller = new AbortController();
    api("/inventory/drift", { signal: controller.signal })
      .then((report) => {
        if (!controller.signal.aborted) {
          const items = new Map(report.items.map((row) => [row.item_id, row]));
          setDrift({ key: driftChecks, report, items, error: "" });
        }
      })
      .catch((error) => {
        if (!controller.signal.aborted) setDrift({ key: driftChecks, report: null, items: null, error: error.message });
      });
    return () => controller.abort();
  }, [driftChecks]);

  async function retrySync() {
    setBusy("retry");
    try {
      const outcome = await api("/inventory/sync-pending", { method: "POST" });
      setNotice(
        outcome.failed.length
          ? { tone: "error", text: `${outcome.synced} of ${outcome.retried} updated on Shopify; ${outcome.failed.length} still failing.` }
          : { tone: "success", text: `${outcome.synced} ${outcome.synced === 1 ? "SKU" : "SKUs"} updated on Shopify.` },
      );
      setReloads((n) => n + 1);
      setDriftChecks((n) => n + 1);
    } catch (error) {
      setNotice({ tone: "error", text: error.message });
    } finally {
      setBusy("");
    }
  }

  async function pushToShopify(row) {
    setBusy(`push:${row.item_id}`);
    try {
      const outcome = await api(`/inventory/${row.item_id}/sync`, { method: "POST" });
      setNotice(
        outcome.synced === false
          ? { tone: "error", text: `${row.sku}: Shopify wasn't updated: ${outcome.sync_error}` }
          : { tone: "success", text: `${row.sku}: Shopify now shows ${Math.max(outcome.item.stock_qty, 0)}.` },
      );
      setReloads((n) => n + 1);
      setDriftChecks((n) => n + 1);
    } catch (error) {
      setNotice({ tone: "error", text: error.message });
    } finally {
      setBusy("");
    }
  }

  const { data } = result;
  const rows = data?.items ?? [];
  const failedSyncs = rows.filter((row) => row.shopify?.last_sync_error).length;
  const drifted = drift.report?.mismatched ?? 0;
  const first = data ? (page - 1) * PAGE_SIZE + 1 : 0;
  const last = data ? first + rows.length - 1 : 0;

  return (
    <section>
      <div className="page-header spread">
        <div className="page-header">
          <h1 className="page-title">{low ? "Low stock" : "Inventory"}</h1>
          {data && (
            <p className="muted" aria-live="polite">
              {data.total} {data.total === 1 ? "item" : "items"}
            </p>
          )}
        </div>
        <div className="actions">
          <Link to="/inventory/adjust" className="button link-like">
            Adjust stock
          </Link>
        </div>
      </div>

      {data && (
        <div className="summary-bar" aria-label="Stock summary">
          <div>
            <strong>{data.summary.items}</strong>
            <span className="muted small">Items (variants)</span>
          </div>
          <button type="button" className={`summary-low${low ? " active" : ""}`} onClick={() => updateFilters({ low: !low })} aria-pressed={low}>
            <strong>{data.summary.low_stock}</strong>
            <span className="small">Low stock (≤ 5)</span>
          </button>
          <div className="summary-out">
            <strong>{data.summary.out_of_stock}</strong>
            <span className="small">Out of stock</span>
          </div>
          <div>
            <strong>{driftLoading ? "…" : drift.error ? "—" : drifted}</strong>
            <span className="muted small">
              {driftLoading ? "Checking Shopify…" : drift.error ? "Couldn't check Shopify" : "Differ from Shopify"}
            </span>
            {!driftLoading && (
              <button type="button" className="link-button small" onClick={() => setDriftChecks((n) => n + 1)}>
                Check again
              </button>
            )}
          </div>
        </div>
      )}

      {notice && (
        <p className={`notice ${notice.tone}`} role="status">
          {notice.text}
        </p>
      )}
      {drift.error && !driftLoading && <p className="notice error small">Shopify comparison unavailable: {drift.error}</p>}
      {failedSyncs > 0 && (
        <div className="notice error sync-banner" role="alert">
          <span>
            {failedSyncs} {failedSyncs === 1 ? "SKU's" : "SKUs'"} stock didn't reach Shopify.
          </span>
          <button type="button" className="button secondary" onClick={retrySync} disabled={busy !== ""}>
            {busy === "retry" ? "Retrying…" : "Retry Shopify sync"}
          </button>
        </div>
      )}

      <form className="filters" role="search" onSubmit={(event) => event.preventDefault()}>
        <label className="field search">
          <span>Search</span>
          <input
            type="search"
            placeholder="Product name, SKU or barcode"
            value={search}
            maxLength={100}
            onChange={(event) => setSearch(event.target.value)}
          />
        </label>
        <button type="button" className={`chip${low ? " active" : ""}`} aria-pressed={low} onClick={() => updateFilters({ low: !low })}>
          Show low stock only
        </button>
        {(q || low) && (
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
        <Loading label="Loading stock…" />
      ) : rows.length === 0 ? (
        <EmptyState title={q || low ? "No items match" : "No stock yet"}>
          {q || low ? (
            <span className="muted">Try another search, or clear the filters.</span>
          ) : (
            <Link to="/products" className="link-button">
              Import or add products first
            </Link>
          )}
        </EmptyState>
      ) : (
        <>
          <div className="table-wrap">
            <table className={`data-table${loading ? " is-updating" : ""}`} aria-busy={loading}>
              <thead>
                <tr>
                  <th scope="col">Product</th>
                  <th scope="col">SKU</th>
                  <th scope="col" className="number">Stock</th>
                  <th scope="col">Status</th>
                  <th scope="col">Shopify</th>
                  <th scope="col">
                    <span className="sr-only">Actions</span>
                  </th>
                </tr>
              </thead>
              <tbody>
                {rows.map((row) => {
                  const rowDrift = drift.items?.get(row.item_id);
                  return (
                    <tr key={row.item_id} className={rowDrift?.state === "drift" ? "drift-row" : undefined}>
                      <td>
                        <div className="product-cell">
                          <ProductThumbnail key={row.image_url} url={row.image_url} name={row.article_name} size={40} />
                          <div>
                            <Link to={`/products/${row.article_id}`} className="order-link">
                              {row.article_name}
                            </Link>
                            {variantLabel(row.variant_values) && (
                              <div className="muted small">{variantLabel(row.variant_values)}</div>
                            )}
                          </div>
                        </div>
                      </td>
                      <td>
                        <div>{row.sku || "—"}</div>
                        {row.barcode && <div className="muted small">{row.barcode}</div>}
                      </td>
                      <td className="number">{row.stock_qty}</td>
                      <td>
                        <span className={`badge stock-${row.stock_status}`}>{STOCK_LABELS[row.stock_status]}</span>
                      </td>
                      <td>
                        <ShopifyCell row={row} drift={rowDrift} onPush={pushToShopify} pushing={busy === `push:${row.item_id}`} />
                      </td>
                      <td>
                        <Link to={`/inventory/${row.item_id}`} className="button secondary link-like small-button">
                          Adjust
                        </Link>
                      </td>
                    </tr>
                  );
                })}
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
