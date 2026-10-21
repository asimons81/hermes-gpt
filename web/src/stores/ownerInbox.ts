import { create } from 'zustand';

import { api, ApiError } from '../api/client';
import { parseOwnerInbox } from '../flight/schemas';
import type { OwnerInboxPayload } from '../flight/types';

interface OwnerInboxState {
  payload: OwnerInboxPayload | null;
  status: 'idle' | 'loading' | 'ready' | 'error' | 'denied';
  error: string | null;
  fetchedAt: number | null;
  fetchInbox(limit?: number): Promise<void>;
}

/**
 * Owner inbox store: read-only GET /api/ops/owner-inbox. There is no mutation
 * path here; suggested tools are shown as text and acted on from an MCP client.
 * The payload is schema-validated before it reaches the UI.
 */
export const useOwnerInboxStore = create<OwnerInboxState>((set) => ({
  payload: null,
  status: 'idle',
  error: null,
  fetchedAt: null,

  async fetchInbox(limit = 50) {
    set({ status: 'loading', error: null });
    try {
      const raw = await api.get<unknown>(`/api/ops/owner-inbox?limit=${encodeURIComponent(String(limit))}`);
      const payload = parseOwnerInbox(raw);
      set({ payload, status: 'ready', error: null, fetchedAt: Date.now() });
    } catch (err) {
      if (err instanceof ApiError && err.code === 'AUTHZ_DENIED') {
        set({ payload: null, status: 'denied', error: null });
        return;
      }
      const message = err instanceof Error ? err.message : 'owner inbox query failed';
      set({ payload: null, status: 'error', error: message });
    }
  },
}));
