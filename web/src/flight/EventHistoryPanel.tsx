// EventHistoryPanel — the normalized timeline with filters (poll-driven).
import { useEffect } from 'react';

import { useEventHistoryStore } from '../stores/eventHistory';
import { EmptyState, ErrorState, LoadingState, SourceTag, StatusChip, safeText } from './ui';

const SOURCES = ['', 'audit', 'swarm', 'codex', 'cron', 'kanban'];

function formatTs(ts: string): string {
  try {
    const d = new Date(ts);
    return d.toLocaleString();
  } catch {
    return ts;
  }
}

export function EventHistoryPanel() {
  const { events, envelope, filters, status, error, setFilters, fetchEvents } = useEventHistoryStore();

  useEffect(() => {
    void fetchEvents();
  }, [fetchEvents, filters.source, filters.kind, filters.subject_id, filters.limit]);

  return (
    <section className="fd-surface" data-surface="events">
      <header className="fd-surface-head">
        <h2>Event History</h2>
        <p className="fd-surface-desc">
          Normalized timeline{envelope ? ` · retention ${envelope.retention_max_age_days}d` : ''}
        </p>
        <button type="button" className="fd-btn fd-btn--ghost" onClick={() => void fetchEvents()}>
          Refresh
        </button>
      </header>

      <div className="fd-filterbar" role="search">
        <label className="fd-field">
          <span>Source</span>
          <select
            value={filters.source}
            onChange={(e) => setFilters({ source: e.target.value })}
            aria-label="Source filter"
          >
            {SOURCES.map((s) => (
              <option key={s} value={s}>
                {s === '' ? 'all' : s}
              </option>
            ))}
          </select>
        </label>
        <label className="fd-field">
          <span>Subject</span>
          <input
            type="text"
            value={filters.subject_id}
            placeholder="subject id"
            onChange={(e) => setFilters({ subject_id: e.target.value })}
          />
        </label>
        <label className="fd-field">
          <span>Kind</span>
          <input type="text" value={filters.kind} placeholder="kind" onChange={(e) => setFilters({ kind: e.target.value })} />
        </label>
        <label className="fd-field">
          <span>Limit</span>
          <input
            type="number"
            min={1}
            max={200}
            value={filters.limit}
            onChange={(e) => setFilters({ limit: Number(e.target.value) || 50 })}
          />
        </label>
      </div>

      {status === 'loading' && events.length === 0 ? <LoadingState label="Loading events…" /> : null}
      {status === 'error' && events.length === 0 ? <ErrorState message={error ?? 'events failed'} onRetry={() => void fetchEvents()} /> : null}

      {events.length === 0 && status === 'ready' ? <EmptyState label="No events match" /> : null}

      {events.length > 0 ? (
        <div className="fd-events">
          {events.map((ev) => (
            <article key={ev.event_id} className="fd-event" data-source={ev.source}>
              <div className="fd-event-head">
                <SourceTag source={ev.source} />
                <span className="fd-event-kind">{safeText(ev.kind, 40)}</span>
                <time className="fd-event-ts">{formatTs(ev.ts)}</time>
                {ev.status_after ? <StatusChip tone="flight" label={safeText(ev.status_after, 30)} /> : null}
              </div>
              <p className="fd-event-summary">{safeText(ev.summary, 200)}</p>
              <div className="fd-event-meta">
                {ev.actor ? <span className="fd-mono">{safeText(ev.actor, 40)}</span> : null}
                {ev.subject_id ? <span className="fd-mono">{safeText(ev.subject_id, 60)}</span> : null}
              </div>
            </article>
          ))}
        </div>
      ) : null}

      {envelope?.warnings?.length ? (
        <div className="fd-warnings">
          {envelope.warnings.map((w, idx) => (
            <div key={idx} className="fd-warning">
              {safeText(w, 200)}
            </div>
          ))}
        </div>
      ) : null}
    </section>
  );
}
