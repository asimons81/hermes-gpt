// Generic SurfacePanel — renders any Mission Control surface from a schema.
//
// A surface is described by a small schema: which counts to show, which
// top-level data fields are cards vs. tables, and how to derive a status
// tone. This keeps the 12 mission surfaces cheap to build and consistent.
import { useEffect } from 'react';

import { selectSurface, useOperatorStore } from '../stores/operator';
import type { MissionEnvelope } from './types';
import { EmptyState, ErrorState, LoadingState, PanelCard, StaleBanner, StatusChip, safeText } from './ui';
import type { StatusTone } from './ui';

export interface SurfaceField {
  key: string;
  label: string;
  /** How to render the field value. */
  kind?: 'text' | 'json' | 'list' | 'count' | 'status';
  max?: number;
}

export interface SurfaceSchema {
  surface: string;
  title: string;
  description?: string;
  /** Which status tone to derive from the envelope (by key value). */
  tone?: (envelope: MissionEnvelope) => StatusTone;
  /** Fields rendered as stat cards at the top (from envelope.data). */
  stats?: SurfaceField[];
  /** Fields rendered as full-width detail sections. */
  sections?: SurfaceField[];
}

function toneFromOverall(envelope: MissionEnvelope): StatusTone {
  const overall = String((envelope.data as Record<string, unknown> | undefined)?.overall ?? '');
  if (overall === 'pass' || overall === 'ok') return 'ok';
  if (overall === 'warn') return 'warn';
  if (overall === 'fail') return 'deny';
  return 'neutral';
}

function renderValue(field: SurfaceField, value: unknown) {
  const max = field.max ?? 160;
  if (field.kind === 'count') {
    return <span className="fd-stat-value">{typeof value === 'number' ? value : safeText(value, 20)}</span>;
  }
  if (field.kind === 'status') {
    const tone = String(value ?? '').toLowerCase() as StatusTone;
    return <StatusChip tone={tone in { ok: 1, warn: 1, deny: 1, review: 1, flight: 1, neutral: 1 } ? tone : 'neutral'} label={safeText(value, 40)} />;
  }
  if (field.kind === 'json') {
    return <pre className="fd-pre">{safeText(JSON.stringify(value), 400)}</pre>;
  }
  if (field.kind === 'list') {
    if (!Array.isArray(value)) return <span>{safeText(value, max)}</span>;
    if (value.length === 0) return <EmptyState label="None" />;
    return (
      <ul className="fd-list">
        {value.slice(0, 12).map((item, idx) => (
          <li key={idx} className="fd-list-item">
            {typeof item === 'object' && item !== null ? (
              <pre className="fd-pre fd-pre--row">{safeText(JSON.stringify(item), max)}</pre>
            ) : (
              <span>{safeText(item, max)}</span>
            )}
          </li>
        ))}
        {value.length > 12 ? <li className="fd-list-more">+{value.length - 12} more</li> : null}
      </ul>
    );
  }
  return <span>{safeText(value, max)}</span>;
}

export function SurfacePanel({ schema }: { schema: SurfaceSchema }) {
  const fetchSurface = useOperatorStore((s) => s.fetchSurface);
  const { entry, stale } = useOperatorStore((s) => selectSurface(s, schema.surface));
  const envelope = entry.data;

  useEffect(() => {
    void fetchSurface(schema.surface);
  }, [fetchSurface, schema.surface]);

  if (entry.status === 'loading' && !envelope) {
    return (
      <section className="fd-surface" data-surface={schema.surface}>
        <header className="fd-surface-head">
          <h2>{schema.title}</h2>
        </header>
        <LoadingState label={`Loading ${schema.title}…`} />
      </section>
    );
  }

  if (entry.status === 'error' && !envelope) {
    return (
      <section className="fd-surface" data-surface={schema.surface}>
        <header className="fd-surface-head">
          <h2>{schema.title}</h2>
        </header>
        <ErrorState message={entry.error ?? 'surface failed'} onRetry={() => void fetchSurface(schema.surface, true)} />
      </section>
    );
  }

  if (!envelope) {
    return (
      <section className="fd-surface" data-surface={schema.surface}>
        <header className="fd-surface-head">
          <h2>{schema.title}</h2>
        </header>
        <EmptyState label="No data" />
      </section>
    );
  }

  const tone = schema.tone ? schema.tone(envelope) : toneFromOverall(envelope);
  const data = (envelope.data ?? {}) as Record<string, unknown>;
  const counts = (envelope.counts ?? {}) as Record<string, unknown>;

  return (
    <section className="fd-surface" data-surface={schema.surface}>
      <header className="fd-surface-head">
        <div>
          <h2>{schema.title}</h2>
          {schema.description ? <p className="fd-surface-desc">{schema.description}</p> : null}
        </div>
        <div className="fd-surface-meta">
          <StatusChip tone={tone} label={envelope.available === false ? 'unavailable' : tone} />
          {stale ? <StaleBanner onRefresh={() => void fetchSurface(schema.surface, true)} /> : null}
        </div>
      </header>

      {envelope.available === false ? (
        <div className="fd-unavailable" role="status">
          {envelope.unavailable_reason || envelope.safe_message || 'Surface unavailable'}
          {envelope.suggested_action ? <p className="fd-hint">{envelope.suggested_action}</p> : null}
        </div>
      ) : null}

      {schema.stats?.length ? (
        <div className="fd-stats">
          {schema.stats.map((field) => (
            <div key={field.key} className="fd-stat" data-label={field.label}>
              {renderValue(field, data[field.key] ?? counts[field.key])}
              <span className="fd-stat-label">{field.label}</span>
            </div>
          ))}
        </div>
      ) : null}

      {schema.sections?.length ? (
        <div className="fd-sections">
          {schema.sections.map((field) => (
            <PanelCard key={field.key} title={field.label}>
              {renderValue(field, data[field.key])}
            </PanelCard>
          ))}
        </div>
      ) : null}

      {envelope.warnings?.length ? (
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
