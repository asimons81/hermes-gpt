/**
 * Connection transport store (t_7266e74c — architecture.md §10, §13).
 *
 * Owns transport health only: SSE status, reconnect backoff, and server
 * restart detection. Account status lives in the account store
 * (flight card, t_1135e15b); this store never imports it.
 *
 * Restart detection: the server exposes a per-process ``serverStartupId``
 * (GET /api/connection). When an observed id differs from the last seen id,
 * the server restarted mid-session — the UI marks the in-flight turn as
 * interrupted (persisted messages + turn lease make it recoverable) and
 * reconnects instead of ghosting a turn.
 */

import { create } from "zustand";

export type SseStatus = "connected" | "disconnected" | "reconnecting";

export type ReconnectPhase =
  | { phase: "idle" }
  | { phase: "backoff"; attempt: number; nextAttemptAt: number }
  | { phase: "server-restart" };

export interface ConnectionState {
  sseStatus: SseStatus;
  reconnect: ReconnectPhase;
  /** Startup id of the server we are currently connected to (or null). */
  serverStartupId: string | null;
  /** Startup id observed by the last successful poll/connect. */
  lastSeenServerStartupId: string | null;
  /** True when a server restart was detected and not yet acknowledged. */
  serverRestart: boolean;
  staleLeaseSeconds: number;

  markConnected: (startupId: string) => void;
  markDisconnected: () => void;
  markReconnecting: (attempt: number, nextAttemptAt: number) => void;
  markServerRestart: () => void;
  /** Observe a fresh startup id; flags restart when it changed. */
  observeServerStartupId: (startupId: string) => void;
  setStaleLeaseSeconds: (seconds: number) => void;
  acknowledgeRestart: () => void;
  reset: () => void;
}

const initialReconnect: ReconnectPhase = { phase: "idle" };

export const useConnectionStore = create<ConnectionState>((set, get) => ({
  sseStatus: "disconnected",
  reconnect: initialReconnect,
  serverStartupId: null,
  lastSeenServerStartupId: null,
  serverRestart: false,
  staleLeaseSeconds: 600,

  markConnected: (startupId) =>
    set({
      sseStatus: "connected",
      reconnect: { phase: "idle" },
      serverStartupId: startupId || null,
    }),

  markDisconnected: () => set({ sseStatus: "disconnected" }),

  markReconnecting: (attempt, nextAttemptAt) =>
    set({
      sseStatus: "reconnecting",
      reconnect: { phase: "backoff", attempt, nextAttemptAt },
    }),

  markServerRestart: () =>
    set({ reconnect: { phase: "server-restart" }, serverRestart: true }),

  observeServerStartupId: (startupId) => {
    const { lastSeenServerStartupId } = get();
    if (!startupId) {
      return;
    }
    if (lastSeenServerStartupId === null) {
      // First observation — baseline, no restart.
      set({ lastSeenServerStartupId: startupId, serverStartupId: startupId });
      return;
    }
    if (lastSeenServerStartupId !== startupId) {
      // The process restarted between observations.
      set({
        lastSeenServerStartupId: startupId,
        serverStartupId: startupId,
        serverRestart: true,
        sseStatus: "reconnecting",
        reconnect: { phase: "server-restart" },
      });
    }
  },

  setStaleLeaseSeconds: (seconds) => set({ staleLeaseSeconds: seconds }),

  acknowledgeRestart: () =>
    set({ serverRestart: false, reconnect: { phase: "idle" } }),

  reset: () =>
    set({
      sseStatus: "disconnected",
      reconnect: initialReconnect,
      serverStartupId: null,
      lastSeenServerStartupId: null,
      serverRestart: false,
    }),
}));
