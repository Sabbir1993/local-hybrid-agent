/* speech-corpus.js - admin panel for the public voice-collection page (settings.html #sec-speech).
 * GET /control/speech/status|stats, POST /control/speech/enabled|code|prompts|contributor/delete; needs speech.manage.
 * Deleting a speaker needs the MFA step-up (withStepUp from mfa-panel.js). */
'use strict';

// one height for the link box and the buttons next to it (the shared .btn styles add their own margin/padding)
const VC_ROW_BTN = 'width:auto;height:40px;min-height:40px;margin:0;padding:0 16px;box-sizing:border-box;display:inline-flex;align-items:center;justify-content:center;white-space:nowrap;box-shadow:none';

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
    <div style="padding:10px 4px;display:flex;flex-direction:column;gap:16px;width:100%;box-sizing:border-box">
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
      <div style="display:flex;flex-direction:column;gap:8px;width:100%">
        <div style="font-weight:600;font-size:13px">Link for colleagues</div>
        <div id="vc-link" style="display:${st.link_path ? 'flex' : 'none'};gap:8px;align-items:stretch;flex-wrap:wrap;width:100%;box-sizing:border-box">
          <input type="text" id="vc-link-text" readonly value="${esc(st.link_path ? window.location.origin + st.link_path : '')}" style="flex:1 1 280px;min-width:0;height:40px;box-sizing:border-box;margin:0;font-family:monospace;font-size:12px">
          <div style="display:flex;gap:8px">
            <button type="button" class="btn accent" id="vc-copy" style="${VC_ROW_BTN}">Copy link</button>
            <button type="button" class="btn" id="vc-share" style="${VC_ROW_BTN}">Share…</button>
          </div>
        </div>
        <div id="vc-link-note" class="dim" style="font-size:12px">${
          st.link_path ? 'Share it only inside the company.'
            : st.has_code ? 'A link exists, but this server cannot show it (its keychain is unavailable). Create a new link to see one; the old link then stops working.'
              : 'No link yet.'}</div>
        <div>
          <button type="button" class="btn" id="vc-rotate" style="width:auto;padding:6px 14px">${st.has_code ? 'Create new link (old link stops working)' : 'Create link'}</button>
        </div>
        <div class="vc-window" style="margin-top:6px;padding:14px;border:1px solid var(--sp-line-strong,var(--border,#3a4150));border-radius:12px;display:flex;flex-direction:column;gap:12px;width:100%;box-sizing:border-box">
          <div style="display:flex;align-items:center;justify-content:space-between;gap:8px;flex-wrap:wrap">
            <span style="font-weight:600;font-size:13px">Open dates</span>
            <span id="vc-window-state" style="font-size:12px;font-weight:600;padding:3px 10px;border-radius:999px"></span>
          </div>
          <div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(220px,1fr));gap:12px">
            <div style="display:flex;flex-direction:column;gap:6px">
              <span class="dim" style="font-size:12px;font-weight:600">Opens</span>
              <div style="display:flex;gap:6px;align-items:center">
                <input type="datetime-local" id="vc-from" style="flex:1;min-width:0">
                <button type="button" class="btn" data-clear="vc-from" title="Clear: open from now" style="width:38px;padding:0 !important">✕</button>
              </div>
              <span class="dim" style="font-size:11px">Empty = open from now</span>
            </div>
            <div style="display:flex;flex-direction:column;gap:6px">
              <span class="dim" style="font-size:12px;font-weight:600">Closes</span>
              <div style="display:flex;gap:6px;align-items:center">
                <input type="datetime-local" id="vc-until" style="flex:1;min-width:0">
                <button type="button" class="btn" data-clear="vc-until" title="Clear: no end date" style="width:38px;padding:0 !important">✕</button>
              </div>
              <span class="dim" style="font-size:11px">Empty = no end date</span>
            </div>
          </div>
          <div>
            <button type="button" class="btn accent" id="vc-window-save" style="width:auto;padding:8px 18px">Save dates</button>
          </div>
          <div style="border-top:1px solid var(--sp-line,var(--border,#2a303b));padding-top:12px;display:flex;flex-direction:column;gap:8px">
            <span class="dim" style="font-size:12px;font-weight:600">Extend closing time</span>
            <div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(96px,1fr));gap:8px">
              <button type="button" class="btn" data-extend="1" style="width:100%">+1 day</button>
              <button type="button" class="btn" data-extend="7" style="width:100%">+7 days</button>
              <button type="button" class="btn" data-extend="30" style="width:100%">+30 days</button>
              <button type="button" class="btn" id="vc-no-end" style="width:100%">No end date</button>
            </div>
          </div>
          <div class="dim" style="font-size:11px;line-height:1.5">Times are in this computer's time zone. Outside these dates the page tells people when it opens or that it has closed; someone already recording can finish their lines. Extending keeps the same link.</div>
        </div>
      </div>
      <div style="width:100%">
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
        <label id="vc-drop" for="vc-prompts" style="display:flex;flex-direction:column;align-items:center;justify-content:center;gap:4px;padding:18px 12px;border:1.5px dashed var(--border,#3a4150);border-radius:10px;cursor:pointer;text-align:center;transition:border-color .15s,background .15s;width:100%;box-sizing:border-box">
          <input type="file" id="vc-prompts" accept=".jsonl,.json,.txt" style="display:none">
          <span style="font-size:22px;line-height:1">📄</span>
          <span style="font-size:13px">Drop a <code>.jsonl</code> file here, or <span style="text-decoration:underline">choose a file</span></span>
        </label>
        <div id="vc-file" style="display:none;margin-top:8px;padding:10px 12px;border:1px solid var(--border,#3a4150);border-radius:10px;font-size:12px;width:100%;box-sizing:border-box">
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

  // ---- open dates ----
  const pad = (n) => String(n).padStart(2, '0');
  const toInput = (ep) => { if (!ep) return ''; const d = new Date(ep * 1000); return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}T${pad(d.getHours())}:${pad(d.getMinutes())}`; };
  const fromInput = (v) => (v ? Math.round(new Date(v).getTime() / 1000) : null);
  const fmt = (ep) => new Date(ep * 1000).toLocaleString(undefined, { day: 'numeric', month: 'short', year: 'numeric', hour: '2-digit', minute: '2-digit' });
  const showWindow = (w) => {
    st.valid_from = w.valid_from; st.valid_until = w.valid_until; st.state = w.state;
    box.querySelector('#vc-from').value = toInput(w.valid_from);
    box.querySelector('#vc-until').value = toInput(w.valid_until);
    const el = box.querySelector('#vc-window-state');
    const now = Date.now() / 1000;
    const days = (s) => { const d = Math.round(s / 86400); return d >= 1 ? `${d} day${d === 1 ? '' : 's'}` : `${Math.max(1, Math.round(s / 3600))} hour(s)`; };
    const pill = (text, rgb) => { el.textContent = text; el.style.color = `rgb(${rgb})`; el.style.background = `rgba(${rgb},0.14)`; };
    if (w.state === 'not_yet') pill(`Not open yet · opens ${fmt(w.valid_from)}`, '245,158,11');
    else if (w.state === 'expired') pill(`Closed since ${fmt(w.valid_until)}`, '248,113,113');
    else if (w.valid_until) pill(`Open · closes ${fmt(w.valid_until)} (in ${days(w.valid_until - now)})`, '74,222,128');
    else pill('Open · no end date', '74,222,128');
  };
  const saveWindow = async (from, until, okMsg) => {
    const { r: rr, d } = await speechPost('/control/speech/window', { valid_from: from, valid_until: until });
    if (!rr.ok) { say(d.error || d.detail || 'Could not save the dates.', true); showWindow(st); return; }
    showWindow(d);
    say(okMsg);
  };
  // the native picker's calendar icon follows color-scheme: match the theme (light or dark panel)
  const bg = window.getComputedStyle(document.body).backgroundColor.match(/\d+/g) || [255, 255, 255];
  const dark = (0.299 * bg[0] + 0.587 * bg[1] + 0.114 * bg[2]) < 128;
  box.querySelectorAll('input[type="datetime-local"]').forEach((i) => { i.style.colorScheme = dark ? 'dark' : 'light'; });
  box.querySelectorAll('[data-clear]').forEach((b) => b.addEventListener('click', () => {
    box.querySelector('#' + b.getAttribute('data-clear')).value = '';
  }));
  showWindow(st);
  box.querySelector('#vc-window-save').addEventListener('click', () =>
    saveWindow(fromInput(box.querySelector('#vc-from').value), fromInput(box.querySelector('#vc-until').value), 'Dates saved.'));
  box.querySelectorAll('[data-extend]').forEach((b) => b.addEventListener('click', () => {
    const n = Number(b.getAttribute('data-extend'));
    const base = Math.max(st.valid_until || 0, Date.now() / 1000);       // an expired link is extended from now
    const until = Math.round(base + n * 86400);
    saveWindow(st.valid_from, until, `Extended: now closes ${fmt(until)}.`);
  }));
  box.querySelector('#vc-no-end').addEventListener('click', () => saveWindow(st.valid_from, null, 'The link now has no end date.'));

  // ---- share ----
  const shareText = () => {
    const url = box.querySelector('#vc-link-text').value;
    const until = st.valid_until ? ` Open until ${fmt(st.valid_until)}.` : '';
    return { url, text: `আমাদের বাংলা স্পিচ মডেলের জন্য আপনার কণ্ঠ দিন: ${st.lines_per_session}টি লাইন পড়ুন, ৫-১০ মিনিট লাগবে।
