import { useEffect, useState } from "react";
import { Link, useParams } from "react-router-dom";
import { api } from "../api";
import { EmptyState, ErrorMessage, Loading } from "../components/Feedback";
import ProductThumbnail from "../components/ProductThumbnail";
import { formatDateTime } from "../format";
import {
  MANUAL_REASONS,
  STOCK_LABELS,
  adjustmentErrors,
  describeSync,
  quantityChange,
  reasonLabel,
  signed,
  variantLabel,
} from "../inventory/stock";

const EMPTY_FORM = { mode: "add", quantity: "", reason: "", note: "" };

function AdjustForm({ item, onSaved }) {
  const [form, setForm] = useState(EMPTY_FORM);
  const [errors, setErrors] = useState({});
  const [saving, setSaving] = useState(false);
  const [outcome, setOutcome] = useState(null);
  const change = quantityChange(form.mode, form.quantity);

  function update(changes) {
    setForm((current) => ({ ...current, ...changes }));
    setErrors({});
  }

  async function save(event) {
    event.preventDefault();
    const found = adjustmentErrors(form, item.stock_qty);
    setErrors(found);
    if (Object.keys(found).length) return;
    setSaving(true);
    setOutcome(null);
    try {
      const result = await api("/inventory/adjust", {
        method: "POST",
        body: { item_id: item.item_id, quantity_change: change, reason: form.reason, note: form.note.trim() || null },
      });
      const sync = describeSync(result);
      setOutcome({
        tone: sync.tone,
        text: `Stock updated: ${item.stock_qty} → ${result.item.stock_qty}. ${sync.text}`,
      });
      setForm(EMPTY_FORM);
      onSaved(result.item);
    } catch (error) {
      setOutcome({ tone: "error", text: `Not saved: ${error.message}` });
    } finally {
      setSaving(false);
    }
  }

  return (
    <form className="card adjust-form" onSubmit={save} noValidate>
      <h2>Adjust stock</h2>
      {outcome && (
        <p className={`notice ${outcome.tone}`} role="status">
          {outcome.text}
        </p>
      )}
      <div className="segmented" role="radiogroup" aria-label="Add or remove">
        {[
          ["add", "Add"],
          ["remove", "Remove"],
        ].map(([mode, label]) => (
          <button
            key={mode}
            type="button"
            role="radio"
            aria-checked={form.mode === mode}
            className={form.mode === mode ? "active" : undefined}
            onClick={() => update({ mode })}
          >
            {label}
          </button>
        ))}
      </div>
      <label className="field">
        <span>Quantity *</span>
        <input
          inputMode="numeric"
          value={form.quantity}
          onChange={(event) => update({ quantity: event.target.value })}
          aria-invalid={Boolean(errors.quantity)}
        />
        {errors.quantity && (
          <span className="form-error small" role="alert">
            {errors.quantity}
          </span>
        )}
      </label>
      <label className="field">
        <span>Reason *</span>
        <select value={form.reason} onChange={(event) => update({ reason: event.target.value })} aria-invalid={Boolean(errors.reason)}>
          <option value="">Choose a reason</option>
          {MANUAL_REASONS.map((reason) => (
            <option key={reason.value} value={reason.value}>
              {reason.label}
            </option>
          ))}
        </select>
        {errors.reason && (
          <span className="form-error small" role="alert">
            {errors.reason}
          </span>
        )}
      </label>
      <label className="field">
        <span>Note</span>
        <textarea rows={2} maxLength={500} value={form.note} onChange={(event) => update({ note: event.target.value })} />
      </label>
      <p className="preview" aria-live="polite">
        Current <strong>{item.stock_qty}</strong> → New <strong>{change === null ? "—" : item.stock_qty + change}</strong>
      </p>
      <button type="submit" className="button" disabled={saving}>
        {saving ? "Saving…" : "Save adjustment"}
      </button>
    </form>
  );
}

