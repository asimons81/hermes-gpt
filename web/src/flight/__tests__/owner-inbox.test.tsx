// Owner inbox panel: honest source state, denial, schema checks, and the
// v0.16 per-item actions (dry-run first, then a separate confirm).
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

const BUDGET_ID = '0123456789abcdef01234567';
const MISSION_ID = '89abcdef0123456789abcdef';
const AUTOPILOT_ID = 'fedcba9876543210fedcba98';

// Oldest first on purpose: the panel must still list newest first.
const items = [
  {
    item_id: BUDGET_ID,
    kind: 'budget_breaker',
    mission_id: '',
    summary: 'Budget limit reached; new work is on hold.',
    since: '2026-10-20T10:00:00Z',
    suggested_tool: 'hermes_budget_check',
    requires: 'owner',
  },
  {
    item_id: MISSION_ID,
    kind: 'mission_awaiting_approval',
    mission_id: 'msn-approve',
    summary: 'Mission is awaiting final approval.',
    since: '2026-10-21T09:00:00Z',
    suggested_tool: 'hermes_mission_approve',
    requires: 'owner',
  },
];

const autopilotItem = {
  item_id: AUTOPILOT_ID,
  kind: 'autopilot_waiting_owner',
  mission_id: 'msn-auto',
  summary: 'Autopilot waits on Owner (state=waiting_for_owner); frontier nodes: gate',
  since: '2026-10-21T11:00:00Z',
  suggested_tool: 'hermes_plan_node_transition',
  requires: 'owner',
  plan_id: 'msn-auto',
  frontier_node_ids: ['gate'],
};

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

