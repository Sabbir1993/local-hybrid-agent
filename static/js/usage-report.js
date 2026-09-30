/* ---------------- usage report ---------------- */
/* The period buttons at the top (Today / 7d / 30d / All) drive the summary cards only. The two tables
   below have their own date range, filters and pager, and load their rows separately. */
function repSummaryHtml(d) {
  // big totals read 26.7M; the exact figure stays in the tooltip
  const fmt = n => n == null ? '0' : repTok(n);
  const exact = n => (n == null ? 0 : Number(n)).toLocaleString();
  return `
      <div class="rep-grid">
        <div class="rep-cell"><b title="${exact(d.total_tokens)}">${fmt(d.total_tokens)}</b><span>Total Tokens</span></div>
        <div class="rep-cell"><b title="${exact(d.prompt_tokens)}">${fmt(d.prompt_tokens)}</b><span>Prompt Tokens</span></div>
        <div class="rep-cell"><b title="${exact(d.completion_tokens)}">${fmt(d.completion_tokens)}</b><span>Generated</span></div>
        <div class="rep-cell" style="border-color: rgba(56,189,248,0.35); background: rgba(56,189,248,0.06);" title="Total tokens read directly from prompt and output cache">
          <b style="color: #38bdf8;" title="${exact(d.total_cached_tokens)}">${fmt(d.total_cached_tokens)}</b>
          <span>Total Read From Cache (${d.cache_hit_rate || 0}% hit)</span>
        </div>
        <div class="rep-cell" title="Input tokens read from prompt / KV cache without re-evaluation">
          <b class="rep-cache-val" title="${exact(d.prompt_cached_tokens)}">${fmt(d.prompt_cached_tokens)}</b>
          <span>Input Cache Read</span>
        </div>
        <div class="rep-cell" title="Output tokens read from speculative draft / accepted cache">
          <b class="rep-gen-val" title="${exact(d.completion_cached_tokens)}">${fmt(d.completion_cached_tokens)}</b>
          <span>Output Cache Read</span>
        </div>
        <div class="rep-cell" title="Total calls and tokens executed by orchestrator models (executor, vision, embedder, router)">
          <b style="color: #f59e0b;">⚡ ${exact(d.orchestrator_requests)} <small style="font-size:11px; font-weight:normal; color:var(--dim);">(${fmt(d.orchestrator_total_tokens)} tok)</small></b>
          <span>Orchestrator Models</span>
        </div>
        <div class="rep-cell"><b>${exact(d.requests)}</b><span>Total Requests</span></div>
        <div class="rep-cell"><b>${d.avg_tps} t/s</b><span>Avg Speed (${d.avg_duration_s}s)</span></div>
      </div>`;
}

async function loadReport(days) {
  const box = $('rep-content');
  if (!$('rep-summary')) {
    box.innerHTML = '<div id="rep-summary"><div class="mon-empty">Loading…</div></div><div id="rep-tables"></div>';
    box.oninput = box.onchange = repOnFilter;
    box.onclick = repOnClick;
  }
  try {
    const summary = $('rep-summary');
    const [d, wide] = await Promise.all([
      (await fetch('/control/report?days=' + days)).json(),
      // the tables read the whole retained history once; their own filters narrow it down
      _repData ? Promise.resolve(_repData) : fetch('/control/report?days=365').then(r => r.json()),
    ]);
    summary.innerHTML = repSummaryHtml(d);
    if (!_repData) {
      _repData = wide;
      _repModels = wide.by_model || [];
      $('rep-tables').innerHTML = repCardsHtml(wide);
      if (REP.model.from || REP.model.to) await repFetchModels();
      repRenderModels();
      repRenderDays();
    }
  } catch (e) {
    box.innerHTML = '<div class="mon-empty">Report failed: ' + esc(e.message) + '</div>';
  }
}

/* By Model rows for its own date range (the server groups by model for exactly those days). */
let _repModels = [];
let _repModelTimer = null;
let _repRangeIgnored = false;
async function repFetchModels() {
  const s = REP.model;
  if (!s.from && !s.to) { _repModels = (_repData && _repData.by_model) || []; return; }
  const qs = new URLSearchParams({ days: '365' });
  if (s.from) qs.set('start', s.from);
  if (s.to) qs.set('end', s.to);
  try {
    const j = await (await fetch('/control/report?' + qs)).json();
    _repModels = j.by_model || [];
    // a server that predates date ranges ignores start/end and answers with everything
    _repRangeIgnored = !j.range;
  } catch (e) { _repModels = []; }
}

