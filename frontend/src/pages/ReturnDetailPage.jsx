import { useEffect, useState } from "react";
import { Link, useParams } from "react-router-dom";
import { api } from "../api";
import { EmptyState, ErrorMessage, Loading } from "../components/Feedback";
import { capitalize, formatDateTime } from "../format";
import { outstanding, qcLines, returnTypeLabel } from "../returns/returns";

const OPEN = ["open", "received"];

function QcForm({ ret, onSaved }) {
  const lines = ret.lines.filter((line) => outstanding(line) > 0 && line.status !== "cancelled");
  const [values, setValues] = useState({});
  const [note, setNote] = useState("");
  const [errors, setErrors] = useState({});
  const [saving, setSaving] = useState(false);
  const reportsToShopify = ret.return_type === "customer_return";

  function set(lineId, field, value) {
    setValues((current) => ({ ...current, [lineId]: { ...current[lineId], [field]: value } }));
    setErrors({});
  }

  async function save(event) {
    event.preventDefault();
    const checked = qcLines(lines, values);
    setErrors(checked.errors);
    if (Object.keys(checked.errors).length) return;
    setSaving(true);
    try {
      const updated = await api(`/returns/${ret.return_id}/qc`, {
        method: "POST",
        body: { lines: checked.lines, note: note.trim() || null },
      });
      const accepted = checked.lines.reduce((sum, line) => sum + line.accepted, 0);
      const rejected = checked.lines.reduce((sum, line) => sum + line.rejected, 0);
      setValues({});
      setNote("");
      onSaved(updated, {
        tone: "success",
        text:
          `QC saved: ${accepted} back in stock, ${rejected} to damaged.` +
          (reportsToShopify ? " Reported to Shopify." : ""),
      });
    } catch (problem) {
      setErrors({ form: problem.message });
    } finally {
      setSaving(false);
    }
  }

  if (lines.length === 0) return null;
  return (
    <form className="card qc-form" onSubmit={save} noValidate>
      <h2>Quality check</h2>
      <p className="muted small">
        Accepted units go back into sellable stock; rejected ones to damaged.
        {reportsToShopify && " The result is reported to Shopify and can't be changed afterwards."}
      </p>
      {lines.map((line) => (
        <fieldset key={line.return_line_id} className="qc-line">
          <legend>
            {line.product_name}
            {line.variant_name && <span className="muted"> · {line.variant_name}</span>}{" "}
            <span className="muted small">({outstanding(line)} to check)</span>
          </legend>
          {!line.can_report_qc && (
            <p className="form-error small">Shopify has no reverse fulfilment for this line yet; approve the return in Shopify first.</p>
          )}
          <label className="field">
            <span>Accepted</span>
            <input
              inputMode="numeric"
              aria-label={`Accepted for ${line.sku || line.product_name}`}
              value={values[line.return_line_id]?.accepted ?? ""}
              onChange={(event) => set(line.return_line_id, "accepted", event.target.value)}
            />
          </label>
          <label className="field">
            <span>Rejected (damaged)</span>
            <input
              inputMode="numeric"
              aria-label={`Rejected for ${line.sku || line.product_name}`}
              value={values[line.return_line_id]?.rejected ?? ""}
              onChange={(event) => set(line.return_line_id, "rejected", event.target.value)}
            />
          </label>
          {errors[line.return_line_id] && (
            <span className="form-error small" role="alert">
              {errors[line.return_line_id]}
            </span>
          )}
        </fieldset>
      ))}
      <label className="field">
        <span>QC note</span>
        <textarea rows={2} maxLength={500} value={note} onChange={(event) => setNote(event.target.value)} />
      </label>
      {errors.form && (
        <p className="form-error" role="alert">
          {errors.form}
        </p>
      )}
      <button type="submit" className="button" disabled={saving}>
        {saving ? "Saving…" : "Save QC"}
      </button>
    </form>
  );
}

