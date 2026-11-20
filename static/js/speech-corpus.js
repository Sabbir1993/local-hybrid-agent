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
      <div style="display:flex;flex-direction:column;gap:8px">
        <div style="font-weight:600;font-size:13px">Link for colleagues</div>
        <div id="vc-link" style="display:${st.link_path ? 'flex' : 'none'};gap:8px;align-items:center;flex-wrap:wrap">
          <input type="text" id="vc-link-text" readonly value="${esc(st.link_path ? window.location.origin + st.link_path : '')}" style="flex:1;min-width:280px;font-family:monospace;font-size:12px">
          <button type="button" class="btn accent" id="vc-copy" style="width:auto;padding:6px 14px">Copy link</button>
        </div>
        <div id="vc-link-note" class="dim" style="font-size:12px">${
          st.link_path ? 'Share it only inside the company.'
            : st.has_code ? 'A link exists, but this server cannot show it (its keychain is unavailable). Create a new link to see one; the old link then stops working.'
              : 'No link yet.'}</div>
        <div>
          <button type="button" class="btn" id="vc-rotate" style="width:auto;padding:6px 14px">${st.has_code ? 'Create new link (old link stops working)' : 'Create link'}</button>
        </div>
      </div>
      <div>
        <div style="display:flex;align-items:center;justify-content:space-between;gap:10px;flex-wrap:wrap;margin-bottom:6px">
          <div style="font-weight:600;font-size:13px">Lines people read</div>
          <button type="button" class="btn" id="vc-sample" style="width:auto;padding:6px 14px">⬇ Download sample file</button>
        </div>
        <div class="dim" style="font-size:12px;line-height:1.6;margin-bottom:8px">
          A <code>.jsonl</code> file with one line per row: <code>{"text": "...", "category": "standard"}</code>.<br>
          Categories: <strong>standard</strong> everyday Bangla · <strong>domain</strong> payments and support ·
          <strong>banglish</strong> Bangla and English mixed · <strong>numbers</strong> amounts, dates, times.
          Lines already imported are skipped; lines with card-like numbers are refused.
        </div>
        <label id="vc-drop" for="vc-prompts" style="display:flex;flex-direction:column;align-items:center;justify-content:center;gap:4px;padding:18px 12px;border:1.5px dashed var(--border,#3a4150);border-radius:10px;cursor:pointer;text-align:center;transition:border-color .15s,background .15s">
          <input type="file" id="vc-prompts" accept=".jsonl,.json,.txt" style="display:none">
          <span style="font-size:22px;line-height:1">📄</span>
          <span style="font-size:13px">Drop a <code>.jsonl</code> file here, or <span style="text-decoration:underline">choose a file</span></span>
        </label>
        <div id="vc-file" style="display:none;margin-top:8px;padding:10px 12px;border:1px solid var(--border,#3a4150);border-radius:10px;font-size:12px">
          <div style="display:flex;align-items:center;gap:10px;flex-wrap:wrap">
            <span style="font-weight:600" id="vc-file-name"></span>
            <span class="dim" id="vc-file-info"></span>
            <span style="margin-left:auto;display:flex;gap:8px">
              <button type="button" class="btn" id="vc-file-clear" style="width:auto;padding:6px 12px">Remove</button>
              <button type="button" class="btn accent" id="vc-import" style="width:auto;padding:6px 14px">Import lines</button>
            </span>
          </div>
          <div class="dim" id="vc-file-preview" style="margin-top:6px;line-height:1.6"></div>
        </div>
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
    box.querySelector('#vc-link').style.display = 'flex';
    box.querySelector('#vc-link-text').value = window.location.origin + d.link_path;
    box.querySelector('#vc-rotate').textContent = 'Create new link (old link stops working)';
    box.querySelector('#vc-link-note').textContent = d.link_saved === false
      ? 'Copy it now: this server cannot store it (its keychain is unavailable), so it will not be shown again.'
      : 'Share it only inside the company. The old link no longer works.';
    say('New link created.');
  });

  box.querySelector('#vc-copy').addEventListener('click', async () => {
    const t = box.querySelector('#vc-link-text');
    try { await navigator.clipboard.writeText(t.value); say('Link copied.'); } catch (_) { t.select(); say('Press Ctrl+C to copy.'); }
  });

  box.querySelector('#vc-sample').addEventListener('click', async () => {
    try {
      const rr = await fetch('/control/speech/prompts/sample');
      if (!rr.ok) { const d = await rr.json().catch(() => ({})); say(d.error || d.detail || 'Could not download the sample.', true); return; }
      const url = URL.createObjectURL(await rr.blob());
      const a = document.createElement('a');
      a.href = url; a.download = 'voice-lines-sample.jsonl';
      document.body.appendChild(a); a.click(); a.remove();
      setTimeout(() => URL.revokeObjectURL(url), 5000);
    } catch (_) {
      say('Could not download the sample.', true);
    }
  });

  // import: pick or drop a file, see what is in it, then import
  const input = box.querySelector('#vc-prompts');
  const drop = box.querySelector('#vc-drop');
  const card = box.querySelector('#vc-file');
  let picked = null;
  const clearPick = () => { picked = null; input.value = ''; card.style.display = 'none'; drop.style.display = 'flex'; };
  const showPick = async (f) => {
    if (!f) return;
    if (f.size > 8 * 1024 * 1024) { say('That file is larger than 8 MB.', true); return; }
    const text = await f.text();
    const rows = text.split(/\r?\n/).filter((l) => l.trim());
    const cats = {};
    let unreadable = 0;
    const sample = [];
    for (const row of rows) {
      try {
        const o = JSON.parse(row);
        const c = String(o.category || '?');
        cats[c] = (cats[c] || 0) + 1;
        if (sample.length < 3 && o.text) sample.push(String(o.text));
      } catch (_) { unreadable++; }
    }
    picked = { name: f.name, text };
    box.querySelector('#vc-file-name').textContent = f.name;
    box.querySelector('#vc-file-info').textContent = `${rows.length} lines · ` +
      Object.entries(cats).map(([c, n]) => `${c} ${n}`).join(' · ') + (unreadable ? ` · ${unreadable} unreadable` : '');
    box.querySelector('#vc-file-preview').innerHTML = sample.map((t) => '“' + esc(t) + '”').join('<br>');
    drop.style.display = 'none';
    card.style.display = 'block';
    say('');
  };
  input.addEventListener('change', () => showPick(input.files && input.files[0]));
  const hot = (on) => { drop.style.borderColor = on ? 'var(--accent,#4f8cff)' : ''; drop.style.background = on ? 'rgba(79,140,255,0.06)' : ''; };
  drop.addEventListener('dragover', (e) => { e.preventDefault(); hot(true); });
  drop.addEventListener('dragleave', () => hot(false));
  drop.addEventListener('drop', (e) => { e.preventDefault(); hot(false); showPick(e.dataTransfer.files && e.dataTransfer.files[0]); });
  box.querySelector('#vc-file-clear').addEventListener('click', clearPick);
  box.querySelector('#vc-import').addEventListener('click', async () => {
    if (!picked) return;
    const btn = box.querySelector('#vc-import');
    btn.disabled = true;
    say('Importing…');
    const { r: rr, d } = await speechPost('/control/speech/prompts', { jsonl: picked.text });
    btn.disabled = false;
    if (!rr.ok) { say(d.error || d.detail || 'Import failed.', true); return; }
    clearPick();
    const bad = (d.rejected || []).slice(0, 5).map((x) => `line ${x.line}: ${x.reason}`).join('; ');
    say(`Added ${d.added} new lines, ${d.duplicates} were already there, ${d.rejected_count} refused${bad ? ' (' + bad + ')' : ''}.`, d.rejected_count > 0);
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
