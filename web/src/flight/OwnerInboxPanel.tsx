// OwnerInboxPanel — what is waiting on the Owner, with the gated actions that
// resolve it. Every action goes through GatedActionButton: a dry-run POST to
// /api/ops/action first, then a separate confirm gesture. The server enforces
// every gate (Owner Mode, level, apply mode); the UI adds no authority. The
// inbox is refetched after any action that was actually applied.
import { useEffect, useState } from 'react';
import { Link } from 'react-router-dom';

import { useOwnerInboxStore } from '../stores/ownerInbox';
import { GatedActionButton } from './GatedActionButton';
import type { OwnerInboxItem } from './types';
import { EmptyState, ErrorState, LoadingState, PanelCard, StatusChip, safeText } from './ui';

const KIND_LABELS: Record<string, string> = {
  contract_review_pending: 'Work ready for review',
  mission_awaiting_approval: 'Mission awaiting approval',
  autopilot_waiting_owner: 'Autopilot waiting for Owner',
  budget_breaker: 'Budget breaker',
  red_health: 'Red health',
  no_capable_target: 'No capable target',
};

/** Kinds whose actions are wired in the browser. Everything else is guidance text. */
const WIRED_KINDS = new Set(['mission_awaiting_approval', 'autopilot_waiting_owner']);

// Same shape the server accepts for plan node ids (operator_mission_plan.NODE_ID_RE).
const NODE_ID_RE = /^[a-z][a-z0-9_-]{0,63}$/;
const APPROVAL_REFERENCE_MAX = 256;
const SUMMARY_MAX = 240;
// The dry-run preview lists the approve walk's steps; the default 400 cuts them off.
const APPROVE_PREVIEW_CHARS = 1600;

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

interface ActionProps {
  item: OwnerInboxItem;
  onApplied: () => void;
}

/** mission_awaiting_approval: hermes_mission_approve needs an Owner-supplied approval reference. */
function MissionApproveAction({ item, onApplied }: ActionProps) {
  const [reference, setReference] = useState('');
  const trimmed = reference.trim();
  return (
    <div className="fd-stack">
      <label className="fd-hint">
        Approval reference
        <input
          type="text"
          className="fd-input"
          maxLength={APPROVAL_REFERENCE_MAX}
          value={reference}
          onChange={(event) => setReference(event.target.value)}
        />
      </label>
      {/* Keyed on the reference: editing it discards any dry-run shown for the old one. */}
      <GatedActionButton
        key={trimmed}
        tool="hermes_mission_approve"
        args={{ mission_id: item.mission_id, approval_reference: trimmed }}
        label="Approve Mission…"
        levelTag="owner"
        disabled={trimmed === '' || item.mission_id === ''}
        onApplied={onApplied}
      />
    </div>
  );
}

/** autopilot_waiting_owner: per frontier node approve/reject, plus stop. */
function AutopilotActions({ item, onApplied }: ActionProps) {
  const nodes = (item.frontier_node_ids ?? []).filter((id) => NODE_ID_RE.test(id));
  const missionId = item.mission_id;
  return (
    <div className="fd-stack">
      {nodes.map((nodeId) => (
        <div key={nodeId} className="fd-stack">
          <span className="fd-mono">node {safeText(nodeId, 64)}</span>
          {/* Approve walks the legal state path to completed server-side (hermes_plan_approve_gate). */}
          <GatedActionButton
            tool="hermes_plan_approve_gate"
            args={{ mission_id: missionId, node_id: nodeId }}
            label={`Approve node ${nodeId}…`}
            levelTag="owner"
            previewChars={APPROVE_PREVIEW_CHARS}
            disabled={missionId === ''}
            onApplied={onApplied}
          />
          <GatedActionButton
            tool="hermes_plan_node_transition"
            args={{ mission_id: missionId, node_id: nodeId, target_state: 'failed' }}
            label={`Reject node ${nodeId}…`}
            levelTag="owner"
            disabled={missionId === ''}
            onApplied={onApplied}
          />
        </div>
      ))}
      <GatedActionButton
        tool="hermes_autopilot_stop"
        args={{ mission_id: missionId }}
        label="Stop Autopilot…"
        levelTag="workspace"
        disabled={missionId === ''}
        onApplied={onApplied}
      />
    </div>
  );
}

function ItemActions({ item, onApplied }: ActionProps) {
  return (
    <div className="fd-stack">
      {item.kind === 'contract_review_pending' ? <Link className="fd-btn fd-btn--primary" to={`/ops/review/${item.item_id}`}>Review evidence</Link> : null}
      {item.kind === 'mission_awaiting_approval' ? <MissionApproveAction item={item} onApplied={onApplied} /> : null}
      {item.kind === 'autopilot_waiting_owner' ? <AutopilotActions item={item} onApplied={onApplied} /> : null}
      {/* Acknowledge hides the item only; it never resolves the condition. No confirm argument exists. */}
      <GatedActionButton
        tool="hermes_owner_inbox_ack"
        args={{ item_ids: [item.item_id] }}
        label="Acknowledge"
        confirmArg={false}
        onApplied={onApplied}
      />
    </div>
  );
}

export function OwnerInboxPanel() {
  const { payload, status, error, fetchedAt, fetchInbox } = useOwnerInboxStore();
  useEffect(() => {
    void fetchInbox();
  }, [fetchInbox]);

  const refetch = () => void fetchInbox();
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
          What is waiting on you. Each action is a dry-run first, then a separate confirm; the server enforces every gate.
        </p>
        {fetchedAt ? (
          <button type="button" className="fd-btn fd-btn--ghost" onClick={refetch}>
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
        <ErrorState message={error ?? 'owner inbox failed'} onRetry={refetch} />
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
            {newestFirst(payload.items).map((item) => (
              <PanelCard
                key={item.item_id}
                title={KIND_LABELS[item.kind] ?? safeText(item.kind, 60)}
                tone={item.requires === 'owner' ? 'review' : 'warn'}
                aside={<StatusChip tone={item.requires === 'owner' ? 'review' : 'warn'} label={`requires ${item.requires}`} />}
              >
                <p className="fd-mono">mission {item.mission_id ? safeText(item.mission_id, 128) : '—'}</p>
                <p>{safeText(item.summary, SUMMARY_MAX)}</p>
                <p><SinceLine since={item.since} now={now} /></p>
                <p className="fd-hint">
                  {item.suggested_tool ? (
                    WIRED_KINDS.has(item.kind) ? (
                      <>
                        Gated tool <code>{safeText(item.suggested_tool, 80)}</code>
                      </>
                    ) : (
                      <>
                        Act with <code>{safeText(item.suggested_tool, 80)}</code> from your MCP client
                      </>
                    )
                  ) : (
                    'No single tool suggested; inspect in Flight Deck.'
                  )}
                </p>
                <ItemActions item={item} onApplied={refetch} />
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
