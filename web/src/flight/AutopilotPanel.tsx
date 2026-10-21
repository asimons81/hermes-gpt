import { useCallback, useEffect, useRef, useState } from 'react';
import { ApiError, api } from '../api/client';
import { EmptyState, ErrorState, LoadingState, PanelCard, StatusChip, safeText } from './ui';

interface Attention { code: string; severity?: string; nodes?: string[] }
interface Validation { verdict?: string; checks?: Array<{ kind: string; status: string; failure_codes?: string[] }> }
interface AutopilotView {
  found: boolean;
  effective_state?: string;
  stale?: boolean;
  worker?: { liveness?: string };
  run?: { updated_at?: string };
  summary?: {
    available: boolean;
    progress?: { total: number; completed: number; in_flight: number; ready: number };
    budget?: { configured?: boolean; error?: boolean; status?: string; spend?: number; quota?: number; unit?: string };
    recovery?: { retries?: number; replans_used?: number; max_replans?: number; failed_nodes?: string[] };
    limits?: { runtime_remaining_seconds?: number; max_concurrency?: number };
    workers?: Array<{ node_id: string; state?: string; delegation_state?: string; attempt?: number;
      validation?: Validation; validation_failure_since?: string }>;
    attention?: Attention[];
  };
}

const ATTENTION: Record<string, string> = {
  mission_awaiting_approval: 'Review the results and give final Mission approval.',
  owner_gate_node: 'An approval or high-impact task needs your decision.',
  node_failed: 'A task failed. Review its evidence before resuming work.',
  budget_crossed: 'The budget limit was reached. New work is on hold.',
  budget_invalid: 'The budget cannot be verified. New work is on hold.',
  budget_check_failed: 'The budget is unavailable. New work is on hold.',
  runtime_exceeded: 'The runtime limit was reached. New work has stopped.',
  worker_silent: 'The worker has not reported recently. Check its status.',
  validation_pending: 'Execution finished but completion evidence is not yet verified. Check the task’s acceptance results.',
};
const CHECKS: Record<string, string> = { run_state: 'Execution', artifacts: 'Deliverables', tests: 'Tests',
  review: 'Review', forbidden: 'Scope', authorization: 'Authorization' };
const ARTIFACT_REASONS: Record<string, string> = { artifact_missing: 'Required file missing',
  artifact_too_small: 'File below minimum size', artifact_too_large: 'File exceeds maximum size',
  artifact_hash_mismatch: 'Content hash differs from requirement', artifact_remote_mismatch: 'Remote artifact differs from requirement',
  artifact_not_file: 'Deliverable is not a regular file', artifact_unreadable: 'File cannot be verified' };

