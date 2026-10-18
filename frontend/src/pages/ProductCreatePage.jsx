import { useState } from "react";
import { Link, useNavigate } from "react-router-dom";
import { api } from "../api";
import { fieldErrors, suggestedSku, variantRows } from "../products/form";

const SHOPIFY_MAX_OPTIONS = 3;
const EMPTY_ROW = { sku: "", skuEdited: false, barcode: "", mrp: "", stock_qty: "0", weight: "", removed: false };

function FieldError({ errors, name }) {
  const messages = errors[name];
  return messages ? (
    <span className="form-error small" role="alert">
      {messages.join(" ")}
    </span>
  ) : null;
}

export default function ProductCreatePage() {
  const navigate = useNavigate();
  const [details, setDetails] = useState({ article_code: "", name: "", brand: "", category: "", description: "" });
  const [options, setOptions] = useState([]); // [{ name, values: "Black, White" }]
  const [rows, setRows] = useState({}); // combination key → row fields
  const [images, setImages] = useState([]); // [{ url, filter: "Colour=Black" | "" }]
  const [attributes, setAttributes] = useState([]); // [{ key, value }]
  const [errors, setErrors] = useState({});
  const [formError, setFormError] = useState("");
  const [saving, setSaving] = useState(false);

  const variants = variantRows(options); // [{ key, values: { Colour: "Black", … } }]
  const row = (key) => rows[key] ?? EMPTY_ROW;
  const activeVariants = variants.filter((variant) => !row(variant.key).removed);
  const axes = options.map((option) => option.name.trim()).filter(Boolean);

  function updateRow(key, changes) {
    setRows((current) => ({ ...current, [key]: { ...(current[key] ?? EMPTY_ROW), ...changes } }));
  }

  function skuFor(variant) {
    const current = row(variant.key);
    return current.skuEdited ? current.sku : suggestedSku(details.article_code, variant.values);
  }

  async function save(event) {
    event.preventDefault();
    setSaving(true);
    setErrors({});
    setFormError("");
    const body = {
      ...details,
      brand: details.brand || null,
      category: details.category || null,
      description: details.description || null,
      variation_axes: axes,
      attributes: Object.fromEntries(attributes.filter((a) => a.key.trim()).map((a) => [a.key.trim(), a.value])),
      images: images
        .filter((image) => image.url.trim())
        .map((image) => {
          const [axis, value] = image.filter ? image.filter.split("=") : [];
          return { url: image.url.trim(), filter: axis ? { [axis]: value } : {} };
        }),
      items: activeVariants.map((variant) => {
        const current = row(variant.key);
        return {
          sku: skuFor(variant),
          variant_values: variant.values,
          barcode: current.barcode || null,
          mrp: current.mrp === "" ? null : current.mrp,
          weight: current.weight === "" ? null : current.weight,
          stock_qty: current.stock_qty === "" ? 0 : Number(current.stock_qty),
        };
      }),
    };
    try {
      const product = await api("/products", { method: "POST", body });
      navigate(`/products/${product.article_id}`);
    } catch (problem) {
      const found = fieldErrors(problem.detail, activeVariants.map((variant) => variant.key));
      setErrors(found);
      const unplaced = found[""] ?? [];
      setFormError(
        problem.status === 422
          ? `Please fix the highlighted fields.${unplaced.length ? ` ${unplaced.join(" ")}` : ""}`
          : problem.message,
      );
      setSaving(false);
    }
  }

  return (
    <section>
      <Link to="/products" className="back-link">
        ← Back to products
      </Link>
      <h1 className="page-title">New product</h1>
      <p className="muted">Saved as a draft. Publish it to Shopify from its page when it's ready.</p>

      <form className="product-form" onSubmit={save} noValidate>
        {formError && (
          <p className="notice error" role="alert">
            {formError}
          </p>
        )}

        <div className="card form-grid">
          <h2>Details</h2>
          <label className="field">
            <span>Product name *</span>
            <input value={details.name} maxLength={255} required onChange={(e) => setDetails({ ...details, name: e.target.value })} />
            <FieldError errors={errors} name="name" />
          </label>
          <label className="field">
            <span>Product code *</span>
            <input
              value={details.article_code}
              maxLength={100}
              required
              placeholder="e.g. TSHIRT-BASIC"
              onChange={(e) => setDetails({ ...details, article_code: e.target.value })}
            />
            <FieldError errors={errors} name="article_code" />
          </label>
          <label className="field">
            <span>Brand</span>
            <input value={details.brand} maxLength={100} onChange={(e) => setDetails({ ...details, brand: e.target.value })} />
          </label>
          <label className="field">
            <span>Category</span>
            <input value={details.category} maxLength={255} onChange={(e) => setDetails({ ...details, category: e.target.value })} />
          </label>
          <label className="field wide">
            <span>Description</span>
            <textarea rows={3} value={details.description} onChange={(e) => setDetails({ ...details, description: e.target.value })} />
          </label>
        </div>

        <div className="card">
          <h2>Options</h2>
          <p className="muted small">
            Add options such as Colour or Size if the product comes in variants. Leave empty for a single product.
          </p>
          {options.map((option, index) => (
            <div key={index} className="option-row">
              <label className="field">
                <span>Option name</span>
                <input
                  value={option.name}
                  placeholder="e.g. Colour"
                  onChange={(e) => setOptions(options.map((o, i) => (i === index ? { ...o, name: e.target.value } : o)))}
                />
              </label>
              <label className="field wide">
                <span>Values, separated by commas</span>
                <input
                  value={option.values}
                  placeholder="e.g. Black, White"
                  onChange={(e) => setOptions(options.map((o, i) => (i === index ? { ...o, values: e.target.value } : o)))}
                />
              </label>
              <button
                type="button"
                className="link-button"
                aria-label={`Remove option ${option.name || index + 1}`}
                onClick={() => setOptions(options.filter((_, i) => i !== index))}
              >
                Remove
              </button>
            </div>
          ))}
          <FieldError errors={errors} name="variation_axes" />
          <button type="button" className="button secondary" onClick={() => setOptions([...options, { name: "", values: "" }])}>
            Add option
          </button>
          {options.length > SHOPIFY_MAX_OPTIONS && (
            <p className="notice error small">Shopify allows at most {SHOPIFY_MAX_OPTIONS} options, so this product couldn't be published there.</p>
          )}
        </div>

        <div className="card">
          <h2>Variants ({activeVariants.length})</h2>
          <FieldError errors={errors} name="items" />
          <div className="table-wrap flat">
            <table className="data-table compact variant-grid">
              <thead>
                <tr>
                  <th scope="col">Variant</th>
                  <th scope="col">SKU *</th>
                  <th scope="col">Barcode</th>
                  <th scope="col">MRP (₹)</th>
                  <th scope="col">Opening stock</th>
                  <th scope="col">Weight (g)</th>
                  <th scope="col">
                    <span className="sr-only">Include</span>
                  </th>
                </tr>
              </thead>
              <tbody>
                {variants.map((variant) => {
                  const current = row(variant.key);
                  const name = Object.values(variant.values).join(" / ") || "Single variant";
                  const errorKey = (field) => `items.${variant.key}.${field}`;
                  return (
                    <tr key={variant.key} className={current.removed ? "removed" : undefined}>
                      <td>
                        {name}
                        <FieldError errors={errors} name={errorKey("variant_values")} />
                      </td>
                      <td>
                        <input
                          aria-label={`SKU for ${name}`}
                          value={skuFor(variant)}
                          maxLength={100}
                          disabled={current.removed}
                          onChange={(e) => updateRow(variant.key, { sku: e.target.value, skuEdited: true })}
                        />
                        <FieldError errors={errors} name={errorKey("sku")} />
                      </td>
                      <td>
                        <input aria-label={`Barcode for ${name}`} value={current.barcode} maxLength={50} disabled={current.removed}
                          onChange={(e) => updateRow(variant.key, { barcode: e.target.value })} />
                      </td>
                      <td>
                        <input aria-label={`MRP for ${name}`} type="number" min="0" step="0.01" value={current.mrp} disabled={current.removed}
                          onChange={(e) => updateRow(variant.key, { mrp: e.target.value })} />
                        <FieldError errors={errors} name={errorKey("mrp")} />
                      </td>
                      <td>
                        <input aria-label={`Opening stock for ${name}`} type="number" min="0" step="1" value={current.stock_qty} disabled={current.removed}
                          onChange={(e) => updateRow(variant.key, { stock_qty: e.target.value })} />
                        <FieldError errors={errors} name={errorKey("stock_qty")} />
                      </td>
                      <td>
                        <input aria-label={`Weight for ${name}`} type="number" min="0" step="0.001" value={current.weight} disabled={current.removed}
                          onChange={(e) => updateRow(variant.key, { weight: e.target.value })} />
                      </td>
                      <td>
                        {variants.length > 1 && (
                          <button type="button" className="link-button" onClick={() => updateRow(variant.key, { removed: !current.removed })}>
                            {current.removed ? "Add back" : "Remove"}
                          </button>
                        )}
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        </div>

        <div className="card">
          <h2>Images</h2>
          <p className="muted small">Links to images (https). A Colour-specific image can be tied to that colour.</p>
          {images.map((image, index) => (
            <div key={index} className="option-row">
              <label className="field wide">
                <span>Image URL</span>
                <input
                  type="url"
                  value={image.url}
                  placeholder="https://…"
                  onChange={(e) => setImages(images.map((im, i) => (i === index ? { ...im, url: e.target.value } : im)))}
                />
                <FieldError errors={errors} name={`images.${index}.url`} />
                <FieldError errors={errors} name={`images.${index}.filter`} />
              </label>
              <label className="field">
                <span>Shown for</span>
                <select
                  value={image.filter}
                  onChange={(e) => setImages(images.map((im, i) => (i === index ? { ...im, filter: e.target.value } : im)))}
                >
                  <option value="">All variants</option>
                  {options.flatMap((option) =>
                    option.values
                      .split(",")
                      .map((value) => value.trim())
                      .filter(Boolean)
                      .map((value) => (
                        <option key={`${option.name}=${value}`} value={`${option.name.trim()}=${value}`}>
                          {option.name.trim()}: {value}
                        </option>
                      )),
                  )}
                </select>
              </label>
              <button type="button" className="link-button" onClick={() => setImages(images.filter((_, i) => i !== index))}>
                Remove
              </button>
            </div>
          ))}
          <button type="button" className="button secondary" onClick={() => setImages([...images, { url: "", filter: "" }])}>
            Add image
          </button>
        </div>

        <div className="card">
          <h2>Attributes</h2>
          <p className="muted small">Details shared by every variant, e.g. Fabric: Cotton.</p>
          {attributes.map((attribute, index) => (
            <div key={index} className="option-row">
              <label className="field">
                <span>Name</span>
                <input value={attribute.key} onChange={(e) => setAttributes(attributes.map((a, i) => (i === index ? { ...a, key: e.target.value } : a)))} />
              </label>
              <label className="field wide">
                <span>Value</span>
                <input value={attribute.value} onChange={(e) => setAttributes(attributes.map((a, i) => (i === index ? { ...a, value: e.target.value } : a)))} />
              </label>
              <button type="button" className="link-button" onClick={() => setAttributes(attributes.filter((_, i) => i !== index))}>
                Remove
              </button>
            </div>
          ))}
          <button type="button" className="button secondary" onClick={() => setAttributes([...attributes, { key: "", value: "" }])}>
            Add attribute
          </button>
        </div>

        <div className="actions">
          <button type="submit" className="button" disabled={saving}>
            {saving ? "Saving…" : "Save product"}
          </button>
          <Link to="/products" className="button secondary link-like">
            Cancel
          </Link>
        </div>
      </form>
    </section>
  );
}