/* Token counts: 1,112,811 -> 1.1M (exact value stays in the tooltip). */
function repTok(n) {
  n = Number(n) || 0;
  const a = Math.abs(n);
  if (a < 1000) return String(n);
  const units = [[1e3, 'K'], [1e6, 'M'], [1e9, 'B']];
  let i = a >= 1e9 ? 2 : a >= 1e6 ? 1 : 0;
  const fmt1 = x => (Math.abs(x) >= 100 ? x.toFixed(0) : x.toFixed(1)).replace(/\.0$/, '');
  let txt = fmt1(n / units[i][0]);
  if (Math.abs(parseFloat(txt)) >= 1000 && i < 2) { i++; txt = fmt1(n / units[i][0]); }   // 999,999 -> 1M, not 1000K
  return txt + units[i][1];
}
const repTokCell = (n, cls) => `<td${cls ? ` class="${cls}"` : ''} title="${repFmt(n)}">${repTok(n)}</td>`;

/* ---- By model / By day: each table has its own filters, sort and pager (all in the browser: the
   report already carries every row) ---- */
const REP_DEFAULTS = {
  model: { q: '', role: 'all', source: 'all', sort: 'total', from: '', to: '', page: 0, size: 10 },
  day: { from: '', to: '', minReq: 0, orch: false, sort: 'newest', page: 0, size: 10 },
};
const REP = { model: Object.assign({}, REP_DEFAULTS.model), day: Object.assign({}, REP_DEFAULTS.day) };
let _repData = null;
const repFmt = n => n == null ? '0' : Number(n).toLocaleString();
const repPct = (a, b) => b > 0 ? Math.round((a / b) * 100) + '%' : '-';

const REP_MODEL_SORTS = [['total', 'Most tokens'], ['req', 'Most requests'], ['prompt', 'Most prompt tokens'],
  ['gen', 'Most generated'], ['cache', 'Most cache read'], ['hit', 'Best cache hit rate'], ['name', 'Name A-Z']];
const REP_DAY_SORTS = [['newest', 'Newest first'], ['oldest', 'Oldest first'], ['total', 'Most tokens'],
  ['req', 'Most requests'], ['hit', 'Best cache hit rate']];
const REP_SIZES = [10, 25, 50, 100];

function repOpts(list, cur) {
  return list.map(([v, l]) => `<option value="${esc(v)}"${String(cur) === String(v) ? ' selected' : ''}>${esc(l)}</option>`).join('');
}

/* ---- filter toolbars ---- */
const REP_ICON_SEARCH = '<svg class="rep-ico" width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><circle cx="11" cy="11" r="7"/><path d="m21 21-4.3-4.3"/></svg>';