Please lend your voice to our Bangla speech model: read ${st.lines_per_session} short lines (5-10 minutes).${until}
${url}` };
  };
  box.querySelector('#vc-share').addEventListener('click', async () => {
    const { url, text } = shareText();
    if (!url) { say('Create a link first.', true); return; }
    if (navigator.share) {
      try { await navigator.share({ title: 'Voice collection', text }); return; } catch (e) { if (e && e.name === 'AbortError') return; }
    }
    try { await navigator.clipboard.writeText(text); say('Message with the link copied. Paste it into email, Teams or WhatsApp.'); }
    catch (_) { say('Could not copy. Copy the link above instead.', true); }
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
    ${rows ? `<div style="overflow-x:auto;-webkit-overflow-scrolling:touch;max-width:100%;margin-top:8px"><table style="width:100%;font-size:12px;border-collapse:collapse">
      <tr class="dim"><th style="text-align:left;padding:4px 8px 4px 0">How they speak</th><th style="text-align:right;padding:4px 8px">Speakers</th><th style="text-align:right;padding:4px 8px">Clips</th><th style="text-align:right;padding:4px 0 4px 8px">Hours</th></tr>${rows}</table></div>` : ''}
    ${cats ? `<div style="margin-top:6px">${cats}</div>` : ''}`;
}

window.loadSpeechCorpusPanel = loadSpeechCorpusPanel;
