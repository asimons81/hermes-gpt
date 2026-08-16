// Event History + Approval store tests.
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { useApprovalStore } from '../approval';
import { useEventHistoryStore } from '../eventHistory';

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'Content-Type': 'application/json' },
  });
}

describe('useEventHistoryStore', () => {
  beforeEach(() => {
    useEventHistoryStore.setState({ events: [], envelope: null, status: 'idle', error: null });
  });
  afterEach(() => vi.restoreAllMocks());

  it('fetches events and applies filters to the query string', async () => {
    const fetchMock = vi.fn(async (_input: RequestInfo | URL, _init?: RequestInit) =>
      jsonResponse({
        ok: true,
        data: {
          success: true,
          events: [{ event_id: 'e1', ts: '2026-01-01T00:00:00Z', source: 'audit', kind: 'created' }],
          count_returned: 1,
          count_total: 1,
          truncated: false,
          sources_allowed: ['audit'],
          retention_max_age_days: 90,
          warnings: [],
        },
      }),
    );
    vi.stubGlobal('fetch', fetchMock);

    useEventHistoryStore.getState().setFilters({ source: 'audit', limit: 25 });
    await useEventHistoryStore.getState().fetchEvents();

    const calledUrl = String(fetchMock.mock.calls[0][0]);
    expect(calledUrl).toContain('/api/events?');
    expect(calledUrl).toContain('source=audit');
    expect(calledUrl).toContain('limit=25');

    const s = useEventHistoryStore.getState();
    expect(s.status).toBe('ready');
    expect(s.events).toHaveLength(1);
    expect(s.events[0].event_id).toBe('e1');
  });

  it('records error state on failure', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => jsonResponse({ ok: false, error: { code: 'INTERNAL', message: 'nope' } }, 500)));
    await useEventHistoryStore.getState().fetchEvents();
    expect(useEventHistoryStore.getState().status).toBe('error');
    expect(useEventHistoryStore.getState().error).toContain('nope');
  });
});

describe('useApprovalStore', () => {
  beforeEach(() => {
    useApprovalStore.setState({ items: [], dialog: null, status: 'idle', error: null });
  });
  afterEach(() => vi.restoreAllMocks());

  it('loads pending approvals from the mission surface', async () => {
    vi.stubGlobal('fetch', vi.fn(async () =>
      jsonResponse({
        ok: true,
        data: {
          surface: 'approvals',
          data: {
            success: true,
            data: {
              approvals: [{ kind: 'interrupted_turn', source: 'desktop', id: 'a1', status: 'pending' }],
            },
          },
        },
      }),
    ));
    await useApprovalStore.getState().fetchApprovals();
    const s = useApprovalStore.getState();
    expect(s.status).toBe('ready');
    expect(s.items).toHaveLength(1);
    expect(s.items[0].id).toBe('a1');
  });

  it('surfaces a 409 CONFIRM_REQUIRED as a confirm dialog (gate preserved)', async () => {
    const fetchMock = vi.fn(async () =>
      jsonResponse(
        { ok: false, error: { code: 'CONFIRM_REQUIRED', message: 'confirm required', details: { plan: 'x' } } },
        409,
      ),
    );
    vi.stubGlobal('fetch', fetchMock);

    const result = await useApprovalStore.getState().runGated('hermes_review_accept', { sha: 'abc' }, false);
    expect(result.requiresConfirm).toBe(true);
    const s = useApprovalStore.getState();
    expect(s.dialog).not.toBeNull();
    expect(s.dialog?.tool).toBe('hermes_review_accept');
    expect(s.dialog?.plan).toEqual({ plan: 'x' });
  });

  it('reposts the existing tool confirmation from a server-gated dialog', async () => {
    const fetchMock = vi.fn(async (_input: RequestInfo | URL, _init?: RequestInit) =>
      jsonResponse({
        ok: true,
        data: { tool: 'hermes_review_accept', dry_run: false, requires_confirm: false, result: { success: true } },
      }),
    );
    vi.stubGlobal('fetch', fetchMock);
    useApprovalStore.setState({ dialog: { open: true, tool: 'hermes_review_accept', args: {}, plan: {} } });
    const result = await useApprovalStore.getState().runGated('hermes_review_accept', {}, false);
    expect(result.requiresConfirm).toBe(false);
    expect(useApprovalStore.getState().dialog).toBeNull();
    const request = JSON.parse(String(fetchMock.mock.calls[0][1]?.body ?? '{}'));
    expect(request.dry_run).toBe(false);
    expect(request.args.confirm).toBe(true);
  });
});
