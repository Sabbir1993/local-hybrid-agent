/* speech-corpus.js - admin panel for the public voice-collection page (settings.html #sec-speech).
 * GET /control/speech/status|stats, POST /control/speech/enabled|code|prompts|contributor/delete; needs speech.manage.
 * Deleting a speaker needs the MFA step-up (withStepUp from mfa-panel.js). */
'use strict';

async function speechFetch(url, opts) {
  const r = await fetch(url, opts);
  const d = await r.json().catch(() => ({}));
  return { r, d };
}

function speechPost(url, body) {
  return speechFetch(url, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body || {}) });
}

async function loadSpeechCorpusPanel() {
  const box = document.getElementById('speech-corpus-content');
  if (!box) return;
  const { r, d: st } = await speechFetch('/control/speech/status').catch(() => ({ r: { ok: false }, d: {} }));
  if (!r.ok) {
    box.innerHTML = '<div class="mon-empty">You do not have permission to manage voice collection.</div>';
    return;
  }
  box.innerHTML = `
    <div style="padding:10px 4px;display:flex;flex-direction:column;gap:16px;max-width:640px">
      <label style="display:flex;align-items:center;gap:8px;font-size:13px">
        <input type="checkbox" id="vc-enabled" ${st.enabled ? 'checked' : ''}>
        <span><strong>Enable voice collection page</strong> &nbsp;<code>${esc(st.path)}</code></span>
      </label>
      <div class="dim" style="font-size:12px;line-height:1.5">
        When on, anyone with the current link can open the page, enter their name and employee ID, and record lines.
        When off, the page and its API answer "not found" and open sessions stop at once.
      </div>
      <div style="font-size:12px">
        <div><span class="dim">Corpus folder:</span> <code>${esc(st.dir || '(not set)')}</code></div>
        ${st.dir_error ? `<div style="color:var(--red,#f87171);margin-top:4px">${esc(st.dir_error)}</div>` : ''}
        <div class="dim" style="margin-top:4px">Set <code>speech_corpus.dir</code> in config/app.json (an absolute folder outside the app). ${st.lines_per_session} lines per session, clips up to ${st.max_clip_seconds} s, ${st.sessions_per_day} sessions per person per day.</div>
      </div>
      <div style="display:flex;flex-direction:column;gap:6px">
        <div style="display:flex;align-items:center;gap:10px;flex-wrap:wrap">
          <button type="button" class="btn" id="vc-rotate" style="width:auto;padding:6px 14px">${st.has_code ? 'Create new link (old link stops working)' : 'Create link'}</button>
          <span class="dim" style="font-size:12px">${st.has_code ? 'A link exists. Only a new one can be shown.' : 'No link yet.'}</span>
        </div>
        <div id="vc-link" style="display:none;gap:8px;align-items:center;flex-wrap:wrap">
          <input type="text" id="vc-link-text" readonly style="flex:1;min-width:280px;font-family:monospace;font-size:12px">
          <button type="button" class="btn" id="vc-copy" style="width:auto;padding:6px 14px">Copy link</button>
          <span class="dim" style="font-size:11px;width:100%">Shown once. Share it only inside the company.</span>
        </div>
      </div>
      <div>
        <div style="font-weight:600;font-size:13px;margin-bottom:6px">Lines people read</div>
        <div class="dim" style="font-size:12px;margin-bottom:6px">A .jsonl file, one line each: <code>{"text": "...", "category": "standard|domain|banglish|numbers"}</code>. Lines with card-like numbers are refused.</div>
        <input type="file" id="vc-prompts" accept=".jsonl,.json,.txt">
      </div>
      <div>
        <div style="font-weight:600;font-size:13px;margin-bottom:6px">Collected so far</div>
        <div id="vc-stats" class="dim" style="font-size:12px">Loading…</div>
      </div>
      <div>
        <div style="font-weight:600;font-size:13px;margin-bottom:6px">Delete a speaker</div>
        <div class="dim" style="font-size:12px;margin-bottom:6px">Removes all of that person's recordings and their folder, for example when they withdraw consent. Cannot be undone.</div>
        <div style="display:flex;gap:8px;align-items:center;flex-wrap:wrap">
          <input type="text" id="vc-del-id" placeholder="Employee ID" style="width:180px">
          <button type="button" class="btn" id="vc-del" style="width:auto;padding:6px 14px">Delete</button>
        </div>
      </div>
      <div id="vc-msg" class="dim" style="font-size:12px;line-height:1.5"></div>
    </div>`;
  const msg = box.querySelector('#vc-msg');
  const say = (t, bad) => { msg.textContent = t; msg.style.color = bad ? 'var(--red,#f87171)' : ''; };

  box.querySelector('#vc-enabled').addEventListener('change', async (e) => {
    const want = e.target.checked;
    const { r: rr, d } = await speechPost('/control/speech/enabled', { enabled: want });
    if (!rr.ok) {
      e.target.checked = !want;
      say(d.error || d.detail || 'Could not change it.', true);
      return;
    }
    say(want ? 'Voice collection is on.' : 'Voice collection is off.');
  });

  box.querySelector('#vc-rotate').addEventListener('click', async () => {
    if (st.has_code && !confirm('Create a new link? The current link stops working immediately.')) return;
    const { r: rr, d } = await speechPost('/control/speech/code', {});
    if (!rr.ok) { say(d.error || d.detail || 'Could not create a link.', true); return; }
    st.has_code = true;
    const wrap = box.querySelector('#vc-link');
    wrap.style.display = 'flex';
    box.querySelector('#vc-link-text').value = window.location.origin + d.link_path;
    say('New link created.');
  });

  box.querySelector('#vc-copy').addEventListener('click', async () => {
    const t = box.querySelector('#vc-link-text');
    try { await navigator.clipboard.writeText(t.value); say('Link copied.'); } catch (_) { t.select(); say('Press Ctrl+C to copy.'); }
  });

  box.querySelector('#vc-prompts').addEventListener('change', async (e) => {
    const f = e.target.files && e.target.files[0];
    if (!f) return;
    say('Importing…');
    const text = await f.text();
    const { r: rr, d } = await speechPost('/control/speech/prompts', { jsonl: text });
    e.target.value = '';
    if (!rr.ok) { say(d.error || d.detail || 'Import failed.', true); return; }
    const bad = (d.rejected || []).slice(0, 5).map((x) => `line ${x.line}: ${x.reason}`).join('; ');
    say(`Added ${d.added}, already there ${d.duplicates}, refused ${d.rejected_count}${bad ? ' (' + bad + ')' : ''}.`, d.rejected_count > 0);
    loadSpeechStats(box);
  });

  box.querySelector('#vc-del').addEventListener('click', async () => {
    const id = box.querySelector('#vc-del-id').value.trim();
    if (!id) return;
    if (!confirm(`Delete every recording of employee ${id}? This cannot be undone.`)) return;
    const res = await withStepUp(box, msg, () => speechPost('/control/speech/contributor/delete', { employee_id: id }));
    if (!res.r.ok) { say(res.d.error || res.d.detail || 'Could not delete.', true); return; }
    say(`Deleted ${res.d.recordings_deleted} recordings of speaker ${res.d.speaker_code}.`);
    box.querySelector('#vc-del-id').value = '';
    loadSpeechStats(box);
  });

  loadSpeechStats(box);
}

