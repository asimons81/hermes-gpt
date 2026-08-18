// GatedActionButton — three-stage dangerous-action treatment (media spec §5):
// idle -> dry-run plan -> confirm. Confirm is never pre-enabled; the dry-run
// result must exist first. All calls go through POST /api/ops/action.
import { useState } from 'react';

import { ApiError, api } from '../api/client';

interface GatedActionButtonProps {
  tool: string;
  args: Record<string, unknown>;
  label: string;
  /** Required level tag shown next to the label (e.g. "owner"). */
  levelTag?: string;
  disabled?: boolean;
  onResult?: (result: unknown) => void;
  onError?: (message: string) => void;
}

interface PlanState {
  plan: Record<string, unknown>;
  dryRun: boolean;
}

export function GatedActionButton({ tool, args, label, levelTag, disabled = false, onResult, onError }: GatedActionButtonProps) {
  const [stage, setStage] = useState<'idle' | 'dryrun' | 'confirm' | 'busy'>('idle');
  const [plan, setPlan] = useState<PlanState | null>(null);
  const [error, setError] = useState<string | null>(null);

  async function runDryRun() {
    setStage('busy');
    setError(null);
    try {
      const body = await api.post<{ tool: string; dry_run: boolean; requires_confirm: boolean; result: unknown }>(
        '/api/ops/action',
        { tool, args, dry_run: true },
      );
      setPlan({ plan: (body.result ?? {}) as Record<string, unknown>, dryRun: body.dry_run });
      // A successful plan is always visibly a dry-run first.  The confirm
      // control is intentionally a second, distinct user gesture.
      setStage('dryrun');
      onResult?.(body.result);
    } catch (err) {
      const message = err instanceof ApiError ? `${err.code}: ${err.message}` : 'action failed';
      setError(message);
      onError?.(message);
      setStage('idle');
    }
  }

  async function runConfirm() {
    setStage('busy');
    setError(null);
    try {
      const body = await api.post<{ tool: string; dry_run: boolean; requires_confirm: boolean; result: unknown }>(
        '/api/ops/action',
        // `confirm` is an existing operator-tool argument, not an adapter
        // bypass.  Keep it inside args so the server's per-tool allowlist
        // and gate path receive exactly the same shape as the MCP tool.
        { tool, args: { ...args, confirm: true }, dry_run: false },
      );
      setPlan({ plan: (body.result ?? {}) as Record<string, unknown>, dryRun: false });
      setStage('dryrun');
      onResult?.(body.result);
    } catch (err) {
      const message = err instanceof ApiError ? `${err.code}: ${err.message}` : 'action failed';
      setError(message);
      onError?.(message);
      setStage('dryrun');
    }
  }

  return (
    <div className="fd-gated" data-gate-step={stage}>
      {stage === 'idle' ? (
        <button type="button" className="fd-btn fd-btn--secondary" disabled={disabled} onClick={() => void runDryRun()}>
          {label}
          {levelTag ? <span className="fd-level-tag">{levelTag}</span> : null}
        </button>
      ) : null}

      {stage === 'dryrun' || stage === 'busy' ? (
        <div className="fd-gated-plan">
          {plan ? (
            <pre className="fd-pre">{JSON.stringify(plan.plan, null, 2).slice(0, 400)}</pre>
          ) : (
            <span className="fd-hint">Preparing plan…</span>
          )}
          {stage === 'dryrun' ? (
            <div className="fd-gated-actions">
              <span className="fd-chip fd-chip--warn">DRY-RUN · NO-OP</span>
              <button type="button" className="fd-btn fd-btn--ghost" onClick={() => setStage('idle')}>
                Reset
              </button>
              <button type="button" className="fd-btn fd-btn--primary" onClick={() => void runConfirm()}>
                Confirm {label.toLowerCase()}
              </button>
            </div>
          ) : null}
        </div>
      ) : null}

      {stage === 'confirm' && plan ? (
        <div className="fd-gated-actions">
          <span className="fd-chip fd-chip--warn">CONFIRM REQUIRED</span>
          <pre className="fd-pre">{JSON.stringify(plan.plan, null, 2).slice(0, 400)}</pre>
          <button type="button" className="fd-btn fd-btn--ghost" onClick={() => setStage('idle')}>
            Cancel
          </button>
          <button type="button" className="fd-btn fd-btn--primary" onClick={() => void runConfirm()}>
            Confirm {label.toLowerCase()}
          </button>
        </div>
      ) : null}

      {error ? (
        <div className="fd-inline-error" role="alert">
          {error}
        </div>
      ) : null}
    </div>
  );
}
