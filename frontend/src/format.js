// Display formats. Dates are shown in Indian time, money in the order's currency.

const dateTime = new Intl.DateTimeFormat("en-IN", {
  timeZone: "Asia/Kolkata",
  day: "2-digit",
  month: "short",
  year: "numeric",
  hour: "2-digit",
  minute: "2-digit",
});

export function formatDateTime(value) {
  return value ? dateTime.format(new Date(value)) : "—";
}

// Amounts arrive as strings ("2949.00") so no precision is lost on the way.
export function formatMoney(amount, currency = "INR") {
  const number = Number(amount);
  if (!Number.isFinite(number)) return "—";
  try {
    return new Intl.NumberFormat("en-IN", { style: "currency", currency }).format(number);
  } catch {
    return `${number.toFixed(2)} ${currency}`; // unknown currency code
  }
}

export function capitalize(text) {
  return text ? text.charAt(0).toUpperCase() + text.slice(1).replaceAll("_", " ") : "";
}
