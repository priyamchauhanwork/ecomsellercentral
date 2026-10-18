import { useEffect, useState } from "react";
import { Link, useLocation, useParams } from "react-router-dom";
import { api } from "../api";
import { EmptyState, ErrorMessage, Loading } from "../components/Feedback";
import ProductThumbnail from "../components/ProductThumbnail";
import Switch from "../components/Switch";
import { capitalize, formatDateTime, formatMoney } from "../format";

const money = (amount) => (amount == null ? "—" : formatMoney(amount, "INR"));
const STOCK_LABELS = { ok: "OK", low: "Low", out: "Out" };

// Shopify descriptions are HTML. Shown as plain text: parsing with DOMParser never runs
// scripts or loads images, and only the text is rendered.
function plainText(html) {
  if (!html) return "";
  const doc = new DOMParser().parseFromString(html, "text/html");
  doc.querySelectorAll("script, style, template, noscript, iframe").forEach((node) => node.remove());
  doc.querySelectorAll("br, p, li, div, h1, h2, h3, h4").forEach((node) => node.append("\n"));
  return doc.body.textContent.replace(/\n{3,}/g, "\n\n").trim();
}

function variantLabel(values) {
  const entries = Object.entries(values ?? {});
  return entries.length ? entries.map(([axis, value]) => `${axis}: ${value}`).join(" · ") : "Single variant";
}

