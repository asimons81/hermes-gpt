import { useEffect, useState } from 'react';
import { Link, useParams } from 'react-router-dom';
import { api, ApiError } from '../api/client';
import { GatedActionButton } from './GatedActionButton';
import { ErrorState, LoadingState, PanelCard, safeText, StatusChip } from './ui';
interface ReviewItem {
  item_id: string; mission_id: string; contract_sha256: string; task_id: string; delegation_id: string;
  assignee: string; assignee_identities: string[]; review_revision: string;
  validation: {checks: Array<{kind: string; status: string; failure_codes?: string[]}>};
  expected_artifacts: Array<{basename: string; sha256?: string; min_bytes?: number; max_bytes?: number}>;
}
export function ReviewPanel() {
  const {itemId = ''} = useParams();
  const [item, setItem] = useState<ReviewItem | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [reviewer, setReviewer] = useState('');
  const [reference, setReference] = useState('');
  const [refs, setRefs] = useState('');
  const [verdict, setVerdict] = useState('SATISFIED');
  const [accepted, setAccepted] = useState(false);
  const [generation, setGeneration] = useState(0);
  useEffect(() => {
    let active = true; setItem(null); setError(null);
    api.get<{item: ReviewItem}>(`/api/ops/pending-review/${encodeURIComponent(itemId)}`)
      .then(data => { if (active) setItem(data.item); })
      .catch(err => { if (active) setError(err instanceof ApiError ? `${err.code}: ${err.message}` : 'Review could not be loaded.'); });
    return () => { active = false; };
  }, [itemId, generation]);
  const args = {item_id: itemId, review_revision: item?.review_revision, reviewer: reviewer.trim(), verdict,
    evidence_refs: refs.split('\n').map(s => s.trim()).filter(Boolean), approval_reference: reference.trim()};
  const selfReview = item?.assignee_identities.includes(reviewer.trim());
  const disabled = !reviewer.trim() || Boolean(selfReview) || (verdict === 'SATISFIED' && !refs.trim() && !reference.trim());
  return <section className="fd-surface">
    <header className="fd-surface-head"><h2>Review work</h2><p>Check the evidence before recording your decision. Review acceptance does not approve the Mission.</p><Link to="/ops/owner-inbox">Back to Owner inbox</Link></header>
    {error ? <ErrorState message={error} onRetry={() => { setAccepted(false); setGeneration(g => g + 1); }} /> : null}
    {!item && !error ? <LoadingState label="Loading review evidence…" /> : null}
    {item ? <>
      <PanelCard title="Evidence snapshot">
        <p>Task: {safeText(item.task_id, 128)} · executed by {safeText(item.assignee, 128)}</p>
        <p className="fd-mono">Contract SHA-256: {item.contract_sha256}</p>
        <p><Link to={`/ops/missions/${encodeURIComponent(item.mission_id)}`}>Inspect Mission and delegation</Link></p>
        <ul>{item.validation.checks.map(check => <li key={check.kind}>{safeText(check.kind, 40)} <StatusChip tone={check.status === 'PASS' ? 'ok' : 'warn'} label={safeText(check.status, 32)} /> {check.failure_codes?.join(', ')}</li>)}</ul>
        {item.expected_artifacts.length ? <ul>{item.expected_artifacts.map((artifact, index) => <li key={index}>{safeText(artifact.basename, 120)} — expected SHA-256: {artifact.sha256 ?? 'not declared'}; minimum bytes: {artifact.min_bytes ?? 0}</li>)}</ul> : <p>No artifacts declared.</p>}
        <p className="fd-hint">These are recorded validation results and declared artifact requirements. They are not a fresh artifact read or an automatic acceptance.</p>
      </PanelCard>
      {accepted ? <PanelCard title="Review recorded">
        <p>Reconcile the original delegation to check its current evidence. Final Mission approval remains separate.</p>
        <GatedActionButton tool="hermes_delegation_reconcile" args={{delegation_id: item.delegation_id}} label="Reconcile delegation" confirmArg={false} previewChars={2400} />
      </PanelCard> : <PanelCard title="Your decision"><div className="fd-stack">
        <label>Reviewer identity<input className="fd-input" maxLength={128} value={reviewer} onChange={e => setReviewer(e.target.value)} /></label>
        {selfReview ? <p role="alert">Choose a reviewer who did not execute this work.</p> : null}
        <label>Verdict<select className="fd-input" value={verdict} onChange={e => setVerdict(e.target.value)}><option value="SATISFIED">Requirements satisfied</option><option value="NOT_SATISFIED">Requirements not satisfied</option></select></label>
        <label>Evidence references — one per line<textarea className="fd-input" maxLength={3000} value={refs} onChange={e => setRefs(e.target.value)} /></label>
        <label>Approval reference<input className="fd-input" maxLength={256} value={reference} onChange={e => setReference(e.target.value)} /></label>
        <GatedActionButton key={JSON.stringify(args)} tool="hermes_review_accept" args={args} label="Record review" levelTag="owner" disabled={disabled} previewChars={2400} onApplied={() => setAccepted(true)} />
        <button className="fd-btn fd-btn--ghost" onClick={() => setGeneration(g => g + 1)}>Refresh evidence</button>
      </div></PanelCard>}
    </> : null}
  </section>;
}
