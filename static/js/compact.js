/* ---------------- /compact — context compaction (like Claude Code) ---------------- */

/* Map DB-shaped messages back to in-memory chat objects (mirrors openSession). */
function compactMapMessages(rawMsgs) {
  return (rawMsgs || []).map(m => {
    const meta = m.meta || {};
    const textLen = (m.content || '').length + (meta.reasoning || '').length;
    const tok = (typeof meta.ntok === 'number') ? meta.ntok : Math.max(1, Math.round(textLen / 3.5));
    return {
      role: m.role,
      content: m.content || '',
      displayContent: meta.displayContent || undefined,
      images: meta.images || undefined,
      files: meta.files || undefined,
      reasoning: meta.reasoning || '',
      acts: meta.acts || [],
      tps: meta.tps,
      ntok: tok,
      secs: meta.secs,
      compact: meta.compact || undefined,
      compactBefore: meta.before_tokens,
      compactAfter: meta.after_tokens,
      reductionPct: meta.reduction_pct,
      compactKept: meta.kept_messages,
      modelDisplay: meta.modelDisplay || undefined,
      modelSource: meta.modelSource || undefined,
      modelProvider: meta.modelProvider || undefined,
    };
  });
}

async function doCompact(extraInstructions) {
  if (generating) { toast('Wait for the current generation to finish first', true); return; }
  // Project gate: in agent mode /compact only works with an active project
  // (same rule as Claude Code — agent context is project-scoped).
  if (agentMode && (!curProject || !curProject.id)) {
    if (typeof flashProjectsCard === 'function') flashProjectsCard();
    else toast('Please select a project first', true);
    return;
  }
  const hist = messages.filter(m => (m.role === 'user' || m.role === 'assistant' || m.compact)
    && (m.content || '').trim());
  if (hist.length < 2) { toast('Nothing to compact yet — send a few messages first', true); return; }

  const beforeToks = hist.reduce((a, m) => a + (m.ntok || Math.round((m.content || '').length / 3.5)), 0);
  toast('🧹 Compacting conversation…');

  // Provide visible in-chat feedback that compaction is in progress
  setGenUI(true);
  const pendingMsg = {
    role: 'assistant',
    compactPending: true,
    beforeToks: beforeToks,
    content: extraInstructions ? `Instructions: "${extraInstructions}"` : ''
  };
  messages.push(pendingMsg);
  renderAll();

  try {
    const r = await fetch('/chat/compact', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        messages: hist.map(m => ({ role: m.role, content: m.content })),
        session_id: (curSession && curSession.id) ? curSession.id : null,
        instructions: extraInstructions || undefined,
        agent_mode: !!agentMode,
        project_id: (curProject && curProject.id) ? curProject.id : null,
      }),
    });
    const j = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(j.error || ('HTTP ' + r.status));
    messages = messages.filter(m => !m.compactPending);
    messages.push(compactMapMessages([j.compact_message])[0]);
    renderAll();
    updateContextChip();
    const kb = n => n >= 1000 ? (n / 1000).toFixed(1) + 'k' : String(n);
    const pct = j.before_tokens > 0 ? Math.max(0, Math.round((1 - j.after_tokens / j.before_tokens) * 100)) : 0;
    toast(`🧹 Compacted: ${kb(j.before_tokens)} → ${kb(j.after_tokens)} tokens (${pct}% reduction)`);
  } catch (e) {
    messages = messages.filter(m => !m.compactPending);
    renderAll();
    toast('Compact failed: ' + e.message, true);
  } finally {
    setGenUI(false);
  }
}

/* Auto-compact: agent mode, active project, history close to the context limit. */
async function autoCompactIfNeeded(hist) {
  if (!agentMode || !curProject || !curProject.id) return true;   // gate: needs a project
  const est = hist.reduce((a, m) => a + Math.round((m.content || '').length / 3.5) + 8, 0);
  if (est < curCtxMax * 0.85) return true;
  try {
    const r = await fetch('/chat/compact', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        session_id: (curSession && curSession.id) ? curSession.id : null,
        instructions: 'Automatic compaction: the context window was almost full. Keep everything needed to continue the current task.',
        keep_last: 4,
        agent_mode: true,
        project_id: (curProject && curProject.id) ? curProject.id : null,
      }),
    });
    const j = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(j.error || ('HTTP ' + r.status));
    messages.push(compactMapMessages([j.compact_message])[0]);
    renderAll();
    const kb = n => n >= 1000 ? (n / 1000).toFixed(1) + 'k' : String(n);
    toast(`🧹 Auto-compacted near the context limit: ${kb(j.before_tokens)} → ${kb(j.after_tokens)} tokens`);
    return true;
  } catch (e) {
    console.warn('auto-compact failed:', e);
    return true;   // never block the run because compaction failed
  }
}

/* Context sent to the model: from the latest compact marker forward (plus the
   verbatim tail it preserved), not the full visible transcript. Falls back to
   full history when the session has never been compacted. */
function buildContextMessages() {
  let idx = -1;
  for (let i = messages.length - 1; i >= 0; i--) {
    if (messages[i].compact) { idx = i; break; }
  }
  if (idx === -1) return messages.slice();
  const marker = messages[idx];
  const keptCount = marker.compactKept || 0;
  const keptTail = messages.slice(Math.max(0, idx - keptCount), idx);
  const after = messages.slice(idx + 1);
  return [marker, ...keptTail, ...after];
}

window.doCompact = doCompact;
window.autoCompactIfNeeded = autoCompactIfNeeded;
window.buildContextMessages = buildContextMessages;
