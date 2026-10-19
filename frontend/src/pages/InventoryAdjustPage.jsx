import { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { api } from "../api";
import { ErrorMessage } from "../components/Feedback";
import ProductThumbnail from "../components/ProductThumbnail";
import { variantLabel } from "../inventory/stock";

const SEARCH_DELAY_MS = 300;
const RESULTS = 20;

// Pick the item to adjust when not coming from a row of the inventory list (#23).
export default function InventoryAdjustPage() {
  const [search, setSearch] = useState("");
  const [result, setResult] = useState({ q: null, items: [], error: "" });

  useEffect(() => {
    const q = search.trim();
    const controller = new AbortController();
    const timer = setTimeout(() => {
      api("/inventory", { params: { q, limit: RESULTS }, signal: controller.signal })
        .then((data) => !controller.signal.aborted && setResult({ q, items: data.items, error: "" }))
        .catch((error) => !controller.signal.aborted && setResult({ q, items: [], error: error.message }));
    }, SEARCH_DELAY_MS);
    return () => {
      clearTimeout(timer);
      controller.abort();
    };
  }, [search]);

  return (
    <section>
      <Link to="/inventory" className="back-link">
        ← Back to inventory
      </Link>
      <h1 className="page-title">Adjust stock</h1>
      <p className="muted">Find the item by product name, SKU or barcode.</p>
      <div className="card item-picker">
        <label className="field search">
          <span>Item</span>
          <input
            type="search"
            autoFocus
            placeholder="Search SKU or product"
            value={search}
            maxLength={100}
            onChange={(event) => setSearch(event.target.value)}
          />
        </label>
        {result.error ? (
          <ErrorMessage message={result.error} />
        ) : result.q !== null && result.items.length === 0 ? (
          <p className="muted">No items match “{result.q}”.</p>
        ) : (
          <ul className="picker-results">
            {result.items.map((item) => (
              <li key={item.item_id}>
                <Link to={`/inventory/${item.item_id}`}>
                  <ProductThumbnail key={item.image_url} url={item.image_url} name={item.article_name} size={32} />
                  <span>
                    {item.article_name}
                    {variantLabel(item.variant_values) && <span className="muted"> · {variantLabel(item.variant_values)}</span>}
                    <span className="muted small"> {item.sku}</span>
                  </span>
                  <span className="number">{item.stock_qty} in stock</span>
                </Link>
              </li>
            ))}
          </ul>
        )}
      </div>
    </section>
  );
}
