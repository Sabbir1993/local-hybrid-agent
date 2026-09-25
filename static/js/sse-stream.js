// Shared Server-Sent Events reader for /chat/run and /agent/run, plus the
// event handling both streams do identically. chat.js and agent-run.js keep
// their own handling for everything else.

// Parse one SSE frame ("event: x\ndata: {...}") -> [event, dataText] or null.
// Multi-line data is joined with newlines, per the SSE format.
function parseSSEFrame(raw) {
  let ev = null;
  const data = [];
  for (const line of raw.split('\n')) {
    if (line.startsWith('event:')) ev = line.slice(6).trim();
    else if (line.startsWith('data:')) data.push(line.slice(5).replace(/^ /, ''));
  }
  return ev && data.length ? [ev, data.join('\n')] : null;
}

// Read a fetch() response body as SSE and call onEvent(event, data) per frame.
// A frame with bad JSON is skipped (logged), not fatal. An exception thrown by
// onEvent ends the read and propagates to the caller.
async function readSSE(res, onEvent) {
  const reader = res.body.getReader();
  const dec = new TextDecoder();
  let buf = '';
  const drain = (final) => {
    buf = buf.replace(/\r\n?/g, '\n');
    let i;
    while ((i = buf.indexOf('\n\n')) >= 0 || (final && buf.trim())) {
      let raw;
      if (i >= 0) { raw = buf.slice(0, i); buf = buf.slice(i + 2); }
      else { raw = buf; buf = ''; }
      const frame = parseSSEFrame(raw);
      if (!frame) continue;
      let d;
      try { d = JSON.parse(frame[1]); }
      catch (e) { console.warn('[sse] skipped a malformed', frame[0], 'event'); continue; }
      onEvent(frame[0], d || {});
    }
  };
  while (true) {
    const { done, value } = await reader.read();
    if (done) break;
    window.markActive && window.markActive();   // a running job keeps the session alive
    buf += dec.decode(value, { stream: true });
    drain(false);
  }
  buf += dec.decode();
  drain(true);
}

function _sseHud(state) {
  if (typeof window.setLiveHud === 'function') window.setLiveHud(state);
}

function _ssePath(d) {
  return (d.args && (d.args.path || d.args.file || d.args.filename)) || '';
}

// tool_preparing: the model is still streaming a tool call's arguments
function sseToolPreparing(L, d) {
  const p = d.path ? d.path.split(/[\\/]/).pop() : '';
  const actionVerb = d.name === 'write_file' ? 'Preparing to write' : (d.name === 'edit_file' ? 'Preparing to edit' : `Preparing ${d.name}`);
  const label = p ? `${actionVerb} ${p}...` : `${actionVerb}...`;
  L.statusText = label;
  const bytesStr = d.bytes ? ` (~${Math.round(d.bytes / 4)} tokens)` : '';
  _sseHud({ phase: 'preparing', name: d.name, path: d.path, text: label, subtext: bytesStr });
}

// tool_call: record it, show it in the HUD, and move any preamble the model
// streamed before the call into the reasoning section
function sseToolCall(L, d) {
  if (!L.acts) L.acts = [];
  L.acts.push({ type: 'tool_call', ...d });
  L.statusText = (typeof formatToolStatus === 'function') ? formatToolStatus(d.name, d.args) : `Running ${d.name}...`;
  _sseHud({
    phase: (d.name === 'write_file' || d.name === 'edit_file') ? 'writing' : 'running',
    name: d.name,
    path: _ssePath(d),
    text: L.statusText
  });
  if (L.content && L.content.trim()) {
    L.reasoning = (L.reasoning ? L.reasoning + '\n\n' : '') + L.content.trim();
    L.content = '';
  }
}

// tool_progress: a long tool (image/video generation) reports its step; kept on
// its tool_call act (latest only) so the card can show a bar
function sseToolProgress(L, d) {
  const call = (L.acts || []).slice().reverse().find(a => a.type === 'tool_call' && (a.id === d.id || (!d.id && a.name === d.name)));
  if (!call) return;
  call.progress = { text: d.text || '', pct: d.pct, elapsed: d.elapsed };
  L.statusText = d.text || L.statusText;
  _sseHud({ phase: 'running', name: d.name, text: d.text || 'Working…' });
}

