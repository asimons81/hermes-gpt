import { useEffect, useRef, useState } from 'react';
import { Link } from 'react-router-dom';
import { api, ApiError } from '../api/client';
import { ErrorState, PanelCard, safeText, StatusChip } from './ui';

const STORAGE = 'hermes.pendingLaunch';
interface Receipt { mission_id: string; state: string; result: { steps?: Array<{step: string; outcome: string}> }; }
interface LaunchResult { success: boolean; mission_id?: string; receipt?: Receipt; steps?: Array<{step: string; outcome: string; detail?: string}>; }
const message = (error: unknown) => error instanceof ApiError ? `${error.code}: ${error.message}` : 'Connection lost. Inspect the launch receipt before starting more work.';

export function LaunchPanel() {
  const [title, setTitle] = useState('');
  const [objective, setObjective] = useState('');
  const [criteria, setCriteria] = useState('');
  const [profile, setProfile] = useState('default');
  const [quota, setQuota] = useState('');
  const [unbudgeted, setUnbudgeted] = useState(false);
  const [minutes, setMinutes] = useState('30');
  const [requestId] = useState(() => crypto.randomUUID());
  const [pending, setPending] = useState<string | null>(() => sessionStorage.getItem(STORAGE));
  const initialPending = useRef(pending);
  const [receipt, setReceipt] = useState<Receipt | null>(null);
  const [preview, setPreview] = useState<{input: string; result: LaunchResult} | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const inFlight = useRef(false);
  const args = {
    request_id: requestId,
    spec_json: JSON.stringify({title: title.trim(), objective: objective.trim(), owner_profile: profile.trim(), acceptance_criteria: criteria.split('\n').map(s => s.trim()).filter(Boolean), final_approval_required: true}),
    budget_json: unbudgeted ? null : JSON.stringify({quota: Number(quota)}),
    autopilot_limits_json: JSON.stringify({max_concurrency: 1, max_attempts_per_node: 1, max_replans: 0, max_runtime_seconds: Number(minutes) * 60}),
  };
  const input = JSON.stringify(args);
  const valid = Boolean(title.trim() && objective.trim() && criteria.trim() && profile.trim() && (unbudgeted || (quota !== '' && Number(quota) > 0)) && Number(minutes) > 0);
  async function inspect(id: string) {
    setBusy(true); setError(null);
    try {
      const data = await api.get<{found: boolean; receipt: Receipt | null}>(`/api/ops/launch-receipts/${encodeURIComponent(id)}`);
      setReceipt(data.receipt);
      if (!data.found) setError('No receipt is visible yet. The original request may still be arriving. Inspect again; do not submit another launch.');
    } catch (err) { setError(message(err)); }
    finally { setBusy(false); }
  }
  useEffect(() => { if (initialPending.current) void inspect(initialPending.current); }, []);
  async function submit(confirm: boolean) {
    if (inFlight.current || !valid || (confirm && preview?.input !== input)) return;
    inFlight.current = true; setBusy(true); setError(null);
    try {
      if (confirm) {
        // Persist only the opaque request id BEFORE sending. Never persist the work text.
        sessionStorage.setItem(STORAGE, requestId);
        setPending(requestId);
      }
      const data = await api.post<{result: LaunchResult}>('/api/ops/action', {tool: 'hermes_mission_launch', args: {...args, confirm}, dry_run: !confirm});
      if (confirm) { setReceipt(data.result.receipt ?? null); setError(null); }
      else setPreview({input, result: data.result});
    } catch (err) { setError(message(err)); }
    finally { inFlight.current = false; setBusy(false); }
  }
  if (pending) return <section className="fd-surface">
    <header className="fd-surface-head"><h2>Launch receipt</h2><p>Your original request is saved. Checking it never launches more work.</p></header>
    {error ? <ErrorState message={error} /> : null}
    <PanelCard title="Original attempt">
      <p className="fd-mono">{pending}</p>
      {receipt ? <><StatusChip tone={receipt.state === 'completed' ? 'ok' : 'warn'} label={receipt.state === 'completed' ? 'Launch completed — work still needs validation' : receipt.state === 'in_progress' ? 'In progress or interrupted — outcome unverified' : 'Launch stopped'} />
      <p><Link to={`/ops/missions/${encodeURIComponent(receipt.mission_id)}`}>Inspect Mission</Link></p>
      <ul>{receipt.result.steps?.map(s => <li key={s.step}>{safeText(s.step, 80)}: {safeText(s.outcome, 40)}</li>)}</ul></> : null}
      <button className="fd-btn" disabled={busy} onClick={() => void inspect(pending)}>Check receipt</button>
      {receipt?.state === 'completed' ? <button className="fd-btn fd-btn--ghost" onClick={() => { sessionStorage.removeItem(STORAGE); window.location.reload(); }}>Start a different Mission</button> : <p className="fd-hint">Partial or uncertain work must be reconciled using its original Mission. No automatic retry is offered.</p>}
    </PanelCard>
  </section>;
  return <section className="fd-surface">
    <header className="fd-surface-head"><h2>New Mission</h2><p>Define the result, preview the work, then launch. Final approval stays with you.</p></header>
    <PanelCard title="What should get done?">
      <div className="fd-stack">
        <label>Title<input className="fd-input" maxLength={160} value={title} disabled={busy} onChange={e => setTitle(e.target.value)} /></label>
        <label>Objective<textarea className="fd-input" maxLength={4000} value={objective} disabled={busy} onChange={e => setObjective(e.target.value)} /></label>
        <label>Done when — one criterion per line<textarea className="fd-input" maxLength={4000} value={criteria} disabled={busy} onChange={e => setCriteria(e.target.value)} /></label>
        <label>Owner profile<input className="fd-input" maxLength={64} value={profile} disabled={busy} onChange={e => setProfile(e.target.value)} /></label>
        <label>Budget quota<input className="fd-input" type="number" min="1" value={quota} disabled={busy || unbudgeted} onChange={e => setQuota(e.target.value)} /></label>
        <label><input type="checkbox" checked={unbudgeted} disabled={busy} onChange={e => setUnbudgeted(e.target.checked)} /> I explicitly choose no budget envelope.</label>
        <p className="fd-hint">Quota uses the configured budget unit; it is not automatically a dollar limit. Without an envelope, dispatch is unrestricted by the budget gate.</p>
        <label>Maximum runtime (minutes)<input className="fd-input" type="number" min="1" max="1440" value={minutes} disabled={busy} onChange={e => setMinutes(e.target.value)} /></label>
        <p className="fd-hint">One worker at a time, one attempt per node, no replans. Approval gates remain in place.</p>
        <button className="fd-btn fd-btn--primary" disabled={busy || !valid} onClick={() => void submit(false)}>Preview Mission</button>
      </div>
    </PanelCard>
    {preview?.input === input ? <PanelCard title="Launch preview">
      <ol>{preview.result.steps?.map(s => <li key={s.step}>{safeText(s.detail || s.step, 400)}</li>)}</ol>
      <p className="fd-hint">Preview only. No work has started.</p>
      <button className="fd-btn fd-btn--primary" disabled={busy} onClick={() => void submit(true)}>Confirm launch</button>
    </PanelCard> : null}
    {error ? <ErrorState message={error} /> : null}
  </section>;
}
