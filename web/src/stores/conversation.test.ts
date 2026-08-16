import { beforeEach, describe, expect, it } from 'vitest';
import { useConversationStore } from './conversation';

const reset = (): void => useConversationStore.getState().reset();

describe('useConversationStore', () => {
  beforeEach(reset);

  it('accumulates streamed tokens in one optimistic assistant message', () => {
    const store = useConversationStore.getState();
    store.startStreaming('session-1', 'turn-1');
    store.appendToken('Hello ');
    store.appendToken('Hermes');

    const state = useConversationStore.getState();
    expect(state.sessionId).toBe('session-1');
    expect(state.streaming).toBe(true);
    expect(state.messages).toEqual([
      expect.objectContaining({ role: 'assistant', content: 'Hello Hermes' }),
    ]);
  });

  it('tracks a collapsed tool card and finalizes an interrupted turn', () => {
    const store = useConversationStore.getState();
    store.startStreaming('session-1', 'turn-1');
    store.startTool({ call_id: 'call-1', name: 'hermes_search_files', brief: 'pattern="test"' });
    store.finishTool({ call_id: 'call-1', name: 'hermes_search_files', status: 'ok', summary: '3 matches', duration_ms: 12 });
    store.finishStreaming({ turn_id: 'turn-1', message_id: null, finish_reason: 'interrupted' });

    const state = useConversationStore.getState();
    expect(state.streaming).toBe(false);
    expect(state.messages[0]).toEqual(expect.objectContaining({ interrupted: true }));
    expect(state.activities).toEqual([
      expect.objectContaining({ callId: 'call-1', expanded: false, status: 'ok', summary: '3 matches' }),
    ]);
  });
});