export function AutopilotPanel({ missionId, revision }: { missionId: string; revision?: number }) {
  const [view, setView] = useState<AutopilotView | null>(null);
  const [error, setError] = useState<string | null>(null);
  const generation = useRef(0);
  const previousRevision = useRef(revision);
  const refresh = useCallback(async () => {
    const request = ++generation.current;
    try {
      const data = await api.get<AutopilotView>(`/api/ops/missions/${encodeURIComponent(missionId)}/autopilot`);
      if (request === generation.current) { setView(data); setError(null); }
    } catch (err) {
      if (request === generation.current) setError(err instanceof ApiError ? `${err.code}: ${err.message}` : 'Autopilot status unavailable');
    }
  }, [missionId]);
  useEffect(() => {
    setView(null); setError(null);
    let active = true;
    let timer: ReturnType<typeof setTimeout>;
    const poll = async () => {
      await refresh();
      if (active) timer = setTimeout(() => void poll(), 5000);
    };
    void poll();
    return () => { active = false; ++generation.current; clearTimeout(timer); };
  }, [refresh]);
  useEffect(() => {
    if (revision !== previousRevision.current) { previousRevision.current = revision; void refresh(); }
  }, [revision, refresh]);

  const summary = view?.summary;
  const progress = summary?.progress;
  const budget = summary?.budget;
  const state = view?.effective_state || 'unknown';
  const unverified = view?.found && !['alive', 'terminal'].includes(view.worker?.liveness || '');
  const stale = Boolean(error || view?.stale || unverified);
  const needsDecision = summary?.attention?.some((item) => item.severity === 'owner');
  return <PanelCard title="Autopilot" tone={stale || needsDecision ? 'warn' : 'neutral'}
    aside={<button type="button" className="fd-btn fd-btn--ghost" onClick={() => void refresh()}>Refresh Autopilot</button>}>
    {!view && !error ? <LoadingState label="Checking Autopilot…" /> : null}
    {error ? <ErrorState message={error} onRetry={() => void refresh()} /> : null}
    {view && !view.found ? <EmptyState label="No Autopilot run for this Mission" /> : null}
    {view?.found ? <>
      <div className="fd-row">
        <StatusChip tone={stale ? 'warn' : state === 'failed' ? 'deny' : state === 'running' ? 'flight' : 'review'} label={safeText(state, 40)} />
        {stale ? <StatusChip tone="warn" label="Status needs verification" /> : null}
        <span>worker {safeText(view.worker?.liveness || 'unknown', 40)}</span>
      </div>
      {summary?.available && progress ? <>
        <p>{progress.completed} of {progress.total} tasks complete · {progress.in_flight} in flight · {progress.ready} ready</p>
        <progress aria-label="Mission task completion" value={progress.completed} max={Math.max(1, progress.total)} />
      </> : <p className="fd-hint">Progress summary unavailable. Completion is unverified.</p>}
      <div className="fd-grid fd-grid--2">
        <div><span className="fd-label">Budget</span><p>{budget?.error ? 'Unavailable — new work on hold' : budget?.configured
          ? `${budget.spend ?? 'unknown'} / ${budget.quota ?? 'unknown'} ${safeText(budget.unit || '', 32)} · ${safeText(budget.status || 'unverified', 48)}`
          : budget?.configured === false ? 'No budget configured' : 'Unverified'}</p></div>
        <div><span className="fd-label">Recovery</span><p>{summary?.available
          ? `${summary.recovery?.retries ?? 0} retries · ${summary.recovery?.replans_used ?? 0} of ${summary.recovery?.max_replans ?? 0} replans`
          : 'Unverified'}</p></div>
      </div>
      {summary?.limits?.runtime_remaining_seconds !== undefined ? <p className="fd-hint">{Math.floor(summary.limits.runtime_remaining_seconds / 60)} minutes remaining · concurrency limit {summary.limits.max_concurrency ?? 'unknown'}</p> : null}
      {summary?.attention?.length ? <div role="status"><strong>Needs attention</strong><ul>{summary.attention.map((item, index) => <li key={`${item.code}-${index}`}>
        {ATTENTION[item.code] || safeText(item.code, 120)}{item.nodes?.length ? ` Tasks: ${item.nodes.map((id) => safeText(id, 64)).join(', ')}.` : ''}
      </li>)}</ul></div> : null}
      {summary?.workers?.length ? <ul className="fd-list">{summary.workers.map((worker) => <li className="fd-list-item" key={worker.node_id}>
        <div className="fd-row"><strong>{safeText(worker.node_id, 64)}</strong><span>{safeText(worker.state || 'unknown', 40)}</span><span>attempt {(worker.attempt ?? 0) + 1}</span><span>execution {safeText(worker.delegation_state || 'unverified', 40)}</span></div>
        {worker.validation?.verdict ? <div aria-label={`Acceptance checks for ${safeText(worker.node_id, 64)}`}>
          <p>{worker.validation.verdict === 'SATISFIED' ? 'Completion checks passed' : 'Completion remains unverified'}</p>
          <ul>{worker.validation.checks?.map((check) => <li key={check.kind}>
            {CHECKS[check.kind] || safeText(check.kind, 32)}: {check.status === 'PASS' ? 'Passed' : check.status === 'FAIL' ? 'Failed' : 'Unverified'}
            {check.failure_codes?.length ? ` — ${check.failure_codes.map((code) => ARTIFACT_REASONS[code] || safeText(code, 64)).join('; ')}` : ''}
          </li>)}</ul>
          {worker.validation_failure_since && worker.delegation_state === 'reconciling'
            ? <p className="fd-hint">Delivery grace period: a confirmed artifact failure enters bounded recovery after 30 seconds.</p> : null}
        </div> : <p className="fd-hint">Acceptance evidence unavailable.</p>}
      </li>)}</ul> : null}
      <p className="fd-hint">Approvals, budget changes, and recovery actions use the existing Operator tools. Final Mission approval requires Owner mode.</p>
    </> : null}
  </PanelCard>;
}
