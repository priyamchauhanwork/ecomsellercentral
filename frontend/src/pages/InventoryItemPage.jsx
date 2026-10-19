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
  typeLabel,
  variantLabel,
} from "../inventory/stock";

const EMPTY_FORM = { mode: "add", quantity: "", reason: "", note: "" };

// The item's supply and demand (docs/inventory-erd.md), as cards.
function Figures({ item }) {
  const cards = [
    { label: "Available", value: item.available, hint: "On hand − committed; what Shopify shows", main: true },
    { label: "On hand", value: item.onhand, hint: "Incl. shipped units until delivered" },
    { label: "On the shelf", value: item.on_shelf, hint: "On hand − shipped: what a count should find" },
    {
      label: "Committed",
      value: item.committed,
      hint: `${item.open} open · ${item.allocated} allocated · ${item.shipped} shipped`,
    },
    {
      label: "Returning",
      value: item.return_pending,
      hint: item.return_pending ? `Next expected ${item.next_eta ?? "—"}` : "Nothing on its way back",
    },
    { label: "Damaged", value: item.damaged, hint: "Not sellable" },
    { label: "Future available", value: item.future_available, hint: "Available + returning" },
  ];
  return (
    <div className="figure-cards" aria-label="Stock figures">
      {cards.map((card) => (
        <div key={card.label} className={card.main ? "figure main" : "figure"}>
          <span className="small">{card.label}</span>
          <strong>{card.value}</strong>
          <span className="muted small">{card.hint}</span>
        </div>
      ))}
    </div>
  );
}

function AdjustForm({ item, onSaved }) {
  const [form, setForm] = useState(EMPTY_FORM);
  const [errors, setErrors] = useState({});
  const [saving, setSaving] = useState(false);
  const [outcome, setOutcome] = useState(null);
  const change = quantityChange(form.mode, form.quantity);
  const reasons = MANUAL_REASONS.filter((reason) => form.mode === "remove" || !reason.removeOnly);

  function update(changes) {
    setForm((current) => {
      const next = { ...current, ...changes };
      if (next.mode === "add" && next.reason === "damage") next.reason = "";
      return next;
    });
    setErrors({});
  }

  async function save(event) {
    event.preventDefault();
    const found = adjustmentErrors(form, item.on_shelf);
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
        tone: result.warning || sync.tone === "error" ? "error" : "success",
        text: [
          `On hand: ${item.onhand} → ${result.item.onhand}; available ${result.item.available}.`,
          result.warning,
          sync.text,
        ]
          .filter(Boolean)
          .join(" "),
      });
      setForm(EMPTY_FORM);
      onSaved();
    } catch (error) {
      setOutcome({ tone: "error", text: `Not saved: ${error.message}` });
    } finally {
      setSaving(false);
    }
  }

  return (
    <form className="card adjust-form" onSubmit={save} noValidate>
      <h2>Adjust on-hand stock</h2>
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
          {reasons.map((reason) => (
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
        On hand <strong>{item.onhand}</strong> → <strong>{change === null ? "—" : item.onhand + change}</strong> · available{" "}
        <strong>{change === null ? item.available : item.available + change}</strong>
      </p>
      <button type="submit" className="button" disabled={saving}>
        {saving ? "Saving…" : "Save adjustment"}
      </button>
    </form>
  );
}

// Repair (back on hand) or write off damaged units.
function DamagedActions({ item, onSaved }) {
  const [quantity, setQuantity] = useState("");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState("");
  if (item.damaged <= 0) return null;

  async function act(action) {
    const text = quantity.trim();
    if (!/^\d+$/.test(text) || Number(text) === 0 || Number(text) > item.damaged) {
      setError(`Enter a whole number from 1 to ${item.damaged}.`);
      return;
    }
    setBusy(action);
    setError("");
    try {
      await api(`/inventory/${item.item_id}/damaged`, { method: "POST", body: { action, quantity: Number(text) } });
      setQuantity("");
      onSaved();
    } catch (problem) {
      setError(problem.message);
    } finally {
      setBusy("");
    }
  }

  return (
    <div className="card damaged-actions">
      <h2>Damaged units ({item.damaged})</h2>
      <label className="field">
        <span>Quantity</span>
        <input inputMode="numeric" value={quantity} onChange={(event) => setQuantity(event.target.value)} />
      </label>
      {error && (
        <span className="form-error small" role="alert">
          {error}
        </span>
      )}
      <div className="actions">
        <button type="button" className="button secondary" disabled={Boolean(busy)} onClick={() => act("repair")}>
          {busy === "repair" ? "Saving…" : "Repair (back on hand)"}
        </button>
        <button type="button" className="button secondary" disabled={Boolean(busy)} onClick={() => act("write_off")}>
          {busy === "write_off" ? "Saving…" : "Write off"}
        </button>
      </div>
    </div>
  );
}

function Source({ entry }) {
  if (entry.return_id) {
    return (
      <Link to={`/returns/${entry.return_id}`} className="order-link">
        Return {entry.return_number}
      </Link>
    );
  }
  if (entry.order_header_id) {
    return (
      <Link to={`/orders/${entry.order_header_id}`} className="order-link">
        Order {entry.order_number}
      </Link>
    );
  }
  return entry.side === null || entry.reason === "migration" || entry.reason === "opening_stock" ? "System" : "Manual";
}

function History({ entries }) {
  if (entries.length === 0) {
    return <EmptyState title="No stock changes yet">Adjustments, orders and returns will show here.</EmptyState>;
  }
  return (
    <div className="table-wrap flat">
      <table className="data-table compact">
        <thead>
          <tr>
            <th scope="col">Date</th>
            <th scope="col">Figure</th>
            <th scope="col" className="number">Change</th>
            <th scope="col" className="number">After</th>
            <th scope="col">Reason</th>
            <th scope="col">Source</th>
            <th scope="col">Note</th>
          </tr>
        </thead>
        <tbody>
          {entries.map((entry, index) => {
            const sameEvent = index > 0 && entry.transaction_id && entry.transaction_id === entries[index - 1].transaction_id;
            return (
              <tr key={entry.inventory_log_id} className={sameEvent ? "same-event" : undefined}>
                <td className="nowrap">{sameEvent ? "" : formatDateTime(entry.created_at)}</td>
                <td>{typeLabel(entry.inv_type)}</td>
                <td className={`number ${entry.qty_change > 0 ? "change-up" : "change-down"}`}>{signed(entry.qty_change)}</td>
                <td className="number">{entry.stock_after}</td>
                <td>{reasonLabel(entry.reason)}</td>
                <td>
                  <Source entry={entry} />
                </td>
                <td className="muted">{entry.order_header_id || entry.return_id ? "" : entry.note || ""}</td>
              </tr>
            );
          })}
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
  const reload = () => setReloads((n) => n + 1);

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
        <ErrorMessage message={state.error} onRetry={reload} />
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
          <span className={`badge stock-${item.stock_status}`}>{STOCK_LABELS[item.stock_status]}</span>
        </div>
      </div>
      {item.shopify?.last_sync_error && <p className="notice error">Shopify sync pending: {item.shopify.last_sync_error}</p>}
      {item.available < 0 && (
        <p className="notice error">Oversold: orders need {-item.available} more than you have on hand.</p>
      )}

      <Figures item={item} />

      <div className="inventory-item-grid">
        <div className="stack">
          <AdjustForm item={item} onSaved={reload} />
          <DamagedActions item={item} onSaved={reload} />
        </div>
        <div className="card">
          <h2>Stock history</h2>
          <History entries={history} />
        </div>
      </div>
    </section>
  );
}
