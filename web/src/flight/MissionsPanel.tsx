// MissionsPanel — v0.9 first-class Mission visibility. Read-only by design.
import { useEffect, useState } from 'react';
import { Link, useParams } from 'react-router-dom';

import { api, ApiError } from '../api/client';
import { EmptyState, ErrorState, LoadingState, PanelCard, StatusChip, safeText } from './ui';

type Tone = 'ok' | 'review' | 'flight' | 'warn' | 'deny' | 'neutral';

interface ContextRef { kind?: string; ref?: string; label?: string; sha256?: string }
interface SkillRef { name?: string; version?: string; ref?: string; sha256?: string }
interface Attachment { kind?: string; ref?: string; relationship?: string; state?: string; evidence_ref?: string; verified?: boolean | number; updated_at?: string }
interface MissionEvent { seq?: number; event_type?: string; from_status?: string; to_status?: string; reason_sha256?: string; created_at?: string; details?: Record<string, unknown> }
interface MissionRow {
  mission_id: string;
  title?: string;
  objective?: string;
  owner_profile?: string;
  acceptance_criteria?: string[];
  context_refs?: ContextRef[];
  skills?: SkillRef[];
  final_approval_required?: boolean;
  status?: string;
  version?: number;
  approval?: Record<string, unknown>;
  attachments?: Attachment[];
  events?: MissionEvent[];
  created_at?: string;
  updated_at?: string;
}
interface DelegationRow {
  delegation_id: string;
  mission_id?: string;
  task_id?: string;
  contract_sha256?: string;
  backend?: string;
  state?: string;
  backend_state?: string;
  outcome?: string;
  validation_verdict?: string;
  cancel_requested?: boolean;
  updated_at?: string;
  terminal_at?: string;
}
interface MissionListPayload { missions?: MissionRow[]; count?: number; live_cursor?: number; read_only?: boolean }
interface MissionDetailPayload { mission: MissionRow; delegations?: DelegationRow[]; delegation_count?: number; live_cursor?: number; read_only?: boolean }
interface LiveEventsPayload { cursor: number; next_cursor: number; high_watermark: number; events?: Array<Record<string, unknown>>; count?: number }

function tone(value: unknown): Tone {
  const text = String(value ?? '').toLowerCase();
  if (['succeeded', 'completed', 'approved', 'done'].includes(text)) return 'ok';
  if (['failed', 'cancelled', 'denied'].includes(text)) return 'deny';
  if (['blocked', 'reconciling', 'awaiting_approval'].includes(text)) return 'warn';
  if (['running', 'queued', 'pending', 'active'].includes(text)) return 'flight';
  if (['draft', 'paused', 'review'].includes(text)) return 'review';
  return 'neutral';
}
function validationTone(value: unknown): Tone {
  const text = String(value ?? '').toUpperCase();
  if (text === 'SATISFIED') return 'ok';
  if (['NOT_SATISFIED', 'UNSATISFIED', 'FAILED'].includes(text)) return 'deny';
  return 'warn';
}
function compactHash(value: unknown): string { const text = String(value ?? ''); return text.length > 16 ? `${text.slice(0, 12)}…` : text; }
function fmtTime(value: unknown): string { if (!value) return '—'; const date = new Date(String(value)); return Number.isNaN(date.getTime()) ? safeText(value, 80) : date.toLocaleString(); }
function errorText(err: unknown, fallback: string): string { return err instanceof ApiError ? `${err.code}: ${err.message}` : fallback; }

export function MissionsPanel() {
  const [payload, setPayload] = useState<MissionListPayload | null>(null);
  const [error, setError] = useState<string | null>(null);
  const load = async () => {
    setError(null);
    try { setPayload(await api.get<MissionListPayload>('/api/ops/missions?limit=100')); }
    catch (err: unknown) { setError(errorText(err, 'Mission list failed')); }
  };
  useEffect(() => {
    void load();
    const timer = window.setInterval(() => void load(), 10_000);
    return () => window.clearInterval(timer);
  }, []);
  const rows = payload?.missions ?? [];
  return <section className="fd-surface" data-surface="missions" data-testid="missions-panel">
    <header className="fd-surface-head"><h2>Missions</h2><p className="fd-surface-desc">First-class v0.9 Mission state and lineage. This screen is observational; execution and approval authority remain in gated operator tools.</p></header>
    {!payload && !error ? <LoadingState label="Loading Missions…" /> : null}
    {error ? <ErrorState message={error} onRetry={() => void load()} /> : null}
    {payload ? <PanelCard title={`Missions (${rows.length})`}>
      {rows.length === 0 ? <EmptyState label="No durable Missions" /> : <ul className="fd-list">{rows.map((mission) => <li className="fd-list-item" key={mission.mission_id}>
        <div className="fd-row"><Link to={`/ops/missions/${encodeURIComponent(mission.mission_id)}`}><strong>{safeText(mission.title || mission.mission_id, 120)}</strong></Link><StatusChip tone={tone(mission.status)} label={safeText(mission.status || 'unknown', 40)} /></div>
        <div className="fd-row fd-row--subtle"><span>{safeText(mission.mission_id, 96)}</span><span>owner {safeText(mission.owner_profile || 'default', 64)}</span><span>updated {fmtTime(mission.updated_at)}</span></div>
        {mission.objective ? <p className="fd-hint">{safeText(mission.objective, 260)}</p> : null}
      </li>)}</ul>}
    </PanelCard> : null}
  </section>;
}

