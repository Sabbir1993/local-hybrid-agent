/* ---------------- Settings: agent limits (admin) and the signed-in user's agent memory ---------------- */

const AGENT_LIMIT_FIELDS = [
  ['Output caps (tokens per generation)', [
    ['executor_max_tokens', 'Executor lane', 'A step longer than this is cut off and retried smaller. Keeps a small model from running on for minutes.'],
    ['main_max_tokens', 'Main lane', 'Ceiling for the main model. Your Chat & Sampling "max tokens" can only lower it.'],
    ['vision_max_tokens', 'Vision lane', 'Image descriptions.'],
  ]],
  ['File tools', [
    ['write_file_max_tokens', 'Max size of one write (tokens)', 'Bigger content is refused with "write a skeleton, then append".'],
    ['chunk_target_tokens', 'Target size of one chunk (tokens)', 'Told to the model when it must split a file.'],
    ['read_file_default_limit', 'Lines per read_file', 'Default window when the model gives no limit.'],
    ['read_file_max_chars', 'Max characters per read_file', 'Hard cap on one read.'],
    ['verify_max_retries', 'Fix attempts after a failed syntax check', 'Then the agent stops and reports (an edit is undone automatically).'],
  ]],
  ['Thinking', [
    ['@executor_max_effort', 'Highest thinking level for the executor', 'The executor thinks at most this much, whatever effort you picked for the main model.'],
  ]],
  ['Tools', [
    ['python_prompt_nudge', 'Python scripts before a warning', 'Each run_python script needs your approval. After this many in one task the agent is told to use grep / read_file or combine them.'],
  ]],
  ['Plans', [
    ['@plan_required', 'Require a todo list', 'auto = plan first for multi-step jobs; always = every agent request; off = never enforced. The agent then works one step at a time.'],
    ['plan_item_max_steps', 'Steps without progress before a nudge', 'Steps on one todo item that write nothing and change no status. The agent is then told to split the item or mark it failed. Items are never failed automatically, and steps that write files do not count.'],
    ['plan_max_items', 'Max steps in a plan', 'A longer plan is refused; the model regroups it.'],
    ['plan_item_max_chars', 'Max characters per step', ''],
  ]],
  ['Long-term memory', [
    ['memory_file_max_bytes', 'Max size of one memory file (bytes)', 'Over it, the agent consolidates or splits the file.'],
    ['memory_max_files', 'Max memory files per user', ''],
  ]],
];

