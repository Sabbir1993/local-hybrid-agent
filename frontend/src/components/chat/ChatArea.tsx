import React, { useRef, useEffect, useLayoutEffect, useState } from 'react';
import { useAppStore } from '../../stores/useAppStore';
import { renderCompleteChatMessage } from '../../utils/chatRender';
import { PromptDots } from './PromptDots';
import { QuestionCard } from './QuestionCard';
import { MediaCard } from './MediaCard';
import { parseProseQuestions, parseQuestionnaire } from '../../utils/questions';
import { apiFetch } from '../../services/api';

interface ParsedUserMessage {
  cleanText: string;
  images: string[];
  files: string[];
}

function parseUserMessage(raw: string): ParsedUserMessage {
  if (!raw) return { cleanText: '', images: [], files: [] };

  const images: string[] = [];
  const files: string[] = [];

  // Match --- IMAGE: filename.png ---
  const imgRegex = /--- IMAGE:\s*([^\n\r]+?)\s*---([\s\S]*?)(?:--- END\s*[^\n\r]+?---|(?=--- IMAGE:)|(?=--- FILE:)|$)/g;
  let imgMatch: RegExpExecArray | null;
  while ((imgMatch = imgRegex.exec(raw)) !== null) {
    const name = imgMatch[1].trim();
    if (name && !images.includes(name)) images.push(name);
  }

  // Match --- FILE: filename.ext ---
  const fileRegex = /--- FILE:\s*([^\n\r]+?)\s*---([\s\S]*?)(?:--- END\s*[^\n\r]+?---|(?=--- IMAGE:)|(?=--- FILE:)|$)/g;
  let fileMatch: RegExpExecArray | null;
  while ((fileMatch = fileRegex.exec(raw)) !== null) {
    const name = fileMatch[1].trim();
    if (name && !files.includes(name)) files.push(name);
  }

  // Match [Attached Files] header if present
  const attFilesRegex = /\[Attached Files\]\s*([^\n\r]+)/g;
  let attMatch: RegExpExecArray | null;
  while ((attMatch = attFilesRegex.exec(raw)) !== null) {
    const name = attMatch[1].trim();
    if (name && !files.includes(name)) files.push(name);
  }

  // Match [Attached Files: filename.png] if present
  const attFilesInlineRegex = /\[Attached Files:\s*([^\n\r\]]+)\]/g;
  let attInlineMatch: RegExpExecArray | null;
  while ((attInlineMatch = attFilesInlineRegex.exec(raw)) !== null) {
    const names = attInlineMatch[1].split(',').map(s => s.trim()).filter(Boolean);
    names.forEach(n => {
      if (/\.(png|jpe?g|webp|gif|bmp|svg)$/i.test(n)) {
        if (!images.includes(n)) images.push(n);
      } else {
        if (!files.includes(n)) files.push(n);
      }
    });
  }

  // Clean the text displayed to the user
  let cleanText = raw
    .replace(/--- IMAGE:\s*[^\n\r]+?\s*---[\s\S]*?(?:--- END\s*[^\n\r]+?---|(?=--- IMAGE:)|(?=--- FILE:)|$)/g, '')
    .replace(/--- FILE:\s*[^\n\r]+?\s*---[\s\S]*?(?:--- END\s*[^\n\r]+?---|(?=--- IMAGE:)|(?=--- FILE:)|$)/g, '')
    .replace(/\[Attached Files\][\s\S]*?(?:--- END [^\n]+ ---|(?=\n\n)|$)/g, '')
    .replace(/\[Attached Files:[^\]]*\]/g, '')
    .trim();

  return { cleanText: cleanText || (images.length || files.length ? '' : raw), images, files };
}

// Which <details> (tool calls, thoughts, the "worked for" group) the reader opened or closed, per message. The answer's HTML is
// rebuilt on every streamed update, which would otherwise snap every opened step shut again.
const detailsState = new Map<string, boolean>();
// Where the reader left each scrollable action list: rebuilding the HTML would otherwise send it back to the top on every new tool call.
const listScroll = new Map<string, { top: number; stick: boolean }>();