function postBodies(fetchMock: ReturnType<typeof vi.fn>) {
  return fetchMock.mock.calls
    .filter((call) => (call as unknown as [string, RequestInit?])[1]?.method === 'POST')
    .map((call) => JSON.parse(String((call as unknown as [string, RequestInit])[1].body)));
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

  it('renders items newest first with kind, mission, summary, requires badge and the gated tool as text', async () => {
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
    expect(toolCode.parentElement?.textContent).toBe('Gated tool hermes_mission_approve');

    // Items with an empty mission id render a placeholder, not a blank id.
    expect(screen.getByText('mission —')).toBeInTheDocument();
    expect(screen.queryByText('Nothing is waiting on you.')).not.toBeInTheDocument();
  });

  it('keeps guidance-only kinds as text and offers only Acknowledge for them', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => envelope(payload({ items: [items[0]], count: 1 }))));
    render(<OwnerInboxPanel />);

    await waitFor(() => expect(screen.getByText('Budget limit reached; new work is on hold.')).toBeInTheDocument());
    expect(screen.getByText('hermes_budget_check').parentElement?.textContent).toBe(
      'Act with hermes_budget_check from your MCP client',
    );
    expect(screen.getByRole('button', { name: /acknowledge/i })).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: /approve mission/i })).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: /stop autopilot/i })).not.toBeInTheDocument();
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

  it('issues only GET requests until an action is started', async () => {
    const fetchMock = vi.fn(async () => envelope(payload()));
    vi.stubGlobal('fetch', fetchMock);
    render(<OwnerInboxPanel />);

    await waitFor(() => expect(screen.getByText('mission msn-approve')).toBeInTheDocument());
    fireEvent.click(screen.getByRole('button', { name: 'Refresh' }));
    await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(2));

    for (const call of fetchMock.mock.calls) {
      const [url, init] = call as unknown as [string, RequestInit | undefined];
      expect(url.startsWith('/api/ops/owner-inbox?limit=')).toBe(true);
      expect(init?.method ?? 'GET').toBe('GET');
      expect(init?.body).toBeUndefined();
    }
  });

  it('offers Approve Mission only for mission_awaiting_approval, and disables it until a reference is entered', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => envelope(payload())));
    render(<OwnerInboxPanel />);

    await screen.findByText('Mission is awaiting final approval.');
    expect(screen.getByRole('button', { name: /approve mission/i })).toBeDisabled();
    fireEvent.change(screen.getByRole('textbox'), { target: { value: 'ref-42' } });
    // Editing the reference re-keys the action, so query the button again.
    expect(screen.getByRole('button', { name: /approve mission/i })).toBeEnabled();
  });

  it('approve mission: dry-run POST first, then a second gesture sends confirm, then refetches', async () => {
    const fetchMock = vi.fn(async (_url: string, init?: RequestInit) => {
      if (init?.method === 'POST') {
        const body = JSON.parse(String(init.body));
        return jsonResponse({
          ok: true,
          data: { tool: body.tool, dry_run: body.dry_run, requires_confirm: !body.dry_run ? false : true, result: { success: true, dry_run: body.dry_run } },
        });
      }
      return envelope(payload({ items: [items[1]], count: 1 }));
    });
    vi.stubGlobal('fetch', fetchMock);
    render(<OwnerInboxPanel />);

    await screen.findByText('Mission is awaiting final approval.');
    fireEvent.change(screen.getByRole('textbox'), { target: { value: 'ref-42' } });
    fireEvent.click(screen.getByRole('button', { name: /approve mission/i }));
    await screen.findByText(/DRY-RUN · NO-OP/);

    // No POST has been issued for the confirm gesture yet.
    expect(postBodies(fetchMock)).toHaveLength(1);
    fireEvent.click(screen.getByRole('button', { name: /confirm approve mission/i }));

    await waitFor(() => expect(postBodies(fetchMock)).toHaveLength(2));
    const [dry, real] = postBodies(fetchMock);
    expect(dry).toEqual({ tool: 'hermes_mission_approve', args: { mission_id: 'msn-approve', approval_reference: 'ref-42' }, dry_run: true });
    expect(real).toEqual({
      tool: 'hermes_mission_approve',
      args: { mission_id: 'msn-approve', approval_reference: 'ref-42', confirm: true },
      dry_run: false,
    });
    expect(await screen.findByText('APPLIED')).toBeInTheDocument();
    // The applied action refetches the inbox (a GET after the two POSTs).
    await waitFor(() => {
      const gets = fetchMock.mock.calls.filter((call) => !(call as unknown as [string, RequestInit?])[1]?.method);
      expect(gets.length).toBeGreaterThanOrEqual(2);
    });
    expect(screen.queryByRole('button', { name: /confirm approve mission/i })).not.toBeInTheDocument();
  });

  it('shows the server refusal verbatim when Owner Mode is not active', async () => {
    const refusal = 'mission must be awaiting_approval before Owner approval';
    vi.stubGlobal('fetch', vi.fn(async (_url: string, init?: RequestInit) => {
      if (init?.method === 'POST') {
        return jsonResponse({ ok: false, error: { code: 'MISSION_APPROVAL_REJECTED', message: refusal, tool: 'hermes_mission_approve' } }, 400);
      }
      return envelope(payload({ items: [items[1]], count: 1 }));
    }));
    render(<OwnerInboxPanel />);

    await screen.findByText('Mission is awaiting final approval.');
    fireEvent.change(screen.getByRole('textbox'), { target: { value: 'ref-42' } });
    fireEvent.click(screen.getByRole('button', { name: /approve mission/i }));
    const alert = await screen.findByRole('alert');
    expect(alert.textContent).toBe(`MISSION_APPROVAL_REJECTED: ${refusal}`);
    // Nothing applied, so the confirm control is still offered for a retry and nothing refetched.
    expect(screen.queryByText('APPLIED')).not.toBeInTheDocument();
  });

  it('autopilot item: per frontier node approve/reject and stop, each as dry-run then confirm', async () => {
    const fetchMock = vi.fn(async (_url: string, init?: RequestInit) => {
      if (init?.method === 'POST') {
        const body = JSON.parse(String(init.body));
        return jsonResponse({ ok: true, data: { tool: body.tool, dry_run: body.dry_run, requires_confirm: true, result: { success: true } } });
      }
      return envelope(payload({ items: [autopilotItem], count: 1 }));
    });
    vi.stubGlobal('fetch', fetchMock);
    render(<OwnerInboxPanel />);

    await screen.findByText(/frontier nodes: gate/);
    expect(screen.getByRole('button', { name: /approve node gate/i })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: /reject node gate/i })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: /stop autopilot/i })).toBeInTheDocument();

    fireEvent.click(screen.getByRole('button', { name: /reject node gate/i }));
    await screen.findByText(/DRY-RUN · NO-OP/);
    fireEvent.click(screen.getByRole('button', { name: /confirm reject node gate/i }));
    await waitFor(() => expect(postBodies(fetchMock)).toHaveLength(2));
    const [dry, real] = postBodies(fetchMock);
    expect(dry.tool).toBe('hermes_plan_node_transition');
    expect(dry.dry_run).toBe(true);
    expect(dry.args).toEqual({ mission_id: 'msn-auto', node_id: 'gate', target_state: 'failed' });
    expect(real.dry_run).toBe(false);
    expect(real.args).toEqual({ mission_id: 'msn-auto', node_id: 'gate', target_state: 'failed', confirm: true });
  });

  it('autopilot item without frontier ids offers stop and acknowledge only', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => envelope(payload({ items: [{ ...autopilotItem, frontier_node_ids: [] }], count: 1 }))));
    render(<OwnerInboxPanel />);

    await screen.findByText(/frontier nodes: gate/);
    expect(screen.queryByRole('button', { name: /approve node/i })).not.toBeInTheDocument();
    expect(screen.getByRole('button', { name: /stop autopilot/i })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: /acknowledge/i })).toBeInTheDocument();
  });

  it('acknowledge: dry-run then confirm with no confirm argument, then refetches the inbox', async () => {
    let inbox = payload({ items: [items[0]], count: 1 });
    const fetchMock = vi.fn(async (_url: string, init?: RequestInit) => {
      if (init?.method === 'POST') {
        const body = JSON.parse(String(init.body));
        return jsonResponse({ ok: true, data: { tool: body.tool, dry_run: body.dry_run, requires_confirm: false, result: { success: true, planned: [BUDGET_ID] } } });
      }
      return envelope(inbox);
    });
    vi.stubGlobal('fetch', fetchMock);
    render(<OwnerInboxPanel />);

    await screen.findByText('Budget limit reached; new work is on hold.');
    const getsBefore = fetchMock.mock.calls.filter((call) => !(call as unknown as [string, RequestInit?])[1]?.method).length;
    inbox = payload({ items: [], count: 0, sources: { missions: 'empty', autopilot: 'empty', controller_attention: 'empty' } });

    fireEvent.click(screen.getByRole('button', { name: /acknowledge/i }));
    await screen.findByText(/DRY-RUN · NO-OP/);
    expect(postBodies(fetchMock)).toHaveLength(1);
    fireEvent.click(screen.getByRole('button', { name: /confirm acknowledge/i }));

    await waitFor(() => expect(postBodies(fetchMock)).toHaveLength(2));
    const [dry, real] = postBodies(fetchMock);
    expect(dry).toEqual({ tool: 'hermes_owner_inbox_ack', args: { item_ids: [BUDGET_ID] }, dry_run: true });
    expect(real).toEqual({ tool: 'hermes_owner_inbox_ack', args: { item_ids: [BUDGET_ID] }, dry_run: false });
    expect(await screen.findByText('Nothing is waiting on you.')).toBeInTheDocument();
    const getsAfter = fetchMock.mock.calls.filter((call) => !(call as unknown as [string, RequestInit?])[1]?.method).length;
    expect(getsAfter).toBeGreaterThan(getsBefore);
  });

  it('a dry-run that never applied does not refetch the inbox', async () => {
    const fetchMock = vi.fn(async (_url: string, init?: RequestInit) => {
      if (init?.method === 'POST') {
        return jsonResponse({ ok: true, data: { tool: 'hermes_owner_inbox_ack', dry_run: true, requires_confirm: false, result: { success: true } } });
      }
      return envelope(payload({ items: [items[0]], count: 1 }));
    });
    vi.stubGlobal('fetch', fetchMock);
    render(<OwnerInboxPanel />);

    await screen.findByText('Budget limit reached; new work is on hold.');
    fireEvent.click(screen.getByRole('button', { name: /acknowledge/i }));
    await screen.findByText(/DRY-RUN · NO-OP/);
    expect(postBodies(fetchMock)).toHaveLength(1);
    expect(fetchMock.mock.calls.filter((call) => !(call as unknown as [string, RequestInit?])[1]?.method)).toHaveLength(1);
  });
});

