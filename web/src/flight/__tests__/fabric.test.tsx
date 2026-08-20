import { render, screen, waitFor } from '@testing-library/react';
import { MemoryRouter, Route, Routes } from 'react-router-dom';
import { afterEach, describe, expect, it, vi } from 'vitest';

import { FabricAttemptDetail, FabricPanel } from '../FabricPanel';

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'Content-Type': 'application/json' },
  });
}

afterEach(() => {
  vi.restoreAllMocks();
});

describe('FabricPanel', () => {
  it('renders stale nodes and blocked attempts without optimistic green', async () => {
    vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input);
      if (url.includes('/fabric/nodes')) {
        return jsonResponse({ ok: true, data: {
          success: true,
          available: true,
          health_note: 'Coordinator observation only',
          nodes: [{
            name: 'gaming-4090',
            identity: 'peer-identity',
            availability: 'stale',
            freshness: 'stale',
            active: 1,
            capacity: 4,
            authority_ceiling: 'read_only',
            remote_backends: ['pi_rpc'],
            capabilities: { gpu: { available: true, memory_mb: 24576 } },
          }],
        } });
      }
      return jsonResponse({ ok: true, data: {
        success: true,
        available: true,
        attempts: [{
          attempt_id: 'fatt-123',
          task_id: 'task-123',
          node: 'gaming-4090',
          backend: 'pi_rpc',
          placement_mode: 'auto',
          state: 'BLOCKED',
          blocker: 'FABRIC_PEER_UNAVAILABLE',
        }],
      } });
    }));

    render(<MemoryRouter><FabricPanel /></MemoryRouter>);
    await waitFor(() => expect(screen.getByText('gaming-4090')).toBeInTheDocument());
    expect(screen.getAllByText('stale').length).toBeGreaterThan(0);
    expect(screen.getByText('BLOCKED')).toBeInTheDocument();
    expect(screen.getByText(/FABRIC_PEER_UNAVAILABLE/)).toBeInTheDocument();
  });
});

describe('FabricAttemptDetail', () => {
  it('renders active artifact metadata as isolated and exposes no mutation controls', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => jsonResponse({ ok: true, data: {
      success: true,
      available: true,
      attempt: {
        attempt_id: 'fatt-123',
        node: 'gaming-4090',
        backend: 'pi_rpc',
        placement_mode: 'auto',
        state: 'EVIDENCE_PENDING',
        blocker: 'EVIDENCE_PENDING',
        authority: { granted: 'write_capable', write_epoch: 3 },
        authority_ceiling: 'high_impact',
        routing: {
          explanation_available: true,
          requirements: { gpu: true },
          selected: { node: 'gaming-4090', backend: 'pi_rpc' },
          candidates: [{ node: 'local', eligible: false, exclusions: [{ code: 'CAPABILITY_GPU_MISMATCH' }] }],
        },
        evidence: { terminal_state: 'SUCCEEDED', observations: [{ provenance: 'managed_peer_structured' }] },
        artifacts: [{
          artifact_id: 'fart-1',
          logical_name: 'reports/result.html',
          media_type: 'text/html',
          active_content: true,
          render_policy: 'isolated_metadata_only',
        }],
        events: [{ tool: 'hermes_fabric_status', summary: 'reconcile blocked' }],
        active_content_policy: 'Active HTML/SVG/JavaScript artifacts are never rendered in the trusted Flight Deck origin.',
      },
    } })));

    render(
      <MemoryRouter initialEntries={['/ops/fabric/fatt-123']}>
        <Routes><Route path="/ops/fabric/:attemptId" element={<FabricAttemptDetail />} /></Routes>
      </MemoryRouter>,
    );

    await waitFor(() => expect(screen.getByText('EVIDENCE_PENDING')).toBeInTheDocument());
    expect(screen.getByText('isolated')).toBeInTheDocument();
    expect(screen.getByText(/never rendered in the trusted Flight Deck origin/i)).toBeInTheDocument();
    expect(screen.getByText(/CAPABILITY_GPU_MISMATCH/)).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: /cancel|reconcile|retry/i })).not.toBeInTheDocument();
    expect(screen.queryByRole('iframe')).not.toBeInTheDocument();
  });
});
