import { create } from 'zustand';

import { api } from '../api/client';
import type { MissionEnvelope } from '../flight/types';

interface FleetState {
  fleet: MissionEnvelope | null;
  workflows: MissionEnvelope | null;
  status: 'idle' | 'loading' | 'ready' | 'error';
  error: string | null;
  fetchedAt: number | null;
  fetchFleet(): Promise<void>;
  fetchSwarm(): Promise<void>;
}

/**
 * Fleet/swarm status store: fleet peers (mission fleet surface) and swarm
 * workflow instances (operator_swarm list read). Both are read-only
 * presentation; stage advance happens through the gated approval flow.
 */
export const useFleetStore = create<FleetState>((set) => ({
  fleet: null,
  workflows: null,
  status: 'idle',
  error: null,
  fetchedAt: null,

  async fetchFleet() {
    set({ status: 'loading', error: null });
    try {
      const envelope = await api.get<{ surface: string; data: MissionEnvelope }>('/api/ops/fleet');
      set({ fleet: envelope.data, status: 'ready', error: null, fetchedAt: Date.now() });
    } catch (err) {
      const message = err instanceof Error ? err.message : 'fleet query failed';
      set({ status: 'error', error: message });
    }
  },

  async fetchSwarm() {
    set({ status: 'loading', error: null });
    try {
      const envelope = await api.get<MissionEnvelope>('/api/ops/swarm');
      set({ workflows: envelope, status: 'ready', error: null, fetchedAt: Date.now() });
    } catch (err) {
      const message = err instanceof Error ? err.message : 'swarm query failed';
      set({ status: 'error', error: message });
    }
  },
}));