describe('parseOwnerInbox', () => {
  it('accepts a well-formed payload', () => {
    const parsed = parseOwnerInbox(payload());
    expect(parsed.items).toHaveLength(2);
    expect(parsed.items[0].item_id).toBe(BUDGET_ID);
    expect(parsed.read_only).toBe(true);
  });

  it('keeps the autopilot structured fields when present', () => {
    const parsed = parseOwnerInbox(payload({ items: [autopilotItem], count: 1 }));
    expect(parsed.items[0].plan_id).toBe('msn-auto');
    expect(parsed.items[0].frontier_node_ids).toEqual(['gate']);
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

  it.each([
    ['missing item_id', (item: Record<string, unknown>) => { const { item_id: _omit, ...rest } = item; return rest; }],
    ['non-hex item_id', (item: Record<string, unknown>) => ({ ...item, item_id: 'not-an-id' })],
    ['non-array frontier_node_ids', (item: Record<string, unknown>) => ({ ...item, frontier_node_ids: 'gate' })],
    ['non-string plan_id', (item: Record<string, unknown>) => ({ ...item, plan_id: 7 })],
  ])('rejects an item with a %s', (_label, mutate) => {
    const bad = payload({ items: [mutate(autopilotItem)], count: 1 });
    expect(() => parseOwnerInbox(bad)).toThrow(OwnerInboxSchemaError);
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
