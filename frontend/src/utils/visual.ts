// @ts-nocheck
// ```visual blocks: the model writes a short JSON fence and OUR code draws it (flow / cards / chart).
// Verbatim port of the visual section of static/js/markdown.js: every string is escaped and clamped and the icon /
// tone sets are fixed, so a hostile block can only ever draw plain cards. Left untyped on purpose, it is a direct port.
const esc = (s: unknown) =>
  String(s ?? '').replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;').replace(/'/g, '&#39;');

/* Visual block: the model writes a short JSON fence and OUR code draws it (no model HTML, styles or links):
 *   ```visual {"type":"flow","title":"..","steps":[{"title","text","icon","tone","note"}]}```   stacked cards + arrows
 *   ```visual {"type":"cards","items":[{"title","text","icon","tone"}]}```                    grid of cards
 *   ```visual {"type":"chart","kind":"bar|line|hbar",...}```                                   SVG chart (see _parseChart)
 * `icon` and `tone` are picked from the fixed sets below (anything else is dropped), every string is escaped and
 * length-clamped, and the number of cards is capped, so a bad or hostile block can only ever draw plain cards.
 * parseVisual() returns the validated spec or null (md() then shows the fence as a normal code block). */
const VIS_MAX_ITEMS = 12;
const VIS_TONES = ['blue', 'purple', 'green', 'amber', 'red', 'gray'];
const VIS_ICON_PATHS = {   // 24x24 stroke icons (lucide-style), inline so nothing is fetched and the CSP is untouched
  globe: '<circle cx="12" cy="12" r="9"/><path d="M3 12h18M12 3a14 14 0 0 1 0 18M12 3a14 14 0 0 0 0 18"/>',
  shield: '<path d="M12 3l8 3v6c0 5-3.5 8-8 9-4.5-1-8-4-8-9V6z"/><path d="M9 12l2 2 4-4"/>',
  server: '<rect x="3" y="4" width="18" height="7" rx="2"/><rect x="3" y="13" width="18" height="7" rx="2"/><path d="M7 7.5h.01M7 16.5h.01"/>',
  database: '<ellipse cx="12" cy="6" rx="8" ry="3"/><path d="M4 6v6c0 1.7 3.6 3 8 3s8-1.3 8-3V6M4 12v6c0 1.7 3.6 3 8 3s8-1.3 8-3v-6"/>',
  lock: '<rect x="5" y="11" width="14" height="9" rx="2"/><path d="M8 11V8a4 4 0 0 1 8 0v3"/>',
  user: '<circle cx="12" cy="8" r="4"/><path d="M4 21c0-4 3.6-7 8-7s8 3 8 7"/>',
  cloud: '<path d="M7 18a4 4 0 0 1-.5-8A6 6 0 0 1 18 9a4.5 4.5 0 0 1 0 9z"/>',
  cpu: '<rect x="6" y="6" width="12" height="12" rx="2"/><rect x="9.5" y="9.5" width="5" height="5"/><path d="M9 3v3M15 3v3M9 18v3M15 18v3M3 9h3M3 15h3M18 9h3M18 15h3"/>',
  file: '<path d="M14 3H7a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h10a2 2 0 0 0 2-2V8z"/><path d="M14 3v5h5M9 13h6M9 17h6"/>',
  key: '<circle cx="8" cy="15" r="4"/><path d="M11 12l9-9M16 7l3 3M14 9l2 2"/>',
  gear: '<circle cx="12" cy="12" r="3"/><path d="M12 2v3M12 19v3M2 12h3M19 12h3M4.9 4.9l2.1 2.1M17 17l2.1 2.1M4.9 19.1L7 17M17 7l2.1-2.1"/>',
  check: '<circle cx="12" cy="12" r="9"/><path d="M8 12.5l3 3 5-6"/>',
  alert: '<path d="M12 3l10 18H2z"/><path d="M12 10v5M12 18h.01"/>',
  code: '<path d="M8 8l-5 4 5 4M16 8l5 4-5 4M14 5l-4 14"/>',
  network: '<circle cx="12" cy="5" r="2.5"/><circle cx="5" cy="19" r="2.5"/><circle cx="19" cy="19" r="2.5"/><path d="M12 7.5v4M12 11.5H5v5M12 11.5h7v5"/>',
  mobile: '<rect x="7" y="3" width="10" height="18" rx="2"/><path d="M11 18h2"/>',
  mail: '<rect x="3" y="5" width="18" height="14" rx="2"/><path d="M3 7l9 6 9-6"/>',
  money: '<rect x="3" y="6" width="18" height="12" rx="2"/><circle cx="12" cy="12" r="2.5"/><path d="M6 12h.01M18 12h.01"/>',
};
const VIS_ARROW = '<svg class="vis-arrow-ic" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M12 5v14M6 13l6 6 6-6"/></svg>';

