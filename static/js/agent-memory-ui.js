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
  const v = d.values, r = d.ranges, inp = 'width:84px; margin:0; background:var(--bg-input); color:var(--text); border:1px solid var(--border); border-radius:7px; padding:6px 10px; font-size:12px; box-sizing:border-box;';
  const sel = ([key, label, help]) => `
    <div style="display:flex; align-items:center; gap:6px; margin-top:6px; flex-wrap:wrap;" title="${esc(help)}">
      <span style="min-width:230px;">${esc(label)}</span>
      <select class="al-sel" data-key="${key.slice(1)}" style="margin:0; background:var(--bg-input); color:var(--text); border:1px solid var(--border); border-radius:7px; padding:6px 10px; font-size:12px; box-sizing:border-box;">
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

/* ----- the signed-in user's own memory: the editor is the app's friendly "Add something to remember" screen ----- */
// topics mirror frontend/src/components/modals/AgentMemory.tsx: the file name, description and "- " bullets are built here
const AM_TOPICS = [
  ['about', 'About me', 'who you are, your role, your team', 'profile.md', 'Who the user is and what they do'],
  ['prefs', 'How I like things done', 'tone, language, format, tools you prefer', 'preferences.md', 'How the user likes answers and work done'],
  ['lessons', 'Lessons and corrections', 'mistakes the agent should not repeat', 'lessons.md', 'Corrections and lessons from earlier work'],
  ['project', 'A project', 'facts about one project or product', '', ''],
  ['other', 'Something else', 'any other topic', '', ''],
];

function agentMemoryHtml() {
  return `<div id="agent-memory-group" class="am-card">
    <div class="am-head">
      <div>
        <h3 class="am-title">🧠 My agent memory</h3>
        <p class="dim">Short notes the agent keeps in mind about you between chats, so you do not have to repeat yourself. Only you can see them. Passwords, keys, card and ID numbers are refused.</p>
      </div>
      <button type="button" class="btn ghost am-full" id="btn-open-agent-memory" title="Edit whole notes, search and erase all">Open full editor</button>
    </div>
    <div class="am-add">
      <div class="am-step"><span class="am-num">1</span> What is it about?</div>
      <div class="am-chips" id="am-topics">
        ${AM_TOPICS.map(([k, label, hint], i) => `<button type="button" class="am-chip${i ? '' : ' on'}" data-topic="${k}" title="${esc(hint)}">${esc(label)}</button>`).join('')}
      </div>
      <input type="text" id="am-name" class="am-name" style="display:none;" maxlength="60">
      <div class="am-step"><span class="am-num">2</span> What should it remember? <span class="dim">one point per line, in your own words</span></div>
      <textarea id="am-notes" rows="4" placeholder="I work in the payments team&#10;Keep answers short and use simple English"></textarea>
      <div class="am-actions"><button type="button" class="btn accent" id="am-save" style="width:auto;">Save to memory</button><span id="am-msg" class="dim"></span></div>
    </div>
    <div class="am-step am-list-title">Saved notes <span class="dim" id="am-count"></span></div>
    <div id="am-list" class="am-list"><div class="dim">Loading…</div></div>
  </div>`;
}

async function amLoadList(box) {
  const list = box.querySelector('#am-list');
  if (!list) return;
  try {
    const r = await fetch('/agent/memory');
    const j = await r.json();
    if (!r.ok) throw new Error(j.error || j.detail || r.status);
    const files = j.files || [];
    box.querySelector('#am-count').textContent = files.length ? `${files.length}${j.limits && j.limits.max_files ? ' of ' + j.limits.max_files : ''}` : '';
    if (!files.length) { list.innerHTML = '<div class="dim am-empty">Nothing saved yet. Add your first note above, or just tell the agent a preference in a chat and it is saved here.</div>'; return; }
    const nice = (p) => {
      const t = AM_TOPICS.find(x => x[3] === p);
      if (t) return t[1];
      const m = p.match(/^projects\/(.+)\.md$/);
      const raw = (m ? m[1] : p.replace(/\.md$/, '')).replace(/[-_]+/g, ' ');
      return (m ? 'Project: ' : '') + raw.charAt(0).toUpperCase() + raw.slice(1);
    };
    list.innerHTML = files.map(f => `<div class="am-row"><div class="am-row-main"><b>${esc(nice(f.path))}</b><span class="dim">${esc(f.description || f.path)}</span></div>
      <button type="button" class="btn ghost am-del" data-path="${esc(f.path)}" title="Delete this note">Delete</button></div>`).join('');
    list.querySelectorAll('.am-del').forEach(b => {
      b.onclick = async () => {
        if (!confirm('Delete "' + b.dataset.path + '"?')) return;
        try {
          const rr = await fetch('/agent/memory/' + b.dataset.path.split('/').map(encodeURIComponent).join('/'), { method: 'DELETE' });
          if (!rr.ok) throw new Error((await rr.json().catch(() => ({}))).error || rr.status);
          amLoadList(box);
        } catch (e) { toast('Delete failed: ' + e.message, true); }
      };
    });
  } catch (e) { list.innerHTML = '<div class="dim am-empty">Could not load your notes: ' + esc(e.message) + '</div>'; }
}

/* The editor lives in the React app that hosts this page; ask it to open (see SettingsModal.tsx). */
function wireAgentMemory(box) {
  const b = box.querySelector('#btn-open-agent-memory');
  if (!b) return;
  amLoadList(box);
  let topic = 'about';
  const name = box.querySelector('#am-name'), notes = box.querySelector('#am-notes'), msg = box.querySelector('#am-msg');
  box.querySelectorAll('#am-topics .am-chip').forEach(c => {
    c.onclick = () => {
      topic = c.dataset.topic;
      box.querySelectorAll('#am-topics .am-chip').forEach(x => x.classList.toggle('on', x === c));
      const needsName = topic === 'project' || topic === 'other';
      name.style.display = needsName ? '' : 'none';
      name.placeholder = topic === 'project' ? 'Project name, e.g. Payment portal' : 'Topic name, e.g. Travel';
    };
  });
  box.querySelector('#am-save').onclick = async () => {
    const t = AM_TOPICS.find(x => x[0] === topic);
    const lines = notes.value.split('\n').map(l => l.trim().replace(/^[-*•]\s*/, '')).filter(Boolean).map(l => '- ' + l);
    msg.textContent = '';
    if (!lines.length) { msg.textContent = 'Type at least one thing for the agent to remember.'; return; }
    let path = t[3], desc = t[4];
    if (!path) {
      const slug = name.value.toLowerCase().replace(/[^a-z0-9]+/g, '-').replace(/^-+|-+$/g, '');
      if (!slug) { msg.textContent = topic === 'project' ? 'Give the project a name.' : 'Give this topic a name.'; return; }
      path = (topic === 'project' ? 'projects/' : '') + slug + '.md';
      desc = topic === 'project' ? `Notes about the ${name.value.trim()} project` : `Notes: ${name.value.trim()}`;
    }
    try {
      const url = '/agent/memory/' + path.split('/').map(encodeURIComponent).join('/');
      const ex = await fetch(url);
      let content = `---\ndescription: ${desc}\n---\n${lines.join('\n')}`, version = null;
      if (ex.ok) {                      // an existing note gets the new points at the end instead of being replaced
        const f = await ex.json();
        content = (f.text || '').replace(/\s*$/, '') + '\n' + lines.join('\n');
        version = f.version;
      } else if (ex.status !== 404) throw new Error((await ex.json().catch(() => ({}))).error || ex.status);
      const r = await fetch(url, { method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ content, if_version: version }) });
      const j = await r.json().catch(() => ({}));
      if (!r.ok) throw new Error(j.error || j.detail || r.status);
      notes.value = ''; name.value = '';
      toast('Saved to your agent memory ✓');
      amLoadList(box);
    } catch (e) { msg.textContent = 'Save failed: ' + e.message; }
  };
  b.onclick = () => {
    if (window.parent && window.parent !== window) window.parent.postMessage({ type: 'open-agent-memory' }, location.origin);
    else toast('Open the agent memory from the app.', true);
  };
}