export function MissionDetail() {
  const { missionId = '' } = useParams();
  const [payload, setPayload] = useState<MissionDetailPayload | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [liveState, setLiveState] = useState<'connecting' | 'live' | 'retrying'>('connecting');
  useEffect(() => {
    let active = true;
    let cursor = 0;
    const snapshot = async (): Promise<number> => {
      const data = await api.get<MissionDetailPayload>(`/api/ops/missions/${encodeURIComponent(missionId)}`);
      if (active) { setPayload(data); setError(null); }
      return Number(data.live_cursor ?? cursor);
    };
    const run = async () => {
      try { cursor = await snapshot(); } catch (err: unknown) { if (active) setError(errorText(err, 'Mission detail failed')); return; }
      if (active) setLiveState('live');
      while (active) {
        try {
          const events = await api.get<LiveEventsPayload>(`/api/ops/missions/${encodeURIComponent(missionId)}/events?cursor=${cursor}&limit=100&wait_ms=15000`);
          cursor = Number(events.next_cursor ?? cursor);
          if (!active) return;
          setLiveState('live');
          if ((events.count ?? 0) > 0) cursor = Math.max(cursor, await snapshot());
        } catch (err: unknown) {
          if (!active) return;
          setLiveState('retrying');
          setError(errorText(err, 'Mission live update failed'));
          await new Promise((resolve) => window.setTimeout(resolve, 1500));
        }
      }
    };
    void run();
    return () => { active = false; };
  }, [missionId]);
  const mission = payload?.mission;
  const attachments = mission?.attachments ?? [];
  const delegations = payload?.delegations ?? [];
  const events = mission?.events ?? [];
  const context = mission?.context_refs ?? [];
  const skills = mission?.skills ?? [];
  const acceptance = mission?.acceptance_criteria ?? [];
  return <section className="fd-surface" data-surface="mission-detail" data-testid="mission-detail">
    <header className="fd-surface-head">
      <div className="fd-row"><h2>{safeText(mission?.title || missionId, 140)}</h2>{mission ? <StatusChip tone={tone(mission.status)} label={safeText(mission.status || 'unknown', 40)} /> : null}<StatusChip tone={liveState === 'live' ? 'ok' : 'warn'} label={liveState === 'live' ? 'live updates' : liveState} /></div>
      <p className="fd-surface-desc">Durable Mission snapshot with live wake-up refresh. Read-only presentation; state transitions and approvals remain gated elsewhere.</p>
      <Link className="fd-btn fd-btn--ghost" to="/ops/missions">← Missions</Link>
    </header>
    {!payload && !error ? <LoadingState label="Loading Mission…" /> : null}
    {error ? <ErrorState message={error} /> : null}
    {mission ? <>
      <PanelCard title="Mission">
        <div className="fd-grid fd-grid--2"><div><span className="fd-label">Mission ID</span><div>{safeText(mission.mission_id, 120)}</div></div><div><span className="fd-label">Owner</span><div>{safeText(mission.owner_profile || 'default', 80)}</div></div><div><span className="fd-label">Version</span><div>{String(mission.version ?? '—')}</div></div><div><span className="fd-label">Updated</span><div>{fmtTime(mission.updated_at)}</div></div></div>
        {mission.objective ? <><span className="fd-label">Objective</span><p>{safeText(mission.objective, 900)}</p></> : null}
        <div className="fd-row fd-row--subtle"><span>final approval {mission.final_approval_required ? 'required' : 'not required'}</span>{mission.approval && Object.keys(mission.approval).length > 0 ? <span>approval record present</span> : null}</div>
      </PanelCard>
      <div className="fd-grid fd-grid--2">
        <PanelCard title={`Acceptance criteria (${acceptance.length})`}>{acceptance.length ? <ul className="fd-list">{acceptance.map((item, index) => <li key={`${index}-${item}`} className="fd-list-item">{safeText(item, 500)}</li>)}</ul> : <EmptyState />}</PanelCard>
        <PanelCard title={`Context (${context.length})`}>{context.length ? <ul className="fd-list">{context.map((item, index) => <li className="fd-list-item" key={`${item.ref ?? index}`}><strong>{safeText(item.label || item.ref || item.kind || 'context', 160)}</strong><div className="fd-row fd-row--subtle"><span>{safeText(item.kind || 'ref', 48)}</span><span>{safeText(item.ref || '', 180)}</span>{item.sha256 ? <span>sha {compactHash(item.sha256)}</span> : null}</div></li>)}</ul> : <EmptyState />}</PanelCard>
      </div>
      <PanelCard title={`Skills (${skills.length})`}>{skills.length ? <ul className="fd-list">{skills.map((skill, index) => <li className="fd-list-item" key={`${skill.name ?? index}-${skill.version ?? ''}`}><div className="fd-row"><strong>{safeText(skill.name || 'skill', 120)}</strong>{skill.version ? <span>v{safeText(skill.version, 40)}</span> : null}</div><div className="fd-row fd-row--subtle">{skill.ref ? <span>{safeText(skill.ref, 180)}</span> : null}{skill.sha256 ? <span>sha {compactHash(skill.sha256)}</span> : null}</div></li>)}</ul> : <EmptyState label="No explicit skills manifest" />}</PanelCard>
      <PanelCard title={`Delegations (${delegations.length})`}>{delegations.length ? <ul className="fd-list">{delegations.map((delegation) => <li className="fd-list-item" key={delegation.delegation_id}><div className="fd-row"><strong>{safeText(delegation.delegation_id, 110)}</strong><span className="fd-label">Execution</span><StatusChip tone={tone(delegation.state)} label={safeText(delegation.state || 'unknown', 40)} /><span className="fd-label">Validation</span><StatusChip tone={validationTone(delegation.validation_verdict)} label={safeText(delegation.validation_verdict || 'UNVERIFIED', 64)} /><span>{safeText(delegation.backend || 'runner', 64)}</span></div><div className="fd-row fd-row--subtle">{delegation.task_id ? <span>task {safeText(delegation.task_id, 110)}</span> : null}{delegation.backend_state ? <span>backend {safeText(delegation.backend_state, 80)}</span> : null}<span>updated {fmtTime(delegation.updated_at)}</span></div></li>)}</ul> : <EmptyState label="No linked delegations" />}</PanelCard>
      <PanelCard title={`Attachments (${attachments.length})`}>{attachments.length ? <ul className="fd-list">{attachments.map((attachment, index) => <li className="fd-list-item" key={`${attachment.kind ?? ''}-${attachment.ref ?? index}`}><div className="fd-row"><strong>{safeText(attachment.kind || 'attachment', 64)}</strong><StatusChip tone={tone(attachment.state)} label={safeText(attachment.state || 'unknown', 40)} />{attachment.state === 'succeeded' ? <StatusChip tone={Boolean(attachment.verified) ? 'ok' : 'warn'} label={Boolean(attachment.verified) ? 'verified evidence' : 'verification missing'} /> : null}<span>{safeText(attachment.ref || '', 180)}</span></div><div className="fd-row fd-row--subtle">{attachment.relationship ? <span>{safeText(attachment.relationship, 80)}</span> : null}{attachment.evidence_ref ? <span>evidence {safeText(attachment.evidence_ref, 160)}</span> : null}</div></li>)}</ul> : <EmptyState label="No Mission attachments" />}</PanelCard>
      <PanelCard title={`Mission events (${events.length})`}>{events.length ? <ul className="fd-list">{events.slice(0, 50).map((event, index) => <li className="fd-list-item" key={`${event.seq ?? index}-${event.event_type ?? ''}`}><div className="fd-row"><strong>{safeText(event.event_type || 'mission.event', 100)}</strong><span>{fmtTime(event.created_at)}</span></div><div className="fd-row fd-row--subtle">{event.from_status ? <span>{safeText(event.from_status, 48)}</span> : null}{event.to_status ? <span>→ {safeText(event.to_status, 48)}</span> : null}{event.reason_sha256 ? <span>reason {compactHash(event.reason_sha256)}</span> : null}</div></li>)}</ul> : <EmptyState label="No Mission events" />}</PanelCard>
    </> : null}
  </section>;
}
