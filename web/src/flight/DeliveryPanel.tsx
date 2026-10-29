import { useEffect, useState } from 'react';
import { api } from '../api/client';
import { ErrorState, LoadingState, PanelCard, safeText } from './ui';
interface Delivery {enabled: boolean; configured?: boolean; hermes_gpt_version: string; agent_compatibility: string; inbox_complete?: boolean; error?: string; targets?: Array<{backend: string; target: string; pending: number | null; last_warning?: {code: string}; last_error?: unknown; next_attempt_at?: string; failure_count: number}>;}
export function DeliveryPanel() {
  const [data, setData] = useState<Delivery | null>(null);
  const [error, setError] = useState<string | null>(null);
  async function load() { try { setData(await api.get<Delivery>('/api/ops/delivery-status')); setError(null); } catch { setError('Delivery status is unavailable.'); } }
  useEffect(() => { void load(); }, []);
  return <section className="fd-surface"><header className="fd-surface-head"><h2>Delivery status</h2><p>Read-only notification health. Checking this page sends nothing.</p><button className="fd-btn" onClick={() => void load()}>Refresh</button></header>
    {error ? <ErrorState message={error} /> : null}
    {!data && !error ? <LoadingState /> : null}
    {data ? <><PanelCard title="Compatibility"><p>Hermes GPT {safeText(data.hermes_gpt_version, 40)}</p><p>Agent compatibility has not been probed here. Session control checks finite stream-json support before submission; that check does not prove live Desktop acceptance.</p></PanelCard>
    <PanelCard title={data.enabled ? data.configured ? 'Relay enabled' : 'Relay configuration unavailable' : 'Relay disabled'}>
      {data.error ? <p role="alert">{safeText(data.error, 80)}</p> : null}
      {data.inbox_complete === false ? <p role="alert">Some inbox sources are unavailable; pending counts may be incomplete.</p> : null}
      {data.targets?.map((target, index) => <div className="fd-list-item" key={index}><strong>{safeText(target.backend, 30)} · {safeText(target.target, 80)}</strong><p>Pending: {target.pending ?? 'unknown'} · Failures: {target.failure_count}</p>
        {target.last_warning?.code === 'SENT_UNVERIFIED' ? <p role="status">Sent, delivery unverified. Suppressed from resend to avoid duplicates.</p> : null}
        {target.last_error ? <p>Last error: {safeText(JSON.stringify(target.last_error), 200)}</p> : null}
        {target.next_attempt_at ? <p>Next attempt: {safeText(target.next_attempt_at, 60)}</p> : null}
      </div>)}
    </PanelCard></> : null}
  </section>;
}
