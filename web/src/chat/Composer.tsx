import { FormEvent, KeyboardEvent, useEffect, useRef, useState } from 'react';

export function Composer({ streaming, disabled, profile, model, onSend, onStop }: { streaming: boolean; disabled?: boolean; profile: string; model: string; onSend: (text: string) => void; onStop: () => void }): JSX.Element {
  const [value, setValue] = useState('');
  const input = useRef<HTMLTextAreaElement>(null);
  useEffect(() => { input.current?.focus(); }, []);
  const submit = (event: FormEvent): void => { event.preventDefault(); const text = value.trim(); if (text && !disabled) { onSend(text); setValue(''); } };
  const keyDown = (event: KeyboardEvent<HTMLTextAreaElement>): void => { if (event.key === 'Enter' && !event.shiftKey) { event.preventDefault(); event.currentTarget.form?.requestSubmit(); } };
  return <form className="composer-region" onSubmit={submit}><div className="composer" data-state={streaming ? 'streaming' : 'idle'}>
    <textarea ref={input} className="composer-input" rows={1} value={value} onChange={(event) => setValue(event.target.value)} onKeyDown={keyDown} disabled={disabled} placeholder="Message Hermes…" aria-label="Message Hermes" />
    {streaming ? <button type="button" className="stop-btn" onClick={onStop} aria-label="Stop generating">Stop</button> : <button className="send-btn" disabled={disabled || !value.trim()} aria-label="Send message">Send</button>}
    <div className="composer-hint"><span>profile · {profile}{model ? ` · ${model}` : ''}</span><span>Enter send · Shift+Enter newline</span></div>
  </div></form>;
}
