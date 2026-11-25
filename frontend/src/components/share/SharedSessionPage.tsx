import React, { useEffect, useState, useRef, useCallback } from 'react';
import { Copy, Check, Moon, Sun, AlertCircle, Lock, Eye, EyeOff } from 'lucide-react';
import { renderCompleteChatMessage } from '../../utils/chatRender';
import { renderMermaidIn } from '../../utils/mermaid';
import { parseUserMessage } from '../../utils/userMessage';
import { useAppStore } from '../../stores/useAppStore';
import { DiagramModal } from '../modals/DiagramModal';
import { PreviewModal } from '../modals/PreviewModal';

interface SharedMessage {
  role: 'user' | 'assistant';
  content: string;
  created_at?: number;
  thinking?: string;
  meta?: {
    displayContent?: string;
    images?: string[];
    files?: string;
    timeStr?: string;
    [key: string]: any;
  };
  images?: string[];
  files?: string;
}

interface SharedData {
  title: string;
  created_at: number;
  shared_at: number;
  messages: SharedMessage[];
}

const SharedImageThumb: React.FC<{
  imgUrl: string;
  shareKey: string;
  onPreview: (url: string, title: string) => void;
}> = ({ imgUrl, shareKey, onPreview }) => {
  const isDirect = /^(data:|https?:|\/)/.test(imgUrl);
  const previewSrc = isDirect
    ? imgUrl
    : `/media/shared-session/${encodeURIComponent(shareKey)}/raw?path=${encodeURIComponent(imgUrl)}`;
  const [failed, setFailed] = useState(false);

  // If a data URL fails to load, never render raw base64 as text
  if (failed && isDirect) return null;

  if (failed || !isDirect) {
    if (!failed) {
      return (
        <img
          src={previewSrc}
          className="chat-img-thumb"
          alt="Attachment"
          title="Click to enlarge"
          style={{
            width: '80px',
            height: '56px',
            maxWidth: '80px',
            maxHeight: '56px',
            minWidth: '80px',
            minHeight: '56px',
            objectFit: 'cover',
            borderRadius: '6px',
            cursor: 'zoom-in',
            flexShrink: 0,
          }}
          onClick={(e) => {
            e.stopPropagation();
            onPreview(previewSrc, imgUrl);
          }}
          onError={() => setFailed(true)}
        />
      );
    }
    // Failed to load filename as direct image: show filename pill (never if data URL)
    if (imgUrl.startsWith('data:')) return null;
    return (
      <button
        type="button"
        className="user-file-pill"
        title={`Preview ${imgUrl}`}
        onClick={(e) => {
          e.stopPropagation();
          onPreview(previewSrc, imgUrl);
        }}
      >
        🖼️ {imgUrl}
      </button>
    );
  }

  return (
    <img
      src={previewSrc}
      className="chat-img-thumb"
      alt="Attachment"
      title="Click to enlarge"
      style={{
        width: '80px',
        height: '56px',
        maxWidth: '80px',
        maxHeight: '56px',
        minWidth: '80px',
        minHeight: '56px',
        objectFit: 'cover',
        borderRadius: '6px',
        cursor: 'zoom-in',
        flexShrink: 0,
      }}
      onClick={(e) => {
        e.stopPropagation();
        onPreview(previewSrc, 'Attachment');
      }}
      onError={() => setFailed(true)}
    />
  );
};

