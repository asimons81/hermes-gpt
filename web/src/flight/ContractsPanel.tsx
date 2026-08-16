// ContractsPanel — contract evidence read-model (review acceptances +
// swarm workflow references). Detail view shows review acceptances per sha.
import { useEffect, useState } from 'react';
import { Link, useParams } from 'react-router-dom';

import { api, ApiError } from '../api/client';
import { EmptyState, ErrorState, LoadingState, PanelCard, StatusChip, safeText } from './ui';

interface AcceptanceRecord {
  record_id?: string;
  contract_sha256?: string;
  task_id?: string;
  assignee?: string;
  reviewer?: string;
  verdict?: string;
  evidence_refs?: string[];
  created_at?: string;
  [key: string]: unknown;
}

interface ContractsEnvelope {
  success: boolean;
  source?: string;
  count?: number;
  review_acceptances: AcceptanceRecord[];
  workflows: Array<{ workflow_id?: string; title?: string; status?: string; [key: string]: unknown }>;
}

export function ContractsPanel() {
  const [envelope, setEnvelope] = useState<ContractsEnvelope | null>(null);
  const [status, setStatus] = useState<'idle' | 'loading' | 'ready' | 'error'>('idle');
  const [error, setError] = useState<string | null>(null);

  function load() {
    setStatus('loading');
    setError(null);
    api
      .get<ContractsEnvelope>('/api/ops/contracts')
      .then((env) => {
        setEnvelope(env);
        setStatus('ready');
      })
      .catch((err: unknown) => {
        setError(err instanceof ApiError ? `${err.code}: ${err.message}` : 'contracts read failed');
        setStatus('error');
      });
  }

  useEffect(load, []);

  return (
    <section className="fd-surface" data-surface="contracts">
      <header className="fd-surface-head">
        <h2>Contracts</h2>
        <p className="fd-surface-desc">Work contract evidence — review acceptances and workflow references (bodies never copied).</p>
      </header>

      {status === 'loading' ? <LoadingState label="Loading contracts…" /> : null}
      {status === 'error' ? <ErrorState message={error ?? 'contracts read failed'} onRetry={load} /> : null}

      {envelope ? (
        <div className="fd-stack">
          <PanelCard title={`Review acceptances (${envelope.review_acceptances.length})`}>
            {envelope.review_acceptances.length === 0 ? (
              <EmptyState label="No review acceptances yet" />
            ) : (
              <ul className="fd-list">
                {envelope.review_acceptances.slice(0, 30).map((rec) => (
                  <li key={rec.record_id ?? rec.contract_sha256} className="fd-list-item">
                    <div className="fd-row">
                      <StatusChip tone={rec.verdict === 'SATISFIED' ? 'ok' : 'deny'} label={safeText(rec.verdict, 30)} />
                      <span className="fd-mono">{safeText(rec.contract_sha256, 24)}…</span>
                      <Link className="fd-btn fd-btn--ghost" to={`/ops/contracts/${rec.contract_sha256}`}>
                        Detail
                      </Link>
                    </div>
                    <p className="fd-hint">
                      {safeText(rec.task_id, 60)} · reviewer {safeText(rec.reviewer, 40)}
                    </p>
                  </li>
                ))}
              </ul>
            )}
          </PanelCard>

          <PanelCard title={`Workflow references (${envelope.workflows.length})`}>
            {envelope.workflows.length === 0 ? (
              <EmptyState label="No workflow references" />
            ) : (
              <ul className="fd-list">
                {envelope.workflows.slice(0, 20).map((wf, idx) => (
                  <li key={idx} className="fd-list-item">
                    <div className="fd-row">
                      <span className="fd-mono">{safeText(wf.workflow_id, 60)}</span>
                      <StatusChip tone={wf.status === 'done' ? 'ok' : 'flight'} label={safeText(wf.status, 30)} />
                    </div>
                    <p className="fd-hint">{safeText(wf.title, 120)}</p>
                  </li>
                ))}
              </ul>
            )}
          </PanelCard>
        </div>
      ) : null}
    </section>
  );
}

export function ContractDetail() {
  const { contractSha256 = '' } = useParams<{ contractSha256: string }>();
  const [records, setRecords] = useState<AcceptanceRecord[]>([]);
  const [status, setStatus] = useState<'idle' | 'loading' | 'ready' | 'error' | 'notfound'>('idle');
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    setStatus('loading');
    api
      .get<{ success: boolean; contract_sha256: string; records: AcceptanceRecord[] }>(
        `/api/ops/review/${contractSha256}`,
      )
      .then((env) => {
        if (cancelled) return;
        setRecords(env.records ?? []);
        setStatus('ready');
      })
      .catch((err: unknown) => {
        if (cancelled) return;
        if (err instanceof ApiError && err.code === 'NOT_FOUND') {
          setStatus('notfound');
        } else {
          setError(err instanceof ApiError ? `${err.code}: ${err.message}` : 'contract detail failed');
          setStatus('error');
        }
      });
    return () => {
      cancelled = true;
    };
  }, [contractSha256]);

  return (
    <section className="fd-surface" data-surface="contract-detail">
      <header className="fd-surface-head">
        <h2>Contract {contractSha256.slice(0, 16)}…</h2>
        <Link className="fd-btn fd-btn--ghost" to="/ops/contracts">
          ← All contracts
        </Link>
      </header>

      {status === 'loading' ? <LoadingState label="Loading contract…" /> : null}
      {status === 'error' ? <ErrorState message={error ?? 'contract detail failed'} /> : null}
      {status === 'notfound' ? <EmptyState label="No evidence found for this contract" /> : null}

      {status === 'ready' ? (
        records.length === 0 ? (
          <EmptyState label="No review acceptances for this contract" />
        ) : (
          <div className="fd-stack">
            {records.map((rec) => (
              <PanelCard key={rec.record_id ?? rec.created_at} title="Review acceptance">
                <div className="fd-row">
                  <StatusChip tone={rec.verdict === 'SATISFIED' ? 'ok' : 'deny'} label={safeText(rec.verdict, 30)} />
                  <span className="fd-mono">{safeText(rec.record_id, 40)}</span>
                </div>
                <p className="fd-hint">
                  task {safeText(rec.task_id, 60)} · assignee {safeText(rec.assignee, 40)} · reviewer{' '}
                  {safeText(rec.reviewer, 40)}
                </p>
                {Array.isArray(rec.evidence_refs) ? (
                  <ul className="fd-list">
                    {rec.evidence_refs.slice(0, 10).map((ref, idx) => (
                      <li key={idx} className="fd-list-item">
                        {safeText(ref, 160)}
                      </li>
                    ))}
                  </ul>
                ) : null}
                {rec.created_at ? <p className="fd-hint">{safeText(rec.created_at, 60)}</p> : null}
              </PanelCard>
            ))}
          </div>
        )
      ) : null}
    </section>
  );
}
