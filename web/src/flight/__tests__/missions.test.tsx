import { cleanup, render, screen, waitFor } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { MemoryRouter, Route, Routes } from 'react-router-dom';

import { api } from '../../api/client';
import { MissionDetail, MissionsPanel } from '../MissionsPanel';

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
});

const mission = {
  mission_id: 'msn-ui-1',
  title: 'Launch v0.9',
  objective: 'Ship the bounded v0.9 Mission stack.',
  owner_profile: 'default',
  acceptance_criteria: ['all slices observed'],
  context_refs: [{ kind: 'document', ref: 'docs:v0.9', label: 'v0.9 plan', sha256: 'a'.repeat(64) }],
  skills: [{ name: 'compound-engineering', version: '1', ref: 'skill:compound-engineering' }],
  final_approval_required: true,
  status: 'running',
  version: 3,
  approval: {},
  attachments: [{ kind: 'workflow', ref: 'sw-v09', relationship: 'contains', state: 'running' }],
  events: [{ seq: 2, event_type: 'mission.transition', from_status: 'draft', to_status: 'running', created_at: '2026-08-21T19:00:00Z' }],
  updated_at: '2026-08-21T19:00:00Z',
};

const delegation = {
  delegation_id: 'dlg-v09',
  mission_id: 'msn-ui-1',
  task_id: 'task-v09',
  backend: 'opencode',
  state: 'running',
  backend_state: 'running',
  updated_at: '2026-08-21T19:00:00Z',
};

describe('Missions Flight Deck', () => {
  it('renders the Mission list and links to durable detail', async () => {
    vi.spyOn(api, 'get').mockResolvedValue({ missions: [mission], count: 1, live_cursor: 4, read_only: true });
    render(<MemoryRouter><MissionsPanel /></MemoryRouter>);

    expect(await screen.findByText('Launch v0.9')).toBeInTheDocument();
    const link = screen.getByRole('link', { name: 'Launch v0.9' });
    expect(link).toHaveAttribute('href', '/ops/missions/msn-ui-1');
    expect(screen.getByText('running')).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: /approve|cancel|run/i })).not.toBeInTheDocument();
  });

  it('renders Mission lineage, manifests, delegations, attachments, and events read-only', async () => {
    vi.spyOn(api, 'get').mockImplementation((path: string) => {
      if (path.includes('/events?')) return new Promise(() => undefined);
      return Promise.resolve({ mission, delegations: [delegation], delegation_count: 1, live_cursor: 4, read_only: true });
    });
    render(
      <MemoryRouter initialEntries={['/ops/missions/msn-ui-1']}>
        <Routes><Route path="/ops/missions/:missionId" element={<MissionDetail />} /></Routes>
      </MemoryRouter>,
    );

    expect(await screen.findByText('Launch v0.9')).toBeInTheDocument();
    expect(screen.getByText('v0.9 plan')).toBeInTheDocument();
    expect(screen.getByText('compound-engineering')).toBeInTheDocument();
    expect(screen.getByText('dlg-v09')).toBeInTheDocument();
    expect(screen.getByText('sw-v09')).toBeInTheDocument();
    expect(screen.getByText('mission.transition')).toBeInTheDocument();
    expect(await screen.findByText('live updates')).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: /approve|cancel|run/i })).not.toBeInTheDocument();
  });

  it('refreshes the durable Mission snapshot after a live wake-up', async () => {
    let detailReads = 0;
    let eventReads = 0;
    vi.spyOn(api, 'get').mockImplementation((path: string) => {
      if (path.includes('/events?')) {
        eventReads += 1;
        if (eventReads === 1) return Promise.resolve({ cursor: 4, next_cursor: 5, high_watermark: 5, count: 1, events: [{ kind: 'mission.transition' }] });
        return new Promise(() => undefined);
      }
      detailReads += 1;
      return Promise.resolve({
        mission: { ...mission, status: detailReads === 1 ? 'running' : 'succeeded', version: detailReads + 2 },
        delegations: [{ ...delegation, state: detailReads === 1 ? 'running' : 'succeeded' }],
        delegation_count: 1,
        live_cursor: detailReads === 1 ? 4 : 5,
        read_only: true,
      });
    });
    render(
      <MemoryRouter initialEntries={['/ops/missions/msn-ui-1']}>
        <Routes><Route path="/ops/missions/:missionId" element={<MissionDetail />} /></Routes>
      </MemoryRouter>,
    );

    await waitFor(() => expect(detailReads).toBeGreaterThanOrEqual(2));
    expect(screen.getAllByText('succeeded').length).toBeGreaterThanOrEqual(1);
    expect(eventReads).toBeGreaterThanOrEqual(1);
  });
});
