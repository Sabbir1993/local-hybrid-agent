/* ---------------- Source Control panel ---------------- */

async function updateGitIconVisibility() {
  const btn = $('btn-git-icon');
  if (!btn) return;
  if (!agentMode || !curProject) { btn.style.display = 'none'; return; }
  try {
    const d = await (await fetch('/git/status')).json();
    btn.style.display = d.error ? 'none' : '';
  } catch {
    btn.style.display = 'none';
  }
}

function setGitModal(open) {
  const m = $('git-modal');
  if (!m) return;
  m.hidden = !open;
  if (open) loadGitPanel();
}

$('btn-git-icon')?.addEventListener('click', () => setGitModal(true));
$('git-modal-close')?.addEventListener('click', () => setGitModal(false));
$('git-modal')?.addEventListener('click', (e) => { if (e.target.id === 'git-modal') setGitModal(false); });

async function loadGitPanel() {
  const box = $('git-content');
  if (!box) return;
  try {
    const [st, br] = await Promise.all([
      (await fetch('/git/status')).json(),
      (await fetch('/git/branches')).json(),
    ]);
    if (st.error) { box.innerHTML = '<div class="mon-empty">' + esc(st.error) + '</div>'; return; }

    const staged = st.files.filter(f => f.index_status !== ' ' && f.index_status !== '?');
    const unstaged = st.files.filter(f => f.worktree_status !== ' ' && f.worktree_status !== undefined);
    const branchList = br.branches || [];
    const defaultBase = br.default || 'main';
    const cps = await (await fetch('/git/checkpoints')).json().catch(() => ({ checkpoints: [] }));
    const cpRail = (cps.checkpoints || []).slice(0, 5).map((c) => `
      <div style="display:flex; align-items:center; gap:6px; padding:2px 0;">
        <span class="dim mono" style="font-size:10px; flex:1;" title="${esc(c.ref)}">run ${esc(String(c.run_id).slice(0, 8))} · step ${c.step}</span>
        <button class="btn ghost" style="width:auto; margin:0; padding:1px 8px; font-size:10px;"
                data-checkpoint-run="${esc(c.run_id)}" data-checkpoint-step="${c.step}">◀ Rewind</button>
      </div>`).join('');


    box.innerHTML = `
      <div class="cap-item" style="display:flex; justify-content:space-between; align-items:center;">
        <span>${st.detached ? '⚠ detached at ' : ''}<b>${esc(st.branch || '?')}</b> ${st.ahead ? `<span class="dim">↑${st.ahead}</span>` : ''}${st.behind ? `<span class="dim">↓${st.behind}</span>` : ''}</span>
        <button class="btn ghost" id="git-refresh" style="width:auto; margin:0; padding:2px 10px; font-size:10.5px;">⟳ Refresh</button>
      </div>
      <div class="cap-item" style="margin-bottom:6px;">
        <b style="font-size:10.5px; text-transform:uppercase; letter-spacing:0.5px;">Checkpoints</b>
        ${cpRail || '<div class="dim" style="font-size:10px;">No checkpoints yet.</div>'}
      </div>
      ${gitFileGroup('Staged', staged, true)}
      ${gitFileGroup('Changes', unstaged, false)}
      <div class="cap-item" style="margin-top:8px;">
        <div style="display:flex; justify-content:space-between; align-items:center;">
          <span class="dim" style="font-size:10.5px;">Commit message</span>
          <button class="btn ghost" id="git-suggest-commit-btn" style="width:auto; margin:0; padding:1px 8px; font-size:10px;" title="Generate from the diff using the app's own model">✨ Generate</button>
        </div>
        <textarea id="git-commit-msg" rows="2" placeholder="Commit message" style="width:100%; margin-top:3px; background:var(--bg-input); color:var(--text); border:1px solid var(--border); border-radius:6px; padding:6px 8px; font-size:11.5px; font-family:inherit; resize:vertical;"></textarea>
        <div style="display:flex; gap:6px; margin-top:6px;">
          <button class="btn accent" id="git-commit-btn" style="width:auto; margin:0; padding:4px 12px; font-size:11px;">✅ Commit</button>
          <button class="btn ghost" id="git-push-btn" style="width:auto; margin:0; padding:4px 12px; font-size:11px;">⬆ Push</button>
        </div>
      </div>
      <div class="cap-item" style="margin-top:6px; border-top:1px solid var(--border); padding-top:8px;">
        <div style="display:flex; justify-content:space-between; align-items:center;">
          <b style="font-size:11px;">Create Pull Request <span class="dim" style="font-weight:normal;">from <span class="mono">${esc(st.branch || '?')}</span></span></b>
          <button class="btn ghost" id="git-suggest-pr-btn" style="width:auto; margin:0; padding:1px 8px; font-size:10px;" title="Generate title + description from the diff against base">✨ Generate</button>
        </div>
        <input type="text" id="git-pr-title" placeholder="PR title" style="width:100%; margin-top:4px; background:var(--bg-input); color:var(--text); border:1px solid var(--border); border-radius:6px; padding:5px 8px; font-size:11px;">
        <textarea id="git-pr-body" rows="2" placeholder="Description (optional)" style="width:100%; margin-top:4px; background:var(--bg-input); color:var(--text); border:1px solid var(--border); border-radius:6px; padding:5px 8px; font-size:11px; font-family:inherit; resize:vertical;"></textarea>
        <div style="display:flex; gap:6px; margin-top:4px; align-items:center;">
          <span class="dim" style="font-size:10.5px;">base:</span>
          <select id="git-pr-base" style="background:var(--bg-input); color:var(--text); border:1px solid var(--border); border-radius:6px; padding:4px 6px; font-size:11px;">
            ${branchList.length
              ? branchList.map(b => `<option value="${esc(b)}" ${b === defaultBase ? 'selected' : ''}>${esc(b)}</option>`).join('')
              : `<option value="${esc(defaultBase)}" selected>${esc(defaultBase)}</option>`}
          </select>
          <button class="btn ghost" id="git-pr-btn" style="width:auto; margin-left:auto; padding:4px 12px; font-size:11px;">🔀 Create PR</button>
        </div>
        <div id="git-pr-status" class="dim" style="font-size:10.5px; margin-top:4px;"></div>
      </div>`;

    wireGitFileClicks(box);
    $('git-refresh')?.addEventListener('click', loadGitPanel);
    $('git-commit-btn')?.addEventListener('click', gitCommit);
    $('git-push-btn')?.addEventListener('click', gitPush);
    $('git-pr-btn')?.addEventListener('click', gitCreatePr);
    $('git-suggest-commit-btn')?.addEventListener('click', gitSuggestCommitMessage);
    $('git-suggest-pr-btn')?.addEventListener('click', gitSuggestPr);
    box.querySelectorAll('[data-checkpoint-run]').forEach((b) => b.addEventListener('click', gitRewind));
  } catch (e) {
    box.innerHTML = '<div class="mon-empty">Failed: ' + esc(e.message) + '</div>';
  }
}