function _visItem(o, withNote) {
  const str = (v, n) => (typeof v === 'string' ? v.replace(/\s+/g, ' ').trim().slice(0, n) : '');
  const title = str(o && o.title, 80);
  if (!title) return null;
  const icon = typeof o.icon === 'string' ? o.icon.toLowerCase() : '';
  const tone = typeof o.tone === 'string' ? o.tone.toLowerCase() : '';
  return { title, text: str(o.text, 200), note: withNote ? str(o.note, 80) : '',
    icon: Object.prototype.hasOwnProperty.call(VIS_ICON_PATHS, icon) ? icon : '',
    tone: VIS_TONES.includes(tone) ? tone : '' };
}

/* Charts: {"type":"chart","kind":"bar|line|hbar","title","unit","labels":[..],"series":[{"name","values":[..],"tone"}]}
 * or the short form {"type":"chart","kind":"hbar","items":[{"label","value"}]}. Drawn as inline SVG from numbers we
 * parsed ourselves: labels are escaped text, coordinates are computed, colours come from the tone classes. */
const VIS_CHART_KINDS = ['bar', 'line', 'hbar'];
const VIS_MAX_POINTS = 24, VIS_MAX_SERIES = 4;

function _visNum(v) { const n = typeof v === 'number' ? v : (typeof v === 'string' ? parseFloat(v) : NaN); return Number.isFinite(n) ? n : 0; }

function _parseChart(data) {
  const str = (v, n) => (typeof v === 'string' ? v.replace(/\s+/g, ' ').trim().slice(0, n) : '');
  const kind = VIS_CHART_KINDS.includes(data.kind) ? data.kind : 'bar';
  let labels, series;
  if (Array.isArray(data.items)) {
    const rows = data.items.slice(0, VIS_MAX_POINTS).filter(o => o && typeof o === 'object');
    labels = rows.map(o => str(o.label, 40));
    series = [{ name: '', tone: '', values: rows.map(o => _visNum(o.value)) }];
  } else if (Array.isArray(data.labels) && Array.isArray(data.series)) {
    labels = data.labels.slice(0, VIS_MAX_POINTS).map(l => str(typeof l === 'number' ? String(l) : l, 40));
    series = data.series.slice(0, VIS_MAX_SERIES).filter(s => s && Array.isArray(s.values)).map(s => ({
      name: str(s.name, 40), tone: VIS_TONES.includes(String(s.tone).toLowerCase()) ? String(s.tone).toLowerCase() : '',
      values: labels.map((_, i) => _visNum(s.values[i])) }));
  } else return null;
  if (!labels.length || !series.length) return null;
  return { type: 'chart', kind, unit: str(data.unit, 12), labels, series,
    title: str(data.title, 100) };
}

export function parseVisual(code) {
  let data;
  try { data = JSON.parse(String(code ?? '')); } catch (_) { return null; }
  if (!data || typeof data !== 'object') return null;
  if (data.type === 'chart' || VIS_CHART_KINDS.includes(data.type)) {
    return _parseChart(VIS_CHART_KINDS.includes(data.type) ? { ...data, kind: data.type } : data);
  }
  const type = data.type === 'cards' || (!data.type && Array.isArray(data.items)) ? 'cards' : 'flow';
  const raw = type === 'cards' ? data.items : data.steps;
  if (!Array.isArray(raw)) return null;
  const items = raw.slice(0, VIS_MAX_ITEMS).map(o => _visItem(o, type === 'flow')).filter(Boolean);
  if (!items.length) return null;
  return { type, title: typeof data.title === 'string' ? data.title.replace(/\s+/g, ' ').trim().slice(0, 100) : '', items };
}

