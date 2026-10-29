// GatedActionButton — three-stage dangerous-action treatment (media spec §5):
// idle -> dry-run plan -> confirm. Confirm is never pre-enabled; the dry-run
// result must exist first. All calls go through POST /api/ops/action.
import { useRef, useState } from 'react';

import { ApiError, api } from '../api/client';

interface GatedActionButtonProps {
  tool: string;
  args: Record<string, unknown>;
  label: string;
  /** Required level tag shown next to the label (e.g. "owner"). */
  levelTag?: string;
  disabled?: boolean;
  /**
   * Put `confirm: true` inside args on the second gesture. Existing operator
   * tools with a `confirm` argument use the default. A tool without one (for
   * example hermes_owner_inbox_ack) sets this false; the server would refuse
   * an unknown argument.
   */
  confirmArg?: boolean;
  onResult?: (result: unknown) => void;
  /** Called only after a real (non-dry-run) call succeeds, so the parent can refetch. */
  onApplied?: (result: unknown) => void;
  onError?: (message: string) => void;
  /** Characters of the plan JSON shown in the dry-run preview (default 400). */
  previewChars?: number;
}

interface PlanState {
  plan: Record<string, unknown>;
  dryRun: boolean;
}

export function GatedActionButton({
  tool,
  args,
  label,
  levelTag,
  disabled = false,
  confirmArg = true,
  onResult,
  onApplied,
  onError,
  previewChars = 400,
}: GatedActionButtonProps) {
  const [stage, setStage] = useState<'idle' | 'dryrun' | 'confirm' | 'busy' | 'done'>('idle');
  const inFlight = useRef(false);
  const [plan, setPlan] = useState<PlanState | null>(null);
  const [error, setError] = useState<string | null>(null);

  async function runDryRun() {
    if (inFlight.current || disabled) return;
    inFlight.current = true;
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
    } finally { inFlight.current = false; }
  }

  async function runConfirm() {
    if (inFlight.current || disabled || stage !== 'dryrun') return;
    inFlight.current = true;
    setStage('busy');
    setError(null);
    try {
      const body = await api.post<{ tool: string; dry_run: boolean; requires_confirm: boolean; result: unknown }>(
        '/api/ops/action',
        // `confirm` is an existing operator-tool argument, not an adapter
        // bypass.  Keep it inside args so the server's per-tool allowlist
        // and gate path receive exactly the same shape as the MCP tool.
        { tool, args: confirmArg ? { ...args, confirm: true } : { ...args }, dry_run: false },
      );
      setPlan({ plan: (body.result ?? {}) as Record<string, unknown>, dryRun: false });
      onResult?.(body.result);
      if (body.dry_run === false) {
        // The write happened: no second confirm for the same plan.
        setStage('done');
        onApplied?.(body.result);
      } else {
        setStage('dryrun');
      }
    } catch (err) {
      const message = err instanceof ApiError ? `${err.code}: ${err.message}` : 'action failed';
      setError(message);
      onError?.(message);
      setStage('dryrun');
    } finally { inFlight.current = false; }
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
            <pre className="fd-pre">{JSON.stringify(plan.plan, null, 2).slice(0, previewChars)}</pre>
          ) : (
            <span className="fd-hint">Preparing plan…</span>
          )}
          {stage === 'dryrun' ? (
            <div className="fd-gated-actions">
              <span className="fd-chip fd-chip--warn">DRY-RUN · NO-OP</span>
              <button type="button" className="fd-btn fd-btn--ghost" onClick={() => setStage('idle')}>
                Reset
              </button>
              <button type="button" className="fd-btn fd-btn--primary" disabled={disabled} onClick={() => void runConfirm()}>
                Confirm {label.toLowerCase()}
              </button>
            </div>
          ) : null}
        </div>
      ) : null}

      {stage === 'done' ? (
        <div className="fd-gated-actions">
          <span className="fd-chip fd-chip--ok">APPLIED</span>
        </div>
      ) : null}

      {stage === 'confirm' && plan ? (
        <div className="fd-gated-actions">
          <span className="fd-chip fd-chip--warn">CONFIRM REQUIRED</span>
          <pre className="fd-pre">{JSON.stringify(plan.plan, null, 2).slice(0, previewChars)}</pre>
          <button type="button" className="fd-btn fd-btn--ghost" onClick={() => setStage('idle')}>
            Cancel
          </button>
          <button type="button" className="fd-btn fd-btn--primary" disabled={disabled} onClick={() => void runConfirm()}>
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
