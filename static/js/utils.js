/* ---------------- helpers ---------------- */
function esc(s) { return s.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;'); }

function md(s) {
  // split fences BEFORE escaping: hlCode() escapes internally
  const parts = String(s).split(/```/);
  let out = '';
  for (let i = 0; i < parts.length; i++) {
    if (i % 2 === 1) { // fenced code block: ```lang\ncode
      const m = parts[i].match(/^([\w#+.-]*)\n([\s\S]*)$/);
      const lang = hlLangFor(m ? m[1] : '');
      const code = m ? m[2] : parts[i];
      out += '<pre><code>' + (lang ? hlCode(code, lang) : esc(code)) + '</code></pre>';
    } else {
      let t = esc(parts[i]);
      t = t.replace(/`([^`]+)`/g, '<code>$1</code>');
      t = t.replace(/\*\*([^*]+)\*\*/g, '<strong>$1</strong>');
      t = t.replace(/^#{1,3} (.*)$/gm, '<b>$1</b>');
      t = t.replace(/^\s*[-*] (.*)$/gm, '• $1');
      t = t.replace(/\[([^\]]+)\]\(((?:https?:\/\/|\/agent\/download|\/download)[^)]+)\)/g, (match, text, url) => {
        if (url.startsWith('/agent/download') || url.startsWith('/download')) {
          return `<a href="${url}" class="download-link" download title="Download ${text}">⬇ ${text}</a>`;
        }
        return `<a href="${url}" target="_blank" rel="noopener">${text}</a>`;
      });
      // Parse [DOWNLOAD: filename] markers emitted by the agent
      t = t.replace(/\[DOWNLOAD:\s*([^\]]+)\]/g, (_, fname) => {
        const cleanName = fname.trim();
        const url = `/agent/download?path=${encodeURIComponent(cleanName)}`;
        return `<a href="${url}" class="download-link" download="${esc(cleanName)}" title="Download ${esc(cleanName)}">⬇ ${esc(cleanName)}</a>`;
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
