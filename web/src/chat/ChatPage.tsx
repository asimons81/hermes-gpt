import { useEffect } from 'react';
import { Link, useNavigate, useParams } from 'react-router-dom';
import { getMessages, startChat, stopChat } from '../api/chat';
import { ActivityCard } from './ActivityCard';
import { Composer } from './Composer';
import { Message } from './Message';
import { useConversationStore } from '../stores/conversation';
import { useSessionListStore } from '../stores/session-list';

export function ChatPage(): JSX.Element {
  const { sessionId: routeSessionId } = useParams();
  const navigate = useNavigate();
  const conversation = useConversationStore();
  const sessions = useSessionListStore();
  useEffect(() => { void sessions.load(); }, []);
  useEffect(() => { if (!routeSessionId) { conversation.reset(); return; } void getMessages(routeSessionId).then((data) => conversation.hydrate(routeSessionId, data.messages)).catch((error: unknown) => conversation.setError(error instanceof Error ? error.message : 'Unable to load conversation')); }, [routeSessionId]);
  const send = async (message: string): Promise<void> => {
    conversation.appendUser(message);
    conversation.setError(null);
    try {
      await startChat({ sessionId: conversation.sessionId ?? undefined, message, handlers: {
        onMeta: (meta) => { conversation.startStreaming(meta.session_id, meta.turn_id); if (!routeSessionId) navigate(`/chat/${meta.session_id}`, { replace: true }); },
        onToken: conversation.appendToken, onReasoning: conversation.appendReasoning,
        onToolStart: conversation.startTool, onToolEnd: conversation.finishTool,
        onError: (error) => conversation.setError(error.message),
        onDone: (done) => { conversation.finishStreaming(done); void sessions.load(); },
      }});
    } catch (error) { conversation.setError(error instanceof Error ? error.message : 'Unable to send message'); }
  };
  return <div className="app"><a className="skip-link" href="#thread">Skip to conversation</a><header className="topbar"><Link to="/chat" className="wordmark">HERMES <span>CHAT</span></Link><span className="connection-status" data-status={conversation.error ? 'disconnected' : 'connected'}>{conversation.error ? 'Connection needs attention' : 'Connected'}</span></header>
    <nav className="sidebar" aria-label="Conversations"><Link to="/chat" className="new-chat">+ New conversation</Link><div className="history-label">History</div>{sessions.loading && <p className="muted">Loading…</p>}{sessions.error && <p className="inline-error">{sessions.error}</p>}{sessions.sessions.map((session) => <Link key={session.session_id} to={`/chat/${session.session_id}`} className={session.session_id === routeSessionId ? 'history-row active' : 'history-row'}>{session.title || 'Untitled conversation'}<small>{session.message_count} messages</small></Link>)}</nav>
    <main id="thread" className="thread">{conversation.messages.length === 0 ? <section className="empty-state"><p className="eyebrow">CONVERSATION</p><h1>What are we building?</h1><p>Ask Hermes to investigate, implement, or verify work in your local environment.</p></section> : <div className="thread-inner">{conversation.messages.map((message, index) => <Message key={message.message_id ?? `${message.role}-${index}`} message={message} />)}{conversation.activities.map((activity) => <ActivityCard key={activity.callId} activity={activity} onToggle={() => conversation.toggleActivity(activity.callId)} />)}{conversation.streaming && <p className="stream-status" role="status">Hermes is working…</p>}{conversation.error && <div className="inline-error" role="alert">{conversation.error}<button onClick={() => conversation.lastUserMessage && void send(conversation.lastUserMessage)}>Retry</button></div>}</div>}</main>
    <Composer streaming={conversation.streaming} profile="default" model="" onSend={(message) => void send(message)} onStop={() => conversation.sessionId && void stopChat(conversation.sessionId)} />
  </div>;
}
