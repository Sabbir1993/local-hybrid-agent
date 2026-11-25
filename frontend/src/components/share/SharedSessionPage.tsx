import React, { useEffect, useState, useRef } from 'react';
import { Copy, Check, Moon, Sun, AlertCircle } from 'lucide-react';
import { renderCompleteChatMessage } from '../../utils/chatRender';
import { renderMermaidIn } from '../../utils/mermaid';
import { useAppStore } from '../../stores/useAppStore';
import { DiagramModal } from '../modals/DiagramModal';
import { PreviewModal } from '../modals/PreviewModal';

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

  const { openDiagramModal, openPreviewModal } = useAppStore();

  const chatScrollRef = useRef<HTMLDivElement>(null);
  const innerRef = useRef<HTMLDivElement>(null);

  // Extract share key from /share/:key or /share?k=:key
  const getKey = (): string => {
    const path = window.location.pathname.replace(/^\/share\/?/, '').split('/')[0] || '';
    if (path) return path;
    const sp = new URLSearchParams(window.location.search);
    return sp.get('k') || '';
  };

  const transformSharedHtml = (html: string, shareKey: string): string => {
    if (!shareKey) return html;
    // Route file downloads and raw previews to public shared endpoints
    return html
      .replace(/\/agent\/download\?path=/g, `/media/shared-session/${encodeURIComponent(shareKey)}/download?path=`)
      .replace(/\/agent\/raw\?path=/g, `/media/shared-session/${encodeURIComponent(shareKey)}/raw?path=`);
  };

  useEffect(() => {
    // Ensure body doesn't double-scroll while #chat scrolls smoothly
    document.documentElement.style.height = '100%';
    document.body.style.height = '100%';
    document.body.style.overflow = 'hidden';

    return () => {
      document.documentElement.style.height = '';
      document.body.style.height = '';
      document.body.style.overflow = '';
    };
  }, []);

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
    if (data && innerRef.current) {
      renderMermaidIn(innerRef.current);
    }
  }, [data]);

  // Event delegation for code copy, diagrams, previews, and downloads in shared session
  const handleChatClick = (e: React.MouseEvent) => {
    const target = e.target as HTMLElement;

    // 1. Code copy
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
      return;
    }

    // 2. Diagram enlarge / click to view detail in modal
    const enlargeBtn = target.closest('button[data-click="mermaid-enlarge"]');
    const onDiagram = enlargeBtn ? null : target.closest('.mermaid-viewport, .mermaid-box svg');
    const diagramHit = enlargeBtn || onDiagram;
    if (diagramHit) {
      const box = diagramHit.closest('.mermaid-box');
      const svg = (diagramHit.closest('svg') || box?.querySelector('.mermaid-viewport svg')) as SVGSVGElement | null;
      const picked = window.getSelection();
      if (svg && !(onDiagram && picked && !picked.isCollapsed)) {
        e.preventDefault();
        e.stopPropagation();
        openDiagramModal(svg, 'Diagram');
        return;
      }
    }

    // 3. Lazy image loading button [data-load-src]
    const loadImgBtn = target.closest('button[data-load-src]') as HTMLButtonElement | null;
    if (loadImgBtn) {
      e.preventDefault();
      e.stopPropagation();
      const src = loadImgBtn.getAttribute('data-load-src') || '';
      const alt = loadImgBtn.getAttribute('data-load-alt') || 'Image';
      const img = document.createElement('img');
      img.src = src;
      img.alt = alt;
      img.title = 'Click to enlarge';
      img.setAttribute('data-preview-path', src);
      img.setAttribute('data-preview-title', alt);
      img.style.cursor = 'pointer';
      img.referrerPolicy = 'no-referrer';
      const thumbWrap = loadImgBtn.closest('.media-card-thumb-wrap');
      if (thumbWrap) {
        img.className = 'media-card-thumb chat-inline-img';
        img.style.width = '100%';
        img.style.height = '100%';
        img.style.objectFit = 'cover';
        thumbWrap.replaceChildren(img);
      } else {
        img.className = 'chat-inline-img';
        loadImgBtn.replaceWith(img);
      }
      return;
    }

    // 4. File preview [data-preview-path]
    const prevBtn = target.closest('[data-preview-path]') as HTMLElement | null;
    if (prevBtn) {
      e.preventDefault();
      e.stopPropagation();
      const rawPath = prevBtn.getAttribute('data-preview-path') || '';
      const title = prevBtn.getAttribute('data-preview-title') || prevBtn.innerText || rawPath;
      const shareKey = getKey();
      const previewUrl = (rawPath.startsWith('http://') || rawPath.startsWith('https://') || rawPath.startsWith('/media/'))
        ? rawPath
        : `/media/shared-session/${encodeURIComponent(shareKey)}/raw?path=${encodeURIComponent(rawPath)}`;
      openPreviewModal(previewUrl, title, null, 'modal', true);
      return;
    }

    // 5. Image click for modal preview
    const imgEl = target.closest('.bubble img, .bot-content img, .media-card img, .chat-inline-img') as HTMLImageElement | null;
    if (imgEl && !target.closest('button, a')) {
      e.preventDefault();
      e.stopPropagation();
      const src = imgEl.src || imgEl.getAttribute('data-preview-path') || '';
      const title = imgEl.alt || imgEl.title || 'Image preview';
      if (src) {
        openPreviewModal(src, title, null, 'modal', true);
      }
      return;
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
      className="h-screen h-[100dvh] flex flex-col bg-[var(--bg,#0d1117)] text-[var(--text,#f0f6fc)] overflow-hidden"
      style={{ fontFamily: 'var(--sp-body, system-ui, -apple-system, sans-serif)' }}
    >
      {/* Minimal Header */}
      <header className="shrink-0 border-b border-[var(--border,rgba(240,246,252,0.1))] bg-[var(--bg,#0d1117)] px-4 py-3 sm:px-6 z-20">
        <div className="max-w-[850px] mx-auto flex items-center justify-between gap-4">
          <div className="flex items-center gap-3 min-w-0">
            <img
              src="/static/logo/logo.jpg"
              alt="SSL AI"
              className="w-7 h-7 rounded-lg object-cover border border-[var(--border,rgba(240,246,252,0.15))] shadow-sm shrink-0"
              onError={(e) => {
                (e.target as HTMLElement).style.display = 'none';
              }}
            />
            <div className="flex items-center gap-2 min-w-0">
              <span className="font-semibold text-sm truncate text-[var(--text)]">
                {data?.title || 'Shared Session'}
              </span>
              <span className="text-[10px] font-semibold tracking-wide uppercase px-2 py-0.5 rounded-full bg-emerald-500/15 text-emerald-400 border border-emerald-500/30 shrink-0">
                Read Only
              </span>
            </div>
          </div>

          <div className="flex items-center gap-2 shrink-0">
            <button
              type="button"
              onClick={handleCopyLink}
              title="Copy public link"
              className="inline-flex items-center gap-1.5 px-3 py-1.5 text-xs font-medium rounded-lg border border-[var(--border,rgba(240,246,252,0.15))] bg-[var(--panel2,#21262d)] hover:bg-[var(--border,rgba(240,246,252,0.2))] text-[var(--text)] transition cursor-pointer"
            >
              {copiedLink ? <Check size={13} className="text-emerald-400" /> : <Copy size={13} />}
              <span>{copiedLink ? 'Copied' : 'Share link'}</span>
            </button>
            <button
              type="button"
              onClick={toggleTheme}
              title={isDark ? 'Switch to light mode' : 'Switch to dark mode'}
              aria-label="Toggle theme"
              className="w-8 h-8 inline-flex items-center justify-center rounded-lg border border-[var(--border,rgba(240,246,252,0.15))] bg-[var(--panel2,#21262d)] hover:bg-[var(--border,rgba(240,246,252,0.2))] text-[var(--dim,#8b949e)] hover:text-[var(--text)] transition cursor-pointer"
            >
              {isDark ? <Sun size={15} /> : <Moon size={15} />}
            </button>
          </div>
        </div>
      </header>

      {/* Chat Mode Scroll Area (using native #chat and #chat-inner for exact chat mode layout) */}
      <div
        id="chat"
        ref={chatScrollRef}
        onClick={handleChatClick}
        className="flex-1 overflow-y-auto overflow-x-hidden w-full"
        style={{ WebkitOverflowScrolling: 'touch' }}
      >
        <div id="chat-inner" ref={innerRef} style={{ minHeight: '100%', paddingBottom: '40px' }}>
          {loading && (
            <div className="flex-1 flex flex-col items-center justify-center py-24 gap-3 text-[var(--dim,#8b949e)]">
              <div className="w-8 h-8 rounded-full border-2 border-[var(--accent,#79c0ff)] border-t-transparent animate-spin" />
              <p className="text-sm">Loading conversation…</p>
            </div>
          )}

          {error && (
            <div className="flex-1 flex flex-col items-center justify-center py-20 text-center max-w-md mx-auto">
              <div className="w-12 h-12 rounded-full bg-rose-500/10 text-rose-400 flex items-center justify-center mb-3 border border-rose-500/20">
                <AlertCircle size={24} />
              </div>
              <h2 className="text-base font-semibold text-[var(--text)] mb-1">Session Unavailable</h2>
              <p className="text-xs text-[var(--dim,#8b949e)] leading-relaxed mb-6">{error}</p>
              <a
                href="/"
                className="inline-flex items-center gap-2 px-4 py-2 text-xs font-medium rounded-lg bg-[var(--accent,#79c0ff)] text-black font-semibold hover:opacity-90 transition"
              >
                Go to SSL AI
              </a>
            </div>
          )}

          {data && !loading && !error && (
            <>
              {/* Messages in exact Chat Mode styling */}
              {data.messages.map((m, idx) => {
                const isUser = m.role === 'user';
                const shareKey = getKey();
                if (isUser) {
                  return (
                    <div key={idx} className="msg user">
                      <div className="user-msg-col">
                        <div className="bubble">
                          <div
                            className="user-text select-text"
                            dangerouslySetInnerHTML={{
                              __html: transformSharedHtml(renderCompleteChatMessage(m.content, undefined, undefined, true), shareKey),
                            }}
                          />
                        </div>
                        {m.created_at ? (
                          <div className="user-bubble-bottom-bar" style={{ display: 'flex', justifyContent: 'flex-end', padding: '2px 4px 0' }}>
                            <span className="user-msg-time" style={{ fontSize: '11px', color: 'var(--dim)' }}>
                              {formatDate(m.created_at)}
                            </span>
                          </div>
                        ) : null}
                      </div>
                    </div>
                  );
                }

                // Assistant message: exact Claude/Chat Mode unboxed layout
                return (
                  <div key={idx} className="msg bot">
                    <div className="bubble">
                      <div
                        className="bot-content select-text"
                        dangerouslySetInnerHTML={{
                          __html: transformSharedHtml(renderCompleteChatMessage(m.content, undefined, undefined, true), shareKey),
                        }}
                      />
                      <div
                        className="bot-actions"
                        style={{
                          display: 'flex',
                          alignItems: 'center',
                          justifyContent: 'space-between',
                          marginTop: '12px',
                          paddingTop: '8px',
                          borderTop: '1px solid var(--border-subtle, rgba(255,255,255,0.06))',
                        }}
                      >
                        <span style={{ fontSize: '11px', color: 'var(--dim)' }}>
                          {m.created_at ? formatDate(m.created_at) : ''}
                        </span>
                        <button
                          type="button"
                          onClick={() => handleCopyMessage(m.content, idx)}
                          title="Copy response"
                          className="inline-flex items-center gap-1.5 px-2.5 py-1 text-[11px] rounded-md border border-transparent hover:border-[var(--border)] hover:bg-[var(--panel2)] text-[var(--dim)] hover:text-[var(--text)] transition cursor-pointer"
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
                    </div>
                  </div>
                );
              })}
            </>
          )}
        </div>
      </div>

      {/* Subtle Footer */}
      <footer className="shrink-0 border-t border-[var(--border,rgba(240,246,252,0.08))] py-2.5 text-center text-xs text-[var(--dim,#8b949e)] bg-[var(--bg,#0d1117)]">
        <span>SSL AI · Public Shared Chat View</span>
      </footer>

      {/* Preview & Diagram Modals for shared sessions */}
      <PreviewModal />
      <DiagramModal />
    </div>
  );
};

export default SharedSessionPage;
