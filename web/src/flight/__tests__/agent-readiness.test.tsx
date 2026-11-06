import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import { api } from '../../api/client';
import { AgentReadinessPanel } from '../AgentReadinessPanel';
import { App } from '../../App';

vi.mock('../../api/client', async importOriginal => ({...await importOriginal<typeof import('../../api/client')>(), api: {get: vi.fn(), post: vi.fn()}}));
beforeEach(() => vi.resetAllMocks());
afterEach(cleanup);
const observation = () => ({
  schema_version: 1, read_only: true, checked_at: '2026-11-06T12:00:00+00:00', hermes_gpt_version: '0.18.0',
  cache_seconds: 0, live_acceptance: 'not_verified', session_control_enabled: false,
  agent_source: {state: 'available', identity_sha256: 'a'.repeat(64), revision: 'b'.repeat(40)},
  cli: {state: 'available', resolution: 'agent_venv', identity_sha256: 'c'.repeat(64), matches_agent_source: true},
  source_protocol: {state: 'declared', code: 'DECLARED', parser_sha256: 'd'.repeat(64), checked_at: '2026-11-06T12:00:00+00:00'},
  finite_protocol: {state: 'not_observed', code: 'NOT_OBSERVED', checked_at: null as string | null, provenance: 'not_observed'},
});

it('separates source declaration from no prior CLI observation and unverified live acceptance; refresh is GET only', async () => {
  vi.mocked(api.get).mockResolvedValue(observation());
  render(<AgentReadinessPanel />);
  await screen.findByText('Declared in source');
  expect(screen.getByText('No prior observation')).toBeInTheDocument();
  expect(screen.getByText(/Do not submit work just to test it/)).toBeInTheDocument();
  expect(screen.getByText('Not verified')).toBeInTheDocument();
  expect(screen.getByText(/Session control is disabled/)).toBeInTheDocument();
  expect(screen.getByText(/Git revision:/)).toHaveTextContent('b'.repeat(40));
  fireEvent.click(screen.getByText('Refresh readiness'));
  await waitFor(() => expect(api.get).toHaveBeenCalledTimes(2));
  expect(api.get).toHaveBeenLastCalledWith('/api/ops/agent-readiness');
  expect(api.post).not.toHaveBeenCalled();
  expect(screen.queryByText(/Confirm/)).toBeNull();
});

it('labels PATH outside source and unsupported protocol with local follow-up', async () => {
  const data = observation();
  data.cli.resolution = 'path'; data.cli.matches_agent_source = false;
  data.finite_protocol = {state: 'unsupported', code: 'FORMAT_NOT_ADVERTISED', checked_at: '2026-11-06T11:00:00+00:00', provenance: 'authorized_session_preflight'};
  vi.mocked(api.get).mockResolvedValue(data);
  render(<AgentReadinessPanel />);
  await screen.findByText('Not advertised in prior CLI preflight');
  expect(screen.getByText(/outside the selected Agent source/)).toBeInTheDocument();
  expect(screen.getByText(/Review the intended Agent installation/)).toBeInTheDocument();
});

it('keeps failed probes unknown and gives a bounded timeout explanation', async () => {
  const data = observation();
  data.finite_protocol = {state: 'unavailable', code: 'PROBE_TIMEOUT', checked_at: '2026-11-06T11:00:00+00:00', provenance: 'authorized_session_preflight'};
  vi.mocked(api.get).mockResolvedValue(data);
  render(<AgentReadinessPanel />);
  await screen.findByText('Compatibility unknown');
  expect(screen.getByText(/did not finish within the time limit/)).toBeInTheDocument();
  expect(screen.queryByText('Advertised in prior CLI preflight')).toBeNull();
});

it('discards stale success if refresh fails and does not expose server errors', async () => {
  vi.mocked(api.get).mockResolvedValueOnce(observation()).mockRejectedValueOnce(new Error('private /host/path token=secret'));
  render(<AgentReadinessPanel />);
  await screen.findByText('Declared in source');
  fireEvent.click(screen.getByText('Refresh readiness'));
  await screen.findByText(/Agent readiness is unavailable/);
  expect(screen.queryByText('Declared in source')).toBeNull();
  expect(document.body.textContent).not.toContain('private');
});

it.each([
  ['supported', 'PROBE_TIMEOUT'], ['unsupported', 'SUPPORTED'], ['unavailable', 'SUPPORTED'], ['unavailable', '__proto__'],
])('fails closed on contradictory protocol metadata %s/%s', async (state, code) => {
  const data = observation(); data.finite_protocol = {...data.finite_protocol, state, code};
  vi.mocked(api.get).mockResolvedValue(data);
  render(<AgentReadinessPanel />);
  await screen.findByText(/Agent readiness is unavailable/);
  expect(screen.queryByText('Advertised in prior CLI preflight')).toBeNull();
});

it('shows a historical matching preflight without treating source declaration as runtime acceptance', async () => {
  const data = observation();
  data.finite_protocol = {state: 'supported', code: 'SUPPORTED', checked_at: '2026-11-06T11:00:00+00:00', provenance: 'authorized_session_preflight'};
  vi.mocked(api.get).mockResolvedValue(data);
  render(<AgentReadinessPanel />);
  await screen.findByText('Advertised in prior CLI preflight');
  expect(screen.getByText(/Prior preflight observed 2026-11-06T11:00/)).toBeInTheDocument();
  expect(screen.getByText(/does not prove that a turn was submitted or completed/)).toBeInTheDocument();
  expect(screen.getByText('Not verified')).toBeInTheDocument();
  expect(api.post).not.toHaveBeenCalled();
});

it('rejects source declarations without a bounded source digest', async () => {
  const data = observation();
  vi.mocked(api.get).mockResolvedValue({...data, source_protocol: {...data.source_protocol, parser_sha256: null}});
  render(<AgentReadinessPanel />);
  await screen.findByText(/Agent readiness is unavailable/);
  expect(screen.queryByText('Declared in source')).toBeNull();
});

it('offers direct Flight Deck navigation without unrelated delivery probes', async () => {
  vi.mocked(api.get).mockResolvedValue(observation());
  render(<MemoryRouter initialEntries={['/ops/agent-readiness']}><App /></MemoryRouter>);
  await screen.findByText('Declared in source');
  expect(screen.getByRole('link', {name: 'Agent readiness'})).toHaveAttribute('href', '/ops/agent-readiness');
  expect(api.get).toHaveBeenCalledTimes(1);
  expect(api.post).not.toHaveBeenCalled();
});
