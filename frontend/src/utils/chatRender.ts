import { hlCode, hlLangFor } from './highlight';
import { parseVisual, renderVisualHtml } from './visual';
import { marked, type Tokens } from 'marked';
import DOMPurify from 'dompurify';

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

export function buildAntigravitySubSummary(acts: any[]): string {
  if (!acts || !acts.length) return 'Actions';
  let searches = 0;
  let fileOps = 0;
  let cmdOps = 0;

  acts.forEach((a) => {
    const name = a.name || '';
    if (name.includes('search') || name.includes('grep') || name.includes('fetch')) searches++;
    else if (name.includes('file') || name.includes('project')) fileOps++;
    else if (name.includes('command') || name.includes('python')) cmdOps++;
  });

  if (searches > 0 && fileOps === 0) return 'Searched code';
  if (fileOps > 0 && searches === 0) return 'Explored files';
  if (searches > 0 && fileOps > 0) return 'Searched code and created files';
  return 'Searched code';
}

export function renderAgentActsHtml(acts: any[], secs?: number): string {
  if (!acts || !acts.length) return '';

  const workedText = buildAntigravityWorkedFor(acts, secs);
  const subSummaryText = buildAntigravitySubSummary(acts);

  // Group tool calls & results
  const stream: Array<{
    id: string;
    name: string;
    label: string;
    detail: string;
    badge?: string;
    args?: any;
    result?: any;
    failed?: boolean;
    isThought?: boolean;
    duration_s?: number;
  }> = [];

  const toolMap = new Map<string, any>();
  acts.forEach((a, idx) => {
    if (a.type === 'thought' || a.type === 'reasoning') {
      const dur = a.duration_s || a.secs || 1;
      const thText = a.text || a.content || a.thought || a.reasoning || '';
      stream.push({
        id: a.id || `thought-${idx}`,
        name: 'thought',
        label: `Thought for ${dur}s`,
        detail: thText,
        badge: '',
        duration_s: dur,
        args: {},
        result: null,
        failed: false,
        isThought: true,
      });
    } else if (a.type === 'tool_call') {
      const args = a.args || {};
      let label = a.name || 'tool';
      let detail = '';
      let badge = '';

      if (a.name === 'web_search') {
        badge = 'WEB';
        label = 'Search:';
        detail = args.query || '';
      } else if (a.name === 'web_search_images') {
        badge = 'IMAGES';
        label = 'Search images:';
        detail = args.query || '';
      } else if (a.name === 'web_fetch') {
        label = 'Fetched webpage';
        detail = `"${args.title || args.url || 'webpage'}"`;
      } else if (a.name === 'project_plan') {
        label = 'project plan';
        detail = `project: ${args.project || ''}`;
      } else if (a.name === 'project_write') {
        label = 'project write';
        detail = args.path || '';
      } else if (a.name === 'project_append') {
        label = 'project append';
        detail = args.path || '';
      } else if (a.name === 'project_finish') {
        label = 'project finish';
        detail = `project: ${args.project || ''}`;
      } else {
        label = (a.name || '').replace(/_/g, ' ');
        const k = Object.keys(args);
        detail = k.length ? `${args[k[0]]}` : '';
      }

      const item = {
        id: a.id,
        name: a.name,
        label,
        detail,
        badge,
        args,
        result: null as any,
        failed: false,
      };
      stream.push(item);
      toolMap.set(a.id, item);
    } else if (a.type === 'tool_result') {
      const match = toolMap.get(a.id);
      if (match) {
        match.result = a.result;
        if (a.ok === false || (typeof a.result === 'string' && a.result.includes('failed'))) {
          match.failed = true;
        }
      }
    }
  });

  if (!stream.length) return '';

  let h = `<div class="agy-agent-container" style="margin: 8px 0 14px 0;">`;
  h += `<details class="antigravity-trace-group" open data-card-id="trace-group">`;
  h += `<summary class="antigravity-trace-summary" style="cursor:pointer; display:inline-flex; align-items:center; gap:6px; font-size:12.5px; font-weight:600; color:var(--text, #f3f4f6); user-select:none;">`;
  h += `<span class="antigravity-trace-worked">${workedText}</span>`;
  h += `<span class="antigravity-trace-chev" style="font-size:10px; opacity:0.7;">▾</span>`;
  h += `</summary>`;
  h += `<div class="antigravity-trace-body" style="padding-top:4px; margin-left:4px;">`;
  h += `<div class="antigravity-sub-summary" data-click="toggle-all-antigravity" title="Click to collapse / expand all steps" style="cursor:pointer; display:inline-flex; align-items:center; gap:4px; font-size:12px; color:var(--dim, #8b949e); margin:4px 0 6px 0; user-select:none;">`;
  h += `<span>${subSummaryText}</span>`;
  h += `<span class="antigravity-sub-chev" style="font-size:9px;">▾</span>`;
  h += `</div>`;
  h += `<div class="antigravity-steps-list" style="display:flex; flex-direction:column; gap:2px; padding-left:8px; border-left:1px solid var(--border-subtle, rgba(255,255,255,0.08));">`;

  stream.forEach((it) => {
    if (it.isThought) {
      const dur = it.duration_s || 1;
      const thText = it.detail || '';
      h += `<details class="antigravity-step-item" style="margin:2px 0; border:none; background:transparent;">`;
      h += `<summary class="antigravity-step-row" style="cursor:pointer; display:flex; align-items:center; gap:8px; font-size:12px; color:var(--dim, #8b949e); padding:4px 6px; border-radius:6px; user-select:none; transition:background 0.15s ease;">`;
      h += `<span style="font-size:12px;">💭</span>`;
      h += `<span style="color:var(--text-muted, #94a3b8); font-weight:500;">Thought for ${dur}s</span>`;
      h += `<span class="antigravity-row-chev" style="margin-left:auto; font-size:13px; opacity:0.5;">›</span>`;
      h += `</summary>`;
      h += `<div class="antigravity-step-drawer" style="padding:8px 12px 10px; margin:4px 0 6px 14px; border-left:2px solid var(--accent, #38bdf8); background:rgba(255,255,255,0.02); border-radius:0 6px 6px 0;">`;
      h += `<div class="antigravity-thought-box" style="white-space:pre-wrap; font-style:italic; font-size:12px; color:var(--dim, #8b949e); line-height:1.5;">${(thText || 'Thinking through strategy…').replace(/</g, '&lt;')}</div>`;
      h += `</div></details>`;
      return;
    }

    const rawResult = typeof it.result === 'string' ? it.result : JSON.stringify(it.result || '');
    const previewPath = it.args?.path || it.args?.file || it.args?.filename || (it.name && it.name.includes('project_write') ? it.detail : '');

    h += `<details class="antigravity-step-item" style="margin:2px 0; border:none; background:transparent;">`;
    h += `<summary class="antigravity-step-row" style="cursor:pointer; display:flex; align-items:center; gap:8px; font-size:12px; color:var(--dim, #8b949e); padding:4px 6px; border-radius:6px; user-select:none; transition:background 0.15s ease;">`;
    if (it.badge) {
      h += `<span style="background:rgba(56,189,248,0.15); color:#38bdf8; font-size:9.5px; font-weight:700; padding:1px 5px; border-radius:3px;">${it.badge}</span>`;
    }
    h += `<span style="color:var(--dim);">${it.label}</span>`;
    if (it.detail) {
      h += `<span style="color:var(--text); font-weight:500; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; max-width:480px;">${it.detail}</span>`;
    }
    if (it.failed) {
      h += `<span style="margin-left:auto; background:rgba(239,68,68,0.15); color:#ef4444; font-size:9.5px; font-weight:600; padding:1px 6px; border-radius:4px;">Failed</span>`;
    }
    h += `<span class="antigravity-row-chev" style="margin-left:${it.failed ? '4px' : 'auto'}; font-size:13px; opacity:0.5;">›</span>`;
    h += `</summary>`;

    h += `<div class="antigravity-step-drawer" style="padding:8px 12px 10px; margin:4px 0 6px 14px; border-left:2px solid var(--accent, #38bdf8); background:rgba(255,255,255,0.02); border-radius:0 6px 6px 0;">`;
    const previewAttr = escAttr(previewPath);
    if (previewPath) {
      const isZip = previewPath.toLowerCase().endsWith('.zip');
      const actionBtn = isZip
        ? `<a href="/agent/download?path=${encodeURIComponent(previewPath)}" download="${previewAttr}" class="file-action-badge" style="padding:2px 8px; font-size:10.5px; background:rgba(255,255,255,0.06); border:1px solid var(--border); border-radius:4px; color:var(--text); text-decoration:none;">⬇ Download</a>`
        : `<button type="button" class="file-action-badge primary" data-preview-path="${previewAttr}" data-preview-title="${previewAttr}" data-preview-source="ws" style="padding:2px 8px; font-size:10.5px; background:rgba(56,189,248,0.15); border:1px solid rgba(56,189,248,0.3); border-radius:4px; color:#38bdf8; cursor:pointer;">👁️ Preview</button>`;

      h += `<div style="display:flex; align-items:center; justify-content:space-between; margin-bottom:8px;">
        <span style="font-family:monospace; font-size:11.5px; color:var(--text);">${previewAttr}</span>
        ${actionBtn}
      </div>`;
    }
    if (it.args && Object.keys(it.args).length > 0) {
      h += `<div style="font-size:10px; font-weight:700; text-transform:uppercase; color:var(--dim); margin-bottom:4px;">ARGUMENTS</div>`;
      h += `<pre style="margin:0 0 8px 0; padding:6px 8px; font-size:11px; font-family:monospace; background:var(--bg-code, #0f172a); border-radius:4px; border:1px solid var(--border-subtle); overflow-x:auto;"><code>${JSON.stringify(it.args, null, 2).replace(/</g, '&lt;')}</code></pre>`;
    }
    if (rawResult && rawResult !== '""' && rawResult !== 'null') {
      h += `<div style="font-size:10px; font-weight:700; text-transform:uppercase; color:var(--dim); margin-bottom:4px;">RESULT</div>`;
      h += `<pre style="margin:0; padding:6px 8px; font-size:11px; font-family:monospace; background:var(--bg-code, #0f172a); border-radius:4px; border:1px solid var(--border-subtle); overflow-x:auto; max-height:240px; color:${it.failed ? 'var(--red, #ef4444)' : 'inherit'};"><code>${rawResult.replace(/</g, '&lt;')}</code></pre>`;
    }
    h += `</div></details>`;
  });

  h += `</div></div></details>`;
  h += `<div class="antigravity-divider" style="margin:10px 0;"></div></div>`;
  return h;
}