function gitFileGroup(label, files, staged) {
  if (!files.length) return '';
  return `<div class="cap-item git-group" data-staged="${staged}">
    <div style="display:flex; justify-content:space-between; align-items:center;">
      <b style="font-size:10.5px; text-transform:uppercase; letter-spacing:0.5px;">${esc(label)} (${files.length})</b>
      <button class="btn ghost git-expand-all" style="width:auto; margin:0; padding:1px 8px; font-size:10px;">Expand all</button>
    </div>
    <div style="display:flex; flex-direction:column; gap:2px; margin-top:3px;">
      ${files.map(f => {
        const untracked = f.index_status === '?' || f.worktree_status === '?';
        const code = untracked ? 'U' : (staged ? f.index_status : f.worktree_status);
        return `
        <div class="git-file" data-path="${esc(f.path)}" data-staged="${staged}" data-untracked="${untracked}">
          <div class="git-file-row" style="display:flex; align-items:center; gap:6px; font-size:11px;">
            <button class="btn ghost git-toggle" style="width:auto; margin:0; padding:1px 6px; font-size:10px;" title="${staged ? 'Unstage' : 'Stage'}">${staged ? '−' : '+'}</button>
            <span class="mono git-file-link" style="cursor:pointer; flex:1; word-break:break-all;" title="Show changes"><span class="git-chev dim">▸</span> ${esc(f.path)}</span>
            <span class="mono git-counts dim" style="font-size:10px;"></span>
            <span class="mono dim" style="font-size:10px;" title="${untracked ? 'New file (not tracked yet)' : 'Status'}">${esc(code)}</span>
          </div>
          <pre class="git-inline-diff" style="display:none; max-height:260px; overflow:auto; font-size:10.5px; line-height:1.45; background:var(--bg-input); border:1px solid var(--border); border-radius:6px; padding:6px 8px; margin:3px 0 4px; white-space:pre;"></pre>
        </div>`;
      }).join('')}
    </div>
  </div>`;
}

