/** Browser-facing wire types (interface-contracts.md). */

export interface ApiErrorBody {
  code: string;
  message: string;
  trace_id?: string;
  required?: string;
  details?: unknown;
  tool?: string;
}

export interface ApiEnvelope<T> {
  ok: true;
  data: T;
}

export interface ApiErrorEnvelope {
  ok: false;
  error: ApiErrorBody;
}

export type ApiResponse<T> = ApiEnvelope<T> | ApiErrorEnvelope;

export interface SessionSummary {
  session_id: string;
  title: string;
  profile: string;
  model: string;
  message_count: number;
  last_activity_at: number;
  created_at: number;
}

export interface ToolCall {
  call_id: string | null;
  name: string | null;
  arguments: unknown;
}

export interface ChatMessage {
  message_id: number | null;
  role: 'user' | 'assistant' | 'tool' | 'system';
  content: string;
  ts: number;
  tool_calls?: ToolCall[] | null;
  tool_result?: string | null;
  tool_name?: string | null;
  tool_call_id?: string | null;
  finish_reason?: string | null;
  interrupted?: boolean;
}

export interface SessionListData {
  sessions: SessionSummary[];
}

export interface SessionCreateData {
  session_id: string;
  title: string;
}

export interface MessageListData {
  messages: ChatMessage[];
}

export interface StopData {
  stopped: boolean;
}

/** SSE event payloads (POST /api/chat and GET /api/chat/stream). */
export interface MetaEvent {
  session_id: string;
  title: string;
  ts: number;
  turn_id: string;
}

export interface TokenEvent {
  delta: string;
}

export interface ReasoningEvent {
  delta: string;
}

export interface ToolStartEvent {
  call_id: string;
  name: string;
  brief: string;
}

export interface ToolEndEvent {
  call_id: string;
  name: string;
  status: string;
  summary: string;
  duration_ms: number | null;
}

export interface MessageCompleteEvent {
  message_id: number;
  role: string;
}

export interface ErrorEvent {
  code: string;
  message: string;
}

export interface DoneEvent {
  turn_id: string;
  message_id: number | null;
  finish_reason: 'end_turn' | 'stop' | 'interrupted' | 'error' | string;
}

export interface StreamEvent {
  seq: number;
  event:
    | { type: 'meta'; data: MetaEvent }
    | { type: 'token'; data: TokenEvent }
    | { type: 'reasoning'; data: ReasoningEvent }
    | { type: 'tool_start'; data: ToolStartEvent }
    | { type: 'tool_end'; data: ToolEndEvent }
    | { type: 'message_complete'; data: MessageCompleteEvent }
    | { type: 'error'; data: ErrorEvent }
    | { type: 'done'; data: DoneEvent };
}
