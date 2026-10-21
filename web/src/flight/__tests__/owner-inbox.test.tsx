// Owner inbox panel: read-only rendering, honest source state, denial, and schema checks.
import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { useOwnerInboxStore } from '../../stores/ownerInbox';
import { OwnerInboxSchemaError, parseOwnerInbox } from '../schemas';
import { OwnerInboxPanel } from '../OwnerInboxPanel';

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'Content-Type': 'application/json' },
  });
}

// Oldest first on purpose: the panel must still list newest first.
const items = [
  {
    kind: 'budget_breaker',
    mission_id: '',
    summary: 'Budget limit reached; new work is on hold.',
    since: '2026-10-20T10:00:00Z',
    suggested_tool: 'hermes_budget_check',
    requires: 'owner',
  },
  {
    kind: 'mission_awaiting_approval',
    mission_id: 'msn-approve',
    summary: 'Mission is awaiting final approval.',
    since: '2026-10-21T09:00:00Z',
    suggested_tool: 'hermes_mission_approve',
    requires: 'owner',
  },
];

function payload(overrides: Record<string, unknown> = {}) {
  return {
    items,
    count: items.length,
    truncated: false,
    limit: 50,
    sources: { missions: 'ok', autopilot: 'empty', controller_attention: 'ok' },
    complete: true,
    skipped_sources: ['work_contract_review'],
    read_only: true,
    generated_at: '2026-10-21T10:00:00Z',
    ...overrides,
  };
}

function envelope(data: unknown) {
  return jsonResponse({ ok: true, data });
}

