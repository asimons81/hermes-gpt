import { create } from 'zustand';
import { listSessions } from '../api/chat';
import type { SessionSummary } from '../api/types';

interface SessionListState {
  sessions: SessionSummary[];
  loading: boolean;
  error: string | null;
  load: () => Promise<void>;
  upsert: (session: SessionSummary) => void;
}

export const useSessionListStore = create<SessionListState>((set) => ({
  sessions: [],
  loading: false,
  error: null,
  load: async () => {
    set({ loading: true, error: null });
    try {
      const data = await listSessions();
      set({ sessions: data.sessions, loading: false });
    } catch (error) {
      set({ loading: false, error: error instanceof Error ? error.message : 'Unable to load conversations' });
    }
  },
  upsert: (session) => set((state) => ({
    sessions: [session, ...state.sessions.filter((candidate) => candidate.session_id !== session.session_id)],
  })),
}));