async function loadSpeechStats(box) {
  const out = box.querySelector('#vc-stats');
  const { r, d } = await speechFetch('/control/speech/stats').catch(() => ({ r: { ok: false }, d: {} }));
  if (!r.ok) { out.textContent = d.error || 'Statistics are not available until the corpus folder is set.'; return; }
  const rows = (d.by_region || []).map((x) =>
    `<tr><td>${esc(x.label)}</td><td style="text-align:right">${x.speakers}</td><td style="text-align:right">${x.clips}</td><td style="text-align:right">${x.hours}</td></tr>`).join('');
  const cats = (d.by_category || []).map((x) => `${esc(x.category)}: ${x.clips} clips / ${x.hours} h`).join(' · ');
  out.innerHTML = `
    <div>${d.speakers} speakers · ${d.clips} clips · ${d.hours} hours · ${d.prompts_active} lines (${d.prompts_unrecorded} not recorded yet)${d.flagged ? ` · ${d.flagged} flagged` : ''}</div>
    ${rows ? `<table style="margin-top:8px;font-size:12px;border-collapse:collapse;min-width:360px">
      <tr class="dim"><th style="text-align:left">How they speak</th><th style="text-align:right">Speakers</th><th style="text-align:right">Clips</th><th style="text-align:right">Hours</th></tr>${rows}</table>` : ''}
    ${cats ? `<div style="margin-top:6px">${cats}</div>` : ''}`;
}

window.loadSpeechCorpusPanel = loadSpeechCorpusPanel;
