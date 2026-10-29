import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { MemoryRouter, Route, Routes } from 'react-router-dom';
import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import { api } from '../../api/client';
import { LaunchPanel } from '../LaunchPanel';
import { ReviewPanel } from '../ReviewPanel';
import { DeliveryPanel } from '../DeliveryPanel';
vi.mock('../../api/client', async importOriginal => ({...await importOriginal<typeof import('../../api/client')>(), api: {get: vi.fn(), post: vi.fn()}}));
beforeEach(() => { vi.resetAllMocks(); sessionStorage.clear(); });
afterEach(cleanup);
function launch() {
  render(<MemoryRouter><LaunchPanel /></MemoryRouter>);
  fireEvent.change(screen.getByLabelText('Title'), {target: {value: 'Private launch title'}});
  fireEvent.change(screen.getByLabelText('Objective'), {target: {value: 'Private objective'}});
  fireEvent.change(screen.getByLabelText(/Done when/), {target: {value: 'Evidence checked'}});
  fireEvent.click(screen.getByLabelText(/explicitly choose/));
}
it('invalidates preview after edits and recovers lost confirmation without resubmitting', async () => {
  vi.mocked(api.post).mockResolvedValue({result: {success: true, steps: []}});
  launch(); fireEvent.click(screen.getByText('Preview Mission'));
  await screen.findByText('Confirm launch');
  expect(sessionStorage.length).toBe(0);
  fireEvent.change(screen.getByLabelText('Title'), {target: {value: 'Changed title'}});
  expect(screen.queryByText('Confirm launch')).toBeNull();
  fireEvent.click(screen.getByText('Preview Mission')); await screen.findByText('Confirm launch');
  vi.mocked(api.post).mockImplementationOnce(async (_url, body: any) => {
    expect(sessionStorage.getItem('hermes.pendingLaunch')).toBe(body.args.request_id);
    throw new Error('lost response');
  });
  fireEvent.click(screen.getByText('Confirm launch')); await screen.findByText(/Connection lost/);
  expect(api.post).toHaveBeenCalledTimes(3);
  expect(api.get).not.toHaveBeenCalled();
  expect(JSON.stringify(sessionStorage)).not.toContain('Private');
  expect(screen.queryByText('Confirm launch')).toBeNull();
  vi.mocked(api.get).mockResolvedValueOnce({found: true, receipt: {mission_id: 'msn-original', state: 'in_progress', result: {}}});
  fireEvent.click(screen.getByText('Check receipt')); await screen.findByText(/In progress or interrupted/);
  expect(api.post).toHaveBeenCalledTimes(3);
});
it('reload recovers an uncertain attempt by GET only', async () => {
  sessionStorage.setItem('hermes.pendingLaunch', 'request-original-0123456');
  vi.mocked(api.get).mockResolvedValue({found: true, receipt: {mission_id: 'msn-original', state: 'completed', result: {steps: []}}});
  render(<MemoryRouter><LaunchPanel /></MemoryRouter>); await screen.findByText(/Launch completed/);
  expect(api.post).not.toHaveBeenCalled();
  expect(api.get).toHaveBeenCalledWith('/api/ops/launch-receipts/request-original-0123456');
  expect(screen.getByText('Inspect Mission').getAttribute('href')).toBe('/ops/missions/msn-original');
});
it('deduplicates a double click while preview is pending', async () => {
  vi.mocked(api.post).mockImplementation(() => new Promise(() => {}));
  launch(); const button = screen.getByText('Preview Mission'); fireEvent.click(button); fireEvent.click(button);
  expect(api.post).toHaveBeenCalledTimes(1);
});
it('binds review to evidence revision and requires a distinct reviewer', async () => {
  vi.mocked(api.get).mockResolvedValue({item: {item_id: 'a'.repeat(24), mission_id: 'msn-test', contract_sha256: 'abcd'.repeat(16), task_id: 'task-test', delegation_id: 'dlg-test', assignee: 'coder', assignee_identities: ['coder', 'default'], review_revision: 'rev-one', validation: {checks: []}, expected_artifacts: []}});
  vi.mocked(api.post).mockResolvedValue({dry_run: true, result: {success: true}});
  render(<MemoryRouter initialEntries={['/review/' + 'a'.repeat(24)]}><Routes><Route path="/review/:itemId" element={<ReviewPanel />} /></Routes></MemoryRouter>);
  await screen.findByText(/Contract SHA-256/);
  fireEvent.change(screen.getByLabelText('Reviewer identity'), {target: {value: 'default'}});
  fireEvent.change(screen.getByLabelText(/Evidence references/), {target: {value: 'artifact:checked'}});
  expect(screen.getByText('Record review').closest('button')).toBeDisabled();
  fireEvent.change(screen.getByLabelText('Reviewer identity'), {target: {value: 'reviewer-two'}});
  fireEvent.click(screen.getByText('Record review')); await screen.findByText('Confirm record review');
  expect(api.post).toHaveBeenLastCalledWith('/api/ops/action', expect.objectContaining({dry_run: true, args: expect.objectContaining({review_revision: 'rev-one', item_id: 'a'.repeat(24)})}));
  fireEvent.change(screen.getByLabelText(/Evidence references/), {target: {value: 'artifact:new'}});
  expect(screen.queryByText('Confirm record review')).toBeNull();
});
it('delivery inspection never posts or offers resend', async () => {
  vi.mocked(api.get).mockResolvedValue({enabled: true, configured: true, hermes_gpt_version: '0.17.0', agent_compatibility: 'not_probed', targets: [{backend: 'hermes', target: 'masked', pending: 0, failure_count: 0, last_warning: {code: 'SENT_UNVERIFIED'}}]});
  render(<DeliveryPanel />); await screen.findByText(/Suppressed from resend/);
  fireEvent.click(screen.getByText('Refresh')); await waitFor(() => expect(api.get).toHaveBeenCalledTimes(2));
  expect(api.post).not.toHaveBeenCalled();
});