// tool_result: record it; a file write/edit also gets a toast + HUD with Preview/Reveal
function sseToolResult(L, d) {
  if (!L.acts) L.acts = [];
  L.acts.push({ type: 'tool_result', ...d });
  L.statusText = 'Crunching tool results...';
  if (d.name !== 'write_file' && d.name !== 'edit_file') return;
  const p = _ssePath(d);
  const filename = p ? p.split(/[\\/]/).pop() : 'file';
  const isSuccess = d.ok !== false;
  const verb = d.name === 'write_file' ? 'Saved' : 'Updated';
  const actions = [];
  if (p && isSuccess && typeof openFilePreview === 'function') {
    actions.push({ label: '👁️ Preview', onClick: () => openFilePreview(p, filename) });
  }
  if (p && typeof wsShowFile === 'function') {
    actions.push({
      label: '📂 Reveal',
      onClick: () => {
        if (typeof setWsPanel === 'function') setWsPanel(true);
        if (typeof wsShowFile === 'function') wsShowFile(p);
      }
    });
  }
  if (typeof toast === 'function') {
    toast(isSuccess ? `💾 ${verb} ${filename}` : `⚠️ Failed to write ${filename}`, {
      isErr: !isSuccess,
      duration: 5000,
      actions: actions
    });
  }
  _sseHud({
    phase: isSuccess ? 'done' : 'error',
    name: d.name,
    path: p,
    text: isSuccess ? `💾 ${verb} ${filename}` : `⚠️ Failed to save ${filename}`,
    actions: actions
  });
}

// kb_blocked: data residency kept the KB from a cloud lane; show it as a failed KB step
function sseKbBlocked(L, d) {
  if (!L.acts) L.acts = [];
  L.acts.push({ type: 'tool_call', id: 'kb_blocked', name: 'search_knowledge_base', args: {} });
  L.acts.push({ type: 'tool_result', id: 'kb_blocked', name: 'search_knowledge_base', ok: false, result: d.message || '' });
}

// guard: the output sanitizer redacted part of the response
function sseGuardToast(d) {
  if (typeof toast === 'function') toast('🧼 ' + (d.message || ('Response filtered by policy: ' + (d.rule || ''))));
}

// Split inline reasoning out of an answer: a leading <think>...</think> block, or
// (templates that pre-fill <think> in the prompt) text ending in a bare </think>.
// Also cleans answers saved before the server did this split.
function splitThink(text) {
  const s = text || '';
  const m = s.match(/^\s*<think>([\s\S]*?)<\/think>\s*/);
  if (m) return { reasoning: m[1].trim(), content: s.slice(m[0].length) };
  const i = s.indexOf('</think>');
  if (i >= 0 && s.lastIndexOf('<think>', i) < 0) {
    return { reasoning: s.slice(0, i).trim(), content: s.slice(i + 8).replace(/^\s+/, '') };
  }
  return { reasoning: '', content: s };
}

// Move a parsed think block from L.content into L.reasoning; returns the moved text.
function sseApplyThinkSplit(L) {
  const sp = splitThink(L.content);
  if (!sp.reasoning) return '';
  L.reasoning = L.reasoning ? L.reasoning + '\n\n' + sp.reasoning : sp.reasoning;
  L.content = sp.content;
  return sp.reasoning;
}

// delta_to_thought: the text streamed as the answer so far was reasoning
// (forced-open <think>); returns the moved text
function sseDeltaToThought(L) {
  const moved = (L.content || '').trim();
  L.content = '';
  if (moved) L.reasoning = L.reasoning ? L.reasoning + '\n\n' + moved : moved;
  return moved;
}

if (typeof module !== 'undefined') module.exports = { parseSSEFrame, readSSE, splitThink, sseApplyThinkSplit, sseDeltaToThought };
