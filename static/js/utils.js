/* ---------------- helpers ---------------- */
function esc(s) { return s.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;').replace(/'/g, '&#39;'); }

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
              <button class="btn ghost" style="padding:2px 6px; font-size:10px;" onclick="openFilePreview('diagram.mermaid', 'Mermaid Diagram', this.closest('.mermaid-box').querySelector('.mermaid-code-raw').textContent)">⤢ Fullscreen</button>
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
      let t = esc(parts[i]);
      t = t.replace(/`([^`]+)`/g, '<code>$1</code>');
      t = t.replace(/\*\*([^*]+)\*\*/g, '<strong>$1</strong>');
      t = t.replace(/^#{1,3} (.*)$/gm, '<b>$1</b>');
      t = t.replace(/^\s*[-*] (.*)$/gm, '• $1');

      // 1. Render markdown images: ![alt](url)
      t = t.replace(/!\[([^\]]*)\]\(((?:https?:\/\/|\/agent\/raw|\/raw)[^)]+)\)/g, (match, alt, url) => {
        return `<span class="chat-inline-media"><img src="${url}" alt="${alt}" class="chat-inline-img" loading="lazy" onclick="openFilePreview('${url.replace(/'/g, "\\'")}', '${esc(alt || 'Image').replace(/'/g, "\\'")}')" title="Click to enlarge" /></span>`;
      });

      // 2. Render links & file download buttons
      t = t.replace(/\[([^\]]+)\]\(((?:https?:\/\/|\/agent\/download|\/download)[^)]+)\)/g, (match, text, url) => {
        if (url.startsWith('/agent/download') || url.startsWith('/download')) {
          const m = url.match(/[?&]path=([^&]+)/);
          const fpath = m ? decodeURIComponent(m[1]) : text;
          return `<span style="display:inline-flex; align-items:center; gap:4px; margin:2px 0;">
            <button type="button" class="file-action-badge primary" onclick="openFilePreview('${esc(fpath).replace(/'/g, "\\'")}', '${esc(text).replace(/'/g, "\\'")}')" title="Preview ${esc(text)}">👁️ Preview ${esc(text)}</button>
            <a href="${url}" class="file-action-badge" download title="Download ${esc(text)}">⬇</a>
          </span>`;
        }
        return `<a href="${url}" target="_blank" rel="noopener">${text}</a>`;
      });

      // 3. Parse [DOWNLOAD: filename] markers emitted by the agent
      t = t.replace(/\[DOWNLOAD:\s*([^\]]+)\]/g, (_, fname) => {
        const cleanName = fname.trim();
        const url = `/agent/download?path=${encodeURIComponent(cleanName)}`;
        return `<span style="display:inline-flex; align-items:center; gap:4px; margin:2px 0;">
          <button type="button" class="file-action-badge primary" onclick="openFilePreview('${esc(cleanName).replace(/'/g, "\\'")}', '${esc(cleanName).replace(/'/g, "\\'")}')" title="Preview ${esc(cleanName)}">👁️ Preview ${esc(cleanName)}</button>
          <a href="${url}" class="file-action-badge" download="${esc(cleanName)}" title="Download ${esc(cleanName)}">⬇ Download</a>
        </span>`;
      });
      t = t.replace(/\n/g, '<br>');
      out += t;
    }
  }
  return out;
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
  const mdImgRx = /!\[([^\]]*)\]\((https?:\/\/[^)\s]+|\/agent\/raw[^)\s]+|\/raw[^)\s]+)\)/g;
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
    const cleanTitle = esc(it.title || 'Media');
    const safeUrl = esc(it.url).replace(/'/g, "\\'");

    if (it.type === 'youtube') {
      const vidMatch = it.url.match(/([a-zA-Z0-9_-]{11})/);
      const vidId = vidMatch ? vidMatch[1] : '';
      html += `<div class="media-card media-card-video">
        <div class="media-video-frame">
          <iframe src="https://www.youtube-nocookie.com/embed/${vidId}" frameborder="0" allow="accelerometer; autoplay; clipboard-write; encrypted-media; gyroscope; picture-in-picture" allowfullscreen loading="lazy"></iframe>
        </div>
        <div class="media-card-footer">
          <span class="media-card-name" title="${cleanTitle}">▶ ${cleanTitle}</span>
          <a href="${it.url}" target="_blank" rel="noopener" class="media-card-ext-btn" title="Open YouTube">↗</a>
        </div>
      </div>`;
    } else if (it.type === 'video') {
      html += `<div class="media-card media-card-video">
        <div class="media-video-frame">
          <video controls preload="metadata" src="${it.url}"></video>
        </div>
        <div class="media-card-footer">
          <span class="media-card-name" title="${cleanTitle}">🎬 ${cleanTitle}</span>
          <a href="${it.url}" target="_blank" rel="noopener" class="media-card-ext-btn" title="Open Video">↗</a>
        </div>
      </div>`;
    } else {
      // Image
      html += `<div class="media-card media-card-img" onclick="openFilePreview('${safeUrl}', '${cleanTitle}')">
        <div class="media-card-thumb-wrap">
          <img src="${it.thumb || it.url}" class="media-card-thumb" loading="lazy" alt="${cleanTitle}" onerror="this.closest('.media-card').style.display='none'" />
          <span class="media-badge">IMAGE</span>
        </div>
        <div class="media-card-footer">
          <span class="media-card-name" title="${cleanTitle}">${cleanTitle}</span>
          <a href="${it.url}" target="_blank" rel="noopener" class="media-card-ext-btn" onclick="event.stopPropagation()" title="Open Original">↗</a>
        </div>
      </div>`;
    }
  });

  html += `</div></div>`;
  return html;
}

function toast(msg, isErr) {
  const t = $('toast');
  t.textContent = msg;
  t.className = 'show' + (isErr ? ' err' : '');
  clearTimeout(t._h);
  t._h = setTimeout(() => t.className = '', 4000);
}

function fmtUptime(s) {
  if (s == null) return '';
  s = Math.floor(s);
  const h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60), sec = s % 60;
  return h ? `${h}h ${m}m` : (m ? `${m}m ${sec}s` : `${sec}s`);
}
