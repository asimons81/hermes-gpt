// FleetPanel — fleet peers + authority state (read-only presentation).
import { useEffect } from 'react';

import { useFleetStore } from '../stores/fleet';
import { EmptyState, ErrorState, LoadingState, PanelCard, StatusChip, safeText } from './ui';
import type { MissionEnvelope } from './types';

function FleetBody({ envelope }: { envelope: MissionEnvelope }) {
  const data = (envelope.data ?? {}) as Record<string, unknown>;
  if (envelope.available === false) {
    return (
      <div className="fd-unavailable" role="status">
        {envelope.unavailable_reason || envelope.safe_message || 'Fleet authority not configured'}
      </div>
    );
  }
  const peers = Array.isArray(data.peers) ? (data.peers as unknown[]) : [];
  const served = Array.isArray(data.served_profiles) ? (data.served_profiles as unknown[]) : [];
  return (
    <div className="fd-stack">
      <PanelCard title="Authority" aside={<StatusChip tone={String(data.authority) === 'configured' ? 'ok' : 'warn'} label={safeText(data.authority, 40)} />}>
        <p className="fd-hint">A2A URLs, tokens, and task payloads are never surfaced.</p>
      </PanelCard>
      <PanelCard title={`Peers (${peers.length})`}>
        {peers.length === 0 ? <EmptyState label="No registered peers" /> : <ul className="fd-list">{peers.slice(0, 20).map((p, i) => <li key={i} className="fd-list-item">{safeText(JSON.stringify(p), 200)}</li>)}</ul>}
      </PanelCard>
      <PanelCard title={`Served profiles (${served.length})`}>
        {served.length === 0 ? <EmptyState label="None" /> : <ul className="fd-list">{served.slice(0, 20).map((s, i) => <li key={i} className="fd-list-item">{safeText(s, 120)}</li>)}</ul>}
      </PanelCard>
    </div>
  );
}

export function FleetPanel() {
  const { fleet, status, error, fetchFleet, fetchSwarm } = useFleetStore();
  useEffect(() => {
    void fetchFleet();
    void fetchSwarm();
  }, [fetchFleet, fetchSwarm]);

  return (
    <section className="fd-surface" data-surface="fleet">
      <header className="fd-surface-head">
        <h2>Fleet</h2>
      </header>
      {status === 'loading' && !fleet ? <LoadingState label="Loading fleet…" /> : null}
      {status === 'error' && !fleet ? <ErrorState message={error ?? 'fleet failed'} onRetry={() => void fetchFleet()} /> : null}
      {fleet ? <FleetBody envelope={fleet} /> : null}
    </section>
  );
}
