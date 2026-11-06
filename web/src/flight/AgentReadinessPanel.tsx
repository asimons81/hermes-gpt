import { useEffect, useRef, useState } from 'react';
import { api } from '../api/client';
import { ErrorState, LoadingState, PanelCard, StatusChip } from './ui';

interface Readiness {
  schema_version: 1; read_only: true; checked_at: string; hermes_gpt_version: string;
  cache_seconds: number; live_acceptance: 'not_verified'; session_control_enabled: boolean;
  agent_source: {state: 'available' | 'unavailable'; identity_sha256: string | null; revision: string | null};
  cli: {state: 'available' | 'missing'; resolution: 'agent_venv' | 'path' | 'unavailable'; identity_sha256: string | null; matches_agent_source: boolean | null};
  source_protocol: {state: 'declared' | 'not_declared' | 'unavailable'; code: string; parser_sha256: string | null; checked_at: string};
  finite_protocol: {state: 'supported' | 'unsupported' | 'unavailable' | 'not_observed'; code: string; checked_at: string | null; provenance: 'authorized_session_preflight' | 'not_observed'};
}

function decode(value: unknown): Readiness {
  const data = value as Readiness;
  if (!data || data.schema_version !== 1 || data.read_only !== true || data.live_acceptance !== 'not_verified'
    || typeof data.hermes_gpt_version !== 'string' || typeof data.checked_at !== 'string'
    || typeof data.session_control_enabled !== 'boolean' || typeof data.cache_seconds !== 'number'
    || !['available', 'unavailable'].includes(data.agent_source?.state)
    || !['available', 'missing'].includes(data.cli?.state)
    || !['agent_venv', 'path', 'unavailable'].includes(data.cli?.resolution)
    || ![true, false, null].includes(data.cli.matches_agent_source)
    || !['supported', 'unsupported', 'unavailable', 'not_observed'].includes(data.finite_protocol?.state)
    || !['declared', 'not_declared', 'unavailable'].includes(data.source_protocol?.state)
    || typeof data.source_protocol.checked_at !== 'string'
    || !Number.isFinite(Date.parse(data.checked_at)) || !Number.isFinite(Date.parse(data.source_protocol.checked_at))
    || data.checked_at !== data.source_protocol.checked_at
    || data.cache_seconds < 0 || data.cache_seconds > 300
    || (data.source_protocol.state === 'declared' && data.source_protocol.code !== 'DECLARED')
    || (data.source_protocol.state === 'not_declared' && data.source_protocol.code !== 'NOT_DECLARED')
    || (data.source_protocol.state === 'unavailable' && !['SOURCE_UNAVAILABLE', 'SOURCE_UNREADABLE', 'SOURCE_FORMAT_UNKNOWN'].includes(data.source_protocol.code))
    || (data.source_protocol.state !== 'unavailable' && (data.source_protocol.parser_sha256 === null || data.agent_source.state !== 'available'))
    || (data.finite_protocol.state === 'supported' && data.finite_protocol.code !== 'SUPPORTED')
    || (data.finite_protocol.state === 'unsupported' && data.finite_protocol.code !== 'FORMAT_NOT_ADVERTISED')
    || (data.finite_protocol.state === 'unavailable' && !['CLI_MISSING', 'PROBE_TIMEOUT', 'OUTPUT_LIMIT', 'PROBE_FAILED'].includes(data.finite_protocol.code))
    || (data.finite_protocol.state === 'not_observed' && (data.finite_protocol.code !== 'NOT_OBSERVED' || data.finite_protocol.checked_at !== null || data.finite_protocol.provenance !== 'not_observed'))
    || (data.finite_protocol.state !== 'not_observed' && (typeof data.finite_protocol.checked_at !== 'string' || !Number.isFinite(Date.parse(data.finite_protocol.checked_at)) || data.finite_protocol.provenance !== 'authorized_session_preflight'))
    || (data.cli.state === 'missing' && (data.cli.resolution !== 'unavailable' || data.cli.identity_sha256 !== null || data.finite_protocol.state !== 'not_observed'))
    || (data.cli.state === 'available' && (data.cli.resolution === 'unavailable' || data.cli.identity_sha256 === null))
    || (data.agent_source.state === 'available' && data.agent_source.identity_sha256 === null)
    || (data.agent_source.state === 'unavailable' && (data.agent_source.identity_sha256 !== null || data.agent_source.revision !== null || data.cli.matches_agent_source !== null))) {
    throw new Error('Invalid readiness observation');
  }
  for (const fingerprint of [data.agent_source.identity_sha256, data.cli.identity_sha256, data.source_protocol.parser_sha256]) {
    if (fingerprint !== null && (typeof fingerprint !== 'string' || !/^[0-9a-f]{64}$/.test(fingerprint))) throw new Error('Invalid location identity');
  }
  if (data.agent_source.revision !== null && (typeof data.agent_source.revision !== 'string' || !/^[0-9a-f]{40}(?:[0-9a-f]{24})?$/.test(data.agent_source.revision))) throw new Error('Invalid source revision');
  return data;
}

const advice: Record<string, string> = {
  SUPPORTED: 'During a prior authorized session preflight, this CLI advertised --format stream-json. This does not prove that a turn was submitted or completed.',
  FORMAT_NOT_ADVERTISED: 'During a prior authorized session preflight, this CLI did not advertise the finite protocol. Review the intended Agent installation locally.',
  CLI_MISSING: 'The prior authorized session preflight could not find the CLI. Inspect the intended Agent environment locally.',
  PROBE_TIMEOUT: 'CLI help did not finish within the time limit during the prior authorized preflight. Compatibility remains unknown.',
  OUTPUT_LIMIT: 'CLI help exceeded the output limit during the prior authorized preflight. Compatibility remains unknown.',
  PROBE_FAILED: 'CLI help failed during the prior authorized preflight. Inspect the selected Agent installation locally.',
  NOT_OBSERVED: 'No matching authorized session preflight has been observed in this server process. Compatibility remains unknown. Do not submit work just to test it.',
};

