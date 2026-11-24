import { hlCode, hlLangFor } from './highlight';
import { parseVisual, renderVisualHtml, parseProducts, renderProductsHtml, parseImages, renderImagesHtml } from './visual';
import { marked, type Tokens } from 'marked';
import DOMPurify from 'dompurify';
import { renderAgentTraceHtml } from './agentTrace';

export interface MediaItem {
  type: 'image' | 'video' | 'youtube';
  url: string;
  title: string;
  thumb?: string;
}

export function extractMediaItems(text: string): MediaItem[] {
  if (!text) return [];
  const items: MediaItem[] = [];
  const seen = new Set<string>();

  function addItem(type: 'image' | 'video' | 'youtube', url: string, title?: string, thumb?: string) {
    if (!url || seen.has(url)) return;
    seen.add(url);
    items.push({ type, url, title: title || (type === 'video' ? 'Video' : 'Image'), thumb: thumb || url });
  }

  // 1. Markdown images: ![alt](url)
  const mdImgRx = /!\[([^\]]*)\]\((https?:\/\/[^)\s]+)\)/g;
  let m: RegExpExecArray | null;
  while ((m = mdImgRx.exec(text)) !== null) {
    addItem('image', m[2], m[1] || 'Image');
  }

  // 2. YouTube URLs
  const ytRx = /(?:https?:\/\/)?(?:www\.)?(?:youtube\.com\/(?:watch\?v=|embed\/|v\/)|youtu\.be\/)([a-zA-Z0-9_-]{11})/g;
  while ((m = ytRx.exec(text)) !== null) {
    const vid = m[1];
    addItem('youtube', `https://www.youtube.com/watch?v=${vid}`, 'YouTube Video', `https://img.youtube.com/vi/${vid}/hqdefault.jpg`);
  }

  // 3. Links to media
  const linkRx = /\[([^\]]+)\]\((https?:\/\/[^)\s]+\.(?:png|jpg|jpeg|webp|gif|svg|mp4|webm))(?:\?[^)]*)?\)/gi;
  while ((m = linkRx.exec(text)) !== null) {
    const isVid = /\.(mp4|webm)$/i.test(m[2]);
    addItem(isVid ? 'video' : 'image', m[2], m[1]);
  }

  return items;
}