export default function ReturnDetailPage() {
  const { returnId } = useParams();
  const [state, setState] = useState({ key: null, ret: null, error: null });
  const [reloads, setReloads] = useState(0);
  const [notice, setNotice] = useState(null);
  const [busy, setBusy] = useState("");
  const [confirm, setConfirm] = useState(""); // "cancel" | "close"
  const [eta, setEta] = useState("");
  const requestKey = `${returnId}:${reloads}`;

  useEffect(() => {
    const controller = new AbortController();
    api(`/returns/${returnId}`, { signal: controller.signal })
      .then((ret) => !controller.signal.aborted && setState({ key: requestKey, ret, error: null }))
      .catch((error) => !controller.signal.aborted && setState({ key: requestKey, ret: null, error }));
    return () => controller.abort();
  }, [returnId, requestKey]);

  function show(updated, message) {
    setState({ key: requestKey, ret: updated, error: null });
    setNotice(message);
  }

  async function act(name, path, options, success) {
    setBusy(name);
    setNotice(null);
    try {
      show(await api(path, options), { tone: "success", text: success });
    } catch (error) {
      setNotice({ tone: "error", text: error.message });
    } finally {
      setBusy("");
      setConfirm("");
    }
  }

  const back = (
    <Link to="/returns" className="back-link">
      ← Back to returns
    </Link>
  );
  if (state.error?.status === 404) {
    return (
      <section>
        {back}
        <EmptyState title="Return not found">It may have been removed.</EmptyState>
      </section>
    );
  }
  if (state.error) {
    return (
      <section>
        {back}
        <ErrorMessage message={state.error.message} onRetry={() => setReloads((n) => n + 1)} />
      </section>
    );
  }
  if (!state.ret) {
    return (
      <section>
        {back}
        <Loading label="Loading return…" />
      </section>
    );
  }

  const { ret } = state;
  const open = OPEN.includes(ret.status);
  const isRto = ret.return_type === "rto";
  const nothingCheckedYet = ret.lines.every((line) => !line.received_qty && !line.accepted_qty && !line.rejected_qty);

  return (
    <section>
      {back}
      <div className="detail-header">
        <div>
          <h1 className="page-title">
            {returnTypeLabel(ret.return_type)} {ret.return_number}{" "}
            <span className={`badge return-${ret.status}`}>{capitalize(ret.status)}</span>
          </h1>
          <p className="muted">
            Order{" "}
            <Link to={`/orders/${ret.order_header_id}`} className="order-link">
              {ret.order_number}
            </Link>{" "}
            · created {formatDateTime(ret.created_at)}
            {ret.external_status && ` · Shopify: ${capitalize(ret.external_status.toLowerCase())}`}
          </p>
        </div>
        {isRto && open && (
          <div className="actions">
            {nothingCheckedYet && (
              <button type="button" className="button secondary" disabled={Boolean(busy)} onClick={() => setConfirm("cancel")}>
                Cancel RTO
              </button>
            )}
            <button type="button" className="button secondary" disabled={Boolean(busy)} onClick={() => setConfirm("close")}>
              Close (not received)
            </button>
          </div>
        )}
      </div>

      {confirm && (
        <div className="confirm-panel" role="group" aria-label="Confirm">
          <p>
            <strong>
              {confirm === "cancel"
                ? "Cancel this RTO? Its units count as shipped again."
                : "Close this RTO? Units not received are treated as lost in transit."}
            </strong>
          </p>
          <div className="actions">
            <button
              type="button"
              className="button"
              disabled={Boolean(busy)}
              onClick={() =>
                act(confirm, `/returns/${ret.return_id}/${confirm}`, { method: "POST" }, confirm === "cancel" ? "RTO cancelled." : "RTO closed.")
              }
            >
              {busy ? "Saving…" : confirm === "cancel" ? "Yes, cancel the RTO" : "Yes, close it"}
            </button>
            <button type="button" className="button secondary" onClick={() => setConfirm("")}>
              Keep it
            </button>
          </div>
        </div>
      )}
      {notice && (
        <p className={`notice ${notice.tone}`} role="status">
          {notice.text}
        </p>
      )}

      <div className="card">
        <h2>Items</h2>
        <div className="table-wrap flat">
          <table className="data-table compact">
            <thead>
              <tr>
                <th scope="col">Product</th>
                <th scope="col" className="number">Returned</th>
                <th scope="col" className="number">Received</th>
                <th scope="col" className="number">Accepted</th>
                <th scope="col" className="number">Rejected</th>
                <th scope="col" className="number">Still to come</th>
                <th scope="col">Reason</th>
              </tr>
            </thead>
            <tbody>
              {ret.lines.map((line) => (
                <tr key={line.return_line_id}>
                  <td>
                    {line.item_id ? (
                      <Link to={`/inventory/${line.item_id}`} className="order-link">
                        {line.product_name}
                      </Link>
                    ) : (
                      line.product_name
                    )}
                    {line.variant_name && <div className="muted small">{line.variant_name}</div>}
                    <div className="muted small">{line.sku || "No SKU"}</div>
                  </td>
                  <td className="number">{line.return_qty}</td>
                  <td className="number">{line.received_qty}</td>
                  <td className="number">{line.accepted_qty}</td>
                  <td className="number">{line.rejected_qty}</td>
                  <td className="number">
                    {line.pending_qty}
                    {line.missing_qty > 0 && <div className="muted small">{line.missing_qty} missing</div>}
                  </td>
                  <td>
                    {line.return_reason ? capitalize(line.return_reason.toLowerCase()) : "—"}
                    {line.customer_note && <div className="muted small">“{line.customer_note}”</div>}
                    {line.qc_note && <div className="muted small">QC: {line.qc_note}</div>}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
        {ret.lines.some((line) => !line.item_id) && (
          <p className="muted small">Lines not linked to a product are recorded but don&apos;t change stock.</p>
        )}
      </div>

      {open && (
        <div className="return-actions">
          <div className="card">
            <h2>Expected back</h2>
            <p>
              <strong>{ret.expected_at ?? "Not set"}</strong>
            </p>
            <form
              className="inline-form"
              onSubmit={(event) => {
                event.preventDefault();
                if (eta) act("eta", `/returns/${ret.return_id}`, { method: "PATCH", body: { expected_at: eta } }, "Expected date updated.");
              }}
            >
              <label className="field">
                <span>Change to</span>
                <input type="date" value={eta} onChange={(event) => setEta(event.target.value)} />
              </label>
              <button type="submit" className="button secondary" disabled={!eta || Boolean(busy)}>
                Save date
              </button>
            </form>
            {ret.lines.some((line) => line.received_qty < line.return_qty - line.missing_qty) && (
              <button
                type="button"
                className="button secondary"
                disabled={Boolean(busy)}
                onClick={() =>
                  act(
                    "receive",
                    `/returns/${ret.return_id}/receive`,
                    {
                      method: "POST",
                      body: {
                        lines: ret.lines
                          .filter((line) => line.status !== "cancelled")
                          .map((line) => ({ return_line_id: line.return_line_id, received_qty: line.return_qty - line.missing_qty })),
                      },
                    },
                    "Marked as received. Check the units next.",
                  )
                }
              >
                {busy === "receive" ? "Saving…" : "Mark all received"}
              </button>
            )}
          </div>
          <QcForm ret={ret} onSaved={show} />
        </div>
      )}
    </section>
  );
}