function wireGitFileClicks(box) {
  box.querySelectorAll('.git-file').forEach(file => {
    file.querySelector('.git-file-link').onclick = () => toggleGitDiff(file);
  });
  box.querySelectorAll('.git-expand-all').forEach(btn => {
    btn.onclick = () => {
      const group = btn.closest('.git-group');
      const open = btn.textContent === 'Expand all';
      group.querySelectorAll('.git-file').forEach(file => toggleGitDiff(file, open));
      btn.textContent = open ? 'Collapse all' : 'Expand all';
    };
  });
  box.querySelectorAll('.git-toggle').forEach(btn => {
    btn.onclick = async () => {
      const file = btn.closest('.git-file');
      const staged = file.dataset.staged === 'true';
      const path = file.dataset.path;
      try {
        const r = await fetch(`/git/${staged ? 'unstage' : 'stage'}`, {
          method: 'POST', headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ paths: [path] }),
        });
        if (!r.ok) throw new Error((await r.json()).error || r.status);
        loadGitPanel();
      } catch (e) { toast('Failed: ' + e.message, true); }
    };
  });
  // a few changed files: show their lines right away, no click needed
  box.querySelectorAll('.git-group').forEach(group => {
    const files = group.querySelectorAll('.git-file');
    if (files.length <= 4) files.forEach(file => toggleGitDiff(file, true));
  });
}

// inline, per file: works for staged, unstaged and new (untracked) files alike
async function toggleGitDiff(file, forceOpen) {
  const pre = file.querySelector('.git-inline-diff');
  const chev = file.querySelector('.git-chev');
  const open = forceOpen === undefined ? pre.style.display === 'none' : forceOpen;
  pre.style.display = open ? 'block' : 'none';
  if (chev) chev.textContent = open ? '▾' : '▸';
  if (!open || file.dataset.loaded) return;
  file.dataset.loaded = '1';
  pre.textContent = 'Loading…';
  try {
    const q = `path=${encodeURIComponent(file.dataset.path)}&staged=${file.dataset.staged}&untracked=${file.dataset.untracked}`;
    const d = await (await fetch(`/git/diff?${q}`)).json();
    if (d.error) throw new Error(d.error);
    renderGitDiff(pre, d.diff || '', file.querySelector('.git-counts'));
  } catch (e) {
    file.dataset.loaded = '';
    pre.textContent = 'Failed: ' + e.message;
  }
}

function renderGitDiff(pre, text, countsEl) {
  if (!text.trim()) { pre.textContent = '(no changes)'; return; }
  let add = 0, del = 0;
  pre.innerHTML = text.split('\n').map(line => {
    let color = '';
    if (line.startsWith('+') && !line.startsWith('+++')) { add++; color = '#4ade80'; }
    else if (line.startsWith('-') && !line.startsWith('---')) { del++; color = '#f87171'; }
    else if (line.startsWith('@@')) color = '#60a5fa';
    else if (/^(diff |index |--- |\+\+\+ |new file|deleted file)/.test(line)) color = 'var(--dim)';
    return color ? `<span style="color:${color}">${esc(line)}</span>` : esc(line);
  }).join('\n');
  if (countsEl) countsEl.innerHTML = `<span style="color:#4ade80">+${add}</span> <span style="color:#f87171">−${del}</span>`;
}

