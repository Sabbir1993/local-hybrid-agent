/* Chat Markdown: marked (GitHub-flavoured: tables, quotes, rules, nested/ordered/task lists, italic, strike)
 * followed by DOMPurify. Same shape as other open chat UIs (parser + sanitiser + streaming repair).
 *
 *  - raw HTML in the model's text is never rendered (shown as text; only <br> is kept, models use it in table cells)
 *  - links / images / [DOWNLOAD:] / [VIDEO:] / code blocks become "chips" built by our own code from escaped values;
 *    they are swapped in AFTER sanitising via private-use placeholders, so the sanitiser's allowlist stays tiny
 *    (no a / img / button / style / data-*) and nothing the model writes can reach those tags
 *  - finished blocks are cached, so while streaming only the block that is still growing is parsed again
 *  - if marked / DOMPurify did not load, md() in utils.js falls back to its old renderer
 */

const MD_OPEN = '\uE000', MD_CLOSE = '\uE001';   // private-use chars: stripped from model text first
const MD_TAGS = ['b', 'blockquote', 'br', 'code', 'del', 'div', 'em', 'h1', 'h2', 'h3', 'h4', 'h5', 'h6', 'hr', 'i',
  'li', 'ol', 'p', 'pre', 'span', 'strong', 'sub', 'sup', 'table', 'tbody', 'td', 'th', 'thead', 'tr', 'ul'];
const MD_ATTRS = ['class', 'title', 'align'];
const MD_CACHE_MAX = 400;

let _mdCtx = null;                 // {chips, shown} for the block being rendered
let _mdConfigured = false;
const _mdCache = new Map();

function mdAvailable() {
  return typeof marked !== 'undefined' && typeof marked.use === 'function' && typeof DOMPurify !== 'undefined';
}

function _mdPut(html) {
  _mdCtx.chips.push(html);
  return MD_OPEN + (_mdCtx.chips.length - 1) + MD_CLOSE;
}

