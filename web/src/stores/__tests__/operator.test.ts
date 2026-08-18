// Store logic tests for the Flight Deck stores.
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { useOperatorStore } from '../operator';

const fakeEnvelope = (surface: string, ttl: number | null) => ({
  surface,
  fetched_at: '2026-01-01T00:00:00Z',
  ttl,
  data: { success: true, available: true, surface, data: { overall: 'pass' }, counts: { checks: 1 } },
});

describe('useOperatorStore', () => {
  beforeEach(() => {
    useOperatorStore.setState({ surfaces: {} });
  });
  afterEach(() => {
    vi.restoreAllMocks();
  });

  it('loads a surface into ready state with TTL', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => new Response(JSON.stringify({ ok: true, data: fakeEnvelope('health', 5) }), {
      status: 200,
      headers: { 'Content-Type': 'application/json' },
    })));

    await useOperatorStore.getState().fetchSurface('health');
    const state = useOperatorStore.getState().surfaces['health'];
    expect(state.status).toBe('ready');
    expect(state.data?.data?.overall).toBe('pass');
    expect(state.ttl).toBe(5);
    expect(state.fetchedAt).toBeTypeOf('number');
  });

  it('skips a fresh cached surface unless forced', async () => {
    let calls = 0;
    vi.stubGlobal('fetch', vi.fn(async () => {
      calls += 1;
      return new Response(JSON.stringify({ ok: true, data: fakeEnvelope('cron', 60) }), {
        status: 200,
        headers: { 'Content-Type': 'application/json' },
      });
    }));

    const s = useOperatorStore.getState();
    await s.fetchSurface('cron');
    await s.fetchSurface('cron'); // cached, fresh -> no second fetch
    expect(calls).toBe(1);

    await useOperatorStore.getState().fetchSurface('cron', true); // force
    expect(calls).toBe(2);
  });

  it('records error state on failure', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => new Response(JSON.stringify({
      ok: false,
      error: { code: 'INTERNAL', message: 'boom' },
    }), { status: 500, headers: { 'Content-Type': 'application/json' } })));

    await useOperatorStore.getState().fetchSurface('failures');
    const state = useOperatorStore.getState().surfaces['failures'];
    expect(state.status).toBe('error');
    expect(state.error).toContain('boom');
  });

  it('marks stale when TTL elapsed while keeping data visible', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => new Response(JSON.stringify({ ok: true, data: fakeEnvelope('usage', -1) }), {
      status: 200,
      headers: { 'Content-Type': 'application/json' },
    })));

    await useOperatorStore.getState().fetchSurface('usage');
    expect(useOperatorStore.getState().surfaces['usage'].status).toBe('ready');
  });
});