async function gitCommit() {
  const msg = $('git-commit-msg').value.trim();
  if (!msg) { toast('Commit message required', true); return; }
  try {
    const r = await fetch('/git/commit', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ message: msg }),
    });
    const d = await r.json();
    if (!r.ok) throw new Error(d.error || r.status);
    toast('Committed ✓');
    loadGitPanel();
  } catch (e) { toast('Commit failed: ' + e.message, true); }
}

async function gitPush() {
  if (!confirm('Push committed changes to the remote now?')) return;
  try {
    const r = await fetch('/git/push', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: '{}' });
    const d = await r.json();
    if (!r.ok) throw new Error(d.error || r.status);
    toast('Pushed ✓');
    loadGitPanel();
  } catch (e) { toast('Push failed: ' + e.message, true); }
}

// Backend errors should always be a plain string (see routes/git.py), but never let a
// non-string error value (or a raw Response/status code) render as "[object Object]".
function errText(d, r) {
  if (d && typeof d.error === 'string' && d.error.trim()) return d.error;
  if (d && d.error) { try { return JSON.stringify(d.error); } catch { /* fall through */ } }
  return `request failed (HTTP ${r.status})`;
}

async function gitSuggestCommitMessage() {
  const btn = $('git-suggest-commit-btn');
  btn.disabled = true; btn.textContent = '⏳ …';
  try {
    const r = await fetch('/git/suggest_commit_message', { method: 'POST' });
    const d = await r.json();
    if (!r.ok) throw new Error(errText(d, r));
    if (!d.message || !d.message.trim()) throw new Error('model returned no text — it may have used its whole budget on reasoning; try again');
    $('git-commit-msg').value = d.message;
  } catch (e) { toast('Generate failed: ' + (e && e.message ? e.message : String(e)), true); }
  finally { btn.disabled = false; btn.textContent = '✨ Generate'; }
}

async function gitSuggestPr() {
  const btn = $('git-suggest-pr-btn');
  const base = $('git-pr-base').value.trim() || 'main';
  btn.disabled = true; btn.textContent = '⏳ …';
  try {
    const r = await fetch('/git/suggest_pr', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ base }),
    });
    const d = await r.json();
    if (!r.ok) throw new Error(errText(d, r));
    if (!d.title && !d.body) throw new Error('model returned no text — it may have used its whole budget on reasoning; try again');
    $('git-pr-title').value = d.title || '';
    $('git-pr-body').value = d.body || '';
  } catch (e) { toast('Generate failed: ' + (e && e.message ? e.message : String(e)), true); }
  finally { btn.disabled = false; btn.textContent = '✨ Generate'; }
}

async function gitCreatePr() {
  const title = $('git-pr-title').value.trim();
  const body = $('git-pr-body').value.trim();
  const base = $('git-pr-base').value.trim() || 'main';
  const statusEl = $('git-pr-status');
  if (!title) { toast('PR title required', true); return; }
  if (!confirm(`Open a pull request "${title}" against ${base}?`)) return;
  statusEl.textContent = 'Creating…';
  try {
    const r = await fetch('/git/pr', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ title, body, base }),
    });
    const d = await r.json();
    if (!r.ok) throw new Error(d.error || r.status);
    statusEl.textContent = 'PR created ✓';
    toast('PR created ✓');
  } catch (e) {
    statusEl.textContent = 'Failed: ' + e.message;
    toast('PR failed: ' + e.message, true);
  }
}


async function gitRewind(e) {
  const btn = e.currentTarget;
  const runId = btn.dataset.checkpointRun;
  const step = Number(btn.dataset.checkpointStep || 0);
  if (!confirm('Rewind the workspace to the checkpoint at step ' + step +
               '? This discards changes made after that step (git reset --hard).')) return;
  try {
    const r = await fetch('/git/rewind', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ run_id: runId, step }),
    });
    const d = await r.json();
    if (!r.ok) { toast(d.error || 'rewind failed'); return; }
    toast('Rewound to ' + String(d.rewound_to || '').slice(0, 8));
    loadGitPanel();
  } catch (err) {
    toast('rewind failed: ' + err.message, true);
  }
}
