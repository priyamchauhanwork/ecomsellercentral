import { useState } from "react";
import { api } from "../api";

// Mark shipped, undelivered units as RTO (coming back to the seller, #55). One quantity
// per line, at most what was shipped and neither delivered nor already RTO'd.
export default function RtoPanel({ order, maxFor, onSaved, onCancel }) {
  const eligible = order.lines.filter((line) => maxFor(line) > 0);
  const [quantities, setQuantities] = useState(() =>
    Object.fromEntries(eligible.map((line) => [line.order_line_id, String(maxFor(line))])),
  );
  const [note, setNote] = useState("");
  const [error, setError] = useState("");
  const [saving, setSaving] = useState(false);

  async function save(event) {
    event.preventDefault();
    const lines = [];
    for (const line of eligible) {
      const text = (quantities[line.order_line_id] ?? "").trim();
      if (text === "" || text === "0") continue;
      if (!/^\d+$/.test(text) || Number(text) > maxFor(line)) {
        setError(`${line.sku || line.product_name}: enter a whole number from 0 to ${maxFor(line)}.`);
        return;
      }
      lines.push({ order_line_id: line.order_line_id, quantity: Number(text) });
    }
    if (lines.length === 0) {
      setError("Enter at least one unit.");
      return;
    }
    setSaving(true);
    setError("");
    try {
      onSaved(await api(`/orders/${order.order_header_id}/rto`, { method: "POST", body: { lines, note: note.trim() || null } }));
    } catch (problem) {
      setError(problem.message);
      setSaving(false);
    }
  }

  return (
    <form className="confirm-panel" aria-label="Mark RTO" onSubmit={save} noValidate>
      <p>
        <strong>Mark RTO for order {order.order_number}</strong>
      </p>
      <p className="muted">
        Shipped units that weren&apos;t delivered and are coming back. They leave stock now and wait as
        &quot;returning&quot; until you receive and check them on the return.
      </p>
      {eligible.map((line) => (
        <label key={line.order_line_id} className="field inline-field">
          <span>
            {line.product_name}
            {line.variant_name && ` · ${line.variant_name}`} <span className="muted small">(max {maxFor(line)})</span>
          </span>
          <input
            inputMode="numeric"
            aria-label={`RTO quantity for ${line.sku || line.product_name}`}
            value={quantities[line.order_line_id] ?? ""}
            onChange={(event) => setQuantities({ ...quantities, [line.order_line_id]: event.target.value })}
          />
        </label>
      ))}
      <label className="field">
        <span>Note (optional)</span>
        <textarea rows={2} maxLength={500} value={note} onChange={(event) => setNote(event.target.value)} placeholder="e.g. Customer refused delivery" />
      </label>
      {error && (
        <p className="form-error" role="alert">
          {error}
        </p>
      )}
      <div className="actions">
        <button type="submit" className="button" disabled={saving}>
          {saving ? "Saving…" : "Create RTO"}
        </button>
        <button type="button" className="button secondary" disabled={saving} onClick={onCancel}>
          Cancel
        </button>
      </div>
    </form>
  );
}