// escaped text, with `code` spans (the one inline format models reach for in a card)
function _visText(s) { return esc(s).replace(/`([^`]+)`/g, '<code>$1</code>'); }

function _visFmt(n) {
  const a = Math.abs(n);
  if (a >= 1e9) return +(n / 1e9).toFixed(1) + 'B';
  if (a >= 1e6) return +(n / 1e6).toFixed(1) + 'M';
  if (a >= 1e4) return +(n / 1e3).toFixed(1) + 'k';
  return String(+n.toFixed(a < 10 ? 2 : 1));
}

function _visNiceMax(v) {
  if (v <= 0) return 1;
  const p = Math.pow(10, Math.floor(Math.log10(v))), f = v / p;
  return (f <= 1 ? 1 : f <= 2 ? 2 : f <= 5 ? 5 : 10) * p;
}

function _visChartSvg(spec) {
  const { kind, labels, series, unit } = spec;
  const tone = (s, i) => s.tone || VIS_TONES[i % 5];
  const r1 = (n) => Math.round(n * 10) / 10;
  const vals = series.flatMap(s => s.values);
  const lo = Math.min(0, ...vals), hi = _visNiceMax(Math.max(0, ...vals));
  const withUnit = (n) => _visFmt(n) + (unit ? ' ' + esc(unit) : '');
  let svg = '', h;
  if (kind === 'hbar') {
    const s = series[0], rowH = 30, labW = 150, x0 = labW + 10, maxW = 590 - x0 - 70;
    h = labels.length * rowH + 8;
    const span = (hi - lo) || 1, zero = x0 + (0 - lo) / span * maxW;
    labels.forEach((l, i) => {
      const v = s.values[i], y = 6 + i * rowH, w = Math.abs(v) / span * maxW, x = v >= 0 ? zero : zero - w;
      svg += `<text class="vis-ax" x="${labW}" y="${r1(y + 15)}" text-anchor="end">${esc(l.length > 24 ? l.slice(0, 23) + '…' : l)}</text>`
        + `<rect class="vis-bar vis-tone-${tone(s, 0)}" x="${r1(x)}" y="${y + 3}" width="${r1(Math.max(w, 1))}" height="${rowH - 10}" rx="3"/>`
        + `<text class="vis-val" x="${r1((v >= 0 ? x + w : x) + (v >= 0 ? 6 : -6))}" y="${r1(y + 15)}" text-anchor="${v >= 0 ? 'start' : 'end'}">${withUnit(v)}</text>`;
    });
    return `<svg class="vis-chart" viewBox="0 0 600 ${h}" role="img" aria-label="${esc(spec.title || 'chart')}">${svg}</svg>`;
  }
  const L = 46, R = 590, T = 14, B = 186; h = 226;
  const span = (hi - lo) || 1, yOf = (v) => B - (v - lo) / span * (B - T);
  for (let t = 0; t <= 4; t++) {
    const v = lo + span * t / 4, y = r1(yOf(v));
    svg += `<line class="vis-grid-l" x1="${L}" y1="${y}" x2="${R}" y2="${y}"/>`
      + `<text class="vis-ax" x="${L - 6}" y="${r1(y + 3.5)}" text-anchor="end">${_visFmt(v)}</text>`;
  }
  const n = labels.length, every = Math.ceil(n / 8);
  const slot = (R - L) / n;
  const xBar = (i) => L + slot * i + slot / 2;
  const xLine = (i) => (n === 1 ? (L + R) / 2 : L + 22 + (R - L - 44) * i / (n - 1));   // 22 px in: the first point's label stays off the axis text
  const xAt = kind === 'bar' ? xBar : xLine;
  labels.forEach((l, i) => {
    if (i % every === 0) svg += `<text class="vis-ax" x="${r1(xAt(i))}" y="${B + 16}" text-anchor="middle">${esc(l.length > 14 ? l.slice(0, 13) + '…' : l)}</text>`;
  });
  const showVals = n <= 8 && series.length <= 2;
  if (kind === 'bar') {
    const k = series.length, bw = Math.min(46, slot * 0.7 / k), y0 = yOf(0);
    series.forEach((s, si) => s.values.forEach((v, i) => {
      const x = xBar(i) - bw * k / 2 + bw * si, y = yOf(v);
      svg += `<rect class="vis-bar vis-tone-${tone(s, si)}" x="${r1(x)}" y="${r1(Math.min(y, y0))}" width="${r1(bw - 2)}" height="${r1(Math.max(Math.abs(y0 - y), 1))}" rx="2"/>`;
      if (showVals) svg += `<text class="vis-val" x="${r1(x + (bw - 2) / 2)}" y="${r1(v >= 0 ? y - 4 : y + 12)}" text-anchor="middle">${_visFmt(v)}</text>`;
    }));
  } else {
    series.forEach((s, si) => {
      const pts = s.values.map((v, i) => `${r1(xLine(i))},${r1(yOf(v))}`);
      svg += `<polyline class="vis-line vis-tone-${tone(s, si)}" points="${pts.join(' ')}"/>`;
      s.values.forEach((v, i) => {
        svg += `<circle class="vis-dot vis-tone-${tone(s, si)}" cx="${r1(xLine(i))}" cy="${r1(yOf(v))}" r="3.5"/>`;
        if (showVals) svg += `<text class="vis-val" x="${r1(xLine(i))}" y="${r1(yOf(v) - 8)}" text-anchor="middle">${_visFmt(v)}</text>`;
      });
    });
  }
  return `<svg class="vis-chart" viewBox="0 0 600 ${h}" role="img" aria-label="${esc(spec.title || 'chart')}">${svg}</svg>`;
}

export function renderVisualHtml(spec) {
  if (spec.type === 'chart') {
    const legend = spec.series.length > 1
      ? `<div class="vis-legend">${spec.series.map((s, i) => `<span class="vis-tone-${s.tone || VIS_TONES[i % 5]}"><i class="vis-sw"></i>${esc(s.name || 'Series ' + (i + 1))}</span>`).join('')}</div>` : '';
    return `<div class="vis-box">${spec.title ? `<div class="vis-title">${esc(spec.title)}</div>` : ''}${legend}${_visChartSvg(spec)}</div>`;
  }
  const card = (it, i) => {
    const tone = it.tone || VIS_TONES[i % 5];
    const icon = it.icon
      ? `<span class="vis-icon"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">${VIS_ICON_PATHS[it.icon]}</svg></span>` : '';
    return `<div class="vis-card vis-tone-${tone}">${icon}<div class="vis-ct">${_visText(it.title)}</div>`
      + (it.text ? `<div class="vis-tx">${_visText(it.text)}</div>` : '') + `</div>`;
  };
  const title = spec.title ? `<div class="vis-title">${esc(spec.title)}</div>` : '';
  if (spec.type === 'cards') {
    return `<div class="vis-box">${title}<div class="vis-grid">${spec.items.map(card).join('')}</div></div>`;
  }
  const last = spec.items.length - 1;
  const body = spec.items.map((it, i) => card(it, i)
    + (i < last ? `<div class="vis-arrow">${VIS_ARROW}${it.note ? `<span class="vis-note">${_visText(it.note)}</span>` : ''}</div>` : '')).join('');
  return `<div class="vis-box">${title}<div class="vis-flow">${body}</div></div>`;
}

