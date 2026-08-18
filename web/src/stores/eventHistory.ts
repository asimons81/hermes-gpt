import { create } from 'zustand';

import { api } from '../api/client';
import type { EventsEnvelope, EventRow } from '../flight/types';

interface EventFilters {
  source: string;
  subject_id: string;
  kind: string;
  limit: number;
}

interface EventHistoryState {
  events: EventRow[];
  envelope: EventsEnvelope | null;
  filters: EventFilters;
  status: 'idle' | 'loading' | 'ready' | 'error';
  error: string | null;
  fetchedAt: number | null;
  setFilters(patch: Partial<EventFilters>): void;
  fetchEvents(): Promise<void>;
  refresh(): Promise<void>;
}

function toQuery(filters: EventFilters): string {
  const params = new URLSearchParams();
  if (filters.source) params.set('source', filters.source);
  if (filters.subject_id) params.set('subject_id', filters.subject_id);
  if (filters.kind) params.set('kind', filters.kind);
  params.set('limit', String(filters.limit));
  return params.toString();
}

/**
 * Event History timeline store. Poll-driven (no push — seam gap A2), so the
 * UI refreshes on a timer and on filter change.
 */
export const useEventHistoryStore = create<EventHistoryState>((set, get) => ({
  events: [],
  envelope: null,
  filters: { source: '', subject_id: '', kind: '', limit: 50 },
  status: 'idle',
  error: null,
  fetchedAt: null,

  setFilters(patch) {
    set({ filters: { ...get().filters, ...patch } });
  },

  async fetchEvents() {
    set({ status: 'loading', error: null });
    try {
      const envelope = await api.get<EventsEnvelope>(`/api/events?${toQuery(get().filters)}`);
      set({
        events: envelope.events ?? [],
        envelope,
        status: 'ready',
        error: null,
        fetchedAt: Date.now(),
      });
    } catch (err) {
      const message = err instanceof Error ? err.message : 'events query failed';
      set({ status: 'error', error: message });
    }
  },

  async refresh() {
    await get().fetchEvents();
  },
}));
