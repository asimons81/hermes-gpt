import { create } from 'zustand';

import { api, ApiError } from '../api/client';
import type { MissionEnvelope } from '../flight/types';

export interface ApprovalItem {
  kind: string;
  source: string;
  id: string;
  status: string;
  prompt_sha256?: string;
  created_at?: string;
  [key: string]: unknown;
}

interface ApprovalsState {
  items: ApprovalItem[];
  source: 'mission' | 'error';
  status: 'idle' | 'loading' | 'ready' | 'error';
  error: string | null;
  fetchedAt: number | null;
  // Confirm dialog state (server surfaces gates; the UI never bypasses).
  dialog: { open: boolean; tool: string; args: Record<string, unknown>; plan: unknown } | null;
  fetchApprovals(): Promise<void>;
  openConfirm(tool: string, args: Record<string, unknown>, plan: unknown): void;
  closeConfirm(): void;
  runGated(tool: string, args: Record<string, unknown>, dryRun: boolean): Promise<{ requiresConfirm: boolean; result: unknown }>;
}

/**
 * Approvals store: pending-approval read-model (mission approvals surface) +
 * the gated mutation confirm flow. All mutations go through POST
 * /api/ops/action; a 409 CONFIRM_REQUIRED surfaces the confirm dialog and
 * the UI re-POSTs with confirm:true — the adapter/operator gate is never
 * weakened.
 */
export const useApprovalStore = create<ApprovalsState>((set, get) => ({
  items: [],
  source: 'mission',
  status: 'idle',
  error: null,
  fetchedAt: null,
  dialog: null,

  async fetchApprovals() {
    set({ status: 'loading', error: null });
    try {
      const envelope = await api.get<{ surface: string; data: MissionEnvelope }>('/api/ops/approvals');
      const data = envelope.data.data ?? {};
      const items = Array.isArray(data.approvals) ? (data.approvals as ApprovalItem[]) : [];
      set({ items, source: 'mission', status: 'ready', error: null, fetchedAt: Date.now() });
    } catch (err) {
      const message = err instanceof Error ? err.message : 'approvals query failed';
      set({ status: 'error', error: message });
    }
  },

  openConfirm(tool, args, plan) {
    set({ dialog: { open: true, tool, args, plan } });
  },

  closeConfirm() {
    set({ dialog: null });
  },

  async runGated(tool, args, dryRun) {
    try {
      // A second call from the server-gated dialog carries the existing tool's
      // own confirmation argument through the adapter allowlist.  A first
      // call never gains confirmation implicitly.
      const pendingDialog = get().dialog;
      const actionArgs =
        !dryRun && pendingDialog?.tool === tool
          ? { ...args, confirm: true }
          : args;
      const body = await api.post<{ tool: string; dry_run: boolean; requires_confirm: boolean; result: unknown }>(
        '/api/ops/action',
        { tool, args: actionArgs, dry_run: dryRun },
      );
      set({ dialog: null });
      return { requiresConfirm: body.requires_confirm, result: body.result };
    } catch (err) {
      if (err instanceof ApiError && err.code === 'CONFIRM_REQUIRED') {
        // Server gate: surface the confirm dialog with the plan from details.
        const details = (err.details ?? {}) as Record<string, unknown>;
        set({ dialog: { open: true, tool, args, plan: details } });
        return { requiresConfirm: true, result: details };
      }
      throw err;
    }
  },
}));
