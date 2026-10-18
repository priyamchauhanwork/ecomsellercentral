// Shared loading, error and empty states.

export function Loading({ label = "Loading…" }) {
  return (
    <div className="feedback" role="status">
      <span className="spinner" aria-hidden="true" />
      {label}
    </div>
  );
}

export function ErrorMessage({ message, onRetry }) {
  return (
    <div className="feedback error" role="alert">
      <span>{message}</span>
      {onRetry && (
        <button type="button" className="button" onClick={onRetry}>
          Try again
        </button>
      )}
    </div>
  );
}

export function EmptyState({ title, children }) {
  return (
    <div className="feedback empty">
      <strong>{title}</strong>
      {children}
    </div>
  );
}
