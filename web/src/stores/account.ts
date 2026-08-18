import { create } from 'zustand';

import { api } from '../api/client';
import type { AccountEnvelope } from '../flight/types';

interface AccountState {
  account: AccountEnvelope | null;
  loading: boolean;
  error: string | null;
  fetchedAt: number | null;
  /** load() starts a refresh (invalidate cache) when true. */
  refresh(): Promise<void>;
}

/**
 * Account/authority context store: operator policy summary, OAuth store
 * presence/expiry, and server version. Read-only presentation — never token
 * material. accountStatus for recovery UX (ok/expired/revoked/unauthorized)
 * is resolved by the security card's /api/me; this store keeps the operator
 * side visible even when /api/me is unavailable.
 */
export const useAccountStore = create<AccountState>((set) => ({
  account: null,
  loading: false,
  error: null,
  fetchedAt: null,

  async refresh() {
    set({ loading: true, error: null });
    try {
      const account = await api.get<AccountEnvelope>('/api/ops/account');
      set({ account, loading: false, error: null, fetchedAt: Date.now() });
    } catch (err) {
      const message = err instanceof Error ? err.message : 'account status failed';
      set({ loading: false, error: message });
    }
  },
}));
