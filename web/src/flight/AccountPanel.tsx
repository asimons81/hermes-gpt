// AccountPanel — operator policy + OAuth store status (read-only).
import { useEffect } from 'react';

import { useAccountStore } from '../stores/account';
import { EmptyState, ErrorState, LoadingState, PanelCard, StatusChip, safeText } from './ui';

function levelTone(level: string): 'ok' | 'warn' | 'review' | 'flight' {
  if (level === 'owner') return 'review';
  if (level === 'workspace') return 'flight';
  if (level === 'read_only') return 'ok';
  return 'warn';
}

export function AccountPanel() {
  const { account, loading, error, refresh } = useAccountStore();
  useEffect(() => {
    void refresh();
  }, [refresh]);

  return (
    <section className="fd-surface" data-surface="account">
      <header className="fd-surface-head">
        <h2>Account</h2>
        <p className="fd-surface-desc">Operator policy and token-store status. Token material is never shown.</p>
        {account ? (
          <button type="button" className="fd-btn fd-btn--ghost" onClick={() => void refresh()}>
            Refresh
          </button>
        ) : null}
      </header>

      {loading && !account ? <LoadingState label="Loading account…" /> : null}
      {error && !account ? <ErrorState message={error} onRetry={() => void refresh()} /> : null}

      {account ? (
        <div className="fd-stack">
          <PanelCard
            title="Operator policy"
            aside={
              <StatusChip tone={levelTone(account.policy.level)} label={safeText(account.policy.level, 30)} />
            }
          >
            <div className="fd-kv">
              <div className="fd-kv-row">
                <span>enabled</span>
                <span>{String(account.policy.enabled)}</span>
              </div>
              <div className="fd-kv-row">
                <span>apply mode</span>
                <span>{safeText(account.policy.apply_mode, 30)}</span>
              </div>
              <div className="fd-kv-row">
                <span>owner mode ready</span>
                <span>{String(account.policy.owner_mode_ready)}</span>
              </div>
              <div className="fd-kv-row">
                <span>mutation allowed</span>
                <span>{String(account.policy.mutation_allowed)}</span>
              </div>
              <div className="fd-kv-row">
                <span>capabilities</span>
                <span>{safeText(account.policy.available_capability_groups?.join(', ') ?? '', 120)}</span>
              </div>
            </div>
          </PanelCard>

          <PanelCard
            title="OAuth token store"
            aside={
              <StatusChip
                tone={account.oauth?.presence === 'present' ? 'ok' : 'neutral'}
                label={safeText(account.oauth?.presence, 30)}
              />
            }
          >
            {account.oauth?.presence ? (
              <div className="fd-kv">
                <div className="fd-kv-row">
                  <span>expires</span>
                  <span>{safeText(account.oauth?.expires_at, 60)}</span>
                </div>
                <div className="fd-kv-row">
                  <span>clients</span>
                  <span>{String(account.oauth?.client_count ?? 0)}</span>
                </div>
              </div>
            ) : (
              <EmptyState label="No token store" />
            )}
            <p className="fd-hint">Server version {safeText(account.server_version, 30)}</p>
          </PanelCard>
        </div>
      ) : null}
    </section>
  );
}
