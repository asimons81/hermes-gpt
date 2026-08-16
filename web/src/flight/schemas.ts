// Mission surface schemas for the generic SurfacePanel (v0.7 §14 mapping).
import type { SurfaceSchema } from './SurfacePanel';

export const SURFACE_SCHEMAS: Record<string, SurfaceSchema> = {
  overview: {
    surface: 'overview',
    title: 'Mission Control Overview',
    description: 'Composite summary across all allowed surfaces.',
    stats: [
      { key: 'fleet_health', label: 'Fleet', kind: 'status' },
      { key: 'profiles', label: 'Profiles', kind: 'count' },
      { key: 'pending_approvals', label: 'Pending approvals', kind: 'count' },
      { key: 'failures', label: 'Failures', kind: 'count' },
    ],
    sections: [
      { key: 'surfaces_unavailable', label: 'Unavailable surfaces', kind: 'list', max: 120 },
      { key: 'cron', label: 'Cron summary', kind: 'json', max: 300 },
      { key: 'delegations', label: 'Delegations', kind: 'json', max: 300 },
      { key: 'audit', label: 'Audit', kind: 'json', max: 300 },
    ],
  },
  health: {
    surface: 'health',
    title: 'Health',
    description: 'Derived operational health checks.',
    stats: [{ key: 'overall', label: 'Overall', kind: 'status' }],
    sections: [{ key: 'checks', label: 'Checks', kind: 'list', max: 240 }],
  },
  profiles: {
    surface: 'profiles',
    title: 'Profiles',
    description: 'Per-profile runtime summary (bodies excluded).',
    sections: [{ key: 'profiles', label: 'Profiles', kind: 'list', max: 300 }],
  },
  fleet: {
    surface: 'fleet',
    title: 'Fleet',
    description: 'Registered fleet peers and authority state.',
    stats: [{ key: 'authority', label: 'Authority', kind: 'status' }],
    sections: [
      { key: 'peers', label: 'Peers', kind: 'list', max: 200 },
      { key: 'served_profiles', label: 'Served profiles', kind: 'list', max: 200 },
    ],
  },
  codex: {
    surface: 'codex',
    title: 'Codex',
    description: 'Codex runner + job store status (store may be absent on this host).',
    sections: [
      { key: 'operator_jobs', label: 'Jobs', kind: 'list', max: 240 },
      { key: 'native_sessions', label: 'Native sessions', kind: 'json', max: 240 },
      { key: 'health', label: 'Health', kind: 'json', max: 200 },
    ],
  },
  cron: {
    surface: 'cron',
    title: 'Cron',
    description: 'Cron jobs + executions across profiles (prompt bodies excluded).',
    stats: [
      { key: 'scheduler_live', label: 'Scheduler', kind: 'status' },
      { key: 'executions_by_status', label: 'Executions', kind: 'json', max: 160 },
    ],
    sections: [{ key: 'jobs', label: 'Jobs', kind: 'list', max: 260 }],
  },
  delegations: {
    surface: 'delegations',
    title: 'Delegations',
    description: 'Active delegations + kanban runs (bodies stripped).',
    sections: [
      { key: 'delegations', label: 'Delegations', kind: 'list', max: 240 },
      { key: 'kanban_runs', label: 'Kanban runs', kind: 'list', max: 240 },
    ],
  },
  failures: {
    surface: 'failures',
    title: 'Failures',
    description: 'Recent errors and denials (PII-stripped).',
    sections: [
      { key: 'recent_errors', label: 'Recent errors', kind: 'list', max: 200 },
      { key: 'by_source', label: 'By source', kind: 'json', max: 200 },
      { key: 'vault_denials', label: 'Vault denials', kind: 'list', max: 200 },
    ],
  },
  approvals: {
    surface: 'approvals',
    title: 'Approvals',
    description: 'Pending approvals across sources (raw prompts never shown).',
    sections: [{ key: 'approvals', label: 'Pending', kind: 'list', max: 200 }],
  },
  vault: {
    surface: 'vault',
    title: 'Vault',
    description: 'Credential store status (non-secret only).',
    sections: [
      { key: 'credential_state', label: 'Credential state', kind: 'json', max: 200 },
      { key: 'credential_names', label: 'Credential names', kind: 'list', max: 160 },
      { key: 'leases', label: 'Leases', kind: 'list', max: 160 },
      { key: 'access_summary', label: 'Access summary', kind: 'json', max: 200 },
    ],
  },
  usage: {
    surface: 'usage',
    title: 'Usage',
    description: 'Token + cost summary (no message content).',
    stats: [
      { key: 'sessions_24h', label: 'Sessions 24h', kind: 'count' },
      { key: 'tokens_24h', label: 'Tokens 24h', kind: 'json', max: 60 },
      { key: 'estimated_cost_24h_usd', label: 'Cost 24h ($)', kind: 'count' },
    ],
    sections: [{ key: 'by_profile', label: 'By profile', kind: 'json', max: 240 }],
  },
  audit: {
    surface: 'audit',
    title: 'Audit',
    description: 'Recent operator audit records (summary-level only).',
    sections: [{ key: 'records', label: 'Records', kind: 'list', max: 300 }],
  },
};
