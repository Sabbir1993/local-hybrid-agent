/* ---------------- Settings: Chat limits (admin) - every chat-mode limit in one place (core/chat_limits.py) ---------------- */
const CHAT_LEVELS = [
  ['none', 'None', 'No thinking (direct answer)'], ['low', 'Low', 'Quick check'], ['medium', 'Medium', 'Standard analysis'],
  ['high', 'High', 'Thorough research'], ['extra', 'Extra', 'Deep synthesis'], ['deep', 'Deep research', 'Exhaustive web discovery'],
];
const CHAT_LIMIT_GROUPS = [
  ['Tool loop', [['project_max_rounds', 'Rounds for a website build'], ['doc_max_rounds', 'Rounds for a workbook / report / deck'],
    ['repeat_call_limit', 'Same tool call before it is stopped'], ['runaway_retries', 'Restarts after a runaway reply'],
    ['auto_continue', 'Auto-continue a cut-off reply'], ['run_timeout_s', 'Wall-clock limit of one turn (seconds)']]],
  ['Web', [['web_soft_chars', 'Web result kept per page (characters)']]],
  ['Decks, PDFs and sites', [['deck_research_calls', 'Web calls for a deck / PDF build'], ['deck_max_tokens', 'Tokens of one deck / PDF turn'],
    ['project_max_tokens', 'Tokens of one website turn'], ['deck_repeat_penalty', 'Repeat penalty while writing a deck'],
    ['build_temperature', 'Highest temperature while writing a file']]],
  ['Prompt', [['sticky_max_tokens', 'Manuals kept at the start of the prompt (tokens)']]],
];
const CHAT_FLOAT_KEYS = ['deck_repeat_penalty', 'build_temperature'];
const CHAT_WEB_ROWS = [['full', 'Knowledge Base covers it'], ['partial', 'Covers part of it'], ['open', 'Nothing in it / you asked for the web']];
const CHAT_WEB_TIERS = [['low', 'Low'], ['medium', 'Medium'], ['high', 'High'], ['deep', 'Deep']];

