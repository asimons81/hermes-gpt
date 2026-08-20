// FabricPanel — distributed Fabric placement/evidence visibility, read-only by design.
import { useEffect, useState } from 'react';
import { Link, useParams } from 'react-router-dom';

import { api, ApiError } from '../api/client';
import { EmptyState, ErrorState, LoadingState, PanelCard, StatusChip, safeText } from './ui';

type Tone = 'ok' | 'review' | 'flight' | 'warn';

interface NodeRow {
  name: string;
  identity?: string;
  availability?: string;
  freshness?: string;
  active?: number | null;
  capacity?: number | null;
  authority_ceiling?: string;
  remote_backends?: string[];
  capabilities?: Record<string, unknown>;
}

interface AttemptRow {
  attempt_id: string;
  task_id?: string;
  node?: string;
  backend?: string;
  placement_mode?: string;
  state?: string;
  blocker?: string;
  authority?: Record<string, unknown>;
  authority_ceiling?: string;
  routing?: Record<string, unknown> | null;
  evidence?: Record<string, unknown> | null;
  artifacts?: Array<Record<string, unknown>>;
  events?: Array<Record<string, unknown>>;
  active_content_policy?: string;
}

interface NodesPayload {
  success: boolean;
  available: boolean;
  health_note?: string;
  nodes?: NodeRow[];
}

interface AttemptsPayload {
  success: boolean;
  available: boolean;
  attempts?: AttemptRow[];
}

interface AttemptPayload {
  success: boolean;
  available: boolean;
  code?: string;
  safe_message?: string;
  attempt?: AttemptRow;
}

function tone(value: unknown): Tone {
  const text = String(value ?? '').toLowerCase();
  if (['completed', 'observed', 'fresh', 'succeeded'].includes(text)) return 'ok';
  if (['blocked', 'failed', 'stale', 'disabled', 'submission_ambiguous', 'cancel_ambiguous'].includes(text)) return 'warn';
  if (['evidence_pending', 'reconciling', 'cancel_requested'].includes(text)) return 'review';
  return 'flight';
}

function JsonSummary({ value, limit = 900 }: { value: unknown; limit?: number }) {
  return <pre className="fd-pre fd-pre--row">{safeText(JSON.stringify(value ?? {}), limit)}</pre>;
}

export function FabricPanel() {
  const [nodes, setNodes] = useState<NodesPayload | null>(null);
  const [attempts, setAttempts] = useState<AttemptsPayload | null>(null);
  const [error, setError] = useState<string | null>(null);

  const load = async () => {
    setError(null);
    try {
      const [nodeData, attemptData] = await Promise.all([
        api.get<NodesPayload>('/api/ops/fabric/nodes'),
        api.get<AttemptsPayload>('/api/ops/fabric/attempts?limit=50'),
      ]);
      setNodes(nodeData);
      setAttempts(attemptData);
    } catch (err: unknown) {
      setError(err instanceof ApiError ? `${err.code}: ${err.message}` : 'Fabric read model failed');
    }
  };

  useEffect(() => {
    void load();
  }, []);

  const nodeRows = nodes?.nodes ?? [];
  const attemptRows = attempts?.attempts ?? [];

  return (
    <section className="fd-surface" data-surface="fabric">
      <header className="fd-surface-head">
        <h2>Fabric</h2>
        <p className="fd-surface-desc">Distributed placement, evidence, and recovery state. This screen is observational and cannot contact or mutate a peer.</p>
      </header>

      {!nodes && !attempts && !error ? <LoadingState label="Loading Fabric…" /> : null}
      {error ? <ErrorState message={error} onRetry={() => void load()} /> : null}

      {nodes ? (
        <PanelCard title={`Managed nodes (${nodeRows.length})`}>
          {nodes.health_note ? <p className="fd-hint">{safeText(nodes.health_note, 240)}</p> : null}
          {nodeRows.length === 0 ? <EmptyState label="No enrolled Fabric nodes" /> : (
            <ul className="fd-list">
              {nodeRows.map((node) => (
                <li className="fd-list-item" key={node.name}>
                  <div className="fd-row">
                    <strong>{safeText(node.name, 64)}</strong>
                    <StatusChip tone={tone(node.availability)} label={safeText(node.availability, 40)} />
                    <StatusChip tone={tone(node.freshness)} label={safeText(node.freshness, 40)} />
                  </div>
                  <p className="fd-hint">Identity {safeText(node.identity, 128)} · authority ceiling {safeText(node.authority_ceiling, 40)} · load {String(node.active ?? '?')}/{String(node.capacity ?? '?')}</p>
                  <p className="fd-hint">Runners {safeText((node.remote_backends ?? []).join(', '), 180)}</p>
                  <JsonSummary value={node.capabilities} limit={500} />
                </li>
              ))}
            </ul>
          )}
        </PanelCard>
      ) : null}

      {attempts ? (
        <PanelCard title={`Remote attempts (${attemptRows.length})`}>
          {attemptRows.length === 0 ? <EmptyState label="No Fabric attempts" /> : (
            <ul className="fd-list">
              {attemptRows.map((attempt) => (
                <li className="fd-list-item" key={attempt.attempt_id}>
                  <div className="fd-row">
                    <span className="fd-mono">{safeText(attempt.attempt_id, 80)}</span>
                    <StatusChip tone={tone(attempt.state)} label={safeText(attempt.state, 40)} />
                    <Link className="fd-btn fd-btn--ghost" to={`/ops/fabric/${attempt.attempt_id}`}>Details</Link>
                  </div>
                  <p className="fd-hint">{safeText(attempt.task_id, 100)} · {safeText(attempt.node, 64)} / {safeText(attempt.backend, 64)} · {safeText(attempt.placement_mode, 32)} placement</p>
                  {attempt.blocker ? <p className="fd-hint">Blocked by {safeText(attempt.blocker, 140)}</p> : null}
                </li>
              ))}
            </ul>
          )}
        </PanelCard>
      ) : null}
    </section>
  );
}

