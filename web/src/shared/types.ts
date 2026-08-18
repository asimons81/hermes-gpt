/**
 * Shared browser-facing types for the Hermes ChatGPT UI.
 *
 * These mirror interface-contracts.md §3 (account/capability context) and
 * §7 (error envelope). The account store (flight card, t_1135e15b) and the
 * connection store (security card, t_7266e74c) both consume these shapes.
 */

/** Four-state account status driving the recovery UX (contract §3). */
export type AccountStatus = "ok" | "expired" | "revoked" | "unauthorized";

/** Operator authority level (contract §3; operator_policy LEVELS ladder). */
export type OperatorLevel =
  | "read_only"
  | "cron"
  | "skills"
  | "skills_config"
  | "workspace"
  | "owner";

/** Payload of GET /api/me (contract §3). */
export interface MePayload {
  profile: string;
  accountStatus: AccountStatus;
  operatorLevel: OperatorLevel;
  allowedSurfaces: string[];
  uiCapabilities: string[];
  model: string;
  serverVersion: string;
}

/** Payload of GET /api/connection (security card, restart detection). */
export interface ConnectionPayload {
  serverStartupId: string;
  serverTime: number;
  uiEnabled: boolean;
  staleLeaseSeconds: number;
  toolPreviewBytes: number;
  accountStatus: AccountStatus;
}

/** Success envelope {ok: true, data} (contract §7). */
export interface OkEnvelope<T> {
  ok: true;
  data: T;
}

/** Error envelope {ok: false, error} (contract §7). */
export interface ErrorEnvelope {
  ok: false;
  error: {
    code: string;
    message: string;
    trace_id?: string;
  };
}

export type Envelope<T> = OkEnvelope<T> | ErrorEnvelope;

/** Standard error codes (contract §7). */
export const ERROR_CODES = [
  "TURN_IN_PROGRESS",
  "TURN_NOT_FOUND",
  "LEVEL_REQUIRED",
  "CONFIRM_REQUIRED",
  "GATE_DENIED",
  "MODEL_UNAVAILABLE",
  "IMPORT_UNAVAILABLE",
  "NOT_FOUND",
  "RATE_LIMITED",
  "INTERNAL",
] as const;

export type ErrorCode = (typeof ERROR_CODES)[number];