function History({ entries }) {
  if (entries.length === 0) {
    return <EmptyState title="No stock changes yet">Adjustments and orders will show here.</EmptyState>;
  }
  return (
    <div className="table-wrap flat">
      <table className="data-table compact">
        <thead>
          <tr>
            <th scope="col">Date</th>
            <th scope="col" className="number">Change</th>
            <th scope="col" className="number">Stock after</th>
            <th scope="col">Reason</th>
            <th scope="col">Source</th>
            <th scope="col">Note</th>
          </tr>
        </thead>
        <tbody>
          {entries.map((entry) => (
            <tr key={entry.inventory_log_id}>
              <td className="nowrap">{formatDateTime(entry.created_at)}</td>
              <td className={`number ${entry.qty_change > 0 ? "change-up" : "change-down"}`}>{signed(entry.qty_change)}</td>
              <td className="number">{entry.stock_after}</td>
              <td>{reasonLabel(entry.reason)}</td>
              <td>
                {entry.order_header_id ? (
                  <Link to={`/orders/${entry.order_header_id}`} className="order-link">
                    Order {entry.order_number}
                  </Link>
                ) : (
                  "Manual"
                )}
              </td>
              <td className="muted">{entry.order_header_id ? "" : entry.note || ""}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

export default function InventoryItemPage() {
  const { itemId } = useParams();
  const [state, setState] = useState({ key: null, item: null, history: null, error: "" });
  const [reloads, setReloads] = useState(0);
  const requestKey = `${itemId}:${reloads}`;

  useEffect(() => {
    const controller = new AbortController();
    const options = { signal: controller.signal };
    Promise.all([api(`/inventory/${itemId}`, options), api(`/inventory/${itemId}/history`, options)])
      .then(([item, history]) => !controller.signal.aborted && setState({ key: requestKey, item, history, error: "" }))
      .catch((error) => {
        if (!controller.signal.aborted) {
          setState({ key: requestKey, item: null, history: null, error: error.status === 404 ? "404" : error.message });
        }
      });
    return () => controller.abort();
  }, [itemId, requestKey]);

  const back = (
    <Link to="/inventory" className="back-link">
      ← Back to inventory
    </Link>
  );
  if (state.error === "404") {
    return (
      <section>
        {back}
        <EmptyState title="Item not found">It may have been removed.</EmptyState>
      </section>
    );
  }
  if (state.error) {
    return (
      <section>
        {back}
        <ErrorMessage message={state.error} onRetry={() => setReloads((n) => n + 1)} />
      </section>
    );
  }
  if (!state.item) {
    return (
      <section>
        {back}
        <Loading label="Loading item…" />
      </section>
    );
  }

  const { item, history } = state;
  const variant = variantLabel(item.variant_values);
  return (
    <section>
      {back}
      <div className="page-header item-header">
        <ProductThumbnail key={item.image_url} url={item.image_url} name={item.article_name} size={56} />
        <div>
          <h1 className="page-title">
            {item.article_name}
            {variant && <span className="muted"> · {variant}</span>}
          </h1>
          <p className="muted">
            SKU {item.sku}
            {item.barcode && ` · Barcode ${item.barcode}`} ·{" "}
            <Link to={`/products/${item.article_id}`} className="order-link">
              View product
            </Link>
          </p>
        </div>
        <div className="item-stock">
          <strong>{item.stock_qty}</strong>
          <span className={`badge stock-${item.stock_status}`}>{STOCK_LABELS[item.stock_status]}</span>
        </div>
      </div>
      {item.shopify?.last_sync_error && (
        <p className="notice error">Shopify sync pending: {item.shopify.last_sync_error}</p>
      )}

      <div className="inventory-item-grid">
        <AdjustForm item={item} onSaved={() => setReloads((n) => n + 1)} />
        <div className="card">
          <h2>Stock history</h2>
          <History entries={history} />
        </div>
      </div>
    </section>
  );
}
