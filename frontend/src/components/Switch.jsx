// An on/off switch (a button with role="switch"), e.g. "on sale on Shopify".
export default function Switch({ checked, onChange, label, disabled = false, busy = false }) {
  return (
    <button
      type="button"
      role="switch"
      aria-checked={checked}
      aria-label={label}
      aria-busy={busy}
      disabled={disabled || busy}
      className={`switch${checked ? " on" : ""}${busy ? " busy" : ""}`}
      onClick={() => onChange(!checked)}
    >
      <span className="switch-knob" aria-hidden="true" />
    </button>
  );
}