/** An assistant message body. Re-applies the reader's open / closed choices to the freshly rebuilt markup before it paints. */
const BotContent: React.FC<{ id: string; html: string; follow?: boolean }> = ({ id, html: liveHtml, follow }) => {
  const ref = useRef<HTMLDivElement>(null);
  // While the reader is pressing, clicking or scrolling inside the message, a rebuilt DOM would swallow the click (mouse down and
  // up land on different elements) or yank the scrollbar from under the pointer. New content waits until they let go.
  const [html, setHtml] = useState(liveHtml);
  const latest = useRef(liveHtml);
  latest.current = liveHtml;
  const busy = useRef({ down: false, at: 0 });
  useEffect(() => {
    if (liveHtml === html) return;
    const apply = () => {
      const b = busy.current;
      if (b.down || Date.now() - b.at < 350) { timer = window.setTimeout(apply, 120); return; }
      setHtml(latest.current);
    };
    let timer = 0;
    apply();
    return () => window.clearTimeout(timer);
  }, [liveHtml, html]);
  useEffect(() => {
    const root = ref.current;
    if (!root) return;
    const touch = (down?: boolean) => () => { busy.current = { down: down === undefined ? busy.current.down : down, at: Date.now() }; };
    const dn = touch(true), up = touch(false), tick = touch();
    root.addEventListener('pointerdown', dn, true);
    window.addEventListener('pointerup', up, true);
    window.addEventListener('pointercancel', up, true);
    root.addEventListener('wheel', tick, { capture: true, passive: true });
    root.addEventListener('click', tick, true);
    return () => {
      root.removeEventListener('pointerdown', dn, true);
      window.removeEventListener('pointerup', up, true);
      window.removeEventListener('pointercancel', up, true);
      root.removeEventListener('wheel', tick, true);
      root.removeEventListener('click', tick, true);
    };
  }, []);
  useLayoutEffect(() => {
    const root = ref.current;
    if (!root) return;
    root.querySelectorAll('details').forEach((el, i) => {
      const want = detailsState.get(`${id}:${i}`);
      if (want !== undefined && el.open !== want) el.open = want;
    });
    // action list: stay where the reader scrolled to; while the run is live and they have not scrolled up, follow the newest step
    root.querySelectorAll<HTMLElement>('.antigravity-steps-list').forEach((el, i) => {
      const st = listScroll.get(`${id}:${i}`);
      if (st) el.scrollTop = st.stick ? el.scrollHeight : st.top;
      else if (follow) el.scrollTop = el.scrollHeight;
    });
  }, [id, html, follow]);
  useEffect(() => {
    const root = ref.current;
    if (!root) return;
    // `toggle` does not bubble, so listen in the capture phase
    const onToggle = (e: Event) => {
      const el = e.target as HTMLDetailsElement | null;
      if (!el || el.tagName !== 'DETAILS') return;
      const i = Array.from(root.querySelectorAll('details')).indexOf(el);
      if (i >= 0) detailsState.set(`${id}:${i}`, el.open);
    };
    root.addEventListener('toggle', onToggle, true);
    // `scroll` does not bubble either
    const onScroll = (e: Event) => {
      const el = e.target as HTMLElement | null;
      if (!el || !el.classList || !el.classList.contains('antigravity-steps-list')) return;
      const i = Array.from(root.querySelectorAll('.antigravity-steps-list')).indexOf(el);
      if (i >= 0) listScroll.set(`${id}:${i}`, { top: el.scrollTop, stick: el.scrollHeight - el.scrollTop - el.clientHeight < 24 });
    };
    root.addEventListener('scroll', onScroll, true);
    return () => { root.removeEventListener('toggle', onToggle, true); root.removeEventListener('scroll', onScroll, true); };
  }, [id]);
  return <div ref={ref} className="bot-content" dangerouslySetInnerHTML={{ __html: html }} />;
};

/** The prompt text, clamped to 4 lines; reports whether it actually overflows so "See more" shows only then. */
const ClampedUserText: React.FC<{ html: string; expanded: boolean; onOverflow: (over: boolean) => void }> = ({ html, expanded, onOverflow }) => {
  const ref = useRef<HTMLDivElement>(null);
  useLayoutEffect(() => {
    const el = ref.current;
    if (!el || expanded) return;
    const check = () => onOverflow(el.scrollHeight > el.clientHeight + 1);
    check();
    document.fonts?.ready.then(check).catch(() => {});
    const ro = new ResizeObserver(check);
    ro.observe(el);
    return () => ro.disconnect();
  }, [html, expanded]);
  return <div ref={ref} className={`user-text ${expanded ? '' : 'clamp4'}`} dangerouslySetInnerHTML={{ __html: html }} />;
};

/** Held-back answer (gate mode): shows the stage until the check is done. */
const AnswerGate: React.FC<{ m: any }> = ({ m }) => {
  const { patchMessage } = useAppStore();
  const [, tick] = useState(0);
  useEffect(() => {
    const t = window.setInterval(() => tick((n) => n + 1), 1000);
    return () => window.clearInterval(t);
  }, []);
  const c = m.check || {};
  const checking = c.state === 'checking';
  const now = Date.now();
  const words = String(m.content || '').trim() ? String(m.content).trim().split(/\s+/).length : 0;
  const meta = checking
    ? `${Math.max(0, Math.round((now - (c.t1 || now)) / 1000))}s`
    : `${words ? words + ' words, ' : ''}${Math.max(0, Math.round((now - (c.t0 || now)) / 1000))}s`;
  return (
    <div className="ac-gate">
      <span className="ac-gate-main" role="status" aria-live="polite">
        <span>🛡 <span className="ac-step">{checking ? '2/2' : '1/2'}</span> {checking ? `Double-checking with ${c.checker || 'the checker'}…` : 'Writing the answer…'}</span>
        <span className="ac-gate-hint">{checking ? 'It appears as soon as the check is done.' : 'It is held back until it has been checked.'}</span>
      </span>
      <span className="ac-gate-meta" aria-hidden="true">{meta}</span>
      <button type="button" className="ac-skip" title="Show the answer without waiting for the check" onClick={() => patchMessage(m.id, { check: { ...c, skipped: true } })}>Show now</button>
    </div>
  );
};

