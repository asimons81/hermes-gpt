// SwarmMonitor — workflow instances + stage status + gated stage advance.
import { useEffect, useState } from 'react';
import { Link, useParams } from 'react-router-dom';

import { api, ApiError } from '../api/client';
import { useFleetStore } from '../stores/fleet';
import { GatedActionButton } from './GatedActionButton';
import { EmptyState, ErrorState, LoadingState, PanelCard, StatusChip, safeText } from './ui';
import type { MissionEnvelope } from './types';

interface WorkflowRow {
  workflow_id: string;
  title?: string;
  status?: string;
  [key: string]: unknown;
}

function workflowRows(envelope: MissionEnvelope): WorkflowRow[] {
  const rows = Array.isArray(envelope.workflows) ? envelope.workflows : [];
  return rows as WorkflowRow[];
}

function toneForStatus(status: unknown): 'ok' | 'review' | 'flight' | 'warn' {
  if (status === 'done') return 'ok';
  if (status === 'awaiting_approval') return 'review';
  if (status === 'blocked') return 'warn';
  return 'flight';
}

export function SwarmMonitor() {
  const { workflows, status, error, fetchSwarm } = useFleetStore();
  useEffect(() => {
    void fetchSwarm();
  }, [fetchSwarm]);

  const rows = workflows ? workflowRows(workflows) : [];

  return (
    <section className="fd-surface" data-surface="swarm">
      <header className="fd-surface-head">
        <h2>Swarm Monitor</h2>
        <p className="fd-surface-desc">Workflow instances and stage state. Stage advance stays gated (dry-run → confirm).</p>
      </header>

      {status === 'loading' && !workflows ? <LoadingState label="Loading swarm…" /> : null}
      {status === 'error' && !workflows ? <ErrorState message={error ?? 'swarm failed'} onRetry={() => void fetchSwarm()} /> : null}

      {workflows ? (
        rows.length === 0 ? (
          <EmptyState label="No workflows" />
        ) : (
          <div className="fd-stack">
            {rows.slice(0, 30).map((wf) => (
              <PanelCard
                key={wf.workflow_id}
                title={safeText(wf.title ?? wf.workflow_id, 80)}
                aside={<StatusChip tone={toneForStatus(wf.status)} label={safeText(wf.status, 40)} />}
              >
                <div className="fd-row">
                  <span className="fd-mono">{safeText(wf.workflow_id, 60)}</span>
                  <Link className="fd-btn fd-btn--ghost" to={`/ops/swarm/${wf.workflow_id}`}>
                    Details
                  </Link>
                </div>
              </PanelCard>
            ))}
          </div>
        )
      ) : null}
    </section>
  );
}

export function SwarmDetail() {
  const { workflowId = '' } = useParams<{ workflowId: string }>();
  const [detail, setDetail] = useState<MissionEnvelope | null>(null);
  const [status, setStatus] = useState<'idle' | 'loading' | 'ready' | 'error'>('idle');
  const [error, setError] = useState<string | null>(null);
  const [stageId, setStageId] = useState('');

  useEffect(() => {
    let cancelled = false;
    setStatus('loading');
    setError(null);
    api
      .get<MissionEnvelope>(`/api/ops/swarm/${workflowId}`)
      .then((envelope) => {
        if (cancelled) return;
        setDetail(envelope);
        setStatus('ready');
      })
      .catch((err: unknown) => {
        if (cancelled) return;
        const message = err instanceof ApiError ? `${err.code}: ${err.message}` : 'workflow status failed';
        setError(message);
        setStatus('error');
      });
    return () => {
      cancelled = true;
    };
  }, [workflowId]);

  return (
    <section className="fd-surface" data-surface="swarm-detail">
      <header className="fd-surface-head">
        <h2>Workflow {workflowId}</h2>
        <Link className="fd-btn fd-btn--ghost" to="/ops/swarm">
          ← All workflows
        </Link>
      </header>

      {status === 'loading' ? <LoadingState label="Loading workflow…" /> : null}
      {status === 'error' ? <ErrorState message={error ?? 'workflow status failed'} /> : null}

      {detail ? (
        <div className="fd-stack">
          <PanelCard
            title="Status"
            aside={<StatusChip tone={toneForStatus(detail.status)} label={safeText(detail.status, 40)} />}
          >
            <p className="fd-hint">{safeText(detail.title, 160)}</p>
            {detail.retention_note ? <p className="fd-hint">{safeText(detail.retention_note, 200)}</p> : null}
          </PanelCard>

          {Array.isArray(detail.stages) ? (
            <PanelCard title={`Stages (${detail.stages.length})`}>
              <ul className="fd-list">
                {detail.stages.slice(0, 40).map((st, idx) => (
                  <li key={idx} className="fd-list-item">
                    <div className="fd-row">
                      <span className="fd-mono">{safeText((st as Record<string, unknown>).id, 40)}</span>
                      <StatusChip tone={toneForStatus((st as Record<string, unknown>).status)} label={safeText((st as Record<string, unknown>).status, 40)} />
                    </div>
                    <pre className="fd-pre fd-pre--row">{safeText(JSON.stringify(st), 240)}</pre>
                  </li>
                ))}
              </ul>
            </PanelCard>
          ) : null}

          <PanelCard title="Actions">
            <label className="fd-field" htmlFor="swarm-stage-id">
              <span>Stage ID</span>
              <input
                id="swarm-stage-id"
                value={stageId}
                onChange={(event) => setStageId(event.target.value)}
                placeholder="Select a stage id shown above"
              />
            </label>
            <GatedActionButton
              tool="hermes_swarm_stage_advance"
              args={{ workflow_id: workflowId, stage_id: stageId.trim() }}
              label="Advance stage"
              levelTag="workspace"
              disabled={!stageId.trim()}
            />
            <p className="fd-hint">Planning is disabled until a stage id is supplied. The server remains authoritative for workflow state and policy.</p>
          </PanelCard>
        </div>
      ) : null}
    </section>
  );
}
