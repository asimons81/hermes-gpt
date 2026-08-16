/** Chat + sessions API (core chat shell). */
import { apiFetch, readSseStream, type StreamHandlers } from './client';
import type {
  MessageListData,
  SessionCreateData,
  SessionListData,
  StopData,
} from './types';

export function listSessions(): Promise<SessionListData> {
  return apiFetch<SessionListData>('/api/sessions');
}

export function createSession(): Promise<SessionCreateData> {
  return apiFetch<SessionCreateData>('/api/sessions', { method: 'POST' });
}

export function getMessages(sessionId: string): Promise<MessageListData> {
  return apiFetch<MessageListData>(
    `/api/sessions/${encodeURIComponent(sessionId)}/messages`,
  );
}

export interface ChatStartOptions {
  sessionId?: string;
  message: string;
  profile?: string;
  handlers: StreamHandlers;
  signal?: AbortSignal;
}

/** Start a turn; resolves when the stream reaches a terminal event. */
export function startChat(options: ChatStartOptions): Promise<boolean> {
  const { sessionId, message, profile, handlers, signal } = options;
  const body: Record<string, unknown> = { message };
  if (sessionId) body.session_id = sessionId;
  if (profile) body.profile = profile;
  return readSseStream(
    '/api/chat',
    { method: 'POST', body: JSON.stringify(body) },
    handlers,
    signal,
  );
}

/** Reconnect to an in-flight turn; replay buffered events after `seq`. */
export function reconnectStream(
  sessionId: string,
  turnId: string,
  afterSeq: number,
  handlers: StreamHandlers,
  signal?: AbortSignal,
): Promise<boolean> {
  const params = new URLSearchParams({
    session_id: sessionId,
    turn_id: turnId,
    after: String(afterSeq),
  });
  return readSseStream(`/api/chat/stream?${params.toString()}`, { method: 'GET' }, handlers, signal);
}

export function stopChat(sessionId: string): Promise<StopData> {
  return apiFetch<StopData>('/api/chat/stop', {
    method: 'POST',
    body: JSON.stringify({ session_id: sessionId }),
  });
}
