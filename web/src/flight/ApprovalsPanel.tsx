// ApprovalsPanel — pending approvals + the gated confirm dialog flow.
import { useEffect } from 'react';

import { useApprovalStore } from '../stores/approval';
import { EmptyState, ErrorState, LoadingState, PanelCard, StatusChip, safeText } from './ui';

export function ApprovalsPanel() {
  const { items, status, error, fetchedAt, fetchApprovals, dialog, closeConfirm, runGated } = useApprovalStore();
  useEffect(() => {
    void fetchApprovals();
  }, [fetchApprovals]);

  return (
    <section className="fd-surface" data-surface="approvals">
      <header className="fd-surface-head">
        <h2>Approvals</h2>
        <p className="fd-surface-desc">Pending approvals across sources. Raw prompts are never shown.</p>
        {fetchedAt ? (
          <button type="button" className="fd-btn fd-btn--ghost" onClick={() => void fetchApprovals()}>
            Refresh
          </button>
        ) : null}
      </header>

      {status === 'loading' && items.length === 0 ? <LoadingState label="Loading approvals…" /> : null}
      {status === 'error' && items.length === 0 ? <ErrorState message={error ?? 'approvals failed'} onRetry={() => void fetchApprovals()} /> : null}

      {items.length === 0 && status === 'ready' ? <EmptyState label="No pending approvals" /> : null}

      {items.length > 0 ? (
        <div className="fd-stack">
          {items.slice(0, 30).map((item, idx) => (
            <PanelCard
              key={item.id ?? `${item.source}-${idx}`}
              title={safeText(item.kind, 60)}
              aside={
                <>
                  <StatusChip tone="review" label={safeText(item.status, 30)} />
                  <span className="fd-mono">{safeText(item.source, 40)}</span>
                </>
              }
            >
              <p className="fd-hint">
                id {safeText(item.id, 60)}
                {item.prompt_sha256 ? <> · sha {safeText(item.prompt_sha256, 16)}…</> : null}
              </p>
            </PanelCard>
          ))}
        </div>
      ) : null}

      {dialog ? (
        <div className="fd-modal-root">
          <div className="fd-scrim" onClick={closeConfirm} />
          <div className="fd-modal" role="dialog" aria-modal="true" aria-label={`Confirm ${dialog.tool}`}>
            <h3>Confirm {dialog.tool}</h3>
            <p className="fd-hint">This action requires explicit confirmation. The server gate is never bypassed.</p>
            <pre className="fd-pre">{JSON.stringify(dialog.plan, null, 2).slice(0, 400)}</pre>
            <div className="fd-gated-actions">
              <button type="button" className="fd-btn fd-btn--ghost" onClick={closeConfirm}>
                Cancel
              </button>
              <button
                type="button"
                className="fd-btn fd-btn--primary"
                onClick={() =>
                  void runGated(dialog.tool, dialog.args, false).catch((err: unknown) => {
                    // surface gate errors inline; dialog stays for re-confirm
                    console.error('confirm failed', err);
                  })
                }
              >
                Confirm
              </button>
            </div>
          </div>
        </div>
      ) : null}
    </section>
  );
}