export function renderMediaPreviewSection(text: string): string {
  const items = extractMediaItems(text);
  if (!items.length) return '';

  let html = `<div class="chat-media-preview-section" style="margin-top:16px;">
    <div class="media-preview-header" style="display:flex; align-items:center; justify-content:space-between; margin-bottom:10px;">
      <span class="media-preview-title" style="font-size:12.5px; font-weight:600; color:var(--text, #f3f4f6); display:flex; align-items:center; gap:6px;">
        🖼️ Media Preview (${items.length})
      </span>
      <span class="media-preview-sub" style="font-size:11px; color:var(--dim, #8b949e);">Click thumbnail to view full resolution</span>
    </div>
    <div class="media-preview-grid" style="display:grid; grid-template-columns:repeat(auto-fill, minmax(210px, 1fr)); gap:12px;">`;

  items.forEach((it) => {
    const cleanTitle = (it.title || 'Media').replace(/"/g, '&quot;');
    const safeUrl = it.url.replace(/"/g, '&quot;');
    let host = 'external site';
    try {
      host = new URL(it.url).hostname.replace(/^www\./, '');
    } catch (_) {}

    html += `<div class="media-card media-card-img" style="background:var(--panel2, rgba(255,255,255,0.03)); border:1px solid var(--border, #334155); border-radius:8px; overflow:hidden; display:flex; flex-direction:column;">
      <div class="media-card-thumb-wrap" style="position:relative; width:100%; height:120px; background:rgba(0,0,0,0.3); display:flex; align-items:center; justify-content:center; overflow:hidden;">
        <button type="button" class="file-action-badge" data-load-src="${safeUrl}" data-load-alt="${cleanTitle}" title="${safeUrl}" style="padding:6px 10px; font-size:11px; background:rgba(255,255,255,0.06); border:1px solid var(--border); border-radius:6px; color:var(--text); cursor:pointer;">
          🖼️ Load image from ${host}
        </button>
        <span class="media-badge" style="position:absolute; top:6px; right:6px; background:rgba(0,0,0,0.75); color:#cbd5e1; font-size:9px; font-weight:700; padding:2px 5px; border-radius:4px; letter-spacing:0.04em; pointer-events:none;">IMAGE</span>
      </div>
      <div class="media-card-footer" style="padding:8px 10px; display:flex; align-items:center; justify-content:space-between; border-top:1px solid var(--border-subtle, rgba(255,255,255,0.05)); font-size:11px;">
        <span class="media-card-name" style="overflow:hidden; text-overflow:ellipsis; white-space:nowrap; max-width:160px; font-weight:500; color:var(--text);" title="${cleanTitle}">${cleanTitle}</span>
        <button type="button" data-preview-path="${safeUrl}" data-preview-title="${cleanTitle}" title="View full resolution" style="background:none; border:none; cursor:pointer; font-size:12px; opacity:0.8; padding:0;">👁️</button>
      </div>
    </div>`;
  });

  html += `</div></div>`;
  return html;
}

export function buildAntigravityWorkedFor(acts: any[], secs?: number): string {
  let totalSec = secs ? Math.max(1, Math.round(secs)) : 0;
  if (!totalSec && acts && acts.length) {
    acts.forEach((a) => {
      if (a.duration_s) totalSec += a.duration_s;
    });
  }
  if (!totalSec) return 'Worked for 1s';

  if (totalSec < 60) return `Worked for ${totalSec}s`;
  const mins = Math.floor(totalSec / 60);
  const rem = totalSec % 60;
  return rem > 5 ? `Worked for ${mins}m ${rem}s` : `Worked for ${mins}m`;
}

/** Whole text (formatting kept) made safe for inside <pre><code>. */
function escText(v: unknown): string {
  return String(v ?? '').replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
}

// Hosts models write as placeholders in code samples and API examples: nobody owns a favicon for them
const PLACEHOLDER_HOST = /(^|\.)(example\.(com|org|net)|myapp\.com|mysite\.com|yourapp\.com|yourdomain\.com|yoursite\.com|domain\.com|company\.com|localhost|test|invalid|local|internal|example)$/i;

/** True when a site icon is worth requesting for this host: a real-looking dotted name, not a bare word ("stag"), an IP, or a
 *  placeholder. Every request for one that does not exist is a 404 in the console. */
export function faviconWorthFetching(host: string): boolean {
  return /^[a-z0-9-]+(\.[a-z0-9-]+)*\.[a-z]{2,}$/i.test(host) && !PLACEHOLDER_HOST.test(host);
}

// Marked Configuration
marked.use({
  gfm: true,
  breaks: true,
  renderer: {
    // Web links always open in a new tab. A link whose text is a citation number ([1]), a bare address or a domain becomes
    // a source pill (site icon + domain), so a "Sources:" line reads as a row of chips.
    link({ href, title, tokens, text }: Tokens.Link): string {
      const url = String(href || '');
      const inner = this.parser.parseInline(tokens);
      const t = title ? ` title="${escAttr(title)}"` : '';
      if (url.startsWith('/') && !url.startsWith('//')) return `<a href="${escAttr(url)}"${t}>${inner}</a>`;   // the app's own files and pages
      if (!/^https?:\/\//i.test(url)) return url ? `${inner} (${escAttr(url)})` : inner;                     // mailto:, javascript: ... stay inert text
      let domain = '';
      try { domain = new URL(url).hostname.replace(/^www\./i, ''); } catch (_) { /* no domain: a plain link */ }
      const label = String(text || '').trim();
      const isSource = /^(\d+|\[\d+\]|ref\.?\s*\d+|\^?\d+\^?)$/i.test(label) || /^https?:\/\//i.test(label) || /^[\w-]+(\.[\w-]+)+(\/\S*)?$/.test(label);
      if (domain && isSource) {
        const d = escAttr(domain);
        const fav = escAttr(`https://www.google.com/s2/favicons?domain=${encodeURIComponent(domain)}&sz=32`);
        const icon = faviconWorthFetching(domain)
          ? `<img src="${fav}" class="citation-favicon" alt="" loading="lazy" referrerpolicy="no-referrer" data-hide-on-error />`
          : '';
        return `<a href="${escAttr(url)}" target="_blank" rel="noopener noreferrer" class="citation-pill" title="${d} - Click to open source">`
          + `${icon}<span class="citation-host">${d}</span></a>`;
      }
      return `<a href="${escAttr(url)}"${t} target="_blank" rel="noopener noreferrer">${inner}</a>`;
    },
    code({ text, lang }: Tokens.Code): string {
      const rawLang = (lang || '').trim();
      if (rawLang === 'visual') {
        // a valid ```visual block is drawn by our own code; an invalid one stays a normal code block
        const spec = parseVisual(text);
        if (spec) return renderVisualHtml(spec);
      }
      if (rawLang === 'products') {
        const spec = parseProducts(text);
        if (spec) return renderProductsHtml(spec);
      }
      if (rawLang === 'images') {
        const spec = parseImages(text);
        if (spec) return renderImagesHtml(spec);
      }
      if (rawLang.toLowerCase() === 'mermaid' && text.trim()) {
        // drawn after render by utils/mermaid.ts; the source stays visible until then and if it is not valid Mermaid.
        // The Copy button (code-copy) copies the source, like any code block.
        return `<div class="code-block mermaid-box"><div class="code-block-head"><span class="code-block-lang">Diagram</span>`
          + `<span class="code-block-actions"><button type="button" class="code-block-btn" data-click="mermaid-enlarge" title="Open the diagram in a large view with zoom">⤢ Enlarge</button><button type="button" class="code-block-btn" data-click="code-copy" title="Copy the Mermaid source">📋 Copy</button></span></div>`
          + `<div class="mermaid-viewport" title="Click to enlarge"></div><pre class="mermaid-code-raw"><code>${escText(text)}</code></pre></div>`;
      }
      const label = rawLang || 'code';
      const escaped = hlCode(text, hlLangFor(rawLang));   // escapes, then wraps tokens
      return `<div class="code-block" style="margin:10px 0; border:1px solid var(--border, #334155); border-radius:8px; overflow:hidden; background:var(--bg-code, #0f172a);"><div class="code-block-head" style="display:flex; align-items:center; justify-content:space-between; padding:6px 12px; background:rgba(255,255,255,0.03); border-bottom:1px solid var(--border, #334155);"><span class="code-block-lang" style="font-size:11px; font-weight:600; color:var(--dim, #8b949e); text-transform:uppercase;">${label}</span><span class="code-block-actions"><button type="button" class="code-block-btn" data-click="code-copy" title="Copy code" style="padding:2px 8px; font-size:11px; background:transparent; border:none; color:var(--dim); cursor:pointer;">📋 Copy</button></span></div><pre style="margin:0; padding:12px; overflow-x:auto;"><code style="font-family:monospace; font-size:12.5px; line-height:1.5;">${escaped}</code></pre></div>`;
    },
    codespan({ text }: Tokens.Codespan): string {
      const escaped = hlCode(text, 'cmd');
      return `<code class="inline-cmd">${escaped}</code>`;
    },
  },
});

/** Escape a value for use inside a double-quoted HTML attribute (or text). */
function escAttr(v: unknown): string {
  return String(v ?? '')
    .replace(/&/g, '&amp;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&#39;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;');
}

export function transformMarkdownSpecialTokens(raw: string): string {
  if (!raw) return '';
  // code spans and fences are left exactly as written: the transforms below must not rewrite sample code
  return raw
    .split(/(```[\s\S]*?```|`[^`\n]*`)/)
    .map((part, i) => (i % 2 === 1 ? part : transformTokens(part)))
    .join('');
}

function transformTokens(raw: string): string {
  let t = raw;

  // 1. External images: ![alt](url) -> a card whose picture loads only when you click it
  //    (an image URL can carry data out of the page, so nothing is requested until then)
  t = t.replace(/!\[([^\]]*)\]\((https?:\/\/[^)\s]+)\)/g, (_, alt, url) => {
    let host = 'external site';
    try {
      host = new URL(url).hostname.replace(/^www\./, '');
    } catch (_) {}
    const cleanTitle = escAttr(alt || 'Image');
    const safeUrl = escAttr(url);
    const safeHost = escAttr(host);
    const card = `<div class="chat-inline-media-card" style="display:inline-flex; align-items:center; gap:10px; margin:6px 0; padding:6px 10px; border-radius:8px; background:var(--panel2, rgba(255,255,255,0.03)); border:1px solid var(--border, #334155); max-width:440px; vertical-align:middle;">
      <div class="media-card-thumb-wrap" style="position:relative; width:44px; height:44px; min-width:44px; border-radius:6px; overflow:hidden; background:rgba(0,0,0,0.3); display:flex; align-items:center; justify-content:center; border:1px solid var(--border-subtle, rgba(255,255,255,0.08));">
        <button type="button" data-load-src="${safeUrl}" data-load-alt="${cleanTitle}" title="Load image from ${safeHost}" style="background:none; border:none; cursor:pointer; font-size:16px; color:inherit;">🖼️</button>
      </div>
      <div style="flex:1; min-width:0; display:flex; flex-direction:column; gap:2px;">
        <div style="font-size:12px; font-weight:600; color:var(--text, #f3f4f6); overflow:hidden; text-overflow:ellipsis; white-space:nowrap;" title="${cleanTitle}">${cleanTitle}</div>
        <div style="font-size:10.5px; color:var(--dim, #8b949e); display:flex; align-items:center; gap:8px;">
          <span style="overflow:hidden; text-overflow:ellipsis; white-space:nowrap; max-width:200px;">🌐 ${safeHost}</span>
          <button type="button" class="btn-img-preview" data-preview-path="${safeUrl}" data-preview-title="${cleanTitle}" style="background:none; border:none; color:var(--accent, #38bdf8); font-size:10.5px; cursor:pointer; padding:0; display:inline-flex; align-items:center; gap:3px;" title="View full image">👁️ View</button>
        </div>
      </div>
    </div>`;
    // One line of inline <span>s: a line that starts with a block <div> is an HTML block for the markdown parser, which then
    // leaves the rest of the line ("- **Creator:** ...") as raw text with its asterisks.
    return card.replace(/>\s*\n\s*</g, '><').replace(/<div/g, '<span').replace(/<\/div>/g, '</span>');
  });

  // 2. Download / Preview tokens: [DOWNLOAD: filename]
  // a tag the model cut off (no closing bracket) is dropped; it must not swallow the real one after it
  t = t.replace(/[ \t]*\[DOWNLOAD:[^\]\n[]*(?=\n|$|\[)/g, '');
  t = t.replace(/\[DOWNLOAD:\s*([^\]\n[]+)\]/g, (_, fname) => {
    const cleanName = escAttr(fname.trim());
    const rawName = fname.trim();
    const isZip = cleanName.toLowerCase().endsWith('.zip');
    const downloadUrl = `/agent/download?path=${encodeURIComponent(rawName)}`;

    if (isZip) {
      const projTitle = escAttr(rawName)
        .replace(/-[a-f0-9]{8}\.zip$/i, '')
        .replace(/_/g, ' ')
        .replace(/-/g, ' ')
        .split(' ')
        .map((w: string) => w.charAt(0).toUpperCase() + w.slice(1))
        .join(' ');

      return `<div class="project-zip-wrapper" style="margin:14px 0 16px 0;">
        <div class="project-zip-card" style="display:flex; align-items:flex-start; gap:14px; padding:16px; border-radius:14px; background:rgba(16, 185, 129, 0.08); border:1px solid rgba(16, 185, 129, 0.25); max-width:540px;">
          <div style="width:48px; height:48px; border-radius:12px; background:rgba(16, 185, 129, 0.22); display:flex; align-items:center; justify-content:center; flex-shrink:0;">
            <svg width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="#10b981" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round">
              <path d="M21 8a2 2 0 0 0-1-1.73l-7-4a2 2 0 0 0-2 0l-7 4A2 2 0 0 0 3 8v8a2 2 0 0 0 1 1.73l7 4a2 2 0 0 0 2 0l7-4A2 2 0 0 0 21 16Z"/>
              <path d="m3.3 7 8.7 5 8.7-5"/>
              <path d="M12 22V12"/>
              <path d="m9 12 2 2 4-4"/>
            </svg>
          </div>
          <div style="flex:1; min-width:0;">
            <div style="font-weight:700; font-size:15px; color:#ffffff; letter-spacing:-0.2px;">${projTitle || 'Complete Project Package'}</div>
            <div style="font-size:12px; color:rgba(255,255,255,0.7); margin-top:3px;">HTML5 · CSS3 · Vanilla JavaScript · Responsive design</div>
            <div style="font-size:11.5px; color:rgba(255,255,255,0.5); margin-top:3px;">Project files · Ready to extract and run</div>
          </div>
        </div>
        <div style="margin:8px 0 6px 2px;">
          <a href="${downloadUrl}" download="${cleanName}" class="zip-download-link" style="color:#ffffff; font-size:13.5px; font-weight:600; text-decoration:underline; text-decoration-style:dotted; text-underline-offset:4px;" title="Download complete project ZIP">
            Download the complete project ZIP ↗
          </a>
        </div>
      </div>`;
    }

    // a file card: type badge, readable name (the unique id the writer adds is hidden; the tooltip has the real name) and two
    // buttons. Layout and the phone breakpoint live in style.css (.file-card)
    const ext = (cleanName.split('.').pop() || 'file').slice(0, 5).toUpperCase();
    const shownName = cleanName.replace(/-[0-9a-f]{8}(\.[A-Za-z0-9]+)$/i, '$1');
    const eyeIcon = `<svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M2 12s3.6-7 10-7 10 7 10 7-3.6 7-10 7-10-7-10-7Z"/><circle cx="12" cy="12" r="3"/></svg>`;
    const downIcon = `<svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M12 3v12"/><path d="m7 11 5 5 5-5"/><path d="M5 21h14"/></svg>`;
    return `<div class="file-card">
      <div class="file-card-main">
        <span class="file-card-ext">${ext}</span>
        <span class="file-card-name" title="${cleanName}">${shownName}</span>
      </div>
      <div class="file-card-actions">
        <button type="button" class="fc-btn primary" data-preview-path="${cleanName}" data-preview-title="${cleanName}">${eyeIcon}<span>Preview</span></button>
        <a class="fc-btn" href="${downloadUrl}" download="${cleanName}">${downIcon}<span>Download</span></a>
      </div>
    </div>`;
  });

  // 3. Prevent raw un-fenced full HTML documents from dumping directly into chat DOM
  if (/<!DOCTYPE\s+html|<html[\s>]/i.test(t) && !t.includes('```html') && !t.includes('```xml')) {
    t = t.replace(/(?:<!DOCTYPE\s+html[\s\S]*?<\/html>|<html[\s\S]*?<\/html>)/gi, (match) => {
      return `\n\`\`\`html\n${match}\n\`\`\`\n`;
    });
  }

  return t;
}

// A favicon that fails to load (offline, blocked) disappears instead of leaving a broken-image icon. The error event does
// not bubble, so listen in the capture phase; sanitized markup cannot carry an onerror handler.
if (typeof document !== 'undefined') {
  document.addEventListener('error', (e) => {
    const el = e.target as HTMLElement | null;
    if (el && el.tagName === 'IMG' && el.hasAttribute('data-hide-on-error')) {
      el.style.display = 'none';
    } else if (el && el.tagName === 'IMG' && el.hasAttribute('data-hide-tile-on-error')) {
      // a gallery picture that cannot load: the tile stays with a "Preview unavailable" box and its source link, so a
      // failed search never leaves the answer looking empty (the tile used to vanish without a trace)
      const tile = el.closest('figure');
      el.style.display = 'none';
      if (tile) tile.classList.add('img-gal-broken');
    }
  }, true);
}

if (typeof window !== 'undefined') {
  try {
    DOMPurify.removeHook('afterSanitizeAttributes');
    DOMPurify.addHook('afterSanitizeAttributes', (node) => {
      if (node.tagName === 'A' && node.getAttribute('target')) node.setAttribute('rel', 'noopener noreferrer');
    });
    DOMPurify.removeHook('uponSanitizeAttribute');
    DOMPurify.addHook('uponSanitizeAttribute', (_node, data) => {
      // Disarm fixed/absolute positioning leakage from chat messages
      if (data.attrName === 'style' && data.attrValue) {
        data.attrValue = data.attrValue
          .replace(/\/\*[\s\S]*?\*\//g, '')   // comments and CSS escapes could hide a keyword from the checks below
          .replace(/\\/g, '')
          .replace(/position\s*:\s*(?:fixed|sticky)/gi, 'position: relative')
          .replace(/z-index\s*:\s*-?\d{2,}/gi, 'z-index: 10');
      }
      // Disarm Tailwind fixed/z-index escape classes
      if (data.attrName === 'class' && data.attrValue) {
        data.attrValue = data.attrValue
          .replace(/\bfixed\b/g, 'relative')
          .replace(/\btop-0\b/g, '')
          .replace(/\bz-(?:[345]\d|50|[6-9]\d|\d{3,})\b/g, 'z-0')
          .replace(/\bz-\[[^\]]+\]/g, 'z-0');
      }
    });
  } catch (_) {}
}

function sanitizeHtml(html: string): string {
  if (typeof window !== 'undefined') {
    const purifyConfig = {
      FORBID_TAGS: ['style', 'title', 'meta', 'link', 'script', 'iframe', 'frame', 'applet', 'object', 'embed', 'base', 'form', 'input', 'textarea', 'select', 'option'],
      FORBID_ATTR: ['formaction', 'action', 'onerror', 'onload', 'onclick', 'onmouseover'],
      ADD_ATTR: [
        'data-click',
        'data-arg',
        'data-load-src',
        'data-load-alt',
        'data-preview-path',
        'data-preview-title',
        'target',
        'class',
        'style',
        'download',
        'open',
        'src',
        'alt',
        'loading',
        'rel',
        'referrerpolicy',
        'data-hide-on-error',
        'data-hide-tile-on-error',
      ],
    };
    if (typeof DOMPurify.sanitize === 'function') {
      return DOMPurify.sanitize(html, purifyConfig);
    }
    try {
      const purifyInstance = (DOMPurify as any)(window);
      if (purifyInstance && typeof purifyInstance.sanitize === 'function') {
        return purifyInstance.sanitize(html, purifyConfig);
      }
    } catch (_) {}
  }
  return html;
}

export function renderCompleteChatMessage(content: string, acts?: any[], secs?: number, isChatMode?: boolean, thinking?: string, live = false): string {
  if (!content && (!acts || !acts.length) && !thinking) return '';

  let actsHtml = '';
  // Chat mode shows the answer only by default. With Conversation Steps = Verbose,
  // chat also shows the work (tool calls / results), like Agent and Code modes.
  const verboseSteps = typeof document !== 'undefined'
    && document.documentElement.getAttribute('data-conversation-steps') === 'verbose';
  if (!isChatMode || verboseSteps) {
    // only these act types draw anything; lane / step / plan / stopped records must not produce an empty block
    let finalActs = acts ? acts.filter((a) => ['thought', 'reasoning', 'tool_call', 'tool_result', 'narration'].includes(a.type)) : [];
    if (thinking && thinking.trim() && !finalActs.some((a) => a.type === 'thought' || a.type === 'reasoning')) {
      finalActs = [{ type: 'thought', text: thinking.trim(), duration_s: secs || 1 }, ...finalActs];
    }
    if (finalActs.length) {
      const prose = (md: string) => sanitizeHtml(marked.parse(transformMarkdownSpecialTokens(md), { async: false }) as string);
      actsHtml = renderAgentTraceHtml(finalActs, prose, live,
        finalActs.some((a) => a.type === 'tool_call') ? buildAntigravityWorkedFor(finalActs, secs) : '',
        /* collapsed */ isChatMode && verboseSteps);
    }
  }

  let rawContent = content || '';
  if (isChatMode && (rawContent.includes('<think>') || rawContent.includes('</think>'))) {
    rawContent = rawContent.replace(/<think>[\s\S]*?<\/think>/gi, '').replace(/^[\s\S]*?<\/think>/i, '').trim();
  }

  // a "|" inside a link address would cut a table cell in two (cells are split before inline parsing)
  const transformed = transformMarkdownSpecialTokens(rawContent).replace(/\]\((https?:\/\/[^)\s]*\|[^)\s]*)\)/g, (_m, u: string) => '](' + u.replace(/\|/g, '%7C') + ')');
  // a wide table scrolls inside its own frame (.md-table-wrap): the page must not pan sideways and cut the text around it
  const parsedMarkdown = (marked.parse(transformed, { async: false }) as string)
    .replace(/<table>/g, '<div class="md-table-wrap"><table>').replace(/<\/table>/g, '</table></div>');
  const sanitized = sanitizeHtml(parsedMarkdown);

  return actsHtml + sanitized;
}
