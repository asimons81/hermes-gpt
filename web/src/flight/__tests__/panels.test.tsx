// SurfacePanel + GatedActionButton rendering tests.
import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { MemoryRouter, Route, Routes } from 'react-router-dom';
import { afterEach, describe, expect, it, vi } from 'vitest';

import { GatedActionButton } from '../GatedActionButton';
import { SurfacePanel } from '../SurfacePanel';
import { SwarmDetail } from '../SwarmMonitor';
import type { SurfaceSchema } from '../SurfacePanel';
import { useOperatorStore } from '../../stores/operator';

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'Content-Type': 'application/json' },
  });
}

const healthSchema: SurfaceSchema = {
  surface: 'health',
  title: 'Health',
  stats: [{ key: 'overall', label: 'Overall', kind: 'status' }],
  sections: [{ key: 'checks', label: 'Checks', kind: 'list' }],
};

describe('SurfacePanel', () => {
  afterEach(() => {
    vi.restoreAllMocks();
    useOperatorStore.setState({ surfaces: {} });
  });

  it('renders a ready surface from its schema', async () => {
    vi.stubGlobal('fetch', vi.fn(async () =>
      jsonResponse({
        ok: true,
        data: {
          surface: 'health',
          fetched_at: '2026-01-01T00:00:00Z',
          ttl: 5,
          data: { success: true, available: true, surface: 'health', data: { overall: 'pass', checks: [{ name: 'doctor', status: 'PASS' }] }, counts: { checks: 1 } },
        },
      }),
    ));
    render(<SurfacePanel schema={healthSchema} />);
    expect(screen.getByRole('heading', { name: 'Health' })).toBeInTheDocument();
    await waitFor(() => expect(screen.getByText('pass')).toBeInTheDocument());
    expect(screen.getByText(/"status":"PASS"/)).toBeInTheDocument();
  });

  it('renders unavailable state without throwing', async () => {
    vi.stubGlobal('fetch', vi.fn(async () =>
      jsonResponse({
        ok: true,
        data: {
          surface: 'health',
          fetched_at: '2026-01-01T00:00:00Z',
          ttl: 5,
          data: { success: false, available: false, code: 'AUTHZ_DENIED', unavailable_reason: 'not allowed', surface: 'health', data: {} },
        },
      }),
    ));
    render(<SurfacePanel schema={healthSchema} />);
    await waitFor(() => expect(screen.getByText(/not allowed/)).toBeInTheDocument());
  });

  it('renders error state with retry', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => jsonResponse({ ok: false, error: { code: 'INTERNAL', message: 'boom' } }, 500)));
    render(<SurfacePanel schema={healthSchema} />);
    await waitFor(() => expect(screen.getByRole('alert')).toBeInTheDocument());
    expect(screen.getByText(/boom/)).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Retry' })).toBeInTheDocument();
  });
});

describe('GatedActionButton', () => {
  afterEach(() => {
    vi.restoreAllMocks();
    useOperatorStore.setState({ surfaces: {} });
  });

  it('idle -> dry-run plan -> confirm (never single-click confirm)', async () => {
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(
        jsonResponse({ ok: true, data: { tool: 'hermes_review_accept', dry_run: true, requires_confirm: true, result: { success: true, dry_run: true } } }),
      )
      .mockResolvedValueOnce(
        jsonResponse({ ok: true, data: { tool: 'hermes_review_accept', dry_run: false, requires_confirm: false, result: { success: true } } }),
      );
    vi.stubGlobal('fetch', fetchMock);

    render(
      <MemoryRouter>
        <GatedActionButton tool="hermes_review_accept" args={{ sha: 'abc' }} label="Accept review" />
      </MemoryRouter>,
    );

    // Stage 1: idle — confirm must not be present yet.
    expect(screen.queryByRole('button', { name: /confirm accept review/i })).not.toBeInTheDocument();

    fireEvent.click(screen.getByRole('button', { name: /accept review/i }));
    await waitFor(() => expect(screen.getByText(/DRY-RUN · NO-OP/i)).toBeInTheDocument());
    expect(screen.getByRole('button', { name: /confirm accept review/i })).toBeInTheDocument();

    fireEvent.click(screen.getByRole('button', { name: /confirm accept review/i }));
    await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(2));

    const firstCall = JSON.parse(String(fetchMock.mock.calls[0][1]?.body ?? '{}'));
    const secondCall = JSON.parse(String(fetchMock.mock.calls[1][1]?.body ?? '{}'));
    expect(firstCall.dry_run).toBe(true);
    expect(firstCall.confirm).toBeUndefined(); // never auto-confirm on dry-run
    expect(secondCall.dry_run).toBe(false);
    expect(secondCall.args.confirm).toBe(true);
  });

  it('renders a level gate error verbatim', async () => {
    vi.stubGlobal('fetch', vi.fn(async () =>
      jsonResponse(
        { ok: false, error: { code: 'LEVEL_REQUIRED', message: 'Operator level read_only does not satisfy required level owner', required: 'owner' } },
        403,
      ),
    ));
    render(<GatedActionButton tool="hermes_review_accept" args={{}} label="Accept review" levelTag="owner" />);
    fireEvent.click(screen.getByRole('button', { name: /accept review/i }));
    await waitFor(() => expect(screen.getByRole('alert')).toBeInTheDocument());
    expect(screen.getByText(/LEVEL_REQUIRED/)).toBeInTheDocument();
  });
});

describe('SwarmDetail', () => {
  it('requires a visible stage id before planning workflow advancement', async () => {
    const fetchMock = vi.fn(async (input: RequestInfo | URL) => {
      if (String(input).startsWith('/api/ops/swarm/')) {
        return jsonResponse({ ok: true, data: { workflow_id: 'wf-1', title: 'test', status: 'running', stages: [] } });
      }
      return jsonResponse({ ok: true, data: { tool: 'hermes_swarm_stage_advance', dry_run: true, requires_confirm: true, result: { success: true } } });
    });
    vi.stubGlobal('fetch', fetchMock);
    render(<MemoryRouter initialEntries={['/ops/swarm/wf-1']}><Routes><Route path="/ops/swarm/:workflowId" element={<SwarmDetail />} /></Routes></MemoryRouter>);
    await waitFor(() => expect(screen.getByLabelText('Stage ID')).toBeInTheDocument());
    const advance = screen.getByRole('button', { name: /advance stage/i });
    expect(advance).toBeDisabled();
    fireEvent.change(screen.getByLabelText('Stage ID'), { target: { value: 'implementation' } });
    fireEvent.click(advance);
    await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(2));
    const [, actionInit] = fetchMock.mock.calls[1] as unknown as [unknown, RequestInit | undefined];
    const action = JSON.parse(String(actionInit?.body ?? '{}'));
    expect(action.args.stage_id).toBe('implementation');
    expect(action.dry_run).toBe(true);
  });
});
