import { useState } from 'react';
import ReactMarkdown from 'react-markdown';
import remarkGfm from 'remark-gfm';
import type { ChatMessage } from '../api/types';

export function Message({ message }: { message: ChatMessage }): JSX.Element {
  const [copied, setCopied] = useState(false);
  const copy = async (): Promise<void> => {
    await navigator.clipboard?.writeText(message.content);
    setCopied(true);
    window.setTimeout(() => setCopied(false), 1200);
  };
  return <article className={`msg msg--${message.role}`} data-status={message.interrupted ? 'interrupted' : undefined} tabIndex={0} aria-label={`${message.role} message`}>
    <p className="msg-role" aria-hidden="true">{message.role === 'assistant' ? 'HERMES' : message.role.toUpperCase()}</p>
    <div className="msg-body"><div className="prose"><ReactMarkdown remarkPlugins={[remarkGfm]}>{message.content}</ReactMarkdown></div>
      {message.interrupted && <p className="message-note">Generation interrupted</p>}
      {message.content && <button className="message-copy" onClick={() => void copy()} aria-label="Copy message">{copied ? 'Copied' : 'Copy'}</button>}
    </div>
  </article>;
}