function chatLimitsHtml(d, canEdit) {
  const inp = 'margin:0; background:var(--bg-input); color:var(--text); border:1px solid var(--border); border-radius:7px; padding:6px 10px; font-size:12px; box-sizing:border-box; text-align:right;';
  const dis = canEdit ? '' : 'disabled';
  const lr = d.level_ranges;
  const cell = (lvl, field, w) => `<td style="padding:6px 10px;"><input type="number" class="cl-lv" data-lvl="${lvl}" data-field="${field}" min="${lr[field][0]}" max="${lr[field][1]}" ${field === 'token_budget' ? 'step="500"' : ''}
    value="${d.levels[lvl][field]}" ${dis} style="${inp} width:${w}px;" title="default ${d.level_defaults[lvl][field]}"></td>`;
  const row = ([key, label]) => `
    <div style="display:flex; align-items:center; gap:6px; margin-top:6px; flex-wrap:wrap;" title="${esc(d.help[key])}">
      <span style="min-width:260px;">${esc(label)}</span>
      <input type="number" class="cl-in" data-key="${key}" min="${d.ranges[key][0]}" max="${d.ranges[key][1]}" ${CHAT_FLOAT_KEYS.includes(key) ? 'step="0.05"' : ''}
        value="${d.values[key]}" ${dis} style="${inp} width:94px;">
      <span class="dim" style="font-size:9.5px;">${d.ranges[key][0]}–${d.ranges[key][1]} · default ${d.defaults[key]} · ${esc(d.help[key])}</span>
    </div>`;
  return `
    <div class="cap-item" style="margin-bottom:10px;">
      <b>Per thinking level</b>
      <div class="dim" style="font-size:9.5px; margin-top:4px;">The level is the one picked in the chat prompt bar. Max tool calls caps tool / web calls in one turn (0 = no tools), Max tool rounds caps the model-tool loop, Token budget is the output limit of one turn.</div>
      <div class="dim" style="font-size:9.5px; margin-top:4px;">${esc(d.turn_token_note)} At level None the lower of the two is used.</div>
      <div style="overflow-x:auto; margin-top:8px;">
        <table style="width:100%; border-collapse:collapse; font-size:12px; min-width:480px;">
          <thead><tr style="border-bottom:1px solid var(--border-subtle); color:var(--dim); font-size:11px; text-align:left;">
            <th style="padding:6px 10px; font-weight:600;">Thinking level</th><th style="padding:6px 10px; font-weight:600;">Max tool calls</th>
            <th style="padding:6px 10px; font-weight:600;">Token budget</th><th style="padding:6px 10px; font-weight:600;">Max tool rounds</th></tr></thead>
          <tbody>${CHAT_LEVELS.map(([k, label, desc]) => `<tr style="border-bottom:1px solid rgba(255,255,255,0.04);">
            <td style="padding:8px 10px; font-weight:600; white-space:nowrap;"><div>${esc(label)}</div><div class="dim" style="font-size:9.5px; font-weight:normal;">${esc(desc)}</div></td>
            ${cell(k, 'tool_calls', 85)}${cell(k, 'token_budget', 105)}${cell(k, 'max_rounds', 85)}</tr>`).join('')}</tbody>
        </table>
      </div>
    </div>
    <div class="cap-item" style="margin-bottom:10px;">
      <b>Web calls by Knowledge Base coverage</b>
      <div class="dim" style="font-size:9.5px; margin-top:4px;">How many web calls a turn gets once the Knowledge Base has been checked, by thinking tier (None and Low share Low; Extra and Deep research use Deep).</div>
      <div style="overflow-x:auto; margin-top:8px;">
        <table style="width:100%; border-collapse:collapse; font-size:12px; min-width:420px;">
          <thead><tr style="border-bottom:1px solid var(--border-subtle); color:var(--dim); font-size:11px; text-align:left;">
            <th style="padding:6px 10px; font-weight:600;">Coverage</th>${CHAT_WEB_TIERS.map(([, l]) => `<th style="padding:6px 10px; font-weight:600;">${l}</th>`).join('')}</tr></thead>
          <tbody>${CHAT_WEB_ROWS.map(([r, label]) => `<tr style="border-bottom:1px solid rgba(255,255,255,0.04);"><td style="padding:8px 10px; font-weight:600;">${esc(label)}</td>
            ${CHAT_WEB_TIERS.map(([t]) => `<td style="padding:6px 10px;"><input type="number" class="cl-wb" data-row="${r}" data-tier="${t}" min="${d.web_budget_range[0]}" max="${d.web_budget_range[1]}"
              value="${d.web_budget[r][t]}" ${dis} style="${inp} width:70px;" title="default ${d.web_budget_defaults[r][t]}"></td>`).join('')}</tr>`).join('')}</tbody>
        </table>
      </div>
    </div>
    <div class="cap-item" id="chat-limits-box" style="margin-bottom:10px;">
      ${CHAT_LIMIT_GROUPS.map(([title, rows]) => `<div style="margin-top:8px;"><b>${esc(title)}</b>${rows.map(row).join('')}</div>`).join('')}
      <div class="dim" style="font-size:9.5px; margin-top:10px;">Changes apply to the next chat turn. A value outside its range is brought back into it when saved.</div>
      ${canEdit ? '<div style="margin-top:10px;"><button class="btn accent" id="cl-save" style="width:auto; margin:0; padding:3px 12px; font-size:10.5px;">Save</button></div>' : ''}
    </div>`;
}

function wireChatLimits(box) {
  const save = box.querySelector('#cl-save');
  if (!save) return;
  save.onclick = async () => {
    const body = { thinking_budgets: {}, web_budget: {} };
    box.querySelectorAll('.cl-lv').forEach(i => {
      const n = parseInt(i.value, 10);
      if (!isNaN(n)) (body.thinking_budgets[i.dataset.lvl] = body.thinking_budgets[i.dataset.lvl] || {})[i.dataset.field] = n;
    });
    box.querySelectorAll('.cl-wb').forEach(i => {
      const n = parseInt(i.value, 10);
      if (!isNaN(n)) (body.web_budget[i.dataset.row] = body.web_budget[i.dataset.row] || {})[i.dataset.tier] = n;
    });
    box.querySelectorAll('#chat-limits-box .cl-in').forEach(i => { const n = parseFloat(i.value); if (!isNaN(n)) body[i.dataset.key] = n; });
    try {
      const r = await fetch('/control/chat_limits', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) });
      const j = await r.json();
      if (!r.ok) throw new Error(j.error || j.detail || r.status);
      box.querySelectorAll('.cl-lv').forEach(i => { i.value = j.levels[i.dataset.lvl][i.dataset.field]; });
      box.querySelectorAll('.cl-wb').forEach(i => { i.value = j.web_budget[i.dataset.row][i.dataset.tier]; });
      box.querySelectorAll('#chat-limits-box .cl-in').forEach(i => { if (j.values[i.dataset.key] != null) i.value = j.values[i.dataset.key]; });
      toast('Chat limits saved ✓ (applies to the next chat turn)');
    } catch (e) { toast('Save failed: ' + e.message, true); }
  };
}
