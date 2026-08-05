import { useState, useRef, useEffect } from 'react';
import { useWebSocket } from '../hooks/useWebSocket';
import { useAudioPlayback } from '../hooks/useAudioPlayback';
import { renderMarkdown } from '../components/MarkdownRenderer';

const WS_URL = import.meta.env.VITE_WS_URL || 'ws://localhost:8001/ws';

export default function Assistant() {
  const [messages, setMessages] = useState([]);
  const [input, setInput] = useState('');
  const scrollRef = useRef(null);
  const pendingResponseRef = useRef(false);

  const { ensureAudioContext } = useAudioPlayback();

  const handleMessage = useRef(null);
  handleMessage.current = (msg) => {
    if (msg.type === 'transcript') {
      setMessages(prev => [...prev, { role: 'user', content: msg.transcript || '', id: msg.request_id }]);
    } else if (msg.type === 'llm_token') {
      setMessages(prev => {
        const last = prev[prev.length - 1];
        if (last && last.role === 'assistant' && last.id === msg.request_id) {
          return [...prev.slice(0, -1), { ...last, content: last.content + (msg.token || '') }];
        }
        return [...prev, { role: 'assistant', content: msg.token || '', id: msg.request_id }];
      });
    } else if (msg.type === 'llm_done') {
      if (msg.text) {
        setMessages(prev => {
          const last = prev[prev.length - 1];
          if (last && last.role === 'assistant' && last.id === msg.request_id) {
            return [...prev.slice(0, -1), { ...last, content: msg.text }];
          }
          return [...prev, { role: 'assistant', content: msg.text, id: msg.request_id }];
        });
      }
      pendingResponseRef.current = false;
    } else if (msg.type === 'error') {
      setMessages(prev => [...prev, { role: 'assistant', content: `Error: ${msg.error}`, id: `err-${Date.now()}` }]);
      pendingResponseRef.current = false;
    }
  };

  const { socketState, send } = useWebSocket(WS_URL, (msg) => handleMessage.current?.(msg));

  useEffect(() => {
    scrollRef.current?.scrollTo({ top: scrollRef.current.scrollHeight, behavior: 'smooth' });
  }, [messages]);

  const handleSend = async () => {
    const text = input.trim();
    if (!text || !socketState || socketState !== 'connected' || pendingResponseRef.current) return;

    await ensureAudioContext?.();
    setMessages(prev => [...prev, { role: 'user', content: text, id: `user-${Date.now()}` }]);
    setInput('');
    pendingResponseRef.current = true;
    send({ type: 'user_text', text });
  };

  const handleClear = () => {
    send({ type: 'clear_context' });
    setMessages([]);
  };

  const canSend = socketState === 'connected' && !pendingResponseRef.current && input.trim();

  return (
    <div className="mx-auto flex h-[calc(100vh-3.5rem)] max-w-4xl flex-col">
      {/* Header */}
      <div className="border-b border-gray-200 bg-white px-6 py-4">
        <div className="flex items-center justify-between">
          <div>
            <h1 className="text-xl font-bold text-gray-900">AI Inspection Assistant</h1>
            <p className="text-sm text-gray-500">Ask questions about inspection data, anomalies, or station status</p>
          </div>
          <div className="flex items-center gap-3">
            <div className="flex items-center gap-1.5 text-xs">
              <span className={`h-2 w-2 rounded-full ${socketState === 'connected' ? 'bg-green-500' : 'bg-red-400'}`} />
              <span className="text-gray-500">{socketState === 'connected' ? 'Connected' : 'Disconnected'}</span>
            </div>
            {messages.length > 0 && (
              <button onClick={handleClear} className="rounded-lg border border-gray-200 px-3 py-1 text-xs text-gray-600 hover:bg-gray-50">
                Clear
              </button>
            )}
          </div>
        </div>
      </div>

      {/* Messages */}
      <div ref={scrollRef} className="flex-1 overflow-y-auto bg-gray-50 px-6 py-4">
        <div className="space-y-4">
          {messages.map((m, i) => (
            <div key={i} className={`flex ${m.role === 'user' ? 'justify-end' : 'justify-start'}`}>
              <div className={`max-w-[80%] rounded-2xl px-4 py-3 text-sm leading-relaxed ${
                m.role === 'user'
                  ? 'bg-[#E3002C] text-white rounded-br-md'
                  : 'bg-white text-gray-800 shadow-sm rounded-bl-md border border-gray-100'
              }`}>
                {m.role === 'assistant' ? renderMarkdown(m.content) : (m.content || '...')}
              </div>
            </div>
          ))}
          {pendingResponseRef.current && (
            <div className="flex justify-start">
              <div className="rounded-2xl rounded-bl-md bg-white px-4 py-3 text-sm text-gray-400 shadow-sm border border-gray-100">
                <span className="inline-flex gap-1">
                  <span className="h-2 w-2 animate-bounce rounded-full bg-gray-300" style={{ animationDelay: '0ms' }} />
                  <span className="h-2 w-2 animate-bounce rounded-full bg-gray-300" style={{ animationDelay: '150ms' }} />
                  <span className="h-2 w-2 animate-bounce rounded-full bg-gray-300" style={{ animationDelay: '300ms' }} />
                </span>
              </div>
            </div>
          )}
          {messages.length === 0 && !pendingResponseRef.current && (
            <div className="py-16 text-center">
              <div className="mb-3 text-4xl">🤖</div>
              <p className="text-sm text-gray-400">Ask me anything about the inspection data</p>
              <div className="mt-6 flex flex-wrap justify-center gap-2">
                {['How many anomalies were detected?', 'Show me the critical defects', 'What objects are in inspection #2?'].map(q => (
                  <button
                    key={q}
                    onClick={() => { setInput(q); }}
                    className="rounded-full border border-gray-200 bg-white px-4 py-2 text-xs text-gray-600 transition-colors hover:bg-gray-50 hover:border-[#E3002C]/30"
                  >
                    {q}
                  </button>
                ))}
              </div>
            </div>
          )}
        </div>
      </div>

      {/* Input */}
      <div className="border-t border-gray-200 bg-white px-6 py-4">
        <div className="flex gap-3">
          <input
            className="flex-1 rounded-xl border border-gray-300 px-4 py-3 text-sm placeholder-gray-400 focus:border-[#E3002C] focus:outline-none focus:ring-2 focus:ring-[#E3002C]/20"
            placeholder={socketState === 'connected' ? 'Type your question...' : 'Connecting...'}
            value={input}
            onChange={e => setInput(e.target.value)}
            onKeyDown={e => e.key === 'Enter' && handleSend()}
            disabled={socketState !== 'connected'}
          />
          <button
            onClick={handleSend}
            disabled={!canSend}
            className="rounded-xl bg-[#E3002C] px-6 py-3 text-sm font-semibold text-white shadow-sm transition-colors hover:bg-[#c2001f] disabled:opacity-40"
          >
            Send
          </button>
        </div>
      </div>
    </div>
  );
}