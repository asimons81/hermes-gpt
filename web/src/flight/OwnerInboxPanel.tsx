// OwnerInboxPanel — read-only list of what is waiting on the Owner.
// No buttons mutate anything: suggested tools are shown as text only.
import { useEffect } from 'react';

import { useOwnerInboxStore } from '../stores/ownerInbox';
import type { OwnerInboxItem } from './types';
import { EmptyState, ErrorState, LoadingState, PanelCard, StatusChip, safeText } from './ui';

const KIND_LABELS: Record<string, string> = {
  mission_awaiting_approval: 'Mission awaiting approval',
  autopilot_waiting_owner: 'Autopilot waiting for Owner',
  budget_breaker: 'Budget breaker',
  red_health: 'Red health',
  no_capable_target: 'No capable target',
};

const SUMMARY_MAX = 240;

function sinceEpoch(item: OwnerInboxItem): number {
  if (item.since === null) return Number.NEGATIVE_INFINITY;
  const epoch = Date.parse(item.since);
  return Number.isNaN(epoch) ? Number.NEGATIVE_INFINITY : epoch;
}

/** Newest first; items without a usable timestamp sort last. */
function newestFirst(items: OwnerInboxItem[]): OwnerInboxItem[] {
  return [...items].sort((a, b) => sinceEpoch(b) - sinceEpoch(a));
}

function relativeTime(epoch: number, now: number): string {
  const seconds = Math.max(0, Math.round((now - epoch) / 1000));
  if (seconds < 60) return `${seconds}s ago`;
  const minutes = Math.round(seconds / 60);
  if (minutes < 60) return `${minutes} min ago`;
  const hours = Math.round(minutes / 60);
  if (hours < 48) return `${hours} h ago`;
  return `${Math.round(hours / 24)} d ago`;
}

function SinceLine({ since, now }: { since: string | null; now: number }) {
  if (since === null) return <span className="fd-hint">no timestamp</span>;
  const epoch = Date.parse(since);
  if (Number.isNaN(epoch)) return <span className="fd-hint">{safeText(since, 40)}</span>;
  return (
    <span className="fd-hint">
      <time dateTime={new Date(epoch).toISOString()}>{relativeTime(epoch, now)}</time>
      {' · '}
      {new Date(epoch).toISOString()}
    </span>
  );
}

export function OwnerInboxPanel() {
  const { payload, status, error, fetchedAt, fetchInbox } = useOwnerInboxStore();
  useEffect(() => {
    void fetchInbox();
  }, [fetchInbox]);

  const now = Date.now();
  const unavailable = payload
    ? Object.entries(payload.sources)
        .filter(([, state]) => state === 'unavailable')
        .map(([name]) => safeText(name, 60))
    : [];
  const skipped = payload ? payload.skipped_sources.map((name) => safeText(name, 60)) : [];
  const incomplete = payload !== null && !payload.complete;

  return (
    <section className="fd-surface" data-surface="owner-inbox">
      <header className="fd-surface-head">
        <h2>Owner inbox</h2>
        <p className="fd-surface-desc">
          Read-only list of what is waiting on you. Act with the existing gated tools from your MCP client.
        </p>
        {fetchedAt ? (
          <button type="button" className="fd-btn fd-btn--ghost" onClick={() => void fetchInbox()}>
            Refresh
          </button>
        ) : null}
      </header>

      {status === 'loading' && payload === null ? <LoadingState label="Loading Owner inbox…" /> : null}

      {status === 'denied' ? (
        <div className="fd-state fd-state--error" role="alert">
          <span className="fd-state-glyph" aria-hidden="true">!</span>
          <span>
            Owner inbox is not allowed for this client. The Mission allowlist (HERMES_GPT_MISSION_ALLOWED_SURFACES)
            must permit approvals.
          </span>
        </div>
      ) : null}

      {status === 'error' && payload === null ? (
        <ErrorState message={error ?? 'owner inbox failed'} onRetry={() => void fetchInbox()} />
      ) : null}

      {payload && incomplete ? (
        <div className="fd-stale" role="status">
          <span>
            {payload.count} known items — some sources unavailable:{' '}
            {unavailable.length > 0 ? unavailable.join(', ') : 'unknown'}.
          </span>
        </div>
      ) : null}

      {payload && payload.complete && payload.items.length === 0 ? (
        <EmptyState label="Nothing is waiting on you." />
      ) : null}

      {payload && payload.items.length > 0 ? (
        <>
          <p className="fd-hint">
            {payload.items.length} {payload.items.length === 1 ? 'item' : 'items'}
            {payload.truncated ? ` · showing the newest ${payload.limit}; more items exist` : ''}
          </p>
          <div className="fd-stack">
            {newestFirst(payload.items).map((item, index) => (
              <PanelCard
                key={`${item.kind}-${item.mission_id}-${index}`}
                title={KIND_LABELS[item.kind] ?? safeText(item.kind, 60)}
                tone={item.requires === 'owner' ? 'review' : 'warn'}
                aside={<StatusChip tone={item.requires === 'owner' ? 'review' : 'warn'} label={`requires ${item.requires}`} />}
              >
                <p className="fd-mono">mission {item.mission_id ? safeText(item.mission_id, 128) : '—'}</p>
                <p>{safeText(item.summary, SUMMARY_MAX)}</p>
                <p><SinceLine since={item.since} now={now} /></p>
                <p className="fd-hint">
                  {item.suggested_tool ? (
                    <>
                      Act with <code>{safeText(item.suggested_tool, 80)}</code> from your MCP client
                    </>
                  ) : (
                    'No single tool suggested; inspect in Flight Deck.'
                  )}
                </p>
              </PanelCard>
            ))}
          </div>
        </>
      ) : null}

      {payload && skipped.length > 0 ? (
        <p className="fd-hint">Not checked by this inbox: {skipped.join(', ')}.</p>
      ) : null}
    </section>
  );
}
