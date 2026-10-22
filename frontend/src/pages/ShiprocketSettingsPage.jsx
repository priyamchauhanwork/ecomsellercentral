import { useEffect, useState } from "react";
import { api } from "../api";
import { ErrorMessage, Loading } from "../components/Feedback";
import { capitalize, formatDateTime, formatMoney } from "../format";

const STATUS_LABELS = {
  active: "Connected",
  auth_expired: "Credentials rejected",
  error: "Last call failed",
};

function SetupSteps({ status }) {
  return (
    <div className="card">
      <h2>How to connect</h2>
      <ol className="setup-steps">
        <li>
          In Shiprocket, open <strong>Settings → API → Add New API User</strong>. Use an email that is{" "}
          <em>not</em> your Shiprocket login, give it the Orders, Couriers, Shipments, Labels, Tracking, Pickup
          Addresses, NDR, Returns and Account modules, and set buyer&apos;s details access to <em>Allowed</em>. Copy
          the password: Shiprocket shows it only once.
        </li>
        <li>
          Put the API user in the backend&apos;s environment (<code>.env</code> locally, the service&apos;s Environment
          on Render): <code>SHIPROCKET_EMAIL</code>, <code>SHIPROCKET_PASSWORD</code>, and{" "}
          <code>SHIPROCKET_BASE_URL</code> for the sandbox (production is the default). Restart the backend.
        </li>
        <li>
          For the sandbox, Shiprocket must allow-list this machine&apos;s and Render&apos;s outbound IP addresses;
          until then it answers &quot;HTTP 498&quot;.
        </li>
        <li>
          Click <strong>Test connection</strong> below.
        </li>
      </ol>
      {status && !status.webhook_token_set && (
        <p className="muted small">
          Tracking updates (later) also need <code>SHIPROCKET_WEBHOOK_TOKEN</code>, the security token entered in
          Shiprocket → Settings → API → Webhooks.
        </p>
      )}
    </div>
  );
}

// Shiprocket connection (#57): status, connection test, pickup locations and wallet.
export default function ShiprocketSettingsPage() {
  const [state, setState] = useState({ loaded: false, status: null, error: "" });
  const [reloads, setReloads] = useState(0);
  const [testing, setTesting] = useState(false);
  const [test, setTest] = useState(null); // { result } or { error }

  useEffect(() => {
    const controller = new AbortController();
    api("/shiprocket/status", { signal: controller.signal })
      .then((status) => !controller.signal.aborted && setState({ loaded: true, status, error: "" }))
      .catch((error) => !controller.signal.aborted && setState({ loaded: true, status: null, error: error.message }));
    return () => controller.abort();
  }, [reloads]);

  async function runTest() {
    setTesting(true);
    setTest(null);
    try {
      const result = await api("/shiprocket/test", { method: "POST" });
      setTest({ result });
      setState({ loaded: true, status: result.status, error: "" });
    } catch (error) {
      setTest({ error: error.message });
      setReloads((n) => n + 1); // the failure is recorded on the connection
    } finally {
      setTesting(false);
    }
  }

  if (!state.loaded) return <Loading label="Loading Shiprocket settings…" />;
  if (state.error) return <ErrorMessage message={state.error} onRetry={() => setReloads((n) => n + 1)} />;

  const { status } = state;
  const result = test?.result;
  const verified = result?.pickup_locations.filter((location) => location.phone_verified !== false) ?? [];

  return (
    <section className="settings">
      <div className="page-header spread">
        <h1 className="page-title">Shiprocket</h1>
        <button type="button" className="button" onClick={runTest} disabled={testing || !status.configured}>
          {testing ? "Testing…" : "Test connection"}
        </button>
      </div>

      <div className="card">
        <h2>Connection</h2>
        <dl className="details">
          <dt>Status</dt>
          <dd>
            {!status.configured ? (
              <span className="badge badge-cancelled">Not configured</span>
            ) : status.connection_status ? (
              <span className={`badge connection-${status.connection_status}`}>
                {STATUS_LABELS[status.connection_status] ?? capitalize(status.connection_status)}
              </span>
            ) : (
              <span className="badge badge-pending">Not tested yet</span>
            )}
          </dd>
          <dt>Environment</dt>
          <dd>
            <span className={`badge env-${status.environment}`}>{capitalize(status.environment)}</span>{" "}
            <span className="muted small">{status.base_url}</span>
          </dd>
          <dt>API user</dt>
          <dd>{status.api_user || "—"}</dd>
          <dt>Last connected</dt>
          <dd>{status.last_connected_at ? formatDateTime(status.last_connected_at) : "—"}</dd>
          <dt>Session valid until</dt>
          <dd>{status.token_expires_at ? formatDateTime(status.token_expires_at) : "Logs in on the next call"}</dd>
          <dt>Tracking webhook token</dt>
          <dd>{status.webhook_token_set ? "Set" : "Not set (needed for tracking updates)"}</dd>
        </dl>
        {status.config_error && (
          <p className="notice error" role="alert">
            {status.config_error}
          </p>
        )}
        {status.last_error && !test?.error && (
          <p className="notice error" role="alert">
            Last error: {status.last_error}
          </p>
        )}
        {test?.error && (
          <p className="notice error" role="alert">
            Connection test failed: {test.error}
          </p>
        )}
        {result && (
          <p className="notice success" role="status">
            Connected to Shiprocket as {status.api_user}.
          </p>
        )}
      </div>

      {result && (
        <div className="card">
          <h2>Account</h2>
          <dl className="details">
            <dt>Wallet balance</dt>
            <dd>
              {result.wallet_balance !== null ? (
                formatMoney(result.wallet_balance, "INR")
              ) : (
                <span className="muted">Unavailable: {result.wallet_error}</span>
              )}
            </dd>
          </dl>
          <h2>Pickup locations ({result.pickup_locations.length})</h2>
          {result.pickup_locations.length === 0 ? (
            <p className="notice error">No pickup location yet: add and verify one in Shiprocket before booking shipments.</p>
          ) : (
            <>
              {verified.length === 0 && (
                <p className="notice error">None of the pickup locations is phone-verified; Shiprocket won&apos;t book from them.</p>
              )}
              <ul className="dashboard-list" aria-label="Pickup locations">
                {result.pickup_locations.map((location) => (
                  <li key={location.name}>
                    <div>
                      <strong>{location.name}</strong>
                      {location.is_primary && <span className="badge badge-active">Primary</span>}
                      <div className="muted small">
                        {[location.address, location.city, location.state, location.pincode].filter(Boolean).join(", ")}
                      </div>
                    </div>
                    <div className="end">
                      {location.phone_verified === false ? (
                        <span className="badge badge-pending">Not verified</span>
                      ) : (
                        <span className="badge badge-active">Verified</span>
                      )}
                    </div>
                  </li>
                ))}
              </ul>
            </>
          )}
        </div>
      )}

      <SetupSteps status={status} />
    </section>
  );
}
