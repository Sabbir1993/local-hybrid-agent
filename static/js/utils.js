/* ---------------- helpers ---------------- */
function esc(s) { return s.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;'); }

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
      // Parse [DOWNLOAD: filename] markers emitted by the agent
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