function agentLimitsHtml(d) {
  const v = d.values, r = d.ranges, inp = 'width:78px; margin:0; background:var(--bg-input); color:var(--text); border:1px solid var(--border); border-radius:5px; padding:2px 6px; font-size:11px;';
  const sel = ([key, label, help]) => `
    <div style="display:flex; align-items:center; gap:6px; margin-top:6px; flex-wrap:wrap;" title="${esc(help)}">
      <span style="min-width:230px;">${esc(label)}</span>
      <select class="al-sel" data-key="${key.slice(1)}" style="margin:0; background:var(--bg-input); color:var(--text); border:1px solid var(--border); border-radius:5px; padding:2px 6px; font-size:11px;">
        ${(key === '@plan_required' ? ['auto', 'always', 'off'] : ['none', 'low', 'medium', 'high', 'extra']).map(o => `<option value="${o}" ${v[key.slice(1)] === o ? 'selected' : ''}>${o}</option>`).join('')}
      </select>
      <span class="dim" style="font-size:9.5px;">${esc(help)}</span>
    </div>`;
  const row = ([key, label, help]) => key[0] === '@' ? sel([key, label, help]) : `
    <div style="display:flex; align-items:center; gap:6px; margin-top:6px; flex-wrap:wrap;" title="${esc(help)}">
      <span style="min-width:230px;">${esc(label)}</span>
      <input type="number" class="al-in" data-key="${key}" min="${r[key][0]}" max="${r[key][1]}" value="${v[key]}" style="${inp}">
      <span class="dim" style="font-size:9.5px;">${r[key][0]}–${r[key][1]}${help ? ' · ' + esc(help) : ''}</span>
    </div>`;
  const chk = (key, label, help) => `
    <label style="display:flex; align-items:center; gap:6px; margin-top:6px;" title="${esc(help)}">
      <input type="checkbox" class="al-chk" data-key="${key}" ${v[key] ? 'checked' : ''}>
      <span>${esc(label)}</span> <span class="dim" style="font-size:9.5px;">${esc(help)}</span>
    </label>`;
  const pct = Math.round(v.compaction_threshold * 100);
  return `<details class="cap-group fold" style="margin-top:10px;">
    <summary class="cap-head"><span class="cap-head-title" style="display:flex; align-items:center; gap:6px;">
      <span class="cap-chevron">▶</span><span>🛠️ Agent limits &amp; memory</span></span></summary>
    <div class="cap-body" id="agent-limits-box">
      ${AGENT_LIMIT_FIELDS.map(([title, rows]) => `<div style="margin-top:8px;"><b>${esc(title)}</b>${rows.map(row).join('')}</div>`).join('')}
      <div style="margin-top:8px;"><b>Context</b>
        <div style="display:flex; align-items:center; gap:6px; margin-top:6px;">
          <span style="min-width:230px;">Compact history at (% of the window)</span>
          <input type="number" id="al-compaction" min="${Math.round(d.ranges.compaction_threshold[0] * 100)}" max="${Math.round(d.ranges.compaction_threshold[1] * 100)}" value="${pct}" style="${inp}">
          <span class="dim" style="font-size:9.5px;">older steps are summarized past this point</span>
        </div>
        ${chk('mirror_to_workspace', 'Write PLAN.md and .agent/working_memory.md into the project folder', 'so you can read them next to your code')}
      </div>
      <div style="margin-top:8px;"><b>Memory switches</b>
        ${chk('memory_enabled', 'Agents can use long-term memory', '')}
        ${chk('memory_allow_cloud', 'Also when a cloud model is on the run', 'off = memory is never sent to a cloud provider (recommended: it is personal data)')}
      </div>
      <div style="margin-top:10px;"><button class="btn accent" id="al-save" style="width:auto; margin:0; padding:3px 12px; font-size:10.5px;">Save</button></div>
    </div></details>`;
}

function wireAgentLimits(box) {
  const save = box.querySelector('#al-save');
  if (!save) return;
  save.onclick = async () => {
    const body = {};
    box.querySelectorAll('#agent-limits-box .al-in').forEach(i => { const n = parseInt(i.value, 10); if (!isNaN(n)) body[i.dataset.key] = n; });
    box.querySelectorAll('#agent-limits-box .al-chk').forEach(c => { body[c.dataset.key] = c.checked; });
    box.querySelectorAll('#agent-limits-box .al-sel').forEach(c => { body[c.dataset.key] = c.value; });
    const pct = parseInt(box.querySelector('#al-compaction').value, 10);
    if (!isNaN(pct)) body.compaction_threshold = pct / 100;
    try {
      const r = await fetch('/control/agent_limits', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) });
      const j = await r.json();
      if (!r.ok) throw new Error(j.error || j.detail || r.status);
      box.querySelectorAll('#agent-limits-box .al-in').forEach(i => { if (j.values[i.dataset.key] != null) i.value = j.values[i.dataset.key]; });
      box.querySelector('#al-compaction').value = Math.round(j.values.compaction_threshold * 100);
      toast('Agent limits saved ✓ (applies to the next run)');
    } catch (e) { toast('Save failed: ' + e.message, true); }
  };
}

/* ----- the signed-in user's own memory files ----- */
function agentMemoryHtml() {
  return `<details class="cap-group fold" style="margin-top:10px;" id="agent-memory-group">
    <summary class="cap-head"><span class="cap-head-title" style="display:flex; align-items:center; gap:6px;">
      <span class="cap-chevron">▶</span><span>🧠 My agent memory</span></span></summary>
    <div class="cap-body">
      <div class="dim" style="font-size:10px;">What the agent remembers about you, as small files. Only you can see them. Edit or delete anything; it is never used for anyone else. Passwords, keys, card and ID numbers are refused.</div>
      <div id="agent-memory-list" class="dim" style="margin-top:6px;">Loading…</div>
      <div id="agent-memory-edit" style="display:none; margin-top:8px;"></div>
      <div style="margin-top:8px; display:flex; gap:6px;">
        <button class="btn ghost" id="agent-memory-new" style="width:auto; margin:0; padding:2px 10px; font-size:10.5px;">＋ New file</button>
        <button class="btn ghost" id="agent-memory-erase" style="width:auto; margin:0; padding:2px 10px; font-size:10.5px; color:var(--red);">Erase all my memory</button>
      </div>
    </div></details>`;
}

