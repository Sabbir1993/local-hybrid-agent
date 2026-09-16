/* ---------------- agent SSE runner ---------------- */
async function runAgentSSE(text) {
  if (!curStatus || !curStatus.pid) {
    const sel = $('profile');
    if (!sel || !sel.value) {
      toast('Please select a model from the top dropdown first', true);
      return;
    }
    const mName = sel.value.split('\\').pop().split('/').pop();
    toast(`⏳ Loading ${mName} into GPU VRAM before running task...`);
    await loadSelectedModel();
    await pollStatus();
    if (!curStatus || !curStatus.pid) {
      toast('Model loading failed or still in progress. Please wait a moment and try again.', true);
      return;
    }
  }
  const input = $('input');
  const sentAttachments = attachments.slice();
  const sentImages = sentAttachments.filter(a => a.isImage && a.dataUrl).map(a => a.dataUrl);
  const sentFiles = sentAttachments.map(a => a.name).join(', ');
  const nFiles = sentAttachments.filter(a => a.content != null).length;
  if (input) input.value = '';
  clearAttachments();

  const userMsg = {
    role: 'user',
    content: text || (sentFiles ? `📎 ${sentFiles}` : '(attachment)'),
    displayContent: text,
    images: sentImages.length ? sentImages : undefined,
    files: sentFiles || undefined
  };
  messages.push(userMsg);

  const assistantMsg = {
    role: 'assistant',
    content: '',
    reasoning: '',
    acts: [],
    statusText: sentImages.length ? '🔍 Analyzing image...' : ''
  };
  messages.push(assistantMsg);

  renderAll();
  setGenUI(true);
  ctrl = new AbortController();

  let fullPrompt = text;
  try {
    fullPrompt = await buildPromptText(text, sentAttachments, ctrl.signal);
    userMsg.content = fullPrompt;
  } catch (err) {
    if (err.name === 'AbortError') {
      assistantMsg.content = '⏹️ Generation cancelled';
      assistantMsg.statusText = '';
      setGenUI(false);
      renderLast();
      return;
    }
    console.warn('Agent prompt build warning:', err);
  }
  assistantMsg.statusText = '';
  renderLast();

  const userMeta = {
    images: sentImages.length ? sentImages : undefined,
    files: sentFiles || undefined,
    displayContent: text || undefined
  };
  // await session creation so the structured-plan tools receive a real session_id
  await ensureSession((text || (sentFiles ? `📎 ${sentFiles}` : 'Agent task')).slice(0, 60));
  persistMsg('user', fullPrompt, userMeta);
  const last = () => messages[messages.length - 1];
  (async () => {
    let usage = null;
    const t0 = performance.now();
    try {
      const hist = messages.slice(0, -1).map(m => ({ role: m.role, content: m.content }));
      const engineMode = $('agent-engine') ? $('agent-engine').value : 'tiered';
      // Collect doc attachments that were server-uploaded for context injection
      const docAttachments = sentAttachments
        .filter(a => a.isDoc && a.serverPath)
        .map(a => ({ name: a.name, path: a.serverPath, preview: a.preview || '', truncated: a.truncated || false }));
      const res = await fetch('/agent/run', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          messages: hist,
          mode: engineMode,
          plan: planMode,
          session_id: (curSession && curSession.id) ? curSession.id : null,
          temperature: parseFloat($('temp').value),
          max_tokens: (isNaN(parseInt($('maxtok').value)) || parseInt($('maxtok').value) <= 0) ? -1 : parseInt($('maxtok').value),
          attachments: docAttachments,
        }),
        signal: ctrl.signal,
      });
      if (!res.ok) {
        const e = await res.json().catch(() => ({}));
        throw new Error(e.error || ('HTTP ' + res.status));
      }
      const reader = res.body.getReader();
      const dec = new TextDecoder();
      let buf = '';
      while (true) {
        const { done, value } = await reader.read();
        if (done) break;
        buf += dec.decode(value, { stream: true });
        let i;
        while ((i = buf.indexOf('\n\n')) >= 0) {
          const raw = buf.slice(0, i);
          buf = buf.slice(i + 2);
          const evM = raw.match(/^event: (.+)$/m);
          const dtM = raw.match(/^data: (.+)$/m);
          if (!evM || !dtM) continue;
          const ev = evM[1], d = JSON.parse(dtM[1]);
          const L = last();
          if (ev === 'step') L.acts.push({ type: 'step', ...d });
          else if (ev === 'lane') L.acts.push({ type: 'lane', ...d });
          else if (ev === 'thought') L.acts.push({ type: 'thought', ...d });
          else if (ev === 'thought_delta') {
            L.reasoning = (L.reasoning || '') + (d.delta || '');
          }
          else if (ev === 'tool_call') {
            // If the model was streaming its preamble before calling a tool, keep it as thought/reasoning or preamble
            L.acts.push({ type: 'tool_call', ...d });
            if (L.content && L.content.trim()) {
              if (!L.reasoning) L.reasoning = L.content.trim();
              else L.reasoning += '\n\n' + L.content.trim();
              L.content = '';
            }
          }
          else if (ev === 'tool_result') {
            L.acts.push({ type: 'tool_result', ...d });
            // agent changed a file -> refresh the workspace side panel
            if (wsPanelOpen && (d.name === 'write_file' || d.name === 'edit_file' || d.name === 'revert')) {
              wsRefreshTree();
            }
          }
          else if (ev === 'verify') L.acts.push({ type: 'verify', ...d });
          else if (ev === 'permission_request') showPermModal(d.req_id, d.cmd);
          else if (ev === 'delta') L.content += (d.text || '');
          else if (ev === 'delta_reset') {
            // If replacing content with synthesized final answer, preserve any prior streamed text as reasoning so it doesn't vanish
            if (L.content && L.content.trim()) {
              if (!L.reasoning) L.reasoning = L.content.trim();
              else L.reasoning += '\n\n' + L.content.trim();
            }
            L.content = '';
          }
          else if (ev === 'validated') L.acts.push({ type: 'validated', ...d });
          else if (ev === 'plan') {
            // structured plan checklist — keep only the latest snapshot in acts
            if (!L.acts) L.acts = [];
            const planAct = { type: 'plan', items: d.items || [] };
            const pi = L.acts.findIndex(a => a.type === 'plan');
            if (pi >= 0) L.acts[pi] = planAct; else L.acts.push(planAct);
          }
          else if (ev === 'error') throw new Error(d.error);
          renderLast();
        }
      }
      if (!last().content && last().acts && last().acts.length > 0) {
        last().content = 'Task completed. See tool operations above for details.';
      }
      const dt = (performance.now() - t0) / 1000;
      const fullLen = (last().content || '').length + (last().reasoning || '').length;
      const ntok = Math.max(1, Math.round(fullLen / 3.5));
      last().tps = ntok / dt; last().ntok = ntok; last().secs = dt;
      if (ntok > 1) $('chip-ts').textContent = '⚡ ' + (ntok / dt).toFixed(1) + ' t/s';
      persistMsg('assistant', last().content, { tps: last().tps, ntok, secs: dt, reasoning: last().reasoning || undefined, acts: last().acts });
    } catch (e) {
      if (e.name !== 'AbortError') {
        last().content += (last().content ? '\n\n' : '') + '⚠️ ' + e.message;
      }
    }
    ctrl = null;
    setGenUI(false);
    renderLast();
    updateContextChip();
    if (wsPanelOpen) wsRefreshTree();   // final state of workspace after the task
  })();
}