export function FabricAttemptDetail() {
  const { attemptId = '' } = useParams<{ attemptId: string }>();
  const [payload, setPayload] = useState<AttemptPayload | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    setPayload(null);
    setError(null);
    api.get<AttemptPayload>(`/api/ops/fabric/attempts/${attemptId}`)
      .then((data) => { if (!cancelled) setPayload(data); })
      .catch((err: unknown) => {
        if (!cancelled) setError(err instanceof ApiError ? `${err.code}: ${err.message}` : 'Fabric attempt read failed');
      });
    return () => { cancelled = true; };
  }, [attemptId]);

  const attempt = payload?.attempt;
  const routing = attempt?.routing ?? null;
  const candidates = Array.isArray(routing?.candidates) ? routing.candidates as Array<Record<string, unknown>> : [];
  const artifacts = attempt?.artifacts ?? [];
  const events = attempt?.events ?? [];

  return (
    <section className="fd-surface" data-surface="fabric-attempt">
      <header className="fd-surface-head">
        <div>
          <h2>Fabric attempt</h2>
          <p className="fd-mono">{safeText(attemptId, 100)}</p>
        </div>
        <Link className="fd-btn fd-btn--ghost" to="/ops/fabric">← Fabric</Link>
      </header>

      {!payload && !error ? <LoadingState label="Loading attempt…" /> : null}
      {error ? <ErrorState message={error} /> : null}
      {payload && !attempt ? <EmptyState label={payload.safe_message || payload.code || 'Attempt unavailable'} /> : null}

      {attempt ? (
        <div className="fd-stack">
          <PanelCard title="Placement" aside={<StatusChip tone={tone(attempt.state)} label={safeText(attempt.state, 40)} />}>
            <p className="fd-hint">{safeText(attempt.node, 64)} / {safeText(attempt.backend, 64)} · {safeText(attempt.placement_mode, 32)} placement</p>
            {attempt.blocker ? <p className="fd-hint">Current blocker: {safeText(attempt.blocker, 160)}</p> : <p className="fd-hint">No current blocker recorded.</p>}
          </PanelCard>

          <PanelCard title="Authority received">
            <JsonSummary value={{ authority: attempt.authority, node_ceiling: attempt.authority_ceiling }} limit={700} />
          </PanelCard>

          <PanelCard title="Router explanation">
            {!routing ? <EmptyState label="Explicit placement or no routing receipt" /> : (
              <>
                <JsonSummary value={{ requirements: routing.requirements, selected: routing.selected, explanation_available: routing.explanation_available }} limit={900} />
                {candidates.length ? <ul className="fd-list">{candidates.slice(0, 40).map((candidate, index) => (
                  <li className="fd-list-item" key={index}><JsonSummary value={candidate} limit={700} /></li>
                ))}</ul> : <p className="fd-hint">This routing receipt predates detailed G4-D candidate persistence.</p>}
              </>
            )}
          </PanelCard>

          <PanelCard title="Remote evidence">
            {attempt.evidence ? <JsonSummary value={attempt.evidence} limit={1200} /> : <EmptyState label="No admitted evidence yet" />}
          </PanelCard>

          <PanelCard title={`Artifacts (${artifacts.length})`}>
            <p className="fd-hint">{safeText(attempt.active_content_policy, 260)}</p>
            {artifacts.length === 0 ? <EmptyState label="No admitted artifacts" /> : <ul className="fd-list">{artifacts.map((artifact, index) => (
              <li className="fd-list-item" key={index}>
                <div className="fd-row">
                  <span>{safeText(artifact.logical_name, 180)}</span>
                  {artifact.active_content ? <StatusChip tone="warn" label="isolated" /> : <StatusChip tone="flight" label="metadata" />}
                </div>
                <JsonSummary value={artifact} limit={650} />
              </li>
            ))}</ul>}
          </PanelCard>

          <PanelCard title={`Recovery/event history (${events.length})`}>
            {events.length === 0 ? <EmptyState label="No Fabric audit events for this attempt" /> : <ul className="fd-list">{events.map((event, index) => (
              <li className="fd-list-item" key={index}><JsonSummary value={event} limit={600} /></li>
            ))}</ul>}
          </PanelCard>

          <PanelCard title="Interventions">
            <p className="fd-hint">No direct peer controls exist on this screen. Cancel, reconcile, retry, or other interventions must continue through an existing policy-gated Hermes operator tool.</p>
          </PanelCard>
        </div>
      ) : null}
    </section>
  );
}