/* Product cards: ```products [{"title","price","image","url","store","facts":[..],"note"}] - up to 6 items. Every string is
 * escaped and clamped, only https image / page addresses survive, and pictures load through /media/proxy (never from the
 * shop's own host; the server only serves pictures a search tool returned). Invalid JSON stays a normal code block. */
const PROD_MAX = 6;
export function parseProducts(code) {
  let data;
  try { data = JSON.parse(String(code ?? '')); } catch (_) { return null; }
  if (data && !Array.isArray(data) && Array.isArray(data.items)) data = data.items;
  if (!Array.isArray(data)) return null;
  const str = (v, n) => (typeof v === 'string' ? v.replace(/\s+/g, ' ').trim().slice(0, n) : '');
  const https = v => (typeof v === 'string' && /^https:\/\/[^\s"'<>]+$/i.test(v.trim()) ? v.trim().slice(0, 600) : '');
  const items = data.slice(0, PROD_MAX).map(o => {
    if (!o || typeof o !== 'object') return null;
    const title = str(o.title, 90);
    if (!title) return null;
    const url = https(o.url);
    let store = str(o.store, 40);
    if (!store && url) { try { store = new URL(url).hostname.replace(/^www\./, ''); } catch (_) { /* no store name */ } }
    return { title, price: str(o.price, 30), image: https(o.image), url, store, note: str(o.note, 40),
      facts: (Array.isArray(o.facts) ? o.facts : []).slice(0, 4).map(f => str(f, 60)).filter(Boolean) };
  }).filter(Boolean);
  return items.length ? { type: 'products', items } : null;
}

export function renderProductsHtml(spec) {
  const card = it => {
    // the server serves the model's picture if a search returned it, else the og:image of the card's own page
    const img = (it.image || it.url)
      ? `<img src="${esc('/media/product-image?u=' + encodeURIComponent(it.url) + '&i=' + encodeURIComponent(it.image))}" alt="" loading="lazy" referrerpolicy="no-referrer" data-hide-on-error />` : '';
    const link = it.url
      ? `<a class="prod-link" href="${esc(it.url)}" target="_blank" rel="noopener noreferrer">View${it.store ? ' at ' + esc(it.store) : ''} &#8599;</a>` : '';
    return `<div class="prod-card"><div class="prod-img">${img}</div><div class="prod-body">`
      + (it.note ? `<span class="prod-note">${esc(it.note)}</span>` : '')
      + `<div class="prod-title">${esc(it.title)}</div>`
      + (it.price ? `<div class="prod-price">${esc(it.price)}</div>` : '')
      + (it.facts.length ? `<div class="prod-facts">${it.facts.map(esc).join(' &middot; ')}</div>` : '')
      + (it.store && !it.url ? `<div class="prod-store">${esc(it.store)}</div>` : '') + link + `</div></div>`;
  };
  return `<div class="prod-list">${spec.items.map(card).join('')}</div>`;
}
