/* speech-review.js - Recordings report inside Settings → Voice Collection (settings.html #speech-review-content): who recorded, every clip with its
 * line and a player, filters, paging, and mark OK / Flag. Needs speech.manage.
 * GET /control/speech/contributors, /control/speech/recordings, /control/speech/recordings/<id>/audio;
 * POST /control/speech/recordings/<id>/status. */
'use strict';

const SR_REGIONS = {
  standard: 'Standard', ctg_noakhali: 'Chattogram / Noakhali', sylhet: 'Sylhet',
  barishal_khulna: 'Barishal / Khulna', rangpur_mymensingh_dhaka: 'Rangpur / Mymensingh / Dhaka',
};
const SR_CATEGORIES = ['standard', 'domain', 'banglish', 'numbers'];
const SR_PAGE = 25;
const SR_SPEAKER_PAGE = 10;

async function srGet(url) {
  const r = await fetch(url, { cache: 'no-store' });
  const d = await r.json().catch(() => ({}));
  return { r, d };
}

function srWhen(ep) {
  if (!ep) return '—';
  return new Date(ep * 1000).toLocaleString(undefined, { day: 'numeric', month: 'short', hour: '2-digit', minute: '2-digit' });
}

function srQuery(obj) {
  return Object.entries(obj).filter(([, v]) => v !== '' && v !== 0 && v != null)
    .map(([k, v]) => `${encodeURIComponent(k)}=${encodeURIComponent(v)}`).join('&');
}

