/* ---------------- Source Control panel ---------------- */
let _gitDiffTarget = null; // {path, staged}

async function updateGitIconVisibility() {
  const btn = $('btn-git-icon');
  if (!btn) return;
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

    box.innerHTML = `
      <div class="cap-item" style="display:flex; justify-content:space-between; align-items:center;">
        <span>${st.detached ? '⚠ detached at ' : ''}<b>${esc(st.branch || '?')}</b> ${st.ahead ? `<span class="dim">↑${st.ahead}</span>` : ''}${st.behind ? `<span class="dim">↓${st.behind}</span>` : ''}</span>
        <button class="btn ghost" id="git-refresh" style="width:auto; margin:0; padding:2px 10px; font-size:10.5px;">⟳ Refresh</button>
      </div>
      ${gitFileGroup('Staged', staged, true)}
      ${gitFileGroup('Changes', unstaged, false)}
      <div id="git-diff-view" style="display:none; margin-top:6px;">
        <div style="display:flex; justify-content:space-between; align-items:center;">
          <span class="mono" id="git-diff-path" style="font-size:10.5px;"></span>
          <button class="btn ghost" id="git-diff-close" style="width:auto; margin:0; padding:1px 8px; font-size:10px;">✕</button>
        </div>
        <pre id="git-diff-pre" style="max-height:220px; overflow:auto; font-size:10.5px; background:var(--bg-input); border:1px solid var(--border); border-radius:6px; padding:8px; margin-top:4px;"></pre>
      </div>
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
    $('git-diff-close')?.addEventListener('click', () => { $('git-diff-view').style.display = 'none'; _gitDiffTarget = null; });
    $('git-commit-btn')?.addEventListener('click', gitCommit);
    $('git-push-btn')?.addEventListener('click', gitPush);
    $('git-pr-btn')?.addEventListener('click', gitCreatePr);
    $('git-suggest-commit-btn')?.addEventListener('click', gitSuggestCommitMessage);
    $('git-suggest-pr-btn')?.addEventListener('click', gitSuggestPr);
  } catch (e) {
    box.innerHTML = '<div class="mon-empty">Failed: ' + esc(e.message) + '</div>';
  }
}

function gitFileGroup(label, files, staged) {
  if (!files.length) return '';
  return `<div class="cap-item">
    <b style="font-size:10.5px; text-transform:uppercase; letter-spacing:0.5px;">${esc(label)} (${files.length})</b>
    <div style="display:flex; flex-direction:column; gap:2px; margin-top:3px;">
      ${files.map(f => `
        <div class="git-file-row" data-path="${esc(f.path)}" data-staged="${staged}" style="display:flex; align-items:center; gap:6px; font-size:11px;">
          <button class="btn ghost git-toggle" style="width:auto; margin:0; padding:1px 6px; font-size:10px;" title="${staged ? 'Unstage' : 'Stage'}">${staged ? '−' : '+'}</button>
          <span class="mono git-file-link" style="cursor:pointer; flex:1; word-break:break-all;" title="View diff">${esc(f.path)}</span>
        </div>`).join('')}
    </div>
  </div>`;
}

function wireGitFileClicks(box) {
  box.querySelectorAll('.git-file-link').forEach(el => {
    el.onclick = () => {
      const row = el.closest('.git-file-row');
      showGitDiff(row.dataset.path, row.dataset.staged === 'true');
    };
  });
  box.querySelectorAll('.git-toggle').forEach(btn => {
    btn.onclick = async () => {
      const row = btn.closest('.git-file-row');
      const staged = row.dataset.staged === 'true';
      const path = row.dataset.path;
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
}

async function showGitDiff(path, staged) {
  _gitDiffTarget = { path, staged };
  const view = $('git-diff-view'), pre = $('git-diff-pre'), pathEl = $('git-diff-path');
  view.style.display = 'block';
  pathEl.textContent = path;
  pre.textContent = 'Loading…';
  try {
    const d = await (await fetch(`/git/diff?path=${encodeURIComponent(path)}&staged=${staged}`)).json();
    if (d.error) throw new Error(d.error);
    pre.textContent = d.diff || '(no diff)';
  } catch (e) {
    pre.textContent = 'Failed: ' + e.message;
  }
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