export function AgentReadinessPanel() {
  const [data, setData] = useState<Readiness | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const inFlight = useRef(false);
  const mounted = useRef(true);
  async function load() {
    if (inFlight.current) return;
    inFlight.current = true; setBusy(true); setError(null); setData(null);
    try {
      const observation = decode(await api.get<unknown>('/api/ops/agent-readiness'));
      if (mounted.current) setData(observation);
    } catch {
      if (mounted.current) setError('Agent readiness is unavailable. Check local server access and try again.');
    } finally {
      inFlight.current = false;
      if (mounted.current) setBusy(false);
    }
  }
  useEffect(() => { mounted.current = true; void load(); return () => { mounted.current = false; }; }, []);
  return <section className="fd-surface">
    <header className="fd-surface-head"><h2>Agent readiness</h2>
      <p>Inspect local source declarations and any prior authorized session preflight. Checking executes no Agent code and starts no work.</p>
      <button className="fd-btn" disabled={busy} onClick={() => void load()}>Refresh readiness</button>
    </header>
    {error ? <ErrorState message={error} /> : null}
    {busy ? <LoadingState label="Reading local Agent metadata…" /> : null}
    {data ? <>
      <p className="fd-hint">Source metadata observed {data.checked_at}. Refresh reads local files; it does not run CLI help.</p>
      <PanelCard title="Running sidecar"><p>Hermes GPT {data.hermes_gpt_version.slice(0, 40)}</p>
        <p>Session control is {data.session_control_enabled ? 'enabled' : 'disabled'}. Existing policy and confirmation requirements still apply.</p>
      </PanelCard>
      <PanelCard title="Agent source location">
        <StatusChip tone={data.agent_source.state === 'available' ? 'neutral' : 'warn'} label={data.agent_source.state === 'available' ? 'Location observed' : 'Source location unknown'} />
        <p>This is the running server’s selected source directory, not proof that every Agent module loaded or that a service uses this revision.</p>
        {data.agent_source.identity_sha256 ? <p className="fd-mono">Source location fingerprint: {data.agent_source.identity_sha256}</p> : <p>Inspect the intended Agent source and server launcher locally.</p>}
        <p className="fd-mono">Git revision: {data.agent_source.revision ?? 'unavailable'}</p>
        <p className="fd-hint">A location fingerprint identifies a path without revealing it. A Git revision does not include uncommitted changes.</p>
      </PanelCard>
      <PanelCard title="Selected CLI">
        <p>{data.cli.resolution === 'agent_venv' ? 'Selected from the Agent virtual environment.' : data.cli.resolution === 'path' ? 'Selected from the server’s PATH.' : 'Hermes CLI not found.'}</p>
        {data.cli.identity_sha256 ? <p className="fd-mono">CLI location fingerprint: {data.cli.identity_sha256}</p> : null}
        {data.cli.matches_agent_source === false ? <p role="status">The resolved CLI is outside the selected Agent source. Check locally that both refer to the intended installation.</p> : data.cli.matches_agent_source === true ? <p>The resolved CLI is inside the selected source directory. This does not verify its imported code or running services.</p> : <p>The relationship between source and CLI locations is unknown.</p>}
      </PanelCard>
      <PanelCard title="Source protocol declaration">
        <StatusChip tone={data.source_protocol.state === 'declared' ? 'neutral' : 'warn'} label={data.source_protocol.state === 'declared' ? 'Declared in source' : data.source_protocol.state === 'not_declared' ? 'Not declared in recognized source' : 'Source declaration unknown'} />
        <p>{data.source_protocol.state === 'declared' ? 'The selected source contains a literal chat --format stream-json parser declaration.' : data.source_protocol.state === 'not_declared' ? 'The recognized chat parser does not contain that literal declaration.' : 'The selected source parser could not be read or recognized within the size and syntax bounds.'}</p>
        <p className="fd-hint">Source declarations do not prove selected CLI behavior. Source is read without importing or executing it.</p>
        {data.source_protocol.parser_sha256 ? <p className="fd-mono">Parser SHA-256: {data.source_protocol.parser_sha256}</p> : null}
      </PanelCard>
      <PanelCard title="Prior authorized session preflight">
        <StatusChip tone={data.finite_protocol.state === 'supported' ? 'neutral' : 'warn'} label={data.finite_protocol.state === 'supported' ? 'Advertised in prior CLI preflight' : data.finite_protocol.state === 'unsupported' ? 'Not advertised in prior CLI preflight' : data.finite_protocol.state === 'not_observed' ? 'No prior observation' : 'Compatibility unknown'} />
        <p>{Object.prototype.hasOwnProperty.call(advice, data.finite_protocol.code) ? advice[data.finite_protocol.code] : 'Compatibility could not be established. Inspect the local Agent installation.'}</p>
        {data.finite_protocol.checked_at ? <p>Prior preflight observed {data.finite_protocol.checked_at}.</p> : null}
        <p className="fd-hint">Prior observations are kept only in memory for a matching executable location and file metadata. Restarting this server clears them. This page never runs help, submits a turn, sends a delivery, or changes an installation.</p>
      </PanelCard>
      <PanelCard title="Live acceptance"><StatusChip tone="warn" label="Not verified" />
        <p>These observations do not verify Desktop continuation, gateway response correlation, provider access, notification delivery, or installed-service acceptance.</p>
      </PanelCard>
    </> : null}
  </section>;
}