// /commands and @files highlight; `html` is already escaped
function _mdHl(html) {
  return html
    .replace(/(^|[\s\[({,;:"'])\/([a-zA-Z0-9_\-]+)(?=$|[\s\])}>.,;:!?])/g,
      '$1<span class="token-hl token-cmd" title="Internal directive: /$2">/$2</span>')
    .replace(/(^|[\s\[({,;:"'])@([\w\-./\\]+\.[\w]+)(?=$|[\s\])}>.,;:!?])/g,
      '$1<span class="token-hl token-tag" title="Target file: @$2">@$2</span>');
}

function _mdFileChip(path, label, withDownloadText) {
  const p = esc(path), l = esc(label);
  const url = '/agent/download?path=' + encodeURIComponent(path);
  return `<span class="chat-chip-row">`
    + `<button type="button" class="file-action-badge primary" data-preview-path="${p}" data-preview-title="${l}" title="Preview ${l}">👁️ Preview ${l}</button>`
    + (withDownloadText
      ? `<a href="${esc(url)}" class="file-action-badge" download="${p}" title="Download ${p}">⬇ Download</a>`
      : `<a href="${esc(url)}" class="file-action-badge" download title="Download ${l}">⬇</a>`)
    + `</span>`;
}

function _mdConfigure() {
  if (_mdConfigured) return true;
  if (!mdAvailable()) return false;
  marked.use({
    gfm: true,
    breaks: true,                    // a single newline stays a visible line break (chat feel)
    extensions: [{
      name: 'mdMarker', level: 'inline',
      start(src) { const i = src.search(/\[(?:DOWNLOAD|VIDEO):/); return i < 0 ? undefined : i; },
      tokenizer(src) {
        const m = /^\[(DOWNLOAD|VIDEO):\s*([^\]]+)\]/.exec(src);
        return m ? { type: 'mdMarker', raw: m[0], kind: m[1], name: m[2].trim() } : undefined;
      },
      renderer(tok) {
        if (tok.kind === 'VIDEO') {
          const src = '/agent/raw?path=' + encodeURIComponent(tok.name);
          return _mdPut(`<span class="chat-inline-media"><video class="chat-inline-video" controls preload="metadata" src="${esc(src)}"></video></span>`);
        }
        if (_mdCtx.shown.has(tok.name)) return '';      // already shown as an inline image
        return _mdPut(_mdFileChip(tok.name, tok.name, true));
      },
    }],
    renderer: {
      html(token) {                                      // never raw HTML; models use <br> inside table cells
        const t = String(token.text || '');
        return /^<br\s*\/?>$/i.test(t.trim()) ? '<br>' : esc(t);
      },
      text(token) {
        if (token.tokens && token.tokens.length) return this.parser.parseInline(token.tokens);
        return _mdHl(token.escaped ? token.text : esc(token.text));
      },
      // "- [ ] item" is just a list item: an empty box means nothing in a chat reply. Only "done" keeps a muted tick.
      checkbox(token) { return token.checked ? '<span class="md-task">✓</span> ' : ''; },
      code(token) {                                      // indented / ~~~ blocks (``` fences are split off in md())
        const lang = String(token.lang || '').trim().split(/\s+/)[0].toLowerCase();
        const hl = hlLangFor(lang);
        return _mdPut(codeBlockHtml(lang, token.text, hl ? hlCode(token.text, hl) : esc(token.text)));
      },
      image(token) {
        const href = String(token.href || ''), alt = token.text || '';
        if (!/^(?:https?:\/\/|\/agent\/raw|\/raw)/.test(href)) return esc(alt);
        const url = esc(href), a = esc(alt);
        if (!isLocalMediaUrl(href)) return _mdPut(externalImageButton(url, a));
        return _mdPut(`<span class="chat-inline-media"><img src="${url}" alt="${a}" class="chat-inline-img" loading="lazy" title="Click to enlarge" /></span>`);
      },
      link(token) {
        const href = String(token.href || ''), text = String(token.text || '');
        const inner = token.tokens && token.tokens.length ? this.parser.parseInline(token.tokens) : esc(text);
        if (/^\/(?:agent\/)?download/.test(href)) {
          const m = href.match(/[?&]path=([^&]+)/);
          let path = text;
          if (m) { try { path = decodeURIComponent(m[1]); } catch (_) { path = m[1]; } }
          if (m && _mdCtx.shown.has(path)) return '';
          return _mdPut(`<span class="chat-chip-row"><button type="button" class="file-action-badge primary" data-preview-path="${esc(path)}" data-preview-title="${esc(text)}" title="Preview ${esc(text)}">👁️ Preview ${esc(text)}</button>`
            + `<a href="${esc(href)}" class="file-action-badge" download title="Download ${esc(text)}">⬇</a></span>`);
        }
        if (!/^https?:\/\//i.test(href)) return inner + (href ? ' (' + esc(href) + ')' : '');   // mailto:, javascript:, ... stay inert text
        const domain = extractDomain(href);
        if (domain && /^(\d+|\[\d+\]|ref\.?\s*\d+|\^?\d+\^?)$/i.test(text.trim())) {
          const d = esc(domain);
          const fav = esc(`https://www.google.com/s2/favicons?domain=${encodeURIComponent(domain)}&sz=32`);
          return _mdPut(`<a href="${esc(href)}" target="_blank" rel="noopener noreferrer" class="citation-pill" title="${d} - Click to open source">`
            + `<img src="${fav}" class="citation-favicon" alt="" loading="lazy" data-hide-on-error /><span class="citation-host">${d}</span></a>`);
        }
        const title = token.title ? ` title="${esc(token.title)}"` : '';
        return _mdPut(`<a href="${esc(href)}"${title} target="_blank" rel="noopener noreferrer">`) + inner + _mdPut('</a>');
      },
    },
  });
  _mdConfigured = true;
  return true;
}

// local files already shown as an inline image: their download chips would repeat them
function _mdShownFiles(prose) {
  const shown = new Set();
  prose.replace(/!\[[^\]]*\]\((?:\/agent\/raw|\/raw)\?path=([^)&\s]+)[^)]*\)/g, (all, p) => {
    try { shown.add(decodeURIComponent(p)); } catch (_) { shown.add(p); }
    return all;
  });
  return shown;
}

function _mdRenderBlock(token, shown) {
  _mdCtx = { chips: [], shown };
  try {
    let html = marked.parser([token]);
    html = html.replace(/<table>/g, '<div class="md-table-wrap"><table>').replace(/<\/table>/g, '</table></div>');
    html = DOMPurify.sanitize(html, { ALLOWED_TAGS: MD_TAGS, ALLOWED_ATTR: MD_ATTRS, ALLOW_DATA_ATTR: false, ALLOW_ARIA_ATTR: false });
    const chips = _mdCtx.chips;
    return html.replace(/\uE000(\d+)\uE001/g, (_, i) => chips[+i] ?? '');
  } finally {
    _mdCtx = null;
  }
}

// While a reply is still streaming, close an unfinished `code`, **bold** or ~~strike~~ in the last paragraph so it does
// not flash as raw asterisks. Display only: the stored message is never changed.
function mdHeal(prose) {
  const cut = prose.lastIndexOf('\n\n') + 2;
  const head = prose.slice(0, cut > 1 ? cut : 0);
  let tail = prose.slice(head.length);
  const strip = (s) => s.replace(/`[^`\n]*`/g, '');
  if ((strip(tail).match(/`/g) || []).length % 2) tail += '`';
  const bare = strip(tail);
  if ((bare.match(/\*\*/g) || []).length % 2) tail += '**';
  if ((bare.match(/~~/g) || []).length % 2) tail += '~~';
  return head + tail;
}

/* prose = text between code fences; opts.streaming = the reply is still arriving */
function mdRender(prose, opts) {
  if (!_mdConfigure()) return null;
  let src = String(prose ?? '').replace(/[\uE000\uE001]/g, '');
  if (opts && opts.streaming) src = mdHeal(src);
  const shown = _mdShownFiles(src);
  const shownKey = [...shown].sort().join('|');
  const tokens = marked.lexer(src).filter(t => t.type !== 'space');
  let out = '';
  tokens.forEach((t, i) => {
    const growing = opts && opts.streaming && i === tokens.length - 1;
    const key = shownKey + '\u0001' + t.raw;
    let html = growing ? undefined : _mdCache.get(key);
    if (html === undefined) {
      html = _mdRenderBlock(t, shown);
      if (!growing) {
        if (_mdCache.size >= MD_CACHE_MAX) _mdCache.clear();
        _mdCache.set(key, html);
      }
    }
    out += html;
  });
  return out;
}

if (typeof module !== 'undefined') module.exports = { mdRender, mdHeal, mdAvailable };
