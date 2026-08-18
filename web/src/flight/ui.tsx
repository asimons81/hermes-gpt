// Flight Deck shared UI primitives.
//
// Token policy: the security card owns web/src/shared/tokens.* (hotspot).
// This worktree uses CSS custom properties with neutral fallback values so
// panels render standalone and adopt the shared tokens when they land.
import type { ReactNode } from 'react';

export type StatusTone = 'ok' | 'warn' | 'deny' | 'review' | 'flight' | 'neutral';

const TONE_LABEL: Record<StatusTone, string> = {
  ok: 'ok',
  warn: 'warn',
  deny: 'deny',
  review: 'review',
  flight: 'flight',
  neutral: 'neutral',
};

export function StatusChip({ tone = 'neutral', label }: { tone?: StatusTone; label?: string }) {
  return (
    <span className={`fd-chip fd-chip--${tone}`} data-status={tone}>
      <span className="fd-chip-dot" aria-hidden="true" />
      {label ?? TONE_LABEL[tone]}
    </span>
  );
}

export function RedactionMarker({ reason = 'redacted' }: { reason?: string }) {
  return (
    <span className="fd-redaction" title={reason}>
      [REDACTED]
    </span>
  );
}

export function SourceTag({ source }: { source: string }) {
  return <span className={`fd-src fd-src-${source}`}>{source}</span>;
}

export function LoadingState({ label = 'Loading…' }: { label?: string }) {
  return (
    <div className="fd-state fd-state--loading" role="status">
      <span className="fd-dashes" aria-hidden="true">
        ···
      </span>
      <span>{label}</span>
    </div>
  );
}

export function ErrorState({ message, onRetry }: { message: string; onRetry?: () => void }) {
  return (
    <div className="fd-state fd-state--error" role="alert">
      <span className="fd-state-glyph" aria-hidden="true">
        !
      </span>
      <span>{message}</span>
      {onRetry ? (
        <button type="button" className="fd-btn fd-btn--ghost" onClick={onRetry}>
          Retry
        </button>
      ) : null}
    </div>
  );
}

export function EmptyState({ label = 'No data' }: { label?: string }) {
  return (
    <div className="fd-state fd-state--empty" role="status">
      <span>{label}</span>
    </div>
  );
}

export function StaleBanner({ onRefresh }: { onRefresh: () => void }) {
  return (
    <div className="fd-stale" role="status">
      <span>Data may be stale</span>
      <button type="button" className="fd-btn fd-btn--ghost" onClick={onRefresh}>
        Refresh
      </button>
    </div>
  );
}

export function PanelCard({
  title,
  tone = 'neutral',
  children,
  aside,
}: {
  title: string;
  tone?: StatusTone;
  children: ReactNode;
  aside?: ReactNode;
}) {
  return (
    <section className="fd-card" data-tone={tone}>
      <header className="fd-card-head">
        <h3 className="fd-card-title">{title}</h3>
        {aside ? <div className="fd-card-aside">{aside}</div> : null}
      </header>
      <div className="fd-card-body">{children}</div>
    </section>
  );
}

export function Kbd({ children }: { children: ReactNode }) {
  return <kbd className="fd-kbd">{children}</kbd>;
}

/** Pretty-print a bounded value; renders [REDACTED] for null/undefined. */
export function safeText(value: unknown, max = 160): string {
  if (value === null || value === undefined || value === '') return '[REDACTED]';
  const text = typeof value === 'string' ? value : JSON.stringify(value);
  if (text.length <= max) return text;
  return `${text.slice(0, max)}…[truncated]`;
}