export const SharedSessionPage: React.FC = () => {
  const [data, setData] = useState<SharedData | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [needsPassword, setNeedsPassword] = useState(false);
  const [password, setPassword] = useState('');
  const [showPassword, setShowPassword] = useState(false);
  const [unlockError, setUnlockError] = useState<string | null>(null);
  const [unlocking, setUnlocking] = useState(false);
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

  const loadSharedSession = useCallback((key: string, signal?: { cancelled: boolean }) => {
    setLoading(true);
    setError(null);
    fetch(`/media/shared-session/${encodeURIComponent(key)}`, { credentials: 'same-origin' })
      .then(async (res) => {
        const contentType = res.headers.get('content-type') || '';
        if (!contentType.includes('application/json')) {
          throw new Error('Shared session not found or link has expired.');
        }
        const json = await res.json();
        if (res.status === 401 && json.password_required) {
          return { __passwordRequired: true } as const;
        }
        if (!res.ok) {
          throw new Error(json.error || 'Shared session not found or link has expired.');
        }
        return json as SharedData;
      })
      .then((result) => {
        if (signal?.cancelled) return;
        if ('__passwordRequired' in result) {
          setNeedsPassword(true);
          setLoading(false);
          return;
        }
        setNeedsPassword(false);
        setData(result);
        setLoading(false);
      })
      .catch((err: Error) => {
        if (signal?.cancelled) return;
        setError(err.message || 'Failed to load shared session.');
        setLoading(false);
      });
  }, []);

  useEffect(() => {
    const key = getKey();
    if (!key) {
      setError('No share key provided in URL.');
      setLoading(false);
      return;
    }

    const signal = { cancelled: false };
    loadSharedSession(key, signal);

    return () => {
      signal.cancelled = true;
    };
  }, [loadSharedSession]);

  const handleUnlock = async (e: React.FormEvent) => {
    e.preventDefault();
    const key = getKey();
    if (!key) return;
    if (!password) {
      setUnlockError('Please enter the password.');
      return;
    }
    setUnlocking(true);
    setUnlockError(null);
    try {
      const csrfMatch = document.cookie.match(/(?:^|;\s*)a770_csrf=([^;]+)/);
      const headers: Record<string, string> = { 'Content-Type': 'application/json' };
      if (csrfMatch) {
        headers['X-CSRF-Token'] = decodeURIComponent(csrfMatch[1]);
      }
      const res = await fetch(`/media/shared-session/${encodeURIComponent(key)}/unlock`, {
        method: 'POST',
        credentials: 'same-origin',
        headers,
        body: JSON.stringify({ password }),
      });
      const json = await res.json().catch(() => ({}));
      if (!res.ok) {
        setUnlockError(json.error || 'Incorrect password. Please try again.');
        return;
      }
      setPassword('');
      setNeedsPassword(false);
      loadSharedSession(key);
    } catch (_) {
      setUnlockError('Unable to verify password. Please try again.');
    } finally {
      setUnlocking(false);
    }
  };

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

          {needsPassword && !loading && !error && (
            <div className="flex-1 flex flex-col items-center justify-center py-20 px-4">
              <form
                onSubmit={handleUnlock}
                className="w-full max-w-sm rounded-2xl border border-[var(--border,rgba(240,246,252,0.12))] bg-[var(--panel,#161b22)] p-6 shadow-xl"
              >
                <div className="flex flex-col items-center text-center mb-5">
                  <div className="w-11 h-11 rounded-full bg-[var(--accent,#79c0ff)]/10 text-[var(--accent,#79c0ff)] flex items-center justify-center mb-3 border border-[var(--accent,#79c0ff)]/25">
                    <Lock size={20} />
                  </div>
                  <h2 className="text-base font-semibold text-[var(--text)] mb-1">Password Protected Link</h2>
                  <p className="text-xs text-[var(--dim,#8b949e)] leading-relaxed">
                    Enter the password to view this shared conversation.
                  </p>
                </div>

                <div className="relative mb-3">
                  <input
                    type={showPassword ? 'text' : 'password'}
                    value={password}
                    onChange={(e) => {
                      setPassword(e.target.value);
                      if (unlockError) setUnlockError(null);
                    }}
                    placeholder="Enter password"
                    autoFocus
                    disabled={unlocking}
                    className="w-full rounded-lg border border-[var(--border,rgba(240,246,252,0.15))] bg-[var(--panel2,#21262d)] px-3 py-2.5 pr-10 text-sm text-[var(--text)] placeholder:text-[var(--dim,#8b949e)] focus:outline-none focus:border-[var(--accent,#79c0ff)]"
                  />
                  <button
                    type="button"
                    onClick={() => setShowPassword((v) => !v)}
                    aria-label={showPassword ? 'Hide password' : 'Show password'}
                    className="absolute right-2.5 top-1/2 -translate-y-1/2 text-[var(--dim,#8b949e)] hover:text-[var(--text)] p-1 cursor-pointer"
                  >
                    {showPassword ? <EyeOff size={16} /> : <Eye size={16} />}
                  </button>
                </div>

                {unlockError && (
                  <p className="text-xs text-rose-400 mb-3">{unlockError}</p>
                )}

                <button
                  type="submit"
                  disabled={unlocking}
                  className="w-full py-2.5 px-4 rounded-lg bg-[var(--accent,#79c0ff)] text-black text-sm font-semibold hover:opacity-90 transition cursor-pointer disabled:opacity-60"
                >
                  {unlocking ? 'Verifying…' : 'Unlock Conversation'}
                </button>
              </form>
            </div>
          )}

          {data && !loading && !error && !needsPassword && (
            <>
              {/* Messages in exact Chat Mode styling */}
              {data.messages.map((m, idx) => {
                const isUser = m.role === 'user';
                const shareKey = getKey();
                if (isUser) {
                  const meta = m.meta || {};
                  const { cleanText: parsedClean, images: attachedImages, files: attachedFiles } = parseUserMessage(m.content);

                  let displayText = meta.displayContent || parsedClean;
                  displayText = displayText
                    .replace(/--- IMAGE:[\s\S]*?--- END [^\n]+ ---/g, '')
                    .replace(/--- FILE:[\s\S]*?--- END [^\n]+ ---/g, '')
                    .replace(/\[Attached Files\][\s\S]*?(?:--- END [^\n]+ ---|(?=\n\n)|$)/g, '')
                    .replace(/\[Attached Files:[^\]]*\]/g, '')
                    .trim();

                  const rawImages = [
                    ...(Array.isArray(m.images) ? m.images : []),
                    ...(Array.isArray(meta.images) ? meta.images : []),
                    ...attachedImages,
                  ];
                  const seen = new Set<string>();
                  const uniqueImages: string[] = [];
                  for (const img of rawImages) {
                    if (!img || typeof img !== 'string') continue;
                    const trimmed = img.trim();
                    if (!trimmed || seen.has(trimmed)) continue;
                    seen.add(trimmed);
                    uniqueImages.push(trimmed);
                  }

                  // Filter out plain filenames if we have actual data URLs / HTTP URLs
                  const hasActualUrls = uniqueImages.some((u) => u.startsWith('data:') || u.startsWith('http') || u.startsWith('/'));
                  const allImages = hasActualUrls
                    ? uniqueImages.filter((u) => u.startsWith('data:') || u.startsWith('http') || u.startsWith('/'))
                    : uniqueImages;

                  const rawFiles = [
                    ...(m.files ? m.files.split(',').map((s: string) => s.trim()) : []),
                    ...(meta.files && typeof meta.files === 'string' ? meta.files.split(',').map((s: string) => s.trim()) : []),
                    ...attachedFiles,
                  ];
                  const isImageFileName = (name: string) => /\.(png|jpe?g|webp|gif|bmp|svg)$/i.test(name.trim());
                  const nonImageFiles = Array.from(new Set(rawFiles.filter(Boolean))).filter((fname) => {
                    if (!fname || fname.startsWith('data:')) return false;
                    if (allImages.length > 0 && isImageFileName(fname)) {
                      return false;
                    }
                    return true;
                  });

                  return (
                    <div key={idx} className="msg user">
                      <div className="user-msg-col">
                        <div className="bubble">
                          {allImages.length > 0 && (
                            <div className="user-imgs-gallery" style={{ display: 'flex', flexWrap: 'wrap', gap: '6px', marginBottom: '6px' }}>
                              {allImages.map((imgUrl, iIdx) => (
                                <SharedImageThumb
                                  key={`thumb-${iIdx}`}
                                  imgUrl={imgUrl}
                                  shareKey={shareKey}
                                  onPreview={(url, title) => openPreviewModal(url, title, null, 'modal', true)}
                                />
                              ))}
                            </div>
                          )}

                          <div
                            className="user-text select-text"
                            dangerouslySetInnerHTML={{
                              __html: transformSharedHtml(renderCompleteChatMessage(displayText || (allImages.length || nonImageFiles.length ? '' : m.content), undefined, undefined, true), shareKey),
                            }}
                          />

                          {nonImageFiles.length > 0 && (
                            <div className="user-files-tag" style={{ display: 'flex', flexWrap: 'wrap', gap: '6px', marginTop: '6px', maxWidth: '100%', width: '100%' }}>
                              {nonImageFiles.map((fName, fIdx) => {
                                const fileUrl = /^(data:|https?:|\/)/.test(fName) ? fName : `/media/shared-session/${encodeURIComponent(shareKey)}/raw?path=${encodeURIComponent(fName)}`;
                                return (
                                  <button
                                    key={`file-${fIdx}`}
                                    type="button"
                                    className="user-file-pill"
                                    title={`Preview ${fName} in modal`}
                                    onClick={(e) => {
                                      e.stopPropagation();
                                      openPreviewModal(fileUrl, fName, null, 'modal', true);
                                    }}
                                  >
                                    <span style={{ flexShrink: 0 }}>📎</span>
                                    <span className="user-file-pill-name" style={{ textDecoration: 'underline', textUnderlineOffset: '2px' }}>{fName}</span>
                                  </button>
                                );
                              })}
                            </div>
                          )}
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