/** Verdict under a finished answer. */
const AnswerBadge: React.FC<{ m: any }> = ({ m }) => {
  const { patchMessage, sendPrompt } = useAppStore();
  const c = m.check;
  if (!c || c.state !== 'done' || c.dismissed) return null;
  const who = c.checker || 'the checker';
  const issues: Array<{ severity: string; text: string }> = c.issues || [];
  const list = issues.length > 0 && (
    <ul className="ac-issues">
      {issues.map((i, k) => (
        <li key={k}><span className={`ac-sev ac-sev-${i.severity}`}>{i.severity === 'major' ? 'Important' : 'Minor'}</span> {i.text}</li>
      ))}
    </ul>
  );
  const orig = c.original ? (
    <details className="ac-orig"><summary>Show original answer</summary><div className="ac-orig-body" style={{ whiteSpace: 'pre-wrap' }}>{c.original}</div></details>
  ) : null;
  if (c.verdict === 'unverified') {
    return <div className="ac-badge ac-unverified" role="note">🛡 <span>Could not check this time{c.note ? `: ${c.note}` : ''}</span></div>;
  }
  if (c.verdict === 'pass') {
    return (
      <div className="ac-badge ac-pass" role="note">
        ✅ <span>Checked by {who}{c.fixed ? <> · <b>Fixed {c.fixed} issue{c.fixed > 1 ? 's' : ''}</b></> : null}</span>
        {issues.length > 0 && <details className="ac-more"><summary>{issues.length} small note{issues.length > 1 ? 's' : ''}</summary>{list}</details>}
        {orig}
      </div>
    );
  }
  return (
    <div className="ac-badge ac-fail" role="note">
      <details className="ac-more" open>
        <summary>⚠ <span>{issues.length || 'Some'} possible issue{issues.length === 1 ? '' : 's'} found by {who}</span></summary>
        {list}
        <div className="ac-actions">
          <button type="button" className="btn ghost ac-btn" onClick={() => sendPrompt('Please fix these problems in your last answer:\n' + issues.map((i) => '- ' + i.text).join('\n'))}>Ask to fix</button>
          <button type="button" className="btn ghost ac-btn" onClick={() => patchMessage(m.id, { check: { ...c, dismissed: true } })}>Ignore</button>
        </div>
      </details>
      {orig}
    </div>
  );
};

/** Copy / regenerate / thumbs under an assistant answer; visible on hover (always on touch). */
const BotActions: React.FC<{ m: any; isLast: boolean; streaming: boolean }> = ({ m, isLast, streaming }) => {
  const { regenerateLast, showToast, sendPrompt } = useAppStore();
  const [copied, setCopied] = useState(false);
  const [rating, setRating] = useState<number>(Number(m.meta?.rating || 0));
  if (streaming && isLast) return null;
  const text = String(m.content || '').trim();
  if (!text) return null;

  const copy = async () => {
    try {
      await navigator.clipboard.writeText(text);
    } catch (_) {
      const ta = document.createElement('textarea');
      ta.value = text;
      ta.style.position = 'fixed';
      ta.style.left = '-9999px';
      document.body.appendChild(ta);
      ta.select();
      try { document.execCommand('copy'); } catch (__) { /* nothing more to try */ }
      document.body.removeChild(ta);
    }
    setCopied(true);
    window.setTimeout(() => setCopied(false), 1200);
  };

  // clicking the active thumb again clears the rating
  const rate = async (value: 1 | -1) => {
    const next = rating === value ? 0 : value;
    try {
      const r = await apiFetch('/agent/feedback', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ run_id: m.runId, rating: next }),
      });
      if (!r.ok) throw new Error(`HTTP ${r.status}`);
      setRating(next);
    } catch (e: any) {
      showToast('Could not save feedback: ' + e.message, 'error');
    }
  };

  const btn: React.CSSProperties = { background: 'transparent', border: 'none', cursor: 'pointer', color: 'var(--dim)', padding: '2px 6px', fontSize: 12, borderRadius: 4 };
  return (
    <>
    {isLast && m.canContinue && (
      <button type="button" className="btn accent" style={{ width: 'auto', margin: '8px 0 0', padding: '6px 16px', fontSize: 12.5 }}
        title="Carry on from where it stopped" onClick={() => { void sendPrompt('continue'); }}>Continue</button>
    )}
    <div className="bot-actions" style={{ display: 'flex', gap: 2, marginTop: 6 }}>
      <button type="button" style={btn} title="Copy answer" onClick={copy}>{copied ? 'Copied' : 'Copy'}</button>
      {isLast && <button type="button" style={btn} title="Answer again" onClick={() => regenerateLast()}>Regenerate</button>}
      {m.runId && (
        <>
          <button type="button" style={{ ...btn, color: rating === 1 ? 'var(--green, #4ade80)' : undefined }} title="Good answer" aria-pressed={rating === 1} onClick={() => rate(1)}>👍</button>
          <button type="button" style={{ ...btn, color: rating === -1 ? 'var(--red, #f87171)' : undefined }} title="Bad answer" aria-pressed={rating === -1} onClick={() => rate(-1)}>👎</button>
        </>
      )}
    </div>
    </>
  );
};