async function loadSpeechReviewPanel() {
  const box = document.getElementById('speech-review-content');
  if (!box) return;
  const probe = await srGet('/control/speech/contributors?limit=1').catch(() => ({ r: { ok: false, status: 0 }, d: {} }));
  if (!probe.r.ok) {
    box.innerHTML = `<div class="mon-empty">${probe.r.status === 403 || probe.r.status === 401
      ? 'You do not have permission to review voice recordings.'
      : esc(probe.d.error || 'Recordings are not available until the corpus folder is set (Settings → Voice Collection).')}</div>`;
    return;
  }
  const opt = (v, label, cur) => `<option value="${esc(v)}" ${v === cur ? 'selected' : ''}>${esc(label)}</option>`;
  box.innerHTML = `
    <div style="padding:10px 4px;display:flex;flex-direction:column;gap:18px">
      <div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(170px,1fr));gap:10px;align-items:end">
        <label style="display:flex;flex-direction:column;gap:4px">Search
          <input type="search" id="sr-q" placeholder="Name, employee ID or words of the line">
        </label>
        <label style="display:flex;flex-direction:column;gap:4px">Dialect
          <select id="sr-region">${opt('', 'All dialects', '')}${Object.entries(SR_REGIONS).map(([k, v]) => opt(k, v, '')).join('')}</select>
        </label>
        <label style="display:flex;flex-direction:column;gap:4px">Category
          <select id="sr-category">${opt('', 'All categories', '')}${SR_CATEGORIES.map((c) => opt(c, c, '')).join('')}</select>
        </label>
        <label style="display:flex;flex-direction:column;gap:4px">Status
          <select id="sr-status">${opt('', 'OK and flagged', '')}${opt('ok', 'OK only', '')}${opt('flagged', 'Flagged only', '')}</select>
        </label>
      </div>

      <div>
        <div style="display:flex;align-items:center;justify-content:space-between;gap:8px;margin-bottom:8px;flex-wrap:wrap">
          <div style="font-weight:600;font-size:13px">Speakers</div>
          <div id="sr-sp-pager" style="display:flex;gap:6px;align-items:center;font-size:12px"></div>
        </div>
        <div style="overflow-x:auto"><table id="sr-speakers" style="width:100%;border-collapse:collapse;font-size:12.5px"></table></div>
      </div>

      <div>
        <div style="display:flex;align-items:center;justify-content:space-between;gap:8px;margin-bottom:8px;flex-wrap:wrap">
          <div style="display:flex;align-items:center;gap:8px;flex-wrap:wrap">
            <span style="font-weight:600;font-size:13px">Recordings</span>
            <span id="sr-chip"></span>
          </div>
          <div id="sr-pager" style="display:flex;gap:6px;align-items:center;font-size:12px"></div>
        </div>
        <div id="sr-list" style="display:flex;flex-direction:column;gap:8px"></div>
      </div>
      <div id="sr-msg" class="dim" style="font-size:12px"></div>
    </div>`;

  const S = { q: '', region: '', category: '', status: '', contributor: 0, contributorLabel: '', offset: 0, spOffset: 0 };
  const $ = (sel) => box.querySelector(sel);
  const say = (t, bad) => { const m = $('#sr-msg'); m.textContent = t; m.style.color = bad ? 'var(--red,#f87171)' : ''; };

  const pager = (el, offset, total, size, go) => {
    const from = total ? offset + 1 : 0;
    const to = Math.min(total, offset + size);
    el.innerHTML = `<span class="dim">${from}–${to} of ${total}</span>
      <button type="button" class="btn" data-p="prev" style="width:auto;padding:3px 10px" ${offset <= 0 ? 'disabled' : ''}>‹ Prev</button>
      <button type="button" class="btn" data-p="next" style="width:auto;padding:3px 10px" ${to >= total ? 'disabled' : ''}>Next ›</button>`;
    el.querySelector('[data-p="prev"]').onclick = () => go(Math.max(0, offset - size));
    el.querySelector('[data-p="next"]').onclick = () => go(offset + size);
  };

  const th = (t, right) => `<th style="text-align:${right ? 'right' : 'left'};padding:6px 8px;font-weight:600;border-bottom:1px solid var(--sp-line-strong,#3a4150)" class="dim">${t}</th>`;
  const td = (t, right, extra) => `<td style="padding:7px 8px;border-bottom:1px solid var(--sp-line,#2a303b);text-align:${right ? 'right' : 'left'};${extra || ''}">${t}</td>`;

  async function loadSpeakers() {
    const { r, d } = await srGet('/control/speech/contributors?' + srQuery({ q: S.q, region: S.region, offset: S.spOffset, limit: SR_SPEAKER_PAGE }));
    const t = $('#sr-speakers');
    if (!r.ok) { t.innerHTML = `<tr><td class="dim">${esc(d.error || 'Could not load speakers.')}</td></tr>`; return; }
    if (!d.items.length) { t.innerHTML = '<tr><td class="dim" style="padding:8px">No speakers yet.</td></tr>'; $('#sr-sp-pager').innerHTML = ''; return; }
    t.innerHTML = `<tr>${th('Speaker')}${th('Employee ID')}${th('Dialect')}${th('Clips', 1)}${th('Minutes', 1)}${th('Flagged', 1)}${th('Last upload')}${th('')}</tr>` +
      d.items.map((c) => `<tr>
        ${td(`<strong>${esc(c.name)}</strong> <span class="dim">${esc(c.speaker_code)}</span>`)}
        ${td(esc(c.employee_id), 0, 'font-family:var(--sp-mono,monospace)')}
        ${td(esc(SR_REGIONS[c.region] || c.region) + (c.district ? ` <span class="dim">· ${esc(c.district)}</span>` : ''))}
        ${td(c.clips || 0, 1)}${td(c.minutes || 0, 1)}
        ${td(c.flagged ? `<span style="color:var(--orange,#f59e0b)">${c.flagged}</span>` : '0', 1)}
        ${td(srWhen(c.last_upload))}
        ${td(`<button type="button" class="btn" data-sp="${c.id}" data-label="${esc(c.name)} (${esc(c.employee_id)})" style="width:auto;padding:3px 10px">Recordings</button>`, 1)}
      </tr>`).join('');
    t.querySelectorAll('[data-sp]').forEach((b) => b.addEventListener('click', () => {
      S.contributor = Number(b.getAttribute('data-sp'));
      S.contributorLabel = b.getAttribute('data-label');
      S.offset = 0;
      loadRecordings();
      $('#sr-list').scrollIntoView({ behavior: 'smooth', block: 'start' });
    }));
    pager($('#sr-sp-pager'), S.spOffset, d.total, SR_SPEAKER_PAGE, (o) => { S.spOffset = o; loadSpeakers(); });
  }

  const statusBadge = (st) => st === 'flagged'
    ? '<span style="color:rgb(245,158,11);background:rgba(245,158,11,.14);padding:2px 8px;border-radius:999px;font-size:11px;font-weight:600">Flagged</span>'
    : '<span style="color:rgb(74,222,128);background:rgba(74,222,128,.14);padding:2px 8px;border-radius:999px;font-size:11px;font-weight:600">OK</span>';

  async function loadRecordings() {
    const chip = $('#sr-chip');
    chip.innerHTML = S.contributor
      ? `<span style="font-size:12px;padding:3px 8px;border-radius:999px;border:1px solid var(--sp-line-strong,#3a4150)">Speaker: ${esc(S.contributorLabel)} <button type="button" class="btn" id="sr-chip-x" style="width:auto;padding:0 6px;margin-left:4px;min-height:0;border:none" title="Show everyone">✕</button></span>`
      : '';
    if (S.contributor) $('#sr-chip-x').onclick = () => { S.contributor = 0; S.offset = 0; loadRecordings(); };
    const { r, d } = await srGet('/control/speech/recordings?' + srQuery({
      contributor: S.contributor, q: S.q, region: S.region, category: S.category, status: S.status, offset: S.offset, limit: SR_PAGE }));
    const list = $('#sr-list');
    if (!r.ok) { list.innerHTML = `<div class="dim">${esc(d.error || 'Could not load recordings.')}</div>`; return; }
    if (!d.items.length) { list.innerHTML = '<div class="dim" style="padding:8px 0">No recordings match.</div>'; $('#sr-pager').innerHTML = ''; return; }
    list.innerHTML = d.items.map((x) => {
      const hints = [];
      if (x.rms < 0.02) hints.push('quiet');
      if (x.clipped_ratio > 0.002) hints.push('a little loud');
      return `<div data-row="${x.id}" style="border:1px solid var(--sp-line-strong,#3a4150);border-radius:10px;padding:10px 12px;display:flex;flex-direction:column;gap:8px;${x.status === 'flagged' ? 'border-color:rgba(245,158,11,.6)' : ''}">
        <div style="display:flex;gap:8px;align-items:center;flex-wrap:wrap;font-size:12px">
          <strong>${esc(x.name)}</strong><span class="dim">${esc(x.employee_id)} · ${esc(x.speaker_code)}</span>
          <span class="dim">· ${esc(SR_REGIONS[x.region] || x.region)} · ${esc(x.category)}${x.instruction === 'dialect' ? ' · said in dialect' : ''}</span>
          <span class="dim" style="margin-left:auto">${srWhen(x.created_at)} · ${Number(x.seconds).toFixed(1)} s${hints.length ? ' · ' + hints.join(', ') : ''}</span>
        </div>
        <div lang="bn" style="font-size:15px;line-height:1.5">${esc(x.prompt_text)}</div>
        <div style="display:flex;gap:8px;align-items:center;flex-wrap:wrap">
          <audio controls preload="none" src="/control/speech/recordings/${x.id}/audio" style="height:34px;flex:1 1 260px;min-width:220px;max-width:420px"></audio>
          <span data-badge>${statusBadge(x.status)}</span>
          <span style="margin-left:auto;display:flex;gap:6px">
            <button type="button" class="btn" data-set="ok" style="width:auto;padding:4px 12px" ${x.status === 'ok' ? 'disabled' : ''}>✓ OK</button>
            <button type="button" class="btn" data-set="flagged" style="width:auto;padding:4px 12px" ${x.status === 'flagged' ? 'disabled' : ''}>⚑ Flag</button>
          </span>
        </div>
      </div>`;
    }).join('');
    list.querySelectorAll('[data-row]').forEach((row) => {
      const id = row.getAttribute('data-row');
      row.querySelectorAll('[data-set]').forEach((b) => b.addEventListener('click', async () => {
        const status = b.getAttribute('data-set');
        const r2 = await fetch(`/control/speech/recordings/${id}/status`, {
          method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ status }) });
        const d2 = await r2.json().catch(() => ({}));
        if (!r2.ok) { say(d2.error || d2.detail || 'Could not change it.', true); return; }
        row.querySelector('[data-badge]').innerHTML = statusBadge(status);
        row.style.borderColor = status === 'flagged' ? 'rgba(245,158,11,.6)' : '';
        row.querySelectorAll('[data-set]').forEach((x) => { x.disabled = x.getAttribute('data-set') === status; });
        say(status === 'flagged' ? 'Flagged: it is left out of training exports that keep only OK clips.' : 'Marked OK.');
      }));
    });
    // one clip plays at a time
    list.querySelectorAll('audio').forEach((a) => a.addEventListener('play', () => {
      list.querySelectorAll('audio').forEach((o) => { if (o !== a) o.pause(); });
    }));
    pager($('#sr-pager'), S.offset, d.total, SR_PAGE, (o) => { S.offset = o; loadRecordings(); });
  }

  let timer = 0;
  const refilter = () => { S.offset = 0; S.spOffset = 0; loadSpeakers(); loadRecordings(); };
  $('#sr-q').addEventListener('input', (e) => { clearTimeout(timer); timer = setTimeout(() => { S.q = e.target.value.trim(); refilter(); }, 300); });
  $('#sr-region').addEventListener('change', (e) => { S.region = e.target.value; refilter(); });
  $('#sr-category').addEventListener('change', (e) => { S.category = e.target.value; S.offset = 0; loadRecordings(); });
  $('#sr-status').addEventListener('change', (e) => { S.status = e.target.value; S.offset = 0; loadRecordings(); });

  loadSpeakers();
  loadRecordings();
}

window.loadSpeechReviewPanel = loadSpeechReviewPanel;