// Marked Configuration
marked.use({
  gfm: true,
  breaks: true,
  renderer: {
    code({ text, lang }: Tokens.Code): string {
      const rawLang = (lang || '').trim();
      if (rawLang === 'visual') {
        // a valid ```visual block is drawn by our own code; an invalid one stays a normal code block
        const spec = parseVisual(text);
        if (spec) return renderVisualHtml(spec);
      }
      const label = rawLang || 'code';
      const escaped = hlCode(text, hlLangFor(rawLang));   // escapes, then wraps tokens
      return `<div class="code-block" style="margin:10px 0; border:1px solid var(--border, #334155); border-radius:8px; overflow:hidden; background:var(--bg-code, #0f172a);"><div class="code-block-head" style="display:flex; align-items:center; justify-content:space-between; padding:6px 12px; background:rgba(255,255,255,0.03); border-bottom:1px solid var(--border, #334155);"><span class="code-block-lang" style="font-size:11px; font-weight:600; color:var(--dim, #8b949e); text-transform:uppercase;">${label}</span><span class="code-block-actions"><button type="button" class="code-block-btn" data-click="code-copy" title="Copy code" style="padding:2px 8px; font-size:11px; background:transparent; border:none; color:var(--dim); cursor:pointer;">📋 Copy</button></span></div><pre style="margin:0; padding:12px; overflow-x:auto;"><code style="font-family:monospace; font-size:12.5px; line-height:1.5;">${escaped}</code></pre></div>`;
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
    return `<div class="chat-inline-media-card" style="display:inline-flex; align-items:center; gap:10px; margin:6px 0; padding:6px 10px; border-radius:8px; background:var(--panel2, rgba(255,255,255,0.03)); border:1px solid var(--border, #334155); max-width:440px; vertical-align:middle;">
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
  });

  // 2. Download / Preview tokens: [DOWNLOAD: filename]
  t = t.replace(/\[DOWNLOAD:\s*([^\]]+)\]/g, (_, fname) => {
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

    return `<div style="display:inline-flex; align-items:center; gap:6px; margin:4px 0;">
      <button type="button" class="file-action-badge primary" data-preview-path="${cleanName}" data-preview-title="${cleanName}" style="padding:4px 10px; font-size:11.5px; background:rgba(56,189,248,0.12); border:1px solid rgba(56,189,248,0.3); border-radius:6px; color:#38bdf8; cursor:pointer; display:inline-flex; align-items:center; gap:6px;">👁️ Preview ${cleanName}</button>
      <a href="${downloadUrl}" download="${cleanName}" class="file-action-badge" style="padding:4px 10px; font-size:11.5px; background:rgba(255,255,255,0.05); border:1px solid var(--border, #334155); border-radius:6px; color:var(--text, #f3f4f6); text-decoration:none; display:inline-flex; align-items:center; gap:6px;">⬇ Download</a>
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

export function renderCompleteChatMessage(content: string, acts?: any[], secs?: number, isChatMode?: boolean, thinking?: string): string {
  if (!content && (!acts || !acts.length) && !thinking) return '';

  let actsHtml = '';
  // only these act types draw a card; lane / step / plan / stopped records must not produce an empty "Worked for" block
  let finalActs = acts ? acts.filter((a) => ['thought', 'reasoning', 'tool_call', 'tool_result'].includes(a.type)) : [];

  if (!isChatMode && thinking && thinking.trim() && !finalActs.some((a) => a.type === 'thought' || a.type === 'reasoning')) {
    finalActs = [{ type: 'thought', text: thinking.trim(), duration_s: secs || 1 }, ...finalActs];
  }

  if (finalActs && finalActs.length) {
    const filteredActs = isChatMode
      ? finalActs.filter((a) => a.type !== 'thought' && a.type !== 'reasoning')
      : finalActs;
    if (filteredActs.length > 0) {
      actsHtml = renderAgentActsHtml(filteredActs, secs);
    }
  }

  let rawContent = content || '';
  if (isChatMode && (rawContent.includes('<think>') || rawContent.includes('</think>'))) {
    rawContent = rawContent.replace(/<think>[\s\S]*?<\/think>/gi, '').replace(/^[\s\S]*?<\/think>/i, '').trim();
  }

  const transformed = transformMarkdownSpecialTokens(rawContent);
  const parsedMarkdown = marked.parse(transformed, { async: false }) as string;
  const sanitized = sanitizeHtml(parsedMarkdown);

  return actsHtml + sanitized;
}
