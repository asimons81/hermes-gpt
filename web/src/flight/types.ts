// Flight Deck shared types — mirror the wire contract (interface-contracts.md).

/** One Mission Control surface envelope (GET /api/ops/:surface). */
export interface SurfaceEnvelope {
  surface: string;
  fetched_at: string;
  ttl: number | null;
  data: MissionEnvelope;
}

/** Redacted mission envelope returned by operator_mission builders. */
export interface MissionEnvelope {
  success?: boolean;
  available?: boolean;
  unavailable_reason?: string;
  code?: string;
  safe_message?: string;
  suggested_action?: string;
  schema_version?: string;
  tool?: string;
  surface?: string;
  generated_at?: string;
  counts?: Record<string, number>;
  data?: Record<string, unknown>;
  warnings?: string[];
  trace_id?: string;
  served_from_cache?: boolean;
  age_ms?: number;
  [key: string]: unknown;
}

/** Event History row (operator_events event schema). */
export interface EventRow {
  event_id: string;
  ts: string;
  source: string;
  kind: string;
  actor?: string;
  subject_id?: string;
  status_before?: string;
  status_after?: string;
  summary?: string;
  refs?: string[];
  trace_id?: string;
}

export interface EventsEnvelope {
  success: boolean;
  count_returned: number;
  count_total: number;
  truncated: boolean;
  sources_queried: string[];
  sources_allowed: string[];
  retention_max_age_days: number;
  warnings: string[];
  events: EventRow[];
  [key: string]: unknown;
}

/** Gated mutation response (POST /api/ops/action). */
export interface ActionResult {
  tool: string;
  dry_run: boolean;
  requires_confirm: boolean;
  result: Record<string, unknown>;
}

export interface ActionSuccessEnvelope {
  ok: true;
  data: ActionResult;
}

/** Account status (GET /api/ops/account). */
export interface AccountEnvelope {
  success: boolean;
  policy: {
    enabled: boolean;
    level: string;
    apply_mode: string;
    owner_active: boolean;
    owner_mode_ready: boolean;
    mutation_allowed: boolean;
    available_capability_groups: string[];
    [key: string]: unknown;
  };
  oauth: {
    presence?: string;
    expires_at?: string;
    client_count?: number;
    available?: boolean;
    [key: string]: unknown;
  };
  server_version: string;
  generated_at: string;
}

export const MISSION_SURFACES = [
  'overview',
  'health',
  'profiles',
  'fleet',
  'codex',
  'cron',
  'delegations',
  'failures',
  'approvals',
  'vault',
  'usage',
  'audit',
] as const;

export type MissionSurface = (typeof MISSION_SURFACES)[number];

/** Loading/stale/error state vocabulary shared by every flight store. */
export type FetchStatus = 'idle' | 'loading' | 'ready' | 'error';

export interface FetchState<T> {
  status: FetchStatus;
  data: T | null;
  error: string | null;
  fetchedAt: number | null;
  ttl: number | null;
}

export function initialFetchState<T>(): FetchState<T> {
  return { status: 'idle', data: null, error: null, fetchedAt: null, ttl: null };
}

/** True when a ready surface is past its TTL (stale, but still shown). */
export function isStale(state: Pick<FetchState<unknown>, 'fetchedAt' | 'ttl'>): boolean {
  if (state.fetchedAt == null || state.ttl == null || state.ttl <= 0) return false;
  return Date.now() - state.fetchedAt > state.ttl * 1000;
}

/** Owner inbox item (GET /api/ops/owner-inbox). Read-only projection. */
export interface OwnerInboxItem {
  /** Stable 24-hex id; acknowledgements and the Flight Deck actions key on it. */
  item_id: string;
  kind: string;
  mission_id: string;
  summary: string;
  since: string | null;
  suggested_tool: string | null;
  requires: 'owner' | 'workspace' | 'review';
  /** autopilot_waiting_owner only: the mission's plan key (equals mission_id). */
  plan_id?: string;
  /** autopilot_waiting_owner only: the full sorted frontier node ids. */
  frontier_node_ids?: string[];
  contract_sha256?: string;
  task_id?: string;
  delegation_id?: string;
  review_revision?: string;
}

export type OwnerInboxSourceState = 'ok' | 'unavailable' | 'empty';

/** Owner inbox payload (the `data` of the `{ok, data}` envelope). */
export interface OwnerInboxPayload {
  items: OwnerInboxItem[];
  count: number;
  truncated: boolean;
  limit: number;
  sources: Record<string, OwnerInboxSourceState>;
  complete: boolean;
  skipped_sources: string[];
  read_only: true;
  generated_at: string;
}