async function wireAgentMemory(box) {
  const list = box.querySelector('#agent-memory-list'), edit = box.querySelector('#agent-memory-edit');
  if (!list) return;
  const api = (p, opt) => fetch('/agent/memory' + (p ? '/' + p : ''), opt);
  const closeEdit = () => { edit.style.display = 'none'; edit.innerHTML = ''; };
  const openEdit = (f) => {
    const isNew = !f;
    edit.style.display = 'block';
    edit.innerHTML = `
      ${isNew ? '<input id="am-path" placeholder="path, e.g. projects/my-app.md" style="width:100%; margin:0 0 4px;">' : `<b>${esc(f.path)}</b>`}
      <textarea id="am-text" rows="10" style="width:100%; box-sizing:border-box; font-family:monospace; font-size:11px; background:var(--bg-input); color:var(--text); border:1px solid var(--border); border-radius:5px; padding:6px;">${esc(isNew ? '---\ndescription: what this file covers and when to read it\n---\n- ' : f.text)}</textarea>
      <div style="margin-top:4px; display:flex; gap:6px;">
        <button class="btn accent" id="am-save" style="width:auto; margin:0; padding:2px 10px; font-size:10.5px;">Save</button>
        <button class="btn ghost" id="am-cancel" style="width:auto; margin:0; padding:2px 10px; font-size:10.5px;">Cancel</button>
      </div>`;
    edit.querySelector('#am-cancel').onclick = closeEdit;
    edit.querySelector('#am-save').onclick = async () => {
      const path = isNew ? edit.querySelector('#am-path').value.trim() : f.path;
      if (!path) { toast('Enter a file path', true); return; }
      try {
        const r = await api(path, { method: 'PUT', headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ content: edit.querySelector('#am-text').value, if_version: isNew ? null : f.version }) });
        const j = await r.json();
        if (!r.ok) throw new Error(j.error || r.status);
        toast('Saved ✓'); closeEdit(); refresh();
      } catch (e) { toast('Save failed: ' + e.message, true); }
    };
  };
  const refresh = async () => {
    try {
      const j = await (await api('')).json();
      const files = j.files || [];
      list.innerHTML = files.length ? files.map(f => `
        <div style="display:flex; align-items:center; gap:8px; margin:3px 0;">
          <code style="min-width:150px;">${esc(f.path)}</code>
          <span class="dim" style="flex:1; font-size:10.5px;">${esc(f.description)}</span>
          <span class="dim" style="font-size:9.5px;">${f.bytes} B</span>
          <button class="btn ghost am-open" data-p="${esc(f.path)}" style="width:auto; margin:0; padding:1px 8px; font-size:10px;">View / edit</button>
          <button class="btn ghost am-del" data-p="${esc(f.path)}" style="width:auto; margin:0; padding:1px 8px; font-size:10px; color:var(--red);">Delete</button>
        </div>`).join('') : 'Nothing remembered yet. Tell the agent a preference and it will be saved here.';
      list.querySelectorAll('.am-open').forEach(b => b.onclick = async () => {
        const f = await (await api(b.dataset.p)).json();
        if (f.error) { toast(f.error, true); return; }
        openEdit(f);
      });
      list.querySelectorAll('.am-del').forEach(b => b.onclick = async () => {
        if (!confirm(`Delete ${b.dataset.p}?`)) return;
        await api(b.dataset.p, { method: 'DELETE' });
        closeEdit(); refresh();
      });
    } catch (e) { list.textContent = 'Could not load memory: ' + e.message; }
  };
  box.querySelector('#agent-memory-new').onclick = () => openEdit(null);
  box.querySelector('#agent-memory-erase').onclick = async () => {
    if (!confirm('Erase everything the agent remembers about you? This cannot be undone.')) return;
    const j = await (await api('', { method: 'DELETE' })).json();
    toast(`Erased ${j.deleted || 0} file(s)`); closeEdit(); refresh();
  };
  refresh();
}
