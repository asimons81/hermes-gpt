import { cleanup, render, screen } from '@testing-library/react';
import { afterEach, expect, it, vi } from 'vitest';
import { RoutedApp } from './App';
import { api } from './api/client';

afterEach(() => { cleanup(); vi.restoreAllMocks(); window.history.replaceState({}, '', '/'); });
it('loads installed UI routes and keeps navigation beneath the UI mount', async () => {
  window.history.replaceState({}, '', '/ui/ops/missions');
  vi.spyOn(api, 'get').mockResolvedValue({ missions: [], count: 0, live_cursor: 0, read_only: true });
  render(<RoutedApp />);
  expect(await screen.findByRole('heading', { name: 'Missions' })).toBeInTheDocument();
  expect(screen.getByRole('link', { name: 'Chat' })).toHaveAttribute('href', '/ui/chat');
  expect(screen.getByRole('link', { name: 'Missions' })).toHaveAttribute('href', '/ui/ops/missions');
  expect(window.location.pathname).toBe('/ui/ops/missions');
});
