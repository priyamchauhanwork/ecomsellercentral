import { useEffect, useState } from "react";
import { Link, useNavigate, useSearchParams } from "react-router-dom";
import { api } from "../api";
import { EmptyState, ErrorMessage, Loading } from "../components/Feedback";
import ProductThumbnail from "../components/ProductThumbnail";
import { capitalize, formatMoney } from "../format";
import {
  CHANNEL_STATUSES,
  PAGE_SIZE,
  PRODUCT_STATUSES,
  SEARCH_MAX_LENGTH,
  SORTS,
  hasProductFilters,
  priceRange,
  productApiParams,
  productFiltersToParams,
  readProductFilters,
} from "../products/filters";

const SEARCH_DELAY_MS = 300;
const money = (amount) => formatMoney(amount, "INR");

function ChannelChips({ channels }) {
  if (channels.length === 0) return <span className="muted small">Not listed</span>;
  return (
    <div className="chips">
      {channels.map((channel) => (
        <span
          key={channel.listing_id}
          className={`channel-chip ${channel.status}`}
          title={`${channel.account_name}: ${channel.status}`}
        >
          {capitalize(channel.channel_code.toLowerCase())} · {channel.status === "live" ? "on sale" : channel.status}
        </span>
      ))}
    </div>
  );
}

function describeImport(summary) {
  const parts = [
    `${summary.products} products read`,
    `${summary.articles_created} new`,
    `${summary.articles_updated} updated`,
  ];
  if (summary.products_deactivated) parts.push(`${summary.products_deactivated} removed on Shopify`);
  if (summary.errors.length) parts.push(`${summary.errors.length} failed`);
  return `Imported from Shopify: ${parts.join(", ")}.`;
}