describe('OwnerInboxPanel', () => {
  beforeEach(() => {
    useOwnerInboxStore.setState({ payload: null, status: 'idle', error: null, fetchedAt: null });
  });
  afterEach(() => {
    cleanup();
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
  });

  it('renders items newest first with kind, mission, summary, requires badge and suggested tool as text', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => envelope(payload())));
    render(<OwnerInboxPanel />);

    await waitFor(() => expect(screen.getByText('Mission is awaiting final approval.')).toBeInTheDocument());
    const titles = screen.getAllByRole('heading', { level: 3 }).map((node) => node.textContent);
    expect(titles).toEqual(['Mission awaiting approval', 'Budget breaker']);

    expect(screen.getByText('mission msn-approve')).toBeInTheDocument();
    expect(screen.getAllByText('requires owner')).toHaveLength(2);
    expect(screen.getByText('2026-10-21T09:00:00.000Z', { exact: false })).toBeInTheDocument();

    const toolCode = screen.getByText('hermes_mission_approve');
    expect(toolCode.tagName).toBe('CODE');
    expect(toolCode.parentElement?.textContent).toBe('Act with hermes_mission_approve from your MCP client');

    // Items with an empty mission id render a placeholder, not a blank id.
    expect(screen.getByText('mission —')).toBeInTheDocument();
    expect(screen.queryByText('Nothing is waiting on you.')).not.toBeInTheDocument();
  });

  it('warns about unavailable and skipped sources instead of reassuring when incomplete', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => envelope(payload({
      complete: false,
      sources: { missions: 'ok', autopilot: 'unavailable', controller_attention: 'empty' },
    }))));
    render(<OwnerInboxPanel />);

    expect(await screen.findByText('2 known items — some sources unavailable: autopilot.')).toBeInTheDocument();
    expect(screen.getByText('Not checked by this inbox: work_contract_review.')).toBeInTheDocument();
    expect(screen.queryByText('Nothing is waiting on you.')).not.toBeInTheDocument();
  });

  it('never shows "Nothing is waiting on you." when incomplete and empty', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => envelope(payload({
      items: [],
      count: 0,
      complete: false,
      sources: { missions: 'unavailable', autopilot: 'empty', controller_attention: 'empty' },
    }))));
    render(<OwnerInboxPanel />);

    expect(await screen.findByText('0 known items — some sources unavailable: missions.')).toBeInTheDocument();
    expect(screen.queryByText('Nothing is waiting on you.')).not.toBeInTheDocument();
  });

  it('shows the complete-empty state only when every source was read', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => envelope(payload({
      items: [],
      count: 0,
      sources: { missions: 'empty', autopilot: 'empty', controller_attention: 'empty' },
    }))));
    render(<OwnerInboxPanel />);

    expect(await screen.findByText('Nothing is waiting on you.')).toBeInTheDocument();
  });

  it('shows the truncated notice when more items exist', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => envelope(payload({ truncated: true, limit: 2 }))));
    render(<OwnerInboxPanel />);

    expect(await screen.findByText(/showing the newest 2; more items exist/)).toBeInTheDocument();
  });

  it('renders the 403 denied state without a retry or any inbox content', async () => {
    vi.stubGlobal('fetch', vi.fn(async () =>
      jsonResponse({ ok: false, error: { code: 'AUTHZ_DENIED', message: 'Owner inbox is not allowed for this client.' } }, 403),
    ));
    render(<OwnerInboxPanel />);

    const alert = await screen.findByRole('alert');
    expect(within(alert).getByText(/not allowed/)).toBeInTheDocument();
    expect(within(alert).getByText(/HERMES_GPT_MISSION_ALLOWED_SURFACES/)).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Retry' })).not.toBeInTheDocument();
    expect(screen.queryByText('Nothing is waiting on you.')).not.toBeInTheDocument();
  });

  it('shows an error state with retry for a failed request', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => jsonResponse({ ok: false, error: { code: 'INTERNAL', message: 'boom' } }, 500)));
    render(<OwnerInboxPanel />);

    await waitFor(() => expect(screen.getByRole('alert')).toBeInTheDocument());
    expect(screen.getByText(/boom/)).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Retry' })).toBeInTheDocument();
  });

  it('refuses to render a malformed payload', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => envelope(payload({
      items: [{ ...items[0], requires: 'everyone' }],
      count: 1,
    }))));
    render(<OwnerInboxPanel />);

    const alert = await screen.findByRole('alert');
    expect(within(alert).getByText(/owner inbox payload invalid at items\[0\]\.requires/)).toBeInTheDocument();
    expect(screen.queryByText('Budget breaker')).not.toBeInTheDocument();
    expect(screen.queryByText('Nothing is waiting on you.')).not.toBeInTheDocument();
  });

  it('issues only GET requests and offers no mutating controls', async () => {
    const fetchMock = vi.fn(async () => envelope(payload()));
    vi.stubGlobal('fetch', fetchMock);
    render(<OwnerInboxPanel />);

    await waitFor(() => expect(screen.getByText('mission msn-approve')).toBeInTheDocument());
    // Only Refresh exists; it re-issues a GET.
    const buttons = screen.getAllByRole('button');
    expect(buttons.map((button) => button.textContent)).toEqual(['Refresh']);
    fireEvent.click(screen.getByRole('button', { name: 'Refresh' }));
    await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(2));

    for (const call of fetchMock.mock.calls) {
      const [url, init] = call as unknown as [string, RequestInit | undefined];
      expect(url.startsWith('/api/ops/owner-inbox?limit=')).toBe(true);
      expect(init?.method ?? 'GET').toBe('GET');
      expect(init?.body).toBeUndefined();
    }
  });
});

describe('parseOwnerInbox', () => {
  it('accepts a well-formed payload', () => {
    const parsed = parseOwnerInbox(payload());
    expect(parsed.items).toHaveLength(2);
    expect(parsed.read_only).toBe(true);
  });

  it.each([
    ['read_only false', { read_only: false }],
    ['unknown source state', { sources: { missions: 'maybe' } }],
    ['count mismatch', { count: 5 }],
    ['non-boolean complete', { complete: 'yes' }],
    ['items not an array', { items: 'nope' }],
    ['limit out of range', { limit: 500 }],
    ['skipped_sources not strings', { skipped_sources: [1] }],
  ])('rejects a malformed payload (%s)', (_label, overrides) => {
    expect(() => parseOwnerInbox(payload(overrides))).toThrow(OwnerInboxSchemaError);
  });

  it('rejects an item with a non-string summary', () => {
    const bad = payload({ items: [{ ...items[0], summary: 42 }], count: 1 });
    expect(() => parseOwnerInbox(bad)).toThrow(OwnerInboxSchemaError);
  });

  it('rejects a non-object payload', () => {
    expect(() => parseOwnerInbox(null)).toThrow(OwnerInboxSchemaError);
    expect(() => parseOwnerInbox('inbox')).toThrow(OwnerInboxSchemaError);
  });
});
