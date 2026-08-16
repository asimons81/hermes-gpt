/**
 * ConnectionStatus — transport-health indicator (t_7266e74c, §13).
 *
 * Reads the connection store and renders the current transport state:
 * connected (subtle), reconnecting (backoff with attempt count), or server
 * restart (banner + recovery note). Account-level recovery UX is rendered by
 * <AccountStatusBanner />; this component is transport-only.
 */

import { useConnectionStore } from "../stores/connection";

import "./tokens.css";

const LABELS: Record<string, string> = {
  connected: "Connected",
  disconnected: "Disconnected",
  reconnecting: "Reconnecting…",
  "server-restart": "Server restarted",
};

export function ConnectionStatus() {
  const sseStatus = useConnectionStore((s) => s.sseStatus);
  const reconnect = useConnectionStore((s) => s.reconnect);
  const serverRestart = useConnectionStore((s) => s.serverRestart);
  const staleLeaseSeconds = useConnectionStore((s) => s.staleLeaseSeconds);

  if (sseStatus === "connected" && !serverRestart) {
    return null;
  }

  const attempt =
    reconnect.phase === "backoff" ? ` (attempt ${reconnect.attempt})` : "";

  return (
    <div
      role="status"
      data-testid="connection-status"
      style={{
        display: "flex",
        alignItems: "center",
        gap: "var(--hg-space-2)",
        padding: "var(--hg-space-2) var(--hg-space-3)",
        borderRadius: "var(--hg-radius-pill)",
        background: "var(--hg-color-warning-subtle)",
        color: "var(--hg-color-warning)",
        fontSize: "var(--hg-font-size-sm)",
      }}
    >
      <span aria-hidden>⟳</span>
      <span>{LABELS[sseStatus] ?? LABELS.disconnected}{attempt}</span>
      {reconnect.phase === "server-restart" && (
        <span style={{ color: "var(--hg-color-text-secondary)" }}>
          — your in-flight turn was interrupted; it can be resumed after
          reconnect. Stale turns older than {staleLeaseSeconds}s are shown as
          interrupted, never as running.
        </span>
      )}
    </div>
  );
}