export default function ProductsPage() {
  const [searchParams, setSearchParams] = useSearchParams();
  const filters = readProductFilters(searchParams);
  const { status, channel, sort, low, q, page } = filters;
  const [search, setSearch] = useState(q);
  const [searchedQ, setSearchedQ] = useState(q);
  const [reloads, setReloads] = useState(0);
  const [result, setResult] = useState({ key: null, data: null, error: "" });
  const [selected, setSelected] = useState(() => new Set());
  const [pendingBulk, setPendingBulk] = useState(null); // "live" | "inactive" awaiting confirmation
  const [busy, setBusy] = useState(""); // "import" | "bulk"
  const [notice, setNotice] = useState(null);
  const navigate = useNavigate();
  const requestKey = JSON.stringify([status, channel, sort, low, q, page, reloads]);
  const loading = result.key !== requestKey;

  if (q !== searchedQ) {
    setSearchedQ(q);
    if (search.trim() !== q) setSearch(q);
  }

  // Any change of filters starts from page 1 (unless the page itself is changing).
  function updateFilters(changes, options) {
    setSearchParams(
      (current) => productFiltersToParams({ ...readProductFilters(current), page: 1, ...changes }),
      options,
    );
  }

  function clearFilters() {
    setSearch("");
    setSearchParams({});
  }

  useEffect(() => {
    if (search.trim() === q) return undefined;
    const timer = setTimeout(() => updateFilters({ q: search.trim() }, { replace: true }), SEARCH_DELAY_MS);
    return () => clearTimeout(timer);
  }, [search]); // eslint-disable-line react-hooks/exhaustive-deps

  useEffect(() => {
    const controller = new AbortController();
    api("/products", { params: productApiParams({ status, channel, sort, low, q, page }), signal: controller.signal })
      .then((data) => {
        if (!controller.signal.aborted) {
          setResult({ key: requestKey, data, error: "" });
          setSelected(new Set());
        }
      })
      .catch((error) => {
        if (!controller.signal.aborted) setResult({ key: requestKey, data: null, error: error.message });
      });
    return () => controller.abort();
  }, [status, channel, sort, low, q, page, requestKey]);

  async function importFromShopify() {
    setBusy("import");
    setNotice(null);
    try {
      const summary = await api("/products/import", { method: "POST" });
      setNotice({ tone: summary.errors.length ? "error" : "success", text: describeImport(summary) });
      setReloads((n) => n + 1);
    } catch (error) {
      setNotice({ tone: "error", text: error.message });
    } finally {
      setBusy("");
    }
  }

  async function applyBulk() {
    const target = pendingBulk;
    setPendingBulk(null);
    setBusy("bulk");
    try {
      const results = await api("/products/status", {
        method: "POST",
        body: { article_ids: [...selected], status: target },
      });
      const failed = results.filter((r) => !r.ok);
      const names = new Map(result.data.products.map((p) => [p.article_id, p.name]));
      setNotice({
        tone: failed.length ? "error" : "success",
        text:
          `${results.length - failed.length} of ${results.length} products ${target === "live" ? "put on sale" : "taken off sale"}.` +
          (failed.length ? ` Not changed: ${failed.map((r) => `${names.get(r.article_id)} (${r.message})`).join("; ")}.` : ""),
      });
      setReloads((n) => n + 1);
    } catch (error) {
      setNotice({ tone: "error", text: error.message });
    } finally {
      setBusy("");
    }
  }

  function toggle(articleId) {
    setSelected((current) => {
      const next = new Set(current);
      if (next.has(articleId)) next.delete(articleId);
      else next.add(articleId);
      return next;
    });
  }

  const { data } = result;
  const products = data?.products ?? [];
  const allSelected = products.length > 0 && products.every((p) => selected.has(p.article_id));
  const title = status ? `${capitalize(status)} products` : low ? "Low stock products" : "All products";
  const detailState = { listSearch: searchParams.size ? `?${searchParams}` : "" };
  const first = data ? (page - 1) * PAGE_SIZE + 1 : 0;
  const last = data ? first + products.length - 1 : 0;

  return (
    <section>
      <div className="page-header spread">
        <div className="page-header">
          <h1 className="page-title">{title}</h1>
          {data && (
            <p className="muted" aria-live="polite">
              {data.total} {data.total === 1 ? "product" : "products"}
            </p>
          )}
        </div>
        <div className="actions">
          <button type="button" className="button secondary" onClick={importFromShopify} disabled={busy !== ""}>
            {busy === "import" ? "Importing…" : "Import from Shopify"}
          </button>
          <Link to="/products/new" className="button link-like">
            New product
          </Link>
        </div>
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
            {PRODUCT_STATUSES.map((option) => (
              <option key={option.value} value={option.value}>
                {option.label}
              </option>
            ))}
          </select>
        </label>
        <label className="field">
          <span>Channels</span>
          <select value={channel} onChange={(event) => updateFilters({ channel: event.target.value })}>
            {CHANNEL_STATUSES.map((option) => (
              <option key={option.value} value={option.value}>
                {option.label}
              </option>
            ))}
          </select>
        </label>
        <label className="field">
          <span>Sort</span>
          <select value={sort} onChange={(event) => updateFilters({ sort: event.target.value })}>
            {SORTS.map((option) => (
              <option key={option.value} value={option.value}>
                {option.label}
              </option>
            ))}
          </select>
        </label>
        <label className="field search">
          <span>Search</span>
          <input
            type="search"
            placeholder="Name, code, brand, SKU or barcode"
            value={search}
            maxLength={SEARCH_MAX_LENGTH}
            onChange={(event) => setSearch(event.target.value)}
          />
        </label>
        <button
          type="button"
          className={`chip${low ? " active" : ""}`}
          aria-pressed={low}
          onClick={() => updateFilters({ low: !low })}
        >
          Low stock
        </button>
        {hasProductFilters(filters) && (
          <button type="button" className="link-button" onClick={clearFilters}>
            Clear filters
          </button>
        )}
      </form>

      {selected.size > 0 && (
        <div className="bulk-bar" role="region" aria-label="Bulk actions">
          <span>{selected.size} selected</span>
          {pendingBulk ? (
            <>
              <strong>
                {pendingBulk === "live" ? "Put" : "Take"} {selected.size} {selected.size === 1 ? "product" : "products"}{" "}
                {pendingBulk === "live" ? "on sale" : "off sale"} on Shopify?
              </strong>
              <button type="button" className="button" onClick={applyBulk}>
                Confirm
              </button>
              <button type="button" className="button secondary" onClick={() => setPendingBulk(null)}>
                Cancel
              </button>
            </>
          ) : (
            <>
              <button type="button" className="button secondary" disabled={busy !== ""} onClick={() => setPendingBulk("live")}>
                Put on sale
              </button>
              <button type="button" className="button secondary" disabled={busy !== ""} onClick={() => setPendingBulk("inactive")}>
                {busy === "bulk" ? "Saving…" : "Take off sale"}
              </button>
              <button type="button" className="link-button" onClick={() => setSelected(new Set())}>
                Clear selection
              </button>
            </>
          )}
        </div>
      )}

      {result.error && !loading ? (
        <ErrorMessage message={result.error} onRetry={() => setReloads((n) => n + 1)} />
      ) : !data ? (
        <Loading label="Loading products…" />
      ) : products.length === 0 ? (
        <EmptyState title="No products found">
          {hasProductFilters(filters) ? (
            <button type="button" className="link-button" onClick={clearFilters}>
              Clear the filters
            </button>
          ) : (
            <>
              <span className="muted">Bring in your Shopify catalogue, or add a product by hand.</span>
              <button type="button" className="button" onClick={importFromShopify} disabled={busy !== ""}>
                {busy === "import" ? "Importing…" : "Import from Shopify"}
              </button>
            </>
          )}
        </EmptyState>
      ) : (
        <>
          <div className="table-wrap">
            <table className={`data-table clickable-rows${loading ? " is-updating" : ""}`} aria-busy={loading}>
              <thead>
                <tr>
                  <th scope="col" className="select-cell">
                    <input
                      type="checkbox"
                      aria-label="Select all on this page"
                      checked={allSelected}
                      onChange={() =>
                        setSelected(allSelected ? new Set() : new Set(products.map((p) => p.article_id)))
                      }
                    />
                  </th>
                  <th scope="col">Product</th>
                  <th scope="col">Brand / category</th>
                  <th scope="col" className="number">Variants</th>
                  <th scope="col" className="number">Stock</th>
                  <th scope="col" className="number">Price</th>
                  <th scope="col">Channels</th>
                  <th scope="col">Status</th>
                </tr>
              </thead>
              <tbody>
                {products.map((product) => (
                  <tr
                    key={product.article_id}
                    onClick={(event) => {
                      if (!event.target.closest("a, input, button")) {
                        navigate(`/products/${product.article_id}`, { state: detailState });
                      }
                    }}
                  >
                    <td className="select-cell">
                      <input
                        type="checkbox"
                        aria-label={`Select ${product.name}`}
                        checked={selected.has(product.article_id)}
                        onChange={() => toggle(product.article_id)}
                      />
                    </td>
                    <td>
                      <div className="product-cell">
                        <ProductThumbnail key={product.image_url} url={product.image_url} name={product.name} size={40} />
                        <div>
                          <Link to={`/products/${product.article_id}`} state={detailState} className="order-link">
                            {product.name}
                          </Link>
                          <div className="muted small">{product.article_code}</div>
                        </div>
                      </div>
                    </td>
                    <td>
                      <div>{product.brand || "—"}</div>
                      {product.category && <div className="muted small">{product.category}</div>}
                    </td>
                    <td className="number">
                      {product.item_count}
                      {product.low_stock_items > 0 && <div className="muted small">{product.low_stock_items} low</div>}
                    </td>
                    <td className="number">{product.total_stock}</td>
                    <td className="number nowrap">{priceRange(product.price_min, product.price_max, money)}</td>
                    <td>
                      <ChannelChips channels={product.channels} />
                    </td>
                    <td>
                      <span className={`badge badge-${product.status}`}>{capitalize(product.status)}</span>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          <nav className="pagination" aria-label="Pages">
            <span className="muted">
              {first}–{last} of {data.total}
            </span>
            <button type="button" className="button secondary" disabled={page <= 1} onClick={() => updateFilters({ page: page - 1 })}>
              Previous
            </button>
            <button
              type="button"
              className="button secondary"
              disabled={last >= data.total}
              onClick={() => updateFilters({ page: page + 1 })}
            >
              Next
            </button>
          </nav>
        </>
      )}
    </section>
  );
}
