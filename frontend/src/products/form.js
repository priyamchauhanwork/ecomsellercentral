// Helpers for the new product form (#45).

// "Black, white, , Black" → ["Black", "white"]: trimmed, blanks and repeats (any case) dropped.
function splitValues(text) {
  const seen = new Set();
  const values = [];
  for (const value of text.split(",").map((part) => part.trim())) {
    if (value && !seen.has(value.toLowerCase())) {
      seen.add(value.toLowerCase());
      values.push(value);
    }
  }
  return values;
}

// Every combination of the options' values, in order: Colour [Black, White] × Size [M, L]
// → Black/M, Black/L, White/M, White/L. Each row has a stable key, so what was typed for
// a combination survives edits to the options. No options → one single variant.
export function variantRows(options) {
  const named = options
    .map((option) => ({ name: option.name.trim(), values: splitValues(option.values) }))
    .filter((option) => option.name && option.values.length);
  let combinations = [{}];
  for (const option of named) {
    combinations = combinations.flatMap((values) =>
      option.values.map((value) => ({ ...values, [option.name]: value })),
    );
  }
  return combinations.map((values) => ({
    key: Object.entries(values).map(([axis, value]) => `${axis}=${value}`).join("|") || "single",
    values,
  }));
}

const slug = (text) =>
  text
    .toUpperCase()
    .replace(/[^A-Z0-9]+/g, "-")
    .replace(/^-|-$/g, "");

// A suggested SKU: CODE-BLACK-M. Only a suggestion; the user can type their own.
export function suggestedSku(code, values) {
  return [slug(code), ...Object.values(values).map(slug)].filter(Boolean).join("-");
}

// FastAPI 422 details → { field: [messages] }. Item errors ("items", 3, "sku") are keyed
// by the variant's row key (items.<key>.sku), so they stay with the right row; errors
// that belong to no field are under "".
export function fieldErrors(detail, rowKeys) {
  const found = {};
  const add = (field, message) => (found[field] ??= []).push(message);
  if (!Array.isArray(detail)) {
    if (detail) add("", typeof detail === "string" ? detail : JSON.stringify(detail));
    return found;
  }
  for (const error of detail) {
    const loc = (error.loc ?? []).filter((part) => part !== "body");
    if (loc[0] === "items" && Number.isInteger(loc[1]) && rowKeys[loc[1]] !== undefined) {
      add(["items", rowKeys[loc[1]], ...loc.slice(2, 3)].join("."), error.msg);
    } else if (loc.length) {
      add(loc.slice(0, 3).join("."), error.msg);
    } else {
      add("", error.msg);
    }
  }
  return found;
}
