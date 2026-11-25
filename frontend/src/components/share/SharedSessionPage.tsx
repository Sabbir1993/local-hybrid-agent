import React, { useEffect, useState, useRef } from 'react';
import { Copy, Check, Moon, Sun, Bot, User, AlertCircle } from 'lucide-react';
import { renderCompleteChatMessage } from '../../utils/chatRender';
import { renderMermaidIn } from '../../utils/mermaid';

interface SharedMessage {
  role: 'user' | 'assistant';
  content: string;
  created_at?: number;
  thinking?: string;
}

interface SharedData {
  title: string;
  created_at: number;
  shared_at: number;
  messages: SharedMessage[];
}

export const SharedSessionPage: React.FC = () => {
  const [data, setData] = useState<SharedData | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [copiedLink, setCopiedLink] = useState(false);
  const [copiedMsgIdx, setCopiedMsgIdx] = useState<number | null>(null);
  const [isDark, setIsDark] = useState(() => {
    return document.documentElement.getAttribute('data-theme') !== 'light';
  });

  const containerRef = useRef<HTMLDivElement>(null);

  // Extract share key from /share/:key or /share?k=:key
  const getKey = (): string => {
    const path = window.location.pathname.replace(/^\/share\/?/, '').split('/')[0] || '';
    if (path) return path;
    const sp = new URLSearchParams(window.location.search);
    return sp.get('k') || '';
  };

  useEffect(() => {
    const key = getKey();
    if (!key) {
      setError('No share key provided in URL.');
      setLoading(false);
      return;
    }

    let isMounted = true;
    fetch(`/media/shared-session/${encodeURIComponent(key)}`)
      .then(async (res) => {
        const contentType = res.headers.get('content-type') || '';
        if (!contentType.includes('application/json')) {
          throw new Error('Shared session not found or link has expired.');
        }
        const json = await res.json();
        if (!res.ok) {
          throw new Error(json.error || 'Shared session not found or link has expired.');
        }
        return json;
      })
      .then((json: SharedData) => {
        if (!isMounted) return;
        setData(json);
        setLoading(false);
      })
      .catch((err: Error) => {
        if (!isMounted) return;
        setError(err.message || 'Failed to load shared session.');
        setLoading(false);
      });

    return () => {
      isMounted = false;
    };
  }, []);

  // Theme toggle
  const toggleTheme = () => {
    const nextDark = !isDark;
    setIsDark(nextDark);
    const themeName = nextDark ? 'dark' : 'light';
    document.documentElement.setAttribute('data-theme', themeName);
    document.documentElement.classList.toggle('dark', nextDark);
    try {
      localStorage.setItem('theme', themeName);
    } catch (_) {}
  };

  // Render Mermaid diagrams when content loads
  useEffect(() => {
    if (data && containerRef.current) {
      renderMermaidIn(containerRef.current);
    }
  }, [data]);

  // Code block copy click delegation
  const handleContainerClick = (e: React.MouseEvent) => {
    const target = e.target as HTMLElement;
    const copyBtn = target.closest<HTMLButtonElement>('button[data-click="code-copy"]');
    if (copyBtn) {
      const codeBox = copyBtn.closest('.code-block');
      const code = codeBox?.querySelector('pre code')?.textContent || '';
      if (code) {
        navigator.clipboard.writeText(code);
        const originalText = copyBtn.textContent;
        copyBtn.textContent = '✓ Copied';
        setTimeout(() => {
          copyBtn.textContent = originalText || '📋 Copy';
        }, 2000);
      }
    }
  };

  const handleCopyLink = () => {
    navigator.clipboard.writeText(window.location.href);
    setCopiedLink(true);
    setTimeout(() => setCopiedLink(false), 2000);
  };

  const handleCopyMessage = (text: string, idx: number) => {
    navigator.clipboard.writeText(text);
    setCopiedMsgIdx(idx);
    setTimeout(() => setCopiedMsgIdx(null), 2000);
  };

  const formatDate = (ts?: number) => {
    if (!ts) return '';
    const ms = ts < 1e11 ? ts * 1000 : ts;
    return new Date(ms).toLocaleDateString(undefined, {
      month: 'short',
      day: 'numeric',
      year: 'numeric',
      hour: '2-digit',
      minute: '2-digit',
    });
  };

  return (
    <div
      className="min-h-screen flex flex-col bg-[var(--bg,#111317)] text-[var(--text,#e2e8f0)]"
      style={{ fontFamily: 'var(--sp-body, system-ui, -apple-system, sans-serif)' }}
    >
      {/* Minimal Public Navigation Header */}
      <header className="sticky top-0 z-30 border-b border-[var(--border,#262b35)] bg-[var(--bg,#111317)]/90 backdrop-blur px-4 py-3 sm:px-6">
        <div className="max-w-4xl mx-auto flex items-center justify-between gap-4">
          <div className="flex items-center gap-3 min-w-0">
            <img
              src="/static/logo/logo.jpg"
              alt="SSL AI"
              className="w-8 h-8 rounded-lg object-cover border border-[var(--border,#323946)] shadow-sm shrink-0"
              onError={(e) => {
                (e.target as HTMLElement).style.display = 'none';
              }}
            />
            <div className="min-w-0">
              <div className="flex items-center gap-2">
                <span className="font-semibold text-sm truncate">
                  {data?.title || 'Shared Session'}
                </span>
                <span className="text-[10.5px] font-medium tracking-wide uppercase px-2 py-0.5 rounded-full bg-emerald-500/10 text-emerald-400 border border-emerald-500/25 shrink-0">
                  Read Only
                </span>
              </div>
            </div>
          </div>

          <div className="flex items-center gap-2 shrink-0">
            <button
              type="button"
              onClick={handleCopyLink}
              title="Copy share link"
              className="inline-flex items-center gap-1.5 px-3 py-1.5 text-xs font-medium rounded-lg border border-[var(--border,#323946)] bg-[var(--sp-raised,rgba(255,255,255,0.04))] hover:bg-[var(--border,#323946)] text-[var(--text,#e2e8f0)] transition"
            >
              {copiedLink ? <Check size={13} className="text-emerald-400" /> : <Copy size={13} />}
              <span>{copiedLink ? 'Copied' : 'Share link'}</span>
            </button>
            <button
              type="button"
              onClick={toggleTheme}
              title={isDark ? 'Switch to light mode' : 'Switch to dark mode'}
              aria-label="Toggle theme"
              className="w-8 h-8 inline-flex items-center justify-center rounded-lg border border-[var(--border,#323946)] bg-[var(--sp-raised,rgba(255,255,255,0.04))] hover:bg-[var(--border,#323946)] text-[var(--dim,#94a3b8)] hover:text-[var(--text,#e2e8f0)] transition"
            >
              {isDark ? <Sun size={15} /> : <Moon size={15} />}
            </button>
          </div>
        </div>
      </header>

      {/* Main Content Area */}
      <main
        ref={containerRef}
        onClick={handleContainerClick}
        className="flex-1 max-w-4xl w-full mx-auto px-4 py-8 sm:px-6 flex flex-col gap-6"
      >
        {loading && (
          <div className="flex-1 flex flex-col items-center justify-center py-20 gap-3 text-[var(--dim,#94a3b8)]">
            <div className="w-8 h-8 rounded-full border-2 border-[var(--accent,#f97316)] border-t-transparent animate-spin" />
            <p className="text-sm">Loading shared session…</p>
          </div>
        )}

        {error && (
          <div className="flex-1 flex flex-col items-center justify-center py-20 text-center max-w-md mx-auto">
            <div className="w-12 h-12 rounded-full bg-rose-500/10 text-rose-400 flex items-center justify-center mb-3 border border-rose-500/20">
              <AlertCircle size={24} />
            </div>
            <h2 className="text-base font-semibold text-[var(--text,#e2e8f0)] mb-1">Session Unavailable</h2>
            <p className="text-xs text-[var(--dim,#94a3b8)] leading-relaxed mb-6">{error}</p>
            <a
              href="/"
              className="inline-flex items-center gap-2 px-4 py-2 text-xs font-medium rounded-lg bg-[var(--accent,#f97316)] text-white hover:opacity-90 transition"
            >
              Go to SSL AI
            </a>
          </div>
        )}

        {data && !loading && !error && (
          <>
            {/* Session Metadata Banner */}
            <div className="border-b border-[var(--border,#262b35)] pb-4 mb-2 flex items-center justify-between text-xs text-[var(--dim,#94a3b8)] flex-wrap gap-2">
              <div>
                <span className="font-medium text-[var(--text,#e2e8f0)]">{data.title}</span>
                {data.created_at ? <span> · Created {formatDate(data.created_at)}</span> : null}
              </div>
              <div>{data.messages.length} messages</div>
            </div>

            {/* Conversation Messages */}
            <div className="flex flex-col gap-6">
              {data.messages.map((m, idx) => {
                const isUser = m.role === 'user';
                return (
                  <article
                    key={idx}
                    className={`flex flex-col gap-2 ${
                      isUser
                        ? 'items-end'
                        : 'items-start w-full'
                    }`}
                  >
                    {/* Header info / avatar */}
                    <div className="flex items-center gap-2 text-[11px] text-[var(--dim,#94a3b8)] px-1">
                      {isUser ? (
                        <>
                          <span className="font-semibold text-[var(--text,#e2e8f0)]">You</span>
                          <User size={13} className="text-[var(--accent,#f97316)]" />
                        </>
                      ) : (
                        <>
                          <Bot size={13} className="text-[var(--accent,#f97316)]" />
                          <span className="font-semibold text-[var(--text,#e2e8f0)]">SSL AI</span>
                          {m.created_at ? <span>· {formatDate(m.created_at)}</span> : null}
                        </>
                      )}
                    </div>

                    {/* Message Bubble/Card */}
                    <div
                      className={`relative rounded-2xl p-4 sm:p-5 text-[13.5px] leading-relaxed transition-colors ${
                        isUser
                          ? 'bg-[var(--sp-raised,rgba(255,255,255,0.06))] border border-[var(--border,#323946)] text-[var(--text,#e2e8f0)] max-w-[85%] rounded-tr-sm shadow-sm'
                          : 'bg-transparent border border-[var(--border,#262b35)] text-[var(--text,#e2e8f0)] w-full rounded-tl-sm shadow-sm'
                      }`}
                    >
                      {/* Thought process (if any, kept unobtrusive) */}
                      {m.thinking && (
                        <details className="mb-3 border border-[var(--border,#262b35)] rounded-lg p-2.5 bg-[var(--sp-raised,rgba(255,255,255,0.02))] text-xs text-[var(--dim,#94a3b8)]">
                          <summary className="cursor-pointer font-medium select-none hover:text-[var(--text,#e2e8f0)]">
                            Thought process
                          </summary>
                          <div className="mt-2 whitespace-pre-wrap font-mono text-[11.5px] opacity-90 border-t border-[var(--border,#262b35)] pt-2">
                            {m.thinking}
                          </div>
                        </details>
                      )}

                      {/* Main prose/markdown (ZERO tool calling badges or internal tool JSON) */}
                      <div
                        className="bot-content overflow-hidden select-text"
                        dangerouslySetInnerHTML={{
                          __html: renderCompleteChatMessage(m.content, undefined, undefined, true),
                        }}
                      />

                      {/* Bottom action for assistant messages: copy response */}
                      {!isUser && (
                        <div className="mt-3 pt-3 border-t border-[var(--border,#262b35)] flex items-center justify-end">
                          <button
                            type="button"
                            onClick={() => handleCopyMessage(m.content, idx)}
                            title="Copy response text"
                            className="inline-flex items-center gap-1.5 px-2.5 py-1 text-[11px] rounded-md border border-transparent hover:border-[var(--border,#323946)] hover:bg-[var(--sp-raised,rgba(255,255,255,0.04))] text-[var(--dim,#94a3b8)] hover:text-[var(--text,#e2e8f0)] transition"
                          >
                            {copiedMsgIdx === idx ? (
                              <>
                                <Check size={12} className="text-emerald-400" />
                                <span>Copied</span>
                              </>
                            ) : (
                              <>
                                <Copy size={12} />
                                <span>Copy text</span>
                              </>
                            )}
                          </button>
                        </div>
                      )}
                    </div>
                  </article>
                );
              })}
            </div>
          </>
        )}
      </main>

      {/* Footer */}
      <footer className="border-t border-[var(--border,#262b35)] py-4 text-center text-xs text-[var(--dim,#94a3b8)]">
        <span>SSL AI · Public Shared Chat View</span>
      </footer>
    </div>
  );
};

export default SharedSessionPage;
