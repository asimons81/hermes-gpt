import { act, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { api } from '../../api/client';
import { AutopilotPanel } from '../AutopilotPanel';

afterEach(() => { vi.restoreAllMocks(); vi.useRealTimers(); });
const running = {
  found: true, effective_state: 'running', stale: false, worker: { liveness: 'alive' },
  summary: { available: true, progress: { total: 4, completed: 2, in_flight: 1, ready: 1 },
    budget: { configured: true, status: 'within', spend: 3, quota: 10, unit: 'USD' },
    recovery: { retries: 1, replans_used: 0, max_replans: 2 },
    limits: { runtime_remaining_seconds: 600, max_concurrency: 3 },
    workers: [{ node_id: 'research', state: 'running', attempt: 1, delegation_state: 'running' }], attention: [] },
};

describe('Autopilot supervision', () => {
  it('shows progress, spend, limits, retries, and workers through GET only', async () => {
    const get = vi.spyOn(api, 'get').mockResolvedValue(running);
    const post = vi.spyOn(api, 'post');
    render(<AutopilotPanel missionId="msn-test" />);
    expect(await screen.findByText(/2 of 4 tasks complete/)).toBeInTheDocument();
    expect(screen.getByText('3 / 10 USD · within')).toBeInTheDocument();
    expect(screen.getByText('attempt 2')).toBeInTheDocument();
    expect(screen.getByRole('progressbar')).toHaveAttribute('value', '2');
    expect(get).toHaveBeenCalledWith('/api/ops/missions/msn-test/autopilot');
    expect(post).not.toHaveBeenCalled();
  });
  it('keeps an old snapshot visibly unverified when refresh fails', async () => {
    vi.spyOn(api, 'get').mockResolvedValueOnce(running).mockRejectedValue(new Error('offline'));
    render(<AutopilotPanel missionId="msn-test" />);
    await screen.findByText(/2 of 4 tasks complete/);
    fireEvent.click(screen.getByRole('button', { name: 'Refresh Autopilot' }));
    expect(await screen.findByText('Status needs verification')).toHaveAttribute('data-status', 'warn');
    expect(screen.getByRole('alert')).toHaveTextContent('Autopilot status unavailable');
  });
  it('shows approval and budget holds, and never paints a dead worker as healthy', async () => {
    vi.spyOn(api, 'get').mockResolvedValue({ ...running, effective_state: 'failed', stale: true, worker: { liveness: 'dead' },
      summary: { ...running.summary, attention: [{ code: 'owner_gate_node', severity: 'owner', nodes: ['approve'] }, { code: 'budget_crossed', severity: 'owner', nodes: [] }] } });
    render(<AutopilotPanel missionId="msn-test" />);
    expect(await screen.findByText('failed')).toHaveAttribute('data-status', 'warn');
    expect(screen.getByText(/budget limit was reached/)).toBeInTheDocument();
    expect(screen.getByText(/needs your decision/)).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: /^approve/i })).not.toBeInTheDocument();
  });
  it('reports no run and unavailable progress without inventing completion', async () => {
    const get = vi.spyOn(api, 'get').mockResolvedValue({ found: false });
    render(<AutopilotPanel missionId="msn-test" />);
    await screen.findByText('No Autopilot run for this Mission');
    get.mockResolvedValue({ found: true, effective_state: 'running', worker: { liveness: 'unverified' }, summary: { available: false } });
    fireEvent.click(screen.getByRole('button', { name: 'Refresh Autopilot' }));
    await screen.findByText('Progress summary unavailable. Completion is unverified.');
    expect(screen.queryByRole('progressbar')).not.toBeInTheDocument();
    expect(screen.getByText('Status needs verification')).toBeInTheDocument();
  });
  it('ignores out-of-order reads and stops polling after unmount', async () => {
    vi.useFakeTimers();
    let resolveOld: (value: unknown) => void = () => undefined;
    const get = vi.spyOn(api, 'get').mockImplementationOnce(() => new Promise((resolve) => { resolveOld = resolve; })).mockResolvedValue({ found: false });
    const { unmount } = render(<AutopilotPanel missionId="msn-test" />);
    await act(async () => { fireEvent.click(screen.getByRole('button', { name: 'Refresh Autopilot' })); });
    await act(async () => { resolveOld(running); });
    expect(screen.getByText('No Autopilot run for this Mission')).toBeInTheDocument();
    expect(screen.queryByRole('progressbar')).not.toBeInTheDocument();
    unmount();
    await act(async () => { vi.advanceTimersByTime(10000); });
    expect(get).toHaveBeenCalledTimes(2);
  });
  it('refreshes on a Mission wake-up revision', async () => {
    const get = vi.spyOn(api, 'get').mockResolvedValue({ found: false });
    const { rerender } = render(<AutopilotPanel missionId="msn-test" revision={1} />);
    await screen.findByText('No Autopilot run for this Mission');
    const reads = get.mock.calls.length;
    get.mockResolvedValue(running);
    rerender(<AutopilotPanel missionId="msn-test" revision={2} />);
    await waitFor(() => expect(get.mock.calls.length).toBeGreaterThan(reads));
    await screen.findByText(/2 of 4 tasks complete/);
  });
});
