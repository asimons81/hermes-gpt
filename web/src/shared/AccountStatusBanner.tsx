/**
 * AccountStatusBanner — account recovery UX (t_7266e74c, §13).
 *
 * Presentational: receives the four-state accountStatus from /api/me and
 * renders the recovery surface. Data wiring is the account store's job
 * (flight card, t_1135e15b); this component never fetches.
 *
 * Behavior contract (§13):
 * - ok            -> renders nothing.
 * - expired       -> "Session expired" banner with a re-auth affordance.
 * - revoked       -> "Access tokens revoked" banner.
 * - unauthorized  -> "Authentication required" banner with a re-auth link.
 *
 * In every non-ok state the UI keeps read-only chat history viewable and
 * disables mutating controls — the server already drops the approvals
 * capability from /api/me, and the account store exposes
 * ``mutationsEnabled=false`` for clients to gate controls.
 */

import type { AccountStatus } from "./types";

import "./tokens.css";

interface AccountStatusBannerProps {
  status: AccountStatus;
  /** Re-auth affordance (e.g. navigate to the OAuth authorize URL). */
  onReauth?: () => void;
  reauthHref?: string;
  /** When true, the caller's mutating controls are disabled. */
  mutationsEnabled?: boolean;
}

const CONTENT: Record<
  Exclude<AccountStatus, "ok">,
  { title: string; body: string }
> = {
  expired: {
    title: "Session expired",
    body: "Your access tokens have expired. Re-authenticate to continue using mutating controls; read-only chat history remains available.",
  },
  revoked: {
    title: "Access tokens revoked",
    body: "Your access tokens were revoked. Re-authenticate to restore full access; read-only chat history remains available.",
  },
  unauthorized: {
    title: "Authentication required",
    body: "This server requires authentication. Sign in to continue.",
  },
};

export function AccountStatusBanner({
  status,
  onReauth,
  reauthHref,
  mutationsEnabled = false,
}: AccountStatusBannerProps) {
  if (status === "ok") {
    return null;
  }
  const content = CONTENT[status];
  return (
    <div
      role="alert"
      data-testid="account-status-banner"
      data-status={status}
      style={{
        display: "flex",
        alignItems: "flex-start",
        justifyContent: "space-between",
        gap: "var(--hg-space-3)",
        padding: "var(--hg-space-3) var(--hg-space-4)",
        borderRadius: "var(--hg-radius-md)",
        background: "var(--hg-color-danger-subtle)",
        border: "1px solid var(--hg-color-border)",
      }}
    >
      <div>
        <div style={{ fontWeight: 600, color: "var(--hg-color-text)" }}>
          {content.title}
        </div>
        <div
          style={{
            color: "var(--hg-color-text-secondary)",
            fontSize: "var(--hg-font-size-sm)",
          }}
        >
          {content.body}
          {!mutationsEnabled &&
            " Mutating controls are disabled until you re-authenticate."}
        </div>
      </div>
      {onReauth && (
        <a
          href={reauthHref ?? "#"}
          onClick={(event) => {
            if (!reauthHref) {
              event.preventDefault();
              onReauth();
            }
          }}
          style={{
            flexShrink: 0,
            color: "var(--hg-color-accent)",
            fontWeight: 600,
            textDecoration: "none",
          }}
        >
          Re-authenticate
        </a>
      )}
    </div>
  );
}