function repIso(d) {
  const p = n => String(n).padStart(2, '0');
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())}`;
}
/* {from, to} for a shortcut; 'all' = no limit */
function repPresetRange(id) {
  const today = new Date(), back = n => { const d = new Date(today); d.setDate(d.getDate() - n); return repIso(d); };
  if (id === 'today') return { from: repIso(today), to: repIso(today) };
  if (id === '7d') return { from: back(6), to: repIso(today) };
  if (id === '30d') return { from: back(29), to: repIso(today) };
  return { from: '', to: '' };
}
const REP_PRESETS = [['today', 'Today'], ['7d', '7 days'], ['30d', '30 days'], ['all', 'All']];

function repActiveCount(card) {
  const s = REP[card];
  if (card === 'model') return (s.q.trim() ? 1 : 0) + (s.role !== 'all' ? 1 : 0) + (s.source !== 'all' ? 1 : 0) + (s.from || s.to ? 1 : 0);
  return (s.from || s.to ? 1 : 0) + (s.minReq > 0 ? 1 : 0) + (s.orch ? 1 : 0);
}

function repSeg(card, f, options, cur) {
  return `<div class="rep-seg" role="group">${options.map(([v, l]) =>
    `<button type="button" class="rep-seg-b${cur === v ? ' on' : ''}" data-seg data-card="${card}" data-f="${f}" data-v="${v}" aria-pressed="${cur === v}">${l}</button>`).join('')}</div>`;
}

function repDateGroup(card, s) {
  const range = { from: s.from, to: s.to };
  const on = id => { const r = repPresetRange(id); return r.from === range.from && r.to === range.to; };
  return `<div class="rep-group"><span class="rep-glabel">Dates</span>
    <div class="rep-range${s.from || s.to ? ' active' : ''}">
      <input type="date" data-card="${card}" data-f="from" value="${esc(s.from)}" aria-label="From date">
      <span class="rep-dash" aria-hidden="true">to</span>
      <input type="date" data-card="${card}" data-f="to" value="${esc(s.to)}" aria-label="To date">
    </div>
    <div class="rep-chips">${REP_PRESETS.map(([id, l]) =>
      `<button type="button" class="rep-chip${on(id) ? ' on' : ''}" data-preset="${id}" data-card="${card}">${l}</button>`).join('')}</div>
  </div>`;
}

function repResetBtn(card) {
  const n = repActiveCount(card);
  return `<button type="button" class="rep-reset" data-reset="${card}"${n ? '' : ' hidden'}>Clear filters${n ? ` (${n})` : ''}</button>`;
}

function repBarHtml(card) {
  const s = REP[card];
  if (card === 'model') {
    return `<div class="rep-bar" id="rep-bar-model">
      <label class="rep-search${s.q ? ' active' : ''}">${REP_ICON_SEARCH}
        <input type="search" data-card="model" data-f="q" placeholder="Search model or provider" value="${esc(s.q)}" aria-label="Search models"></label>
      <div class="rep-group"><span class="rep-glabel">Sort</span>
        <select data-card="model" data-f="sort" aria-label="Sort by">${repOpts(REP_MODEL_SORTS, s.sort)}</select></div>
      <div class="rep-group"><span class="rep-glabel">Role</span>${repSeg('model', 'role', [['all', 'All'], ['main', 'Main'], ['orch', 'Orchestrator']], s.role)}</div>
      <div class="rep-group"><span class="rep-glabel">Ran on</span>${repSeg('model', 'source', [['all', 'All'], ['cloud', 'Cloud'], ['local', 'This PC']], s.source)}</div>
      ${repDateGroup('model', s)}
      ${repResetBtn('model')}
    </div>`;
  }
  return `<div class="rep-bar" id="rep-bar-day">
    ${repDateGroup('day', s)}
    <div class="rep-group"><span class="rep-glabel">Min requests</span>
      <input type="text" class="rep-min${s.minReq > 0 ? ' active' : ''}" inputmode="numeric" autocomplete="off" data-card="day" data-f="minReq" value="${s.minReq || ''}" placeholder="0" aria-label="Minimum requests"></div>
    <div class="rep-group"><button type="button" class="rep-chip rep-toggle${s.orch ? ' on' : ''}" data-toggle="orch" data-card="day" aria-pressed="${s.orch}">Orchestrator days only</button></div>
    <div class="rep-group"><span class="rep-glabel">Sort</span>
      <select data-card="day" data-f="sort" aria-label="Sort by">${repOpts(REP_DAY_SORTS, s.sort)}</select></div>
    ${repResetBtn('day')}
  </div>`;
}

/* keep the Clear filters button and the active outlines in step while someone types */
function repSyncBar(card) {
  const bar = $('rep-bar-' + card);
  if (!bar) return;
  const n = repActiveCount(card), b = bar.querySelector('[data-reset]');
  if (b) { b.hidden = !n; b.textContent = n ? `Clear filters (${n})` : 'Clear filters'; }
  const s = REP[card];
  const rng = bar.querySelector('.rep-range'); if (rng) rng.classList.toggle('active', !!(s.from || s.to));
  const q = bar.querySelector('.rep-search'); if (q) q.classList.toggle('active', !!(s.q || '').trim());
  const m = bar.querySelector('.rep-min'); if (m) m.classList.toggle('active', s.minReq > 0);
  bar.querySelectorAll('[data-preset]').forEach(c => {
    const r = repPresetRange(c.dataset.preset);
    c.classList.toggle('on', r.from === s.from && r.to === s.to);
  });
}

/* redraw one toolbar from state (buttons, chips, dates); not used while typing in a text box */
function repRedrawBar(card) {
  const bar = $('rep-bar-' + card);
  if (bar) bar.outerHTML = repBarHtml(card);
}

function repCardsHtml(d) {
  let h = '';
  if (d.by_model && d.by_model.length) {
    h += `
      <div class="rep-sub">
        <span>By Model</span>
        <span style="font-size:9.5px; font-weight:normal; text-transform:none; color:var(--dim);">Includes main &amp; orchestrator lanes</span>
      </div>
      ${repBarHtml('model')}
      <div id="rep-model-out"></div>`;
  }
  if (d.by_day && d.by_day.length) {
    h += `
      <div class="rep-sub">
        <span>By Day</span>
        <span style="font-size:9.5px; font-weight:normal; text-transform:none; color:var(--dim);">Daily usage &amp; cache hits</span>
      </div>
      ${repBarHtml('day')}
      <div id="rep-day-out"></div>`;
  }
  return h;
}

function repPager(card, total, s) {
  const pages = Math.max(1, Math.ceil(total / s.size));
  if (s.page >= pages) s.page = pages - 1;
  const from = total ? s.page * s.size + 1 : 0;
  const to = Math.min(total, (s.page + 1) * s.size);
  const nums = [];
  for (let p = 0; p < pages; p++) if (p === 0 || p === pages - 1 || Math.abs(p - s.page) <= 1) nums.push(p);
  let btns = '', prev = -1;
  nums.forEach(p => {
    if (prev >= 0 && p - prev > 1) btns += '<span class="rep-gap">…</span>';
    btns += `<button type="button" class="rep-pg${p === s.page ? ' on' : ''}" data-card="${card}" data-page="${p}" aria-label="Page ${p + 1}"${p === s.page ? ' aria-current="page"' : ''}>${p + 1}</button>`;
    prev = p;
  });
  return `<div class="rep-pager">
    <span class="rep-count">Showing ${repFmt(from)}-${repFmt(to)} of ${repFmt(total)}</span>
    <span class="rep-pages">
      <button type="button" class="rep-pg" data-card="${card}" data-page="${s.page - 1}" ${s.page === 0 ? 'disabled' : ''} aria-label="Previous page">‹</button>
      ${btns}
      <button type="button" class="rep-pg" data-card="${card}" data-page="${s.page + 1}" ${s.page >= pages - 1 ? 'disabled' : ''} aria-label="Next page">›</button>
    </span>
    <label class="rep-f">Rows <select data-card="${card}" data-f="size">${repOpts(REP_SIZES.map(n => [n, n]), s.size)}</select></label>
  </div>`;
}

function repRenderModels() {
  const out = $('rep-model-out');
  if (!out || !_repData) return;
  const s = REP.model, q = s.q.trim().toLowerCase();
  let rows = _repModels.filter(m => {
    if (q && !((m.model || '') + ' ' + (m.provider || '')).toLowerCase().includes(q)) return false;
    if (s.role === 'main' && m.is_orchestrator) return false;
    if (s.role === 'orch' && !m.is_orchestrator) return false;
    if (s.source === 'cloud' && m.source !== 'cloud') return false;
    if (s.source === 'local' && m.source === 'cloud') return false;
    return true;
  });
  const key = {
    total: m => m.total_tokens, req: m => m.requests, prompt: m => m.prompt_tokens, gen: m => m.completion_tokens,
    cache: m => (m.prompt_cached_tokens || 0) + (m.completion_cached_tokens || 0),
    hit: m => (m.prompt_tokens > 0 ? m.prompt_cached_tokens / m.prompt_tokens : 0),
  }[s.sort];
  rows = rows.slice().sort(key ? (a, b) => (key(b) || 0) - (key(a) || 0)
    : (a, b) => String(a.model || '').localeCompare(String(b.model || '')));
  const sum = rows.reduce((t, m) => t + (m.total_tokens || 0), 0);
  const page = rows.slice(s.page * s.size, (s.page + 1) * s.size);
  const body = page.map(m => {
    const roleBadge = m.is_orchestrator
      ? '<span class="badge-orch" title="Orchestrator sub-model">⚡ Orch</span>'
      : '<span class="badge-main" title="Main model">Main</span>';
    const rawName = (m.model || 'unknown').split('\\').pop().split('/').pop();
    const isCloud = m.source === 'cloud';
    const nameTitle = isCloud && m.provider ? `${m.model || ''} via ${m.provider} (cloud)` : (m.model || '');
    return `<tr>
      <td title="${esc(nameTitle)}">${isCloud ? '☁ ' : ''}${esc(rawName)}</td>
      <td>${roleBadge}</td>
      <td>${repFmt(m.requests)}</td>
      ${repTokCell(m.prompt_tokens)}
      ${repTokCell(m.prompt_cached_tokens, 'rep-cache-val')}
      <td class="dim">${repPct(m.prompt_cached_tokens, m.prompt_tokens)}</td>
      ${repTokCell(m.completion_tokens)}
      ${repTokCell(m.completion_cached_tokens, 'rep-gen-val')}
      <td title="${repFmt(m.total_tokens)}"><b>${repTok(m.total_tokens)}</b></td>
    </tr>`;
  }).join('');
  const stale = (s.from || s.to) && _repRangeIgnored
    ? '<div class="mon-empty">The server is running an older version and cannot filter models by date. Restart the server, then try again.</div>' : '';
  out.innerHTML = stale + (rows.length ? `
    <table class="rep-table">
      <tr><th>Model</th><th>Role</th><th>Req</th><th>Prompt</th><th title="Input tokens read from cache">In Cache</th>
        <th title="Share of prompt tokens served from cache">Hit</th><th>Gen</th>
        <th title="Output tokens read from cache">Out Cache</th><th>Total</th></tr>${body}
    </table>
    <div class="rep-sum">${repFmt(rows.length)} model${rows.length === 1 ? '' : 's'} · ${repTok(sum)} tokens in this selection</div>
    ${repPager('model', rows.length, s)}`
    : '<div class="mon-empty">No models match these filters. <button type="button" class="rep-reset" data-reset="model">Reset filters</button></div>');
}

function repRenderDays() {
  const out = $('rep-day-out');
  if (!out || !_repData) return;
  const s = REP.day;
  let rows = (_repData.by_day || []).filter(x => {
    if (s.from && x.day < s.from) return false;
    if (s.to && x.day > s.to) return false;
    if (s.minReq && (x.requests || 0) < s.minReq) return false;
    if (s.orch && !(x.orchestrator_requests > 0)) return false;
    return true;
  });
  const key = {
    total: x => x.total_tokens, req: x => x.requests,
    hit: x => (x.prompt_tokens > 0 ? x.prompt_cached_tokens / x.prompt_tokens : 0),
  }[s.sort];
  rows = rows.slice().sort(key ? (a, b) => (key(b) || 0) - (key(a) || 0)
    : s.sort === 'oldest' ? (a, b) => a.day.localeCompare(b.day) : (a, b) => b.day.localeCompare(a.day));
  const sum = rows.reduce((t, x) => t + (x.total_tokens || 0), 0);
  const page = rows.slice(s.page * s.size, (s.page + 1) * s.size);
  const body = page.map(x => `<tr>
      <td>${esc(x.day)}</td>
      <td>${repFmt(x.requests)}</td>
      <td style="color:#f59e0b;">${repFmt(x.orchestrator_requests)}</td>
      ${repTokCell(x.prompt_tokens)}
      ${repTokCell(x.prompt_cached_tokens, 'rep-cache-val')}
      <td class="dim">${repPct(x.prompt_cached_tokens, x.prompt_tokens)}</td>
      ${repTokCell(x.completion_tokens)}
      ${repTokCell(x.completion_cached_tokens, 'rep-gen-val')}
      <td title="${repFmt(x.total_tokens)}"><b>${repTok(x.total_tokens)}</b></td>
    </tr>`).join('');
  out.innerHTML = rows.length ? `
    <table class="rep-table">
      <tr><th>Day</th><th>Req</th><th title="Orchestrator requests">Orch</th><th>Prompt</th>
        <th title="Input tokens read from cache">In Cache</th><th title="Share of prompt tokens served from cache">Hit</th>
        <th>Gen</th><th title="Output tokens read from cache">Out Cache</th><th>Total</th></tr>${body}
    </table>
    <div class="rep-sum">${repFmt(rows.length)} day${rows.length === 1 ? '' : 's'} · ${repTok(sum)} tokens in this selection</div>
    ${repPager('day', rows.length, s)}`
    : '<div class="mon-empty">No days match these filters. <button type="button" class="rep-reset" data-reset="day">Reset filters</button></div>';
}

function repOnFilter(e) {
  const t = e.target, card = t.dataset && t.dataset.card, f = t.dataset && t.dataset.f;
  if (!card || !f || !REP[card]) return;
  const s = REP[card];
  if (f === 'minReq') {                       // digits only; the box always shows what is applied
    const digits = t.value.replace(/\D/g, '');
    if (digits !== t.value) t.value = digits;
  }
  s[f] = t.type === 'checkbox' ? t.checked
    : (f === 'size' || f === 'minReq') ? Math.max(0, parseInt(t.value, 10) || 0) : t.value;
  if (f === 'size' && !s.size) s.size = 10;
  s.page = 0;
  repSyncBar(card);
  if (card === 'model' && (f === 'from' || f === 'to')) {
    // the server regroups the models for these exact days; wait for the typing to settle
    clearTimeout(_repModelTimer);
    _repModelTimer = setTimeout(async () => { await repFetchModels(); repRenderModels(); }, 250);
    return;
  }
  if (card === 'model') repRenderModels(); else repRenderDays();
}

function repOnClick(e) {
  const seg = e.target.closest('[data-seg]');
  if (seg) {
    const card = seg.dataset.card;
    REP[card][seg.dataset.f] = seg.dataset.v;
    REP[card].page = 0;
    repRedrawBar(card);
    if (card === 'model') repRenderModels(); else repRenderDays();
    return;
  }
  const pre = e.target.closest('[data-preset]');
  if (pre) {
    const card = pre.dataset.card;
    Object.assign(REP[card], repPresetRange(pre.dataset.preset), { page: 0 });
    repRedrawBar(card);
    if (card === 'model') repFetchModels().then(repRenderModels); else repRenderDays();
    return;
  }
  const tog = e.target.closest('[data-toggle]');
  if (tog) {
    const card = tog.dataset.card;
    REP[card].orch = !REP[card].orch;
    REP[card].page = 0;
    repRedrawBar(card);
    repRenderDays();
    return;
  }
  const reset = e.target.closest('[data-reset]');
  if (reset) {
    const card = reset.dataset.reset;
    Object.assign(REP[card], REP_DEFAULTS[card]);
    if (_repData) {
      _repModels = _repData.by_model || [];
      // redraw the filter bars with their default values; the summary cards stay
      $('rep-tables').innerHTML = repCardsHtml(_repData);
      repRenderModels();
      repRenderDays();
    }
    return;
  }
  const pg = e.target.closest('[data-page]');
  if (pg && !pg.disabled) {
    const card = pg.dataset.card;
    REP[card].page = Math.max(0, parseInt(pg.dataset.page, 10) || 0);
    if (card === 'model') repRenderModels(); else repRenderDays();
  }
}

$('btn-report').onclick = () => {
  $('report-modal').hidden = false;
  _repData = null;                       // reread the tables' rows each time the report is opened
  $('rep-content').innerHTML = '';
  loadReport(30);
};
$('rep-close').onclick = () => { $('report-modal').hidden = true; };
$('report-modal').onclick = e => { if (e.target.id === 'report-modal') $('report-modal').hidden = true; };

/* ---------------- agent docs modal ---------------- */
$('btn-docs').onclick = () => {
  // fill in the currently loaded model's exact ID (what agents must use)
  const mid = $('doc-model-id');
  if (curStatus && curStatus.pid && curStatus.model) {
    mid.textContent = curStatus.model;
  } else if (curStatus && curStatus.model) {
    mid.textContent = curStatus.model;
  } else {
    mid.textContent = '(load a model first — its path becomes the Model ID)';
  }
  // also update the example snippet with the real id
  const ex = $('doc-example');
  const selected = $('profile') && $('profile').value ? $('profile').value.split('\\').pop().split('/').pop() : null;
  if (curStatus && curStatus.model) {
    ex.textContent = ex.textContent.replace(/model="[^"]*"/, `model="${curStatus.model}"`);
  } else if (selected) {
    ex.textContent = ex.textContent.replace(/model="[^"]*"/, `model="E:\\AI\\Models\\${selected}"`);
  }
  $('docs-modal').hidden = false;
};
$('docs-close').onclick = () => { $('docs-modal').hidden = true; };
$('docs-modal').onclick = e => { if (e.target.id === 'docs-modal') $('docs-modal').hidden = true; };
document.querySelectorAll('.doc-copy-btn').forEach(b => {
  b.onclick = () => {
    const txt = $(b.dataset.copy).textContent;
    if (!txt || txt.startsWith('(')) { toast('Nothing to copy yet', true); return; }
    navigator.clipboard.writeText(txt).then(
      () => { b.textContent = '✓ Copied'; setTimeout(() => b.textContent = '📋 Copy', 1200); toast('Copied to clipboard'); },
      () => toast('Copy failed', true)
    );
  };
});
document.querySelectorAll('.rep-day').forEach(b => {
  b.onclick = () => {
    document.querySelectorAll('.rep-day').forEach(x => x.className = 'btn ghost rep-day');
    b.className = 'btn blue rep-day';
    loadReport(b.dataset.days);
  };
});
