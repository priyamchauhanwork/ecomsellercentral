import { useEffect, useState } from "react";
import { Link, useLocation, useParams } from "react-router-dom";
import { api } from "../api";
import { EmptyState, ErrorMessage, Loading } from "../components/Feedback";
import ProductThumbnail from "../components/ProductThumbnail";
import { capitalize, formatDateTime, formatMoney } from "../format";
import { describeLineStock } from "../inventory/stock";
import { describeChange, describeSource } from "../orders/history";
import RtoPanel from "../orders/RtoPanel";
import StatusChangePanel from "../orders/StatusChangePanel";
import { NEXT_STATUSES, attentionText } from "../orders/status";

// Document buttons, shown now so the layout is final; each is wired by its own ticket.
const DOCUMENT_ACTIONS = [
  { label: "Invoice", ticket: "#25" },
  { label: "Packing slip", ticket: "#26" },
  { label: "Label", ticket: "#27" },
];

function Address({ order, prefix }) {
  const part = (name) => order[`${prefix}_${name}`];
  const cityLine = [part("city"), [part("state"), part("pincode")].filter(Boolean).join(" ")]
    .filter(Boolean)
    .join(", ");
  const lines = [
    part("name"),
    part("address_line1"),
    part("address_line2"),
    part("landmark"),
    cityLine,
    part("country"),
    part("phone"),
  ].filter(Boolean);
  if (lines.length === 0) return <p className="muted">No address</p>;
  return (
    <address>
      {lines.map((line, index) => (
        <div key={index}>{line}</div>
      ))}
    </address>
  );
}

function Detail({ label, children }) {
  return (
    <>
      <dt>{label}</dt>
      <dd>{children || "—"}</dd>
    </>
  );
}

// Shipped units that haven't been delivered or sent back yet: what an RTO can cover.
function rtoMax(line) {
  return line.shipped_qty - line.delivered_qty - (line.stock?.rto ?? 0);
}

function Quantity({ line }) {
  const notes = [
    line.shipped_qty > 0 && `${line.shipped_qty} shipped`,
    line.delivered_qty > 0 && `${line.delivered_qty} delivered`,
    line.cancelled_qty > 0 && `${line.cancelled_qty} cancelled`,
    line.returned_qty > 0 && `${line.returned_qty} returned`,
  ].filter(Boolean);
  return (
    <>
      {line.ordered_qty}
      {notes.map((note) => (
        <div key={note} className="muted small">
          {note}
        </div>
      ))}
      {line.stock && (
        <div className="muted small">
          {line.stock.item_id ? (
            <Link to={`/inventory/${line.stock.item_id}`} title="Stock history">
              {describeLineStock(line.stock)}
            </Link>
          ) : (
            describeLineStock(line.stock)
          )}
        </div>
      )}
    </>
  );
}