function relTime(ms: number, now: number): string {
  const sec = Math.max(0, Math.round((now - ms) / 1000));
  if (sec < 45) return 'just now';
  const min = Math.round(sec / 60);
  if (min < 60) return min === 1 ? '1 minute ago' : `${min} minutes ago`;
  const hr = Math.round(min / 60);
  if (hr < 24) return hr === 1 ? '1 hour ago' : `${hr} hours ago`;
  const day = Math.round(hr / 24);
  return day === 1 ? '1 day ago' : `${day} days ago`;
}

// Rendering markdown + highlighting + sanitizing is costly and a streamed token re-renders the whole list:
// finished messages are rendered once and reused.
const renderCache = new Map<string, string>();
function renderCached(key: string | null, render: () => string): string {
  if (key === null) return render();
  const hit = renderCache.get(key);
  if (hit !== undefined) return hit;
  const html = render();
  if (renderCache.size > 400) renderCache.delete(renderCache.keys().next().value as string);
  renderCache.set(key, html);
  return html;
}

export const ChatArea: React.FC = () => {
  const [nowTick, setNowTick] = useState(() => Date.now());
  useEffect(() => {
    const t = window.setInterval(() => setNowTick(Date.now()), 30000);
    return () => window.clearInterval(t);
  }, []);
  const sessionLoading = useAppStore((st) => st.sessionLoading);
  const { sendPrompt, isStreaming, messages, selectedModel, serverStatus, isModelLoading, loadSelectedModel, openPreviewModal, appMode } = useAppStore();
  const [expandedPrompts, setExpandedPrompts] = useState<Record<string, boolean>>({});
  const [overflowPrompts, setOverflowPrompts] = useState<Record<string, boolean>>({});
  const chatEndRef = useRef<HTMLDivElement>(null);
  const chatContainerRef = useRef<HTMLDivElement>(null);
  const isUserScrolledUp = useRef<boolean>(false);
  const [showJump, setShowJump] = useState(false);
  const isChatMode = appMode === 'chat';

  const handleScroll = (e: React.UIEvent<HTMLDivElement>) => {
    const el = e.currentTarget;
    const distanceFromBottom = el.scrollHeight - el.scrollTop - el.clientHeight;
    // If user is more than 80px from bottom, they are actively reading or scrolling up
    isUserScrolledUp.current = distanceFromBottom > 80;
    setShowJump(distanceFromBottom > 240);
  };

  const firstId = messages[0]?.id;
  const lastRole = messages[messages.length - 1]?.role;
  const lastId = messages[messages.length - 1]?.id;
  useEffect(() => {
    isUserScrolledUp.current = false;   // another chat was opened: start at its end
    setShowJump(false);
  }, [firstId]);
  useEffect(() => {
    if (lastRole === 'user') isUserScrolledUp.current = false;   // you just sent something: show it
  }, [lastId, lastRole]);

  useEffect(() => {
    // If the user has scrolled up to inspect previous steps/content, don't move or jump
    if (isUserScrolledUp.current) return;

    if (chatContainerRef.current) {
      chatContainerRef.current.scrollTop = chatContainerRef.current.scrollHeight;
    }
  }, [messages]);

  const handleChatClick = (e: React.MouseEvent) => {
    const target = e.target as HTMLElement;

    // 1. Handle code block copy
    const copyBtn = target.closest('button[data-click="code-copy"]');
    if (copyBtn) {
      e.stopPropagation();
      const codeBlock = copyBtn.closest('.code-block');
      const codeEl = codeBlock?.querySelector('pre code');
      if (codeEl && !copyBtn.hasAttribute('data-copied')) {
        const code = codeEl.textContent || '';
        const done = () => {
          const label = copyBtn.textContent || '📋 Copy';
          copyBtn.setAttribute('data-copied', '1');
          copyBtn.textContent = '✓ Copied';
          setTimeout(() => {
            copyBtn.textContent = label;
            copyBtn.removeAttribute('data-copied');
          }, 1500);
        };
        if (navigator.clipboard && window.isSecureContext) {
          navigator.clipboard.writeText(code).then(done, () => { _copyFallback(code); done(); });
        } else {
          _copyFallback(code);
          done();
        }
      }
      return;
    }

    // 2. Handle sub-summary click: toggle all steps in working trace
    const toggleSub = target.closest('[data-click="toggle-all-antigravity"], [data-click="toggle-all-codex"], .antigravity-sub-summary');
    if (toggleSub) {
      e.preventDefault();
      e.stopPropagation();
      const container = toggleSub.closest('.agy-agent-container') || toggleSub.closest('.antigravity-trace-group');
      if (container) {
        const stepItems = container.querySelectorAll<HTMLDetailsElement>('.antigravity-step-item');
        const anyOpen = Array.from(stepItems).some((el) => el.open);
        stepItems.forEach((el) => {
          el.open = !anyOpen;
        });
      }
      return;
    }

    // 3. Handle lazy image loading button: [data-load-src]
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

    // 4. Handle file preview buttons & elements with [data-preview-path]
    const prevBtn = target.closest('[data-preview-path]') as HTMLElement | null;
    if (prevBtn) {
      e.preventDefault();
      e.stopPropagation();
      const path = prevBtn.getAttribute('data-preview-path') || '';
      const title = prevBtn.getAttribute('data-preview-title') || prevBtn.innerText || path;
      const source = (prevBtn.getAttribute('data-preview-source') || 'common') as 'common' | 'ws';
      if (path) {
        openPreviewModal(path, title, null, source);
      }
      return;
    }

    // 5. Handle image click inside chat bubble or media card for full-resolution preview
    const imgEl = target.closest('.bubble img, .bot-content img, .media-card img, .chat-inline-img, .media-card-thumb') as HTMLImageElement | null;
    if (imgEl && !target.closest('button, a')) {
      e.preventDefault();
      e.stopPropagation();
      const src = imgEl.src || imgEl.getAttribute('data-preview-path') || '';
      const title = imgEl.alt || imgEl.title || imgEl.getAttribute('data-preview-title') || 'Image preview';
      if (src) {
        openPreviewModal(src, title, null, 'modal', true);
      }
      return;
    }
  };

  const isModelLoaded = Boolean(serverStatus?.pid || serverStatus?.cloud_main);
  const isCloudActive = Boolean(serverStatus?.cloud_main);
  const modelName = serverStatus?.cloud_main?.display
    || (serverStatus?.model ? serverStatus.model.split('\\').pop()?.split('/').pop()?.replace(/\.gguf$/i, '') : null)
    || (selectedModel || 'No model');

  const _copyFallback = (str: string) => {
    const ta = document.createElement('textarea');
    ta.value = str;
    ta.style.position = 'fixed';
    ta.style.left = '-9999px';
    document.body.appendChild(ta);
    ta.focus();
    ta.select();
    try { document.execCommand('copy'); } catch (_) {}
    document.body.removeChild(ta);
  };

  const handleCopyPrompt = (promptText: string, e: React.MouseEvent<HTMLButtonElement>) => {
    e.stopPropagation();
    const btn = e.currentTarget;
    if (btn.hasAttribute('data-copied')) return;
    const origHtml = btn.innerHTML;
    const markCopied = () => {
      btn.setAttribute('data-copied', '1');
      btn.innerHTML = '<span style="color:#10b981;font-weight:bold;font-size:11px;">✓</span>';
      setTimeout(() => {
        btn.innerHTML = origHtml;
        btn.removeAttribute('data-copied');
      }, 1200);
    };

    if (navigator.clipboard && window.isSecureContext) {
      navigator.clipboard.writeText(promptText).then(markCopied, () => {
        _copyFallback(promptText);
        markCopied();
      });
    } else {
      _copyFallback(promptText);
      markCopied();
    }
  };

  const handleEditPrompt = (promptText: string, e: React.MouseEvent<HTMLButtonElement>) => {
    e.stopPropagation();
    window.dispatchEvent(new CustomEvent('edit-user-prompt', { detail: { text: promptText } }));
  };

  return (
    <>
      <PromptDots />
      <div id="chat" ref={chatContainerRef} onClick={handleChatClick} onScroll={handleScroll}>
        <div id="chat-inner">
        {(!messages || messages.length === 0) ? (sessionLoading ? <div id="empty" aria-busy="true" /> : (
          <div id="empty">
            <div className="big">⚡</div>
            <h2>Local Agent</h2>
            <p id="empty-model" className="mono">
              {isModelLoaded ? (
                isCloudActive ? (
                  <>
                    {modelName}
                    {' · ☁️ cloud · '}
                    {serverStatus?.cloud_main?.provider || 'API'}
                  </>
                ) : (
                  <>
                    {modelName}
                    {serverStatus?.vision_capable ? ' · 👁️ vision' : ''}
                    {' · split '}{serverStatus?.tensor_split || 'auto'}
                    {serverStatus?.measured_tg_tokens_per_sec
                      ? ` · bench ${Number(serverStatus.measured_tg_tokens_per_sec).toFixed(1)} t/s`
                      : ''}
                  </>
                )
              ) : isModelLoading ? (
                'Loading model into GPU VRAM…'
              ) : (
                'Model unloaded'
              )}
            </p>
            <div className="empty-card">
              {isModelLoaded ? (
                isCloudActive ? (
                  <>
                    <p><b>{modelName} is ready!</b></p>
                    <p className="dim" style={{ marginTop: '6px' }}>
                      Connected to {serverStatus?.cloud_main?.provider || 'Cloud API'} (zero GPU VRAM usage). Type your message below and press Enter to chat.
                    </p>
                  </>
                ) : (
                  <>
                    <p><b>{modelName} is ready!</b></p>
                    <p className="dim" style={{ marginTop: '6px' }}>
                      GPU(s) loaded{serverStatus?.vision_capable ? ' with vision support' : ''}. Type your message below and press Enter to chat.
                    </p>
                  </>
                )
              ) : (
                <>
                  <p><b>No model is currently loaded in GPU VRAM.</b></p>
                  <p className="dim" style={{ marginTop: '6px' }}>
                    Click{' '}
                    <button
                      onClick={loadSelectedModel}
                      style={{
                        fontWeight: 700,
                        color: 'var(--blue, #38bdf8)',
                        background: 'transparent',
                        border: 'none',
                        cursor: 'pointer',
                        padding: 0,
                      }}
                    >
                      ▶ Load
                    </button>{' '}
                    in the bottom bar, or simply type a message below.
                  </p>
                </>
              )}
            </div>
          </div>
        )) : (
          messages.map((m) => {
            if (m.role === 'system' && m.meta?.compact) {
              const kb = (n: number) => (n >= 1000 ? (n / 1000).toFixed(1) + 'k' : String(n));
              const b = Number(m.meta.before_tokens || 0);
              const a = Number(m.meta.after_tokens || 0);
              return (
                <details key={String(m.id)} id={`msg-${m.id}`} className="msg bot" style={{ margin: '14px 0', color: 'var(--dim)', fontSize: 12 }}>
                  <summary style={{ cursor: 'pointer', textAlign: 'center' }}>
                    Earlier conversation compacted{b > 0 ? `: ${kb(b)} to ${kb(a)} tokens` : ''}. Click to read the summary.
                  </summary>
                  <div style={{ whiteSpace: 'pre-wrap', marginTop: 8, padding: '10px 12px', border: '1px solid var(--border)', borderRadius: 8 }}>
                    {String(m.content || '').replace(/^\[COMPACTED CONTEXT SUMMARY\]\s*/, '')}
                  </div>
                </details>
              );
            }
            const isUser = m.role === 'user';
            const contentText = typeof m?.content === 'string'
              ? m.content
              : typeof m?.content === 'object' && m?.content !== null
                ? JSON.stringify(m.content)
                : String(m?.content || '');

            const thinkingText = typeof m?.thinking === 'string'
              ? m.thinking
              : typeof m?.thinking === 'object' && m?.thinking !== null
                ? JSON.stringify(m.thinking)
                : '';

            if (isUser) {
              const meta = m.meta || {};
              const { cleanText: parsedClean, images: attachedImages, files: attachedFiles } = parseUserMessage(contentText);

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
              const hasActualUrls = uniqueImages.some(u => u.startsWith('data:') || u.startsWith('http') || u.startsWith('/'));
              const allImages = hasActualUrls
                ? uniqueImages.filter(u => u.startsWith('data:') || u.startsWith('http') || u.startsWith('/'))
                : uniqueImages;

              const rawFiles = [
                ...(m.files ? m.files.split(',').map((s: string) => s.trim()) : []),
                ...(meta.files && typeof meta.files === 'string' ? meta.files.split(',').map((s: string) => s.trim()) : []),
                ...attachedFiles,
              ];
              const isImageFileName = (name: string) => /\.(png|jpe?g|webp|gif|bmp|svg)$/i.test(name.trim());
              const nonImageFiles = Array.from(new Set(rawFiles.filter(Boolean))).filter((fname) => {
                if (allImages.length > 0 && isImageFileName(fname)) {
                  return false;
                }
                return true;
              });

              const isLong = !!overflowPrompts[String(m.id)];
              const isExpanded = !!expandedPrompts[String(m.id)];
              const promptTime = m.timestamp || (meta && meta.timeStr) || ((m as any).created_at ? new Date((m as any).created_at).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' }) : '');

              const createdMs: number = (m as any).createdAt || 0;
              const absTime = createdMs ? new Date(createdMs).toLocaleString() : '';
              return (
                <div key={String(m.id)} id={`msg-${m.id}`} className="msg user">
                  <div className="user-msg-col">
                  <div className="bubble">
                    {allImages.length > 0 && (
                      <div className="user-imgs-gallery" style={{ display: 'flex', flexWrap: 'wrap', gap: '6px', marginBottom: '6px' }}>
                        {allImages.map((imgUrl, iIdx) => (/^(data:|https?:|\/)/.test(imgUrl) ? (
                          <img
                            key={`thumb-${iIdx}`}
                            src={imgUrl}
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
                              openPreviewModal(imgUrl, 'Attachment', null, 'modal', true);
                            }}
                          />
                        ) : (
                          <span key={`thumb-${iIdx}`} className="user-file-pill" title="The picture itself is not kept in the history">🖼️ {imgUrl}</span>
                        )))}
                      </div>
                    )}
                    <ClampedUserText
                      html={renderCached(`u|${m.id}|${(displayText || contentText).length}|${allImages.length}|${nonImageFiles.length}`, () => renderCompleteChatMessage(displayText || (allImages.length || nonImageFiles.length ? '' : contentText)))}
                      expanded={isExpanded}
                      onOverflow={(over) => setOverflowPrompts((prev) => (prev[String(m.id)] === over || (!over && prev[String(m.id)] === undefined) ? prev : { ...prev, [String(m.id)]: over }))}
                    />
                    {nonImageFiles.length > 0 && (
                      <div className="user-files-tag" style={{ display: 'flex', flexWrap: 'wrap', gap: '6px', marginTop: '6px' }}>
                        {nonImageFiles.map((fName, fIdx) => (
                          <button
                            key={`file-${fIdx}`}
                            type="button"
                            className="user-file-pill"
                            title={`Preview ${fName} in modal`}
                            onClick={(e) => {
                              e.stopPropagation();
                              openPreviewModal(fName, fName, null, 'modal', true);
                            }}
                          >
                            <span>📎</span>
                            <span style={{ textDecoration: 'underline', textUnderlineOffset: '2px' }}>{fName}</span>
                          </button>
                        ))}
                      </div>
                    )}
                  </div>
                  {/* Hover-only footer: [see more/less] · [time] · [copy, edit] */}
                  <div className="user-bubble-bottom-bar">
                    <div className="user-bar-left">
                      {isLong && (
                        <button
                          type="button"
                          className="user-long-btn user-long-more"
                          onClick={(e) => {
                            e.stopPropagation();
                            setExpandedPrompts((prev: Record<string, boolean>) => ({ ...prev, [String(m.id)]: !prev[String(m.id)] }));
                          }}
                          title={isExpanded ? 'Collapse prompt' : 'Expand full prompt'}
                        >
                          {isExpanded ? 'See less' : 'See more'}
                        </button>
                      )}
                    </div>
                    <div className="user-bar-center">
                      {(createdMs || promptTime) && <span className="user-msg-time" title={absTime}>{createdMs ? relTime(createdMs, nowTick) : promptTime}</span>}
                    </div>
                    <div className="user-bar-right">
                      <button
                        type="button"
                        className="user-long-btn user-action-btn user-copy-btn"
                        title="Copy prompt"
                        onClick={(e) => handleCopyPrompt(displayText || contentText, e)}
                      >
                        <svg
                          width="13"
                          height="13"
                          viewBox="0 0 24 24"
                          fill="none"
                          stroke="currentColor"
                          strokeWidth="2"
                          strokeLinecap="round"
                          strokeLinejoin="round"
                          style={{ display: 'inline-block', verticalAlign: 'middle', pointerEvents: 'none' }}
                        >
                          <rect x="9" y="9" width="13" height="13" rx="2" ry="2" />
                          <path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1" />
                        </svg>
                      </button>
                      <button
                        type="button"
                        className="user-long-btn user-action-btn user-edit-btn"
                        title="Edit prompt"
                        onClick={(e) => handleEditPrompt(displayText || contentText, e)}
                      >
                        <svg
                          width="13"
                          height="13"
                          viewBox="0 0 24 24"
                          fill="none"
                          stroke="currentColor"
                          strokeWidth="2"
                          strokeLinecap="round"
                          strokeLinejoin="round"
                          style={{ display: 'inline-block', verticalAlign: 'middle', pointerEvents: 'none' }}
                        >
                          <path d="M11 4H4a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h14a2 2 0 0 0 2-2v-7" />
                          <path d="M18.5 2.5a2.121 2.121 0 0 1 3 3L12 15l-4 1 1-4 9.5-9.5z" />
                        </svg>
                      </button>
                    </div>
                  </div>
                  </div>
                </div>
              );
            }

            // an answer that merely talks about errors is a normal answer: only the message start decides
            const isErrorNotice = Boolean(
              m.errorAlert ||
              (contentText && (contentText.startsWith('Chat error: upstream') || contentText.startsWith('⚠️')))
            );

            const isLastMsg = messages[messages.length - 1] === m;
            const answering = isStreaming && isLastMsg;
            const qz = parseQuestionnaire(contentText);
            const held = Boolean(m.check && m.check.mode === 'gate' && m.check.state !== 'done' && !m.check.skipped);
            const questionCards = !answering && !isErrorNotice && !held
              ? qz ? (qz.quiz ? { list: qz.quiz.questions, title: qz.quiz.title, submit: qz.quiz.submit } : null)
                   : (() => { const l = parseProseQuestions(contentText); return l.length ? { list: l, title: '', submit: '' } : null; })()
              : null;
            return (
              <div key={String(m.id)} id={`msg-${m.id}`} className="msg bot">
                <div className="bubble" style={{ background: 'transparent', border: 'none', boxShadow: 'none', padding: '4px 0 12px 0' }}>
                  {isErrorNotice ? (
                    <div>
                      <div
                        className="chat-alert-box error"
                        style={{
                          display: 'flex',
                          alignItems: 'center',
                          gap: '8px',
                          padding: '10px 14px',
                          borderRadius: '8px',
                          background: 'rgba(245, 158, 11, 0.08)',
                          border: '1px solid rgba(245, 158, 11, 0.25)',
                          color: '#fbbf24',
                          fontSize: '12.5px',
                          fontWeight: 600,
                          marginBottom: '8px',
                        }}
                      >
                        <span style={{ fontSize: '14px' }}>⚠️</span>
                        <span>{contentText.replace(/^[⚠️?,?\s]+/, '')}</span>
                      </div>
                      {Boolean(m.model || m.ntok || m.tps) && (
                        <div className="meta" style={{ display: 'inline-flex', alignItems: 'center', gap: '6px', fontSize: '11px', color: 'var(--dim)' }}>
                          <span>{m.model}</span>
                          {m.ntok ? <span>· {m.ntok} tok</span> : null}
                          {m.tps ? <span>· {m.tps.toFixed(1)} t/s</span> : null}
                          {m.secs ? <span>· {m.secs.toFixed(1)}s</span> : null}
                        </div>
                      )}
                    </div>
                  ) : (
                    <div>
                      {/* Collapsible think block if raw reasoning exists and no acts (Hidden in Chat Mode) */}
                      {thinkingText && (!m.acts || !m.acts.length) && !isChatMode && (
                        <details className="think antigravity-step-item" open style={{ margin: '4px 0 8px 0' }}>
                          <summary
                            className="antigravity-step-row"
                            style={{
                              cursor: 'pointer',
                              display: 'flex',
                              alignItems: 'center',
                              justifyContent: 'space-between',
                              userSelect: 'none',
                              fontWeight: 600,
                              fontSize: '12px',
                              color: 'var(--dim)',
                            }}
                          >
                            <div className="antigravity-step-main">
                              <span>💭 Thinking Process</span>
                            </div>
                            <span className="antigravity-row-chev" style={{ fontSize: '10px' }}>▾</span>
                          </summary>
                          <div className="think-content antigravity-step-drawer" style={{ marginTop: '6px' }}>
                            <div
                              className="antigravity-thought-box"
                              style={{
                                whiteSpace: 'pre-wrap',
                                fontStyle: 'italic',
                                fontSize: '12.5px',
                                color: 'var(--dim)',
                                lineHeight: 1.5,
                                padding: '4px 0',
                              }}
                            >
                              {thinkingText}
                            </div>
                          </div>
                        </details>
                      )}

                      {/* Complete Antigravity Agent Actions + Markdown Prose + Media Preview Grid */}
                      {m.meta?.media ? (
                        <MediaCard m={m} />
                      ) : (m.check && m.check.mode === 'gate' && m.check.state !== 'done' && !m.check.skipped) ? (
                        <AnswerGate m={m} />
                      ) : (
                      <BotContent
                        id={String(m.id)}
                        follow={answering}
                        html={renderCached(answering ? null : `${m.id}|${contentText.length}|${thinkingText.length}|${(m.acts || []).length}|${m.secs || 0}|${isChatMode}|${qz ? 'q' : ''}`, () => renderCompleteChatMessage(qz ? qz.prose : contentText, m.acts, m.secs, isChatMode, thinkingText))}
                      />
                      )}
                      <AnswerBadge m={m} />
                      {questionCards && (
                        <QuestionCard
                          questions={questionCards.list}
                          title={questionCards.title}
                          submitLabel={questionCards.submit}
                          closed={!isLastMsg}
                          onSubmit={(text) => {
                            const st = useAppStore.getState();
                            if (st.isStreaming || (st.appMode === 'code' && !st.activeProjectId)) return false;
                            void sendPrompt(text, { displayContent: text });
                            return true;
                          }}
                        />
                      )}

                      {!held && <BotActions m={m} isLast={messages[messages.length - 1] === m} streaming={isStreaming} />}

                      {/* what the run is doing right now ("Crunching tool results…"): at the bottom, just above the model line */}
                      {m.statusText && (
                        <div className="dim" role="status" style={{ fontSize: '12px', fontStyle: 'italic', marginTop: '10px' }}>
                          {m.statusText}
                        </div>
                      )}

                      {Boolean(m.model || m.ntok || m.tps) && (
                        <div
                          className="meta"
                          style={{
                            display: 'inline-flex',
                            alignItems: 'center',
                            gap: '6px',
                            fontSize: '11px',
                            color: 'var(--dim)',
                            marginTop: '12px',
                            padding: '3px 8px',
                            background: 'rgba(255, 255, 255, 0.03)',
                            borderRadius: '4px',
                          }}
                        >
                          <span>{m.model}</span>
                          {m.ntok ? <span>· {m.ntok} tok</span> : null}
                          {m.tps ? <span>· {m.tps.toFixed(1)} t/s</span> : null}
                          {m.secs ? <span>· {m.secs.toFixed(1)}s</span> : null}
                        </div>
                      )}
                    </div>
                  )}
                </div>
              </div>
            );
          })
        )}
        <div ref={chatEndRef} />
      </div>
    </div>
    {showJump && (
      <button
        id="btn-scroll-bottom"
        className="chat-scroll-btn"
        title="Jump to bottom"
        onClick={() => {
          isUserScrolledUp.current = false;
          setShowJump(false);
          chatContainerRef.current?.scrollTo({ top: chatContainerRef.current.scrollHeight, behavior: 'smooth' });
        }}
      >
        ⬇ Jump to bottom
      </button>
    )}
    </>
  );
};
