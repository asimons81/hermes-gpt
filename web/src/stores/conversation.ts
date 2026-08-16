import { create } from 'zustand';
import type { ChatMessage, DoneEvent, ToolEndEvent, ToolStartEvent } from '../api/types';

export interface ToolActivity {
  callId: string;
  name: string;
  brief: string;
  status: 'running' | 'ok' | 'error';
  summary?: string;
  durationMs?: number | null;
  expanded: boolean;
}

interface ConversationState {
  sessionId: string | null;
  turnId: string | null;
  messages: ChatMessage[];
  activities: ToolActivity[];
  streaming: boolean;
  error: string | null;
  lastUserMessage: string | null;
  hydrate: (sessionId: string, messages: ChatMessage[]) => void;
  startStreaming: (sessionId: string, turnId: string) => void;
  appendUser: (content: string) => void;
  appendToken: (delta: string) => void;
  appendReasoning: (delta: string) => void;
  startTool: (tool: ToolStartEvent) => void;
  finishTool: (tool: ToolEndEvent) => void;
  toggleActivity: (callId: string) => void;
  setError: (message: string | null) => void;
  finishStreaming: (done: DoneEvent) => void;
  reset: () => void;
}

const blankState = {
  sessionId: null,
  turnId: null,
  messages: [] as ChatMessage[],
  activities: [] as ToolActivity[],
  streaming: false,
  error: null,
  lastUserMessage: null,
};

function updateLastAssistant(messages: ChatMessage[], update: (message: ChatMessage) => ChatMessage): ChatMessage[] {
  const index = [...messages].reverse().findIndex((message) => message.role === 'assistant' && message.message_id === null);
  if (index === -1) {
    return [...messages, update({ message_id: null, role: 'assistant', content: '', ts: Date.now() / 1000 })];
  }
  const target = messages.length - 1 - index;
  return messages.map((message, position) => (position === target ? update(message) : message));
}

export const useConversationStore = create<ConversationState>((set) => ({
  ...blankState,
  hydrate: (sessionId, messages) => set({ ...blankState, sessionId, messages }),
  startStreaming: (sessionId, turnId) => set({ sessionId, turnId, streaming: true, error: null }),
  appendUser: (content) => set((state) => ({
    messages: [...state.messages, { message_id: null, role: 'user', content, ts: Date.now() / 1000 }],
    lastUserMessage: content,
  })),
  appendToken: (delta) => set((state) => ({
    messages: updateLastAssistant(state.messages, (message) => ({ ...message, content: message.content + delta })),
  })),
  appendReasoning: (delta) => set((state) => ({
    messages: updateLastAssistant(state.messages, (message) => ({ ...message, content: message.content + delta })),
  })),
  startTool: (tool) => set((state) => ({
    activities: [...state.activities, { callId: tool.call_id, name: tool.name, brief: tool.brief, status: 'running', expanded: false }],
  })),
  finishTool: (tool) => set((state) => ({
    activities: state.activities.map((activity) => activity.callId === tool.call_id
      ? { ...activity, name: tool.name, status: tool.status === 'ok' ? 'ok' : 'error', summary: tool.summary, durationMs: tool.duration_ms }
      : activity),
  })),
  toggleActivity: (callId) => set((state) => ({
    activities: state.activities.map((activity) => ({ ...activity, expanded: activity.callId === callId ? !activity.expanded : false })),
  })),
  setError: (error) => set({ error, streaming: false }),
  finishStreaming: (done) => set((state) => ({
    streaming: false,
    turnId: null,
    messages: updateLastAssistant(state.messages, (message) => ({
      ...message,
      finish_reason: done.finish_reason,
      interrupted: done.finish_reason === 'interrupted',
    })),
  })),
  reset: () => set(blankState),
}));