export default function OrderDetailPage() {
  const { orderId } = useParams();
  const location = useLocation();
  // The list passes its filters along, so "Back" returns to the same view.
  const backTo = `/orders${location.state?.listSearch ?? ""}`;
  const validId = /^\d+$/.test(orderId);
  const [reloads, setReloads] = useState(0);
  const [result, setResult] = useState({ key: null, order: null, error: null });
  const requestKey = `${orderId}:${reloads}`;
  const loading = validId && result.key !== requestKey;
  const [confirming, setConfirming] = useState(null); // the status change being confirmed
  const [markingRto, setMarkingRto] = useState(false);
  const [notice, setNotice] = useState(null); // { text, tone: "success" | "error" }

  // Opening another order starts without a pending confirmation or old message.
  const [shownOrderId, setShownOrderId] = useState(orderId);
  if (shownOrderId !== orderId) {
    setShownOrderId(orderId);
    setConfirming(null);
    setMarkingRto(false);
    setNotice(null);
  }

  useEffect(() => {
    if (!validId) return undefined;
    const controller = new AbortController();
    api(`/orders/${orderId}`, { signal: controller.signal })
      .then((order) => {
        if (!controller.signal.aborted) setResult({ key: requestKey, order, error: null });
      })
      .catch((error) => {
        if (!controller.signal.aborted) setResult({ key: requestKey, order: null, error });
      });
    return () => controller.abort();
  }, [orderId, validId, requestKey]);


  async function dismissAttention() {
    try {
      const updated = await api(`/orders/${orderId}/attention/clear`, { method: "POST" });
      setResult({ key: requestKey, order: updated, error: null });
      setNotice({ text: "Flag dismissed.", tone: "success" });
    } catch (error) {
      setNotice({ text: error.message, tone: "error" });
    }
  }

  const back = (
    <Link to={backTo} className="back-link">
      ← Back to orders
    </Link>
  );

  if (!validId || (!loading && result.error?.status === 404)) {
    return (
      <section>
        {back}
        <EmptyState title="Order not found">
          <span className="muted">It may have been deleted, or the link is wrong.</span>
        </EmptyState>
      </section>
    );
  }
  if (loading) {
    return (
      <section>
        {back}
        <Loading label="Loading order…" />
      </section>
    );
  }
  if (result.error) {
    return (
      <section>
        {back}
        <ErrorMessage message={result.error.message} onRetry={() => setReloads((n) => n + 1)} />
      </section>
    );
  }

  const { order } = result;
  const nextStatuses = NEXT_STATUSES[order.order_status] ?? [];
  const canRto = order.lines.some((line) => rtoMax(line) > 0);
  const money = (amount) => formatMoney(amount, order.currency);
  const lines = [...order.lines].sort((a, b) => a.line_number - b.line_number);

  return (
    <section className="order-detail">
      {back}

      <div className="detail-header">
        <div>
          <h1 className="page-title">
            Order {order.order_number}{" "}
            <span className={`badge badge-${order.order_status}`}>
              {capitalize(order.order_status)}
            </span>
          </h1>
          <p className="muted">
            Placed {formatDateTime(order.order_date)} · {capitalize(order.channel)}
          </p>
        </div>
        <div className="actions" role="group" aria-label="Order actions">
          {nextStatuses.map((change, index) => (
            <button
              key={change.status}
              type="button"
              className={index === 0 ? "button" : "button secondary"}
              disabled={Boolean(confirming) || markingRto}
              onClick={() => {
                setNotice(null);
                setConfirming(change);
              }}
            >
              {change.label}
            </button>
          ))}
          {canRto && (
            <button
              type="button"
              className="button secondary"
              disabled={Boolean(confirming) || markingRto}
              onClick={() => {
                setNotice(null);
                setMarkingRto(true);
              }}
            >
              Mark RTO
            </button>
          )}
          {DOCUMENT_ACTIONS.map((action) => (
            <button
              key={action.label}
              type="button"
              className="button secondary"
              disabled
              title={`Available with ${action.ticket}`}
            >
              {action.label}
            </button>
          ))}
        </div>
      </div>

      {order.attention_reason && (
        <div className="notice error attention-banner" role="alert">
          <span>
            <strong>Needs attention.</strong> {attentionText(order.attention_reason)}
          </span>
          <button type="button" className="button secondary" onClick={dismissAttention}>
            Dismiss
          </button>
        </div>
      )}

      {markingRto && (
        <RtoPanel
          order={order}
          maxFor={rtoMax}
          onCancel={() => setMarkingRto(false)}
          onSaved={(created) => {
            setMarkingRto(false);
            setNotice({
              text: `RTO ${created.return_number} created; the units are expected back by ${created.expected_at}.`,
              tone: "success",
            });
            setReloads((n) => n + 1);
          }}
        />
      )}

      {confirming && (
        <StatusChangePanel
          order={order}
          change={confirming}
          onCancel={() => setConfirming(null)}
          onSaved={(updated) => {
            // The API returns the updated order: status, lines and timeline refresh in place.
            setResult({ key: requestKey, order: updated, error: null });
            setConfirming(null);
            setNotice({ text: `Order marked as ${updated.order_status}.`, tone: "success" });
          }}
          onConflict={(message) => {
            setConfirming(null);
            setNotice({ text: `${message} The order has been reloaded.`, tone: "error" });
            setReloads((n) => n + 1);
          }}
        />
      )}
      {notice && (
        <p className={`notice ${notice.tone}`} role="status">
          {notice.text}
        </p>
      )}

      <div className="detail-grid">
        <div className="card">
          <h2>Customer</h2>
          <dl className="details">
            <Detail label="Name">{order.customer_name || "Guest"}</Detail>
            <Detail label="Email">
              {order.customer_email && (
                <a href={`mailto:${order.customer_email}`}>{order.customer_email}</a>
              )}
            </Detail>
            <Detail label="Phone">
              {order.customer_phone && <a href={`tel:${order.customer_phone}`}>{order.customer_phone}</a>}
            </Detail>
          </dl>
        </div>

        <div className="card">
          <h2>Shipping address</h2>
          <Address order={order} prefix="ship" />
        </div>

        <div className="card">
          <h2>Billing address</h2>
          {order.bill_same_as_ship ? (
            <p className="muted">Same as shipping address</p>
          ) : (
            <Address order={order} prefix="bill" />
          )}
          {order.bill_gstin && <p className="small">GSTIN: {order.bill_gstin}</p>}
        </div>

        <div className="card">
          <h2>Order</h2>
          <dl className="details">
            <Detail label="Payment">{capitalize(order.payment_status)}</Detail>
            <Detail label="Fulfilment">{capitalize(order.fulfillment_status)}</Detail>
            <Detail label="Paid via">{order.payment_gateway}</Detail>
            {order.paid_at && <Detail label="Paid on">{formatDateTime(order.paid_at)}</Detail>}
            {order.cancelled_at && (
              <Detail label="Cancelled">
                {formatDateTime(order.cancelled_at)}
                {order.cancel_reason && ` · ${order.cancel_reason}`}
              </Detail>
            )}
          </dl>
        </div>
      </div>

      <div className="card">
        <h2>Items ({lines.length})</h2>
        <div className="table-wrap flat">
          <table className="data-table">
            <thead>
              <tr>
                <th scope="col">Product</th>
                <th scope="col">SKU</th>
                <th scope="col" className="number">Qty</th>
                <th scope="col" className="number">Unit price</th>
                <th scope="col" className="number">Line total</th>
                <th scope="col">Status</th>
              </tr>
            </thead>
            <tbody>
              {lines.map((line) => (
                <tr key={line.order_line_id}>
                  <td>
                    <div className="product-cell">
                      <ProductThumbnail url={line.image_url} name={line.product_name} />
                      <div>
                        <div>{line.product_name}</div>
                        {line.variant_name && (
                          <div className="muted small">{line.variant_name}</div>
                        )}
                      </div>
                    </div>
                  </td>
                  <td className="muted">{line.sku || "—"}</td>
                  <td className="number">
                    <Quantity line={line} />
                  </td>
                  <td className="number nowrap">
                    {money(line.unit_price)}
                    {Number(line.line_discount_amount) > 0 && (
                      <div className="muted small">−{money(line.line_discount_amount)} discount</div>
                    )}
                  </td>
                  <td className="number nowrap">{money(line.line_total_amount)}</td>
                  <td>
                    <span className={`badge badge-${line.line_status}`}>
                      {capitalize(line.line_status)}
                    </span>
                    {line.tracking_number && (
                      <div className="muted small">
                        {[line.courier_name, line.tracking_number].filter(Boolean).join(" · ")}
                      </div>
                    )}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>

        <dl className="totals">
          <dt>Subtotal</dt>
          <dd>{money(order.subtotal_amount)}</dd>
          {Number(order.discount_amount) > 0 && (
            <>
              <dt>Discount{order.coupon_code && ` (${order.coupon_code})`}</dt>
              <dd>−{money(order.discount_amount)}</dd>
            </>
          )}
          <dt>Shipping</dt>
          <dd>{money(order.shipping_amount)}</dd>
          <dt>Tax</dt>
          <dd>{money(order.tax_amount)}</dd>
          <dt className="total">Total</dt>
          <dd className="total">{money(order.total_amount)}</dd>
          <dt>Paid</dt>
          <dd>{money(order.paid_amount)}</dd>
          {Number(order.refunded_amount) > 0 && (
            <>
              <dt>Refunded</dt>
              <dd>−{money(order.refunded_amount)}</dd>
            </>
          )}
        </dl>
      </div>

      {order.customer_notes && (
        <div className="card">
          <h2>Customer notes</h2>
          <p className="notes">{order.customer_notes}</p>
        </div>
      )}

      {order.returns?.length > 0 && (
        <div className="card">
          <h2>Returns ({order.returns.length})</h2>
          <ul className="plain-list">
            {order.returns.map((ret) => (
              <li key={ret.return_id}>
                <Link to={`/returns/${ret.return_id}`} className="order-link">
                  {ret.return_number}
                </Link>{" "}
                <span className="muted small">{ret.return_type === "rto" ? "RTO" : "Customer return"}</span>{" "}
                <span className={`badge return-${ret.status}`}>{capitalize(ret.status)}</span>
                {ret.expected_at && ["open", "received"].includes(ret.status) && (
                  <span className="muted small"> · expected {ret.expected_at}</span>
                )}
              </li>
            ))}
          </ul>
        </div>
      )}

      <div className="card">
        <h2>Timeline</h2>
        <ol className="timeline">
          {order.history.map((entry) => (
            <li key={entry.order_history_id}>
              <time dateTime={entry.changed_at}>{formatDateTime(entry.changed_at)}</time>
              <div>{describeChange(entry)}</div>
              <div className="muted small">via {describeSource(entry.change_source)}</div>
              {entry.remarks && <div className="small">“{entry.remarks}”</div>}
            </li>
          ))}
        </ol>
      </div>
    </section>
  );
}
