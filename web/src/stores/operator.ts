import { create } from 'zustand';

import { api, ApiError } from '../api/client';
import type { MissionEnvelope, MissionSurface, SurfaceEnvelope } from '../flight/types';

interface SurfaceEntry {
  status: 'idle' | 'loading' | 'ready' | 'error';
  data: MissionEnvelope | null;
  error: string | null;
  fetchedAt: number | null;
  ttl: number | null;
}

interface OperatorState {
  surfaces: Record<string, SurfaceEntry>;
  /** Fetch one Mission Control surface; force bypasses the browser TTL gate. */
  fetchSurface(surface: MissionSurface | string, force?: boolean): Promise<void>;
  /** Clear a surface entry (used on account revoked/expired). */
  reset(surface: string): void;
}

function entry(): SurfaceEntry {
  return { status: 'idle', data: null, error: null, fetchedAt: null, ttl: null };
}

function isStaleEntry(e: SurfaceEntry): boolean {
  if (!e.fetchedAt || !e.ttl || e.ttl <= 0) return false;
  return Date.now() - e.fetchedAt > e.ttl * 1000;
}

/**
 * Mission Control surface store: one entry per surface with the
 * loading/stale/error state vocabulary. `getSurface` (below) is a selector
 * helper that reports stale when the TTL has elapsed while data is still
 * displayed.
 */
export const useOperatorStore = create<OperatorState>((set, get) => ({
  surfaces: {},

  async fetchSurface(surface, force = false) {
    const current = get().surfaces[surface] ?? entry();
    // Cache-hit: ready + fresh (or force refresh requested).
    if (current.status === 'ready' && !isStaleEntry(current) && !force) return;
    if (current.status === 'loading') return;

    set({ surfaces: { ...get().surfaces, [surface]: { ...entry(), status: 'loading' } } });
    try {
      const envelope = await api.get<SurfaceEnvelope>(`/api/ops/${surface}`);
      set({
        surfaces: {
          ...get().surfaces,
          [surface]: {
            status: 'ready',
            data: envelope.data,
            error: null,
            fetchedAt: Date.now(),
            ttl: envelope.ttl,
          },
        },
      });
    } catch (err) {
      const message =
        err instanceof ApiError ? `${err.code}: ${err.message}` : err instanceof Error ? err.message : 'surface failed';
      set({
        surfaces: {
          ...get().surfaces,
          [surface]: { ...entry(), status: 'error', error: message },
        },
      });
    }
  },

  reset(surface) {
    set({ surfaces: { ...get().surfaces, [surface]: entry() } });
  },
}));

/** Selector helper: returns the entry plus a stale flag. */
export function selectSurface(state: OperatorState, surface: string): { entry: SurfaceEntry; stale: boolean } {
  const e = state.surfaces[surface] ?? entry();
  return { entry: e, stale: e.status === 'ready' && isStaleEntry(e) };
}
