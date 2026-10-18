import { useEffect, useRef, useState } from "react";
import { api } from "../api";

const UNCHANGED_LINE_STATUSES = ["cancelled", "returned"];

// In-page confirmation for a manual status change. On success, `onSaved` gets the
// updated order from the API; on a 409 (the order changed meanwhile), `onConflict` gets
// the backend's explanation.
export default function StatusChangePanel({ order, change, onSaved, onCancel, onConflict }) {
  const [remarks, setRemarks] = useState("");
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState("");
  const confirmButton = useRef(null);

  useEffect(() => confirmButton.current?.focus(), []);

  const affectedItems = order.lines.filter(
    (line) => ![...UNCHANGED_LINE_STATUSES, change.status].includes(line.line_status),
  ).length;

  async function confirm(event) {
    event.preventDefault();
    setSaving(true);
    setError("");
    try {
      const updated = await api(`/orders/${order.order_header_id}/status`, {
        method: "POST",
        body: { status: change.status, remarks: remarks.trim() || null },
      });
      onSaved(updated);
    } catch (problem) {
      if (problem.status === 409) {
        onConflict(problem.message);
        return;
      }
      setError(problem.message);
      setSaving(false);
    }
  }

  return (
    <form
      className="confirm-panel"
      aria-label={`${change.label} confirmation`}
      onSubmit={confirm}
      onKeyDown={(event) => {
        if (event.key === "Escape" && !saving) onCancel();
      }}
    >
      <p>
        <strong>
          {change.label} for order {order.order_number}?
        </strong>
      </p>
      <p className="muted">
        {affectedItems} {affectedItems === 1 ? "item" : "items"} will be marked {change.status}.
        This updates SellerCentral only: Shopify isn&apos;t changed, so fulfil the order there too.
      </p>
      <label className="field">
        <span>Note for the timeline (optional)</span>
        <textarea
          rows={2}
          maxLength={500}
          value={remarks}
          disabled={saving}
          placeholder="e.g. Packed by Ravi, courier pickup at 4 pm"
          onChange={(event) => setRemarks(event.target.value)}
        />
      </label>
      {error && (
        <p className="form-error" role="alert">
          {error}
        </p>
      )}
      <div className="actions">
        <button ref={confirmButton} type="submit" className="button" disabled={saving}>
          {saving ? "Saving…" : `Yes, ${change.label.toLowerCase()}`}
        </button>
        <button type="button" className="button secondary" disabled={saving} onClick={onCancel}>
          Cancel
        </button>
      </div>
    </form>
  );
}