function PublishPanel({ product, onPublished, onCancel }) {
  const active = product.items.filter((item) => item.status === "active");
  const [prices, setPrices] = useState(() =>
    Object.fromEntries(active.map((item) => [item.item_id, item.mrp ?? ""])),
  );
  const [enable, setEnable] = useState(true);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState("");

  async function publish(event) {
    event.preventDefault();
    setSaving(true);
    setError("");
    try {
      const result = await api(`/products/${product.article_id}/publish`, {
        method: "POST",
        body: {
          enable,
          prices: Object.fromEntries(Object.entries(prices).filter(([, price]) => price !== "" && price != null)),
        },
      });
      onPublished(result);
    } catch (problem) {
      setError(problem.message);
      setSaving(false);
    }
  }

  return (
    <form className="confirm-panel wide" aria-label="Publish to Shopify" onSubmit={publish}>
      <p>
        <strong>Publish {product.name} to Shopify</strong>
      </p>
      <p className="muted">
        Creates the product on your Shopify store with these {active.length} variants, their stock and images.
      </p>
      <div className="table-wrap flat">
        <table className="data-table compact">
          <thead>
            <tr>
              <th scope="col">Variant</th>
              <th scope="col">SKU</th>
              <th scope="col" className="number">MRP</th>
              <th scope="col" className="number">Selling price (₹)</th>
            </tr>
          </thead>
          <tbody>
            {active.map((item) => (
              <tr key={item.item_id}>
                <td>{variantLabel(item.variant_values)}</td>
                <td className="muted">{item.sku}</td>
                <td className="number">{money(item.mrp)}</td>
                <td className="number">
                  <input
                    type="number"
                    min="0.01"
                    step="0.01"
                    required
                    aria-label={`Selling price for ${item.sku}`}
                    className="price-input"
                    value={prices[item.item_id]}
                    disabled={saving}
                    onChange={(event) => setPrices((current) => ({ ...current, [item.item_id]: event.target.value }))}
                  />
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      <label className="checkbox">
        <input type="checkbox" checked={enable} disabled={saving} onChange={(event) => setEnable(event.target.checked)} />
        Put it on sale on the Online Store now (otherwise it is created as a draft)
      </label>
      {error && (
        <p className="form-error" role="alert">
          {error}
        </p>
      )}
      <div className="actions">
        <button type="submit" className="button" disabled={saving}>
          {saving ? "Publishing…" : "Publish"}
        </button>
        <button type="button" className="button secondary" disabled={saving} onClick={onCancel}>
          Cancel
        </button>
      </div>
    </form>
  );
}

export default function ProductDetailPage() {
  const { productId } = useParams();
  const location = useLocation();
  const backTo = `/products${location.state?.listSearch ?? ""}`;
  const validId = /^\d+$/.test(productId);
  const [reloads, setReloads] = useState(0);
  const [result, setResult] = useState({ key: null, product: null, error: null });
  const [publishing, setPublishing] = useState(false);
  const [busy, setBusy] = useState(""); // "listing-3" / "item-12" while saving
  const [confirm, setConfirm] = useState(null); // a pending "take off sale": { kind, id, label }
  const [notice, setNotice] = useState(null);
  const requestKey = `${productId}:${reloads}`;
  const loading = validId && result.key !== requestKey;

  useEffect(() => {
    if (!validId) return undefined;
    const controller = new AbortController();
    api(`/products/${productId}`, { signal: controller.signal })
      .then((product) => {
        if (!controller.signal.aborted) setResult({ key: requestKey, product, error: null });
      })
      .catch((error) => {
        if (!controller.signal.aborted) setResult({ key: requestKey, product: null, error });
      });
    return () => controller.abort();
  }, [productId, validId, requestKey]);

  const back = (
    <Link to={backTo} className="back-link">
      ← Back to products
    </Link>
  );
  if (!validId || (!loading && result.error?.status === 404)) {
    return (
      <section>
        {back}
        <EmptyState title="Product not found">
          <span className="muted">It may belong to another seller, or the link is wrong.</span>
        </EmptyState>
      </section>
    );
  }
  if (loading) {
    return (
      <section>
        {back}
        <Loading label="Loading product…" />
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

  const { product } = result;
  const itemsById = new Map(product.items.map((item) => [item.item_id, item]));
  const onShopify = product.listings.some((l) => l.channel_code === "SHOPIFY" && l.channel_parent_id);
  const description = plainText(product.description);
  const attributes = Object.entries(product.attributes ?? {});

  async function changeStatus(kind, id, status, label) {
    setConfirm(null);
    setBusy(`${kind}-${id}`);
    setNotice(null);
    const path =
      kind === "listing"
        ? `/products/${product.article_id}/listings/${id}/status`
        : `/products/${product.article_id}/listing-items/${id}/status`;
    try {
      const updated = await api(path, { method: "POST", body: { status } });
      setResult({ key: requestKey, product: updated, error: null });
      setNotice({ tone: "success", text: `${label} ${status === "live" ? "is on sale" : "is off sale"} on Shopify.` });
    } catch (problem) {
      setNotice({ tone: "error", text: `${label}: ${problem.message} Nothing was changed.` });
      setReloads((n) => n + 1); // shows the stored sync error
    } finally {
      setBusy("");
    }
  }

  function toggle(kind, id, checked, label) {
    if (checked) changeStatus(kind, id, "live", label);
    else setConfirm({ kind, id, label });
  }

  return (
    <section className="order-detail">
      {back}

      <div className="detail-header">
        <div className="product-cell">
          <ProductThumbnail key={product.image_url} url={product.image_url} name={product.name} size={64} />
          <div>
            <h1 className="page-title">
              {product.name} <span className={`badge badge-${product.status}`}>{capitalize(product.status)}</span>
            </h1>
            <p className="muted">
              {product.article_code}
              {product.brand && ` · ${product.brand}`}
              {product.category && ` · ${product.category}`} · added {formatDateTime(product.created_at)}
            </p>
          </div>
        </div>
        <div className="actions">
          {!onShopify && product.status !== "archived" && (
            <button type="button" className="button" disabled={publishing} onClick={() => setPublishing(true)}>
              Publish to Shopify
            </button>
          )}
        </div>
      </div>

      {publishing && (
        <PublishPanel
          product={product}
          onCancel={() => setPublishing(false)}
          onPublished={(published) => {
            setPublishing(false);
            setResult({ key: requestKey, product: published.product, error: null });
            setNotice({
              tone: published.warnings.length ? "error" : "success",
              text:
                `Published to Shopify with ${published.variants} variants${published.enabled ? ", on sale" : " as a draft"}.` +
                (published.warnings.length ? ` ${published.warnings.join(" ")}` : ""),
            });
          }}
        />
      )}
      {confirm && (
        <div className="confirm-panel" role="alertdialog" aria-label="Confirm taking off sale">
          <p>
            <strong>Take {confirm.label} off sale on Shopify?</strong>
          </p>
          <p className="muted">
            {confirm.kind === "listing"
              ? "The product becomes a draft on Shopify and disappears from every sales channel. You can put it back on sale at any time."
              : "Shopify can't hide a single variant, so it will show as sold out (quantity 0) until you put it back on sale."}
          </p>
          <div className="actions">
            <button type="button" className="button" onClick={() => changeStatus(confirm.kind, confirm.id, "inactive", confirm.label)}>
              Take off sale
            </button>
            <button type="button" className="button secondary" onClick={() => setConfirm(null)}>
              Cancel
            </button>
          </div>
        </div>
      )}
      {notice && (
        <p className={`notice ${notice.tone}`} role="status">
          {notice.text}
        </p>
      )}

      {(product.images.length > 0 || description || attributes.length > 0) && (
        <div className="detail-grid">
          {product.images.length > 0 && (
            <div className="card">
              <h2>Images ({product.images.length})</h2>
              <ul className="gallery">
                {product.images.map((image, index) => (
                  <li key={`${image.url}-${index}`}>
                    <ProductThumbnail key={image.url} url={image.url} name={`${product.name} image ${index + 1}`} size={88} />
                    {Object.keys(image.filter ?? {}).length > 0 && (
                      <span className="muted small">{variantLabel(image.filter)}</span>
                    )}
                  </li>
                ))}
              </ul>
            </div>
          )}
          {description && (
            <div className="card">
              <h2>Description</h2>
              <p className="notes">{description}</p>
            </div>
          )}
          {attributes.length > 0 && (
            <div className="card">
              <h2>Attributes</h2>
              <dl className="details">
                {attributes.map(([key, value]) => (
                  <div key={key} className="contents">
                    <dt>{key}</dt>
                    <dd>{value}</dd>
                  </div>
                ))}
              </dl>
            </div>
          )}
        </div>
      )}

      <div className="card">
        <h2>Variants ({product.items.length})</h2>
        <div className="table-wrap flat">
          <table className="data-table">
            <thead>
              <tr>
                <th scope="col">Variant</th>
                <th scope="col">SKU</th>
                <th scope="col">Barcode</th>
                <th scope="col" className="number">MRP</th>
                <th scope="col" className="number">Stock</th>
                <th scope="col">Status</th>
              </tr>
            </thead>
            <tbody>
              {product.items.map((item) => (
                <tr key={item.item_id}>
                  <td>
                    <div className="product-cell">
                      <ProductThumbnail key={item.image_url} url={item.image_url} name={item.sku} size={40} />
                      <span>{variantLabel(item.variant_values)}</span>
                    </div>
                  </td>
                  <td>{item.sku}</td>
                  <td className="muted">{item.barcode || "—"}</td>
                  <td className="number nowrap">{money(item.mrp)}</td>
                  <td className="number">
                    {item.stock_qty} <span className={`badge stock-${item.stock_status}`}>{STOCK_LABELS[item.stock_status]}</span>
                  </td>
                  <td>
                    <span className={`badge badge-${item.status}`}>{capitalize(item.status)}</span>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </div>

      <h2 className="section-title">Channels</h2>
      {product.listings.length === 0 ? (
        <div className="card">
          <p className="muted">
            Not listed on any channel yet.{" "}
            {product.status !== "archived" && "Publish it to Shopify to start selling it there."}
          </p>
        </div>
      ) : (
        product.listings.map((listing) => {
          const productLabel = `${product.name} (${listing.account_name})`;
          const live = listing.status === "live";
          return (
            <div key={listing.listing_id} className="card">
              <div className="channel-head">
                <div>
                  <h3>
                    {listing.channel_name} · {listing.account_name}
                  </h3>
                  <p className="muted small">
                    {listing.channel_parent_id ? `Shopify product ${listing.channel_parent_id}` : "Not on the channel yet"}
                    {listing.title && ` · listed as “${listing.title}”`}
                  </p>
                </div>
                <div className="switch-field">
                  <span className={`badge channel-${listing.status}`}>{live ? "On sale" : capitalize(listing.status)}</span>
                  <Switch
                    checked={live}
                    label={`${productLabel} on sale`}
                    busy={busy === `listing-${listing.listing_id}`}
                    disabled={!listing.channel_parent_id || busy !== ""}
                    onChange={(checked) => toggle("listing", listing.listing_id, checked, productLabel)}
                  />
                </div>
              </div>
              <div className="table-wrap flat">
                <table className="data-table compact">
                  <thead>
                    <tr>
                      <th scope="col">Variant</th>
                      <th scope="col">Channel SKU</th>
                      <th scope="col">Channel id</th>
                      <th scope="col" className="number">Price</th>
                      <th scope="col">On sale</th>
                    </tr>
                  </thead>
                  <tbody>
                    {listing.items.map((li) => {
                      const item = itemsById.get(li.item_id);
                      const label = `${item?.sku ?? li.channel_sku}`;
                      return (
                        <tr key={li.listing_item_id}>
                          <td>
                            {variantLabel(item?.variant_values)}
                            {li.last_sync_error && (
                              <div className="form-error small" role="note">
                                Last sync failed: {li.last_sync_error}
                              </div>
                            )}
                          </td>
                          <td className="muted">{li.channel_sku || "—"}</td>
                          <td className="muted">{li.channel_item_id || "—"}</td>
                          <td className="number nowrap">{money(li.selling_price)}</td>
                          <td>
                            <Switch
                              checked={li.status === "live"}
                              label={`${label} on sale`}
                              busy={busy === `item-${li.listing_item_id}`}
                              disabled={!li.channel_item_id || busy !== ""}
                              onChange={(checked) => toggle("item", li.listing_item_id, checked, label)}
                            />
                            {!live && li.status === "live" && <span className="muted small"> (product off sale)</span>}
                          </td>
                        </tr>
                      );
                    })}
                  </tbody>
                </table>
              </div>
            </div>
          );
        })
      )}
    </section>
  );
}
