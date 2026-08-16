/** Fetch helpers: JSON envelope decode + SSE stream reader. */
import type { ApiErrorEnvelope, ApiResponse } from './types';

export class ApiError extends Error {
  code: string;
  trace_id?: string;
  required?: string;
  details?: unknown;
  tool?: string;

  constructor(code: string, message: string, trace_id?: string, details?: unknown, required?: string, tool?: string) {
    super(message);
    this.name = 'ApiError';
    this.code = code;
    this.trace_id = trace_id;
    this.details = details;
    this.required = required;
    this.tool = tool;
  }
}

export async function apiFetch<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(path, {
    ...init,
    headers: {
      'Content-Type': 'application/json',
      ...(init?.headers ?? {}),
    },
  });
  let body: ApiResponse<T>;
  try {
    body = (await response.json()) as ApiResponse<T>;
  } catch {
    throw new ApiError('INTERNAL', `Unexpected non-JSON response (${response.status})`);
  }
  if (!response.ok || !body.ok) {
    const err = body as ApiErrorEnvelope;
    throw new ApiError(err.error.code, err.error.message, err.error.trace_id, err.error.details, err.error.required, err.error.tool);
  }
  return (body as { ok: true; data: T }).data;
}

// Flight Deck shares chat's envelope decoder and error behavior rather than
// creating a second browser transport with different authorization semantics.
export const api = {
  get<T>(path: string): Promise<T> {
    return apiFetch<T>(path);
  },
  post<T>(path: string, body: unknown): Promise<T> {
    return apiFetch<T>(path, { method: 'POST', body: JSON.stringify(body) });
  },
};

export interface StreamHandlers {
  onMeta?: (data: { session_id: string; title: string; ts: number; turn_id: string }) => void;
  onToken?: (delta: string) => void;
  onReasoning?: (delta: string) => void;
  onToolStart?: (data: { call_id: string; name: string; brief: string }) => void;
  onToolEnd?: (data: { call_id: string; name: string; status: string; summary: string; duration_ms: number | null }) => void;
  onMessageComplete?: (data: { message_id: number; role: string }) => void;
  onError?: (data: { code: string; message: string }) => void;
  onDone?: (data: { turn_id: string; message_id: number | null; finish_reason: string }) => void;
}

/**
 * Open an SSE stream (POST /api/chat or GET /api/chat/stream) and dispatch
 * events to the handlers.  Resolves when the stream ends cleanly with a
 * `done` event; rejects with ApiError on a non-200 JSON error response.
 * Returns `false` when the connection dropped without a terminal event
 * (caller drives reconnect), `true` when a terminal `done`/`error` arrived.
 */
export async function readSseStream(
  url: string,
  init: RequestInit,
  handlers: StreamHandlers,
  signal?: AbortSignal,
): Promise<boolean> {
  let response: Response;
  try {
    response = await fetch(url, {
      ...init,
      headers: {
        Accept: 'text/event-stream',
        ...(init?.headers ?? {}),
      },
      signal,
    });
  } catch (err) {
    if (isAbort(err)) throw err;
    return false;
  }

  if (!response.ok || !response.body) {
    let bodyText = '';
    try {
      bodyText = await response.text();
    } catch {
      /* ignore */
    }
    try {
      const parsed = JSON.parse(bodyText) as ApiErrorEnvelope;
      throw new ApiError(parsed.error.code, parsed.error.message, parsed.error.trace_id, parsed.error.details, parsed.error.required, parsed.error.tool);
    } catch (err) {
      if (err instanceof ApiError) throw err;
      throw new ApiError('INTERNAL', `Stream request failed (${response.status})`);
    }
  }

  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = '';
  let finished = false;

  const dispatch = (block: string): void => {
    let event = 'message';
    let seq: number | null = null;
    const dataLines: string[] = [];
    for (const line of block.split('\n')) {
      if (line.startsWith('event: ')) {
        event = line.slice('event: '.length).trim();
      } else if (line.startsWith('id: ')) {
        const parsed = Number(line.slice('id: '.length).trim());
        seq = Number.isFinite(parsed) ? parsed : null;
      } else if (line.startsWith('data: ')) {
        dataLines.push(line.slice('data: '.length));
      }
    }
    if (event === 'message' && dataLines.length === 0) {
      return; // heartbeat comment
    }
    let data: unknown = {};
    try {
      data = JSON.parse(dataLines.join('\n'));
    } catch {
      return; // malformed block — skip
    }
    void seq;
    switch (event) {
      case 'meta':
        handlers.onMeta?.(data as never);
        break;
      case 'token':
        handlers.onToken?.((data as { delta: string }).delta);
        break;
      case 'reasoning':
        handlers.onReasoning?.((data as { delta: string }).delta);
        break;
      case 'tool_start':
        handlers.onToolStart?.(data as never);
        break;
      case 'tool_end':
        handlers.onToolEnd?.(data as never);
        break;
      case 'message_complete':
        handlers.onMessageComplete?.(data as never);
        break;
      case 'error':
        handlers.onError?.(data as { code: string; message: string });
        break;
      case 'done':
        handlers.onDone?.(data as never);
        finished = true;
        break;
    }
  };

  try {
    for (;;) {
      const { done, value } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });
      let boundary = buffer.indexOf('\n\n');
      while (boundary !== -1) {
        const block = buffer.slice(0, boundary);
        buffer = buffer.slice(boundary + 2);
        if (block.trim()) dispatch(block);
        boundary = buffer.indexOf('\n\n');
      }
    }
    // Flush any trailing block (e.g. no final blank line).
    if (buffer.trim()) dispatch(buffer);
  } catch (err) {
    if (isAbort(err)) throw err;
    return false;
  }
  return finished;
}

function isAbort(err: unknown): boolean {
  return err instanceof DOMException && err.name === 'AbortError';
}
