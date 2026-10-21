// Mission surface schemas for the generic SurfacePanel (v0.7 §14 mapping).
import type { SurfaceSchema } from './SurfacePanel';
import type { OwnerInboxItem, OwnerInboxPayload, OwnerInboxSourceState } from './types';

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

// ---------------------------------------------------------------------------
// Owner inbox payload validation. The browser never renders an unvalidated
// inbox: any shape drift throws OwnerInboxSchemaError and the panel shows an
// error state instead of a list (and never a false "nothing waiting").
// ---------------------------------------------------------------------------

export class OwnerInboxSchemaError extends Error {
  constructor(message: string) {
    super(message);
    this.name = 'OwnerInboxSchemaError';
  }
}

const SOURCE_STATES: readonly string[] = ['ok', 'unavailable', 'empty'];
const REQUIRES: readonly string[] = ['owner', 'workspace', 'review'];

function fail(path: string): never {
  throw new OwnerInboxSchemaError(`owner inbox payload invalid at ${path}`);
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value);
}

function isStringArray(value: unknown): value is string[] {
  return Array.isArray(value) && value.every((entry) => typeof entry === 'string');
}

const ITEM_ID_RE = /^[0-9a-f]{24}$/;

function parseItem(value: unknown, path: string): OwnerInboxItem {
  if (!isRecord(value)) fail(path);
  const { item_id, kind, mission_id, summary, since, suggested_tool, requires, plan_id, frontier_node_ids } = value;
  if (typeof item_id !== 'string' || !ITEM_ID_RE.test(item_id)) fail(`${path}.item_id`);
  if (typeof kind !== 'string' || kind === '') fail(`${path}.kind`);
  if (typeof mission_id !== 'string') fail(`${path}.mission_id`);
  if (typeof summary !== 'string') fail(`${path}.summary`);
  if (since !== null && typeof since !== 'string') fail(`${path}.since`);
  if (suggested_tool !== null && typeof suggested_tool !== 'string') fail(`${path}.suggested_tool`);
  if (typeof requires !== 'string' || !REQUIRES.includes(requires)) fail(`${path}.requires`);
  const item: OwnerInboxItem = {
    item_id,
    kind,
    mission_id,
    summary,
    since,
    suggested_tool,
    requires: requires as OwnerInboxItem['requires'],
  };
  // Structured autopilot fields are optional: absent on every other kind.
  if (plan_id !== undefined) {
    if (typeof plan_id !== 'string') fail(`${path}.plan_id`);
    item.plan_id = plan_id;
  }
  if (frontier_node_ids !== undefined) {
    if (!isStringArray(frontier_node_ids)) fail(`${path}.frontier_node_ids`);
    item.frontier_node_ids = frontier_node_ids;
  }
  return item;
}

/** Validate a GET /api/ops/owner-inbox `data` payload. Throws on any mismatch. */
export function parseOwnerInbox(payload: unknown): OwnerInboxPayload {
  if (!isRecord(payload)) fail('$');
  const { items, count, truncated, limit, sources, complete, skipped_sources, read_only, generated_at } = payload;
  if (!Array.isArray(items)) fail('items');
  const parsedItems = items.map((item, index) => parseItem(item, `items[${index}]`));
  if (typeof count !== 'number' || !Number.isInteger(count) || count !== parsedItems.length) fail('count');
  if (typeof truncated !== 'boolean') fail('truncated');
  if (typeof limit !== 'number' || !Number.isInteger(limit) || limit < 1 || limit > 200) fail('limit');
  if (parsedItems.length > limit) fail('items.length');
  if (!isRecord(sources)) fail('sources');
  const parsedSources: Record<string, OwnerInboxSourceState> = {};
  for (const [name, state] of Object.entries(sources)) {
    if (typeof state !== 'string' || !SOURCE_STATES.includes(state)) fail(`sources.${name}`);
    parsedSources[name] = state as OwnerInboxSourceState;
  }
  if (typeof complete !== 'boolean') fail('complete');
  if (!isStringArray(skipped_sources)) fail('skipped_sources');
  if (read_only !== true) fail('read_only');
  if (typeof generated_at !== 'string') fail('generated_at');
  return {
    items: parsedItems,
    count,
    truncated,
    limit,
    sources: parsedSources,
    complete,
    skipped_sources,
    read_only: true,
    generated_at,
  };
}
