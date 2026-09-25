/* ---------------- agent SSE runner ---------------- */
async function runAgentSSE(text) {
  // Agent tasks are project-scoped (like Claude Code): no project -> refuse.
  if (agentMode && (!curProject || !curProject.id)) {
    if (typeof flashProjectsCard === 'function') flashProjectsCard();
    else toast('Please select a project first', true);
    return;
  }
  if (!mainLaneReady()) {
    const sel = $('profile');
    if (!sel || !sel.value) {
      toast('Please select a model from the top dropdown first', true);
      return;
    }
    const mName = sel.value.split('\\').pop().split('/').pop();
    toast(`⏳ Loading ${mName} into GPU VRAM before running task...`);
    await loadSelectedModel();
    await pollStatus();
    if (!mainLaneReady()) {
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
  if (window.renderInputHighlights) window.renderInputHighlights();
  clearAttachments();

  // Auto-compact when the context window is nearly full — agent mode with an
  // active project only (see autoCompactIfNeeded). Runs before the new turn so
  // the streaming placeholder below is preserved. Never blocks the run.
  if (typeof autoCompactIfNeeded === 'function') {
    const ctxMsgs = typeof buildContextMessages === 'function' ? buildContextMessages() : messages;
    await autoCompactIfNeeded(ctxMsgs.map(m => ({ role: m.role, content: m.content })));
  }

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
    statusText: sentImages.length ? '🔍 Analyzing image...' : (nFiles ? '📄 Loading attached files...' : '')
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
  if (!assistantMsg.statusText) assistantMsg.statusText = 'Starting agent workflow...';
  renderLast();

  const userMeta = {
    images: sentImages.length ? sentImages : undefined,
    files: sentFiles || undefined,
    displayContent: text || undefined
  };
  // await session creation so the structured-plan tools receive a real session_id
  const session = await ensureSession((text || (sentFiles ? `📎 ${sentFiles}` : 'Agent task')).slice(0, 60));
  const sessionId = session ? session.id : (curSession ? curSession.id : 0);
  const sessionTitle = session ? session.title : (curSession ? curSession.title : (text || 'Agent task').slice(0, 40));
  persistMsgForSession(sessionId, 'user', fullPrompt, userMeta);

  // Register into background jobs
  const jobCtrl = ctrl;
  const job = {
    id: sessionId,
    title: sessionTitle,
    mode: 'agent',
    ctrl: jobCtrl,
    messages: messages,
    assistantMsg: assistantMsg,
    t0: performance.now(),
  };
  window.bgJobs.set(String(sessionId), job);
  if (typeof updateBgIndicators === 'function') updateBgIndicators();

  const getJobAssistant = () => job.assistantMsg;

  (async () => {
    let usage = null;
    const t0 = performance.now();
    try {
      const ctxMsgs = typeof buildContextMessages === 'function' ? buildContextMessages() : job.messages;
      const hist = ctxMsgs.slice(0, -1).map(m => ({ role: m.role, content: m.content }));
      const engineMode = $('agent-engine') ? $('agent-engine').value : 'all-local';
      const cloudModelOverride = (() => {
        const sel = $('cloud-model-sel');
        return (sel && sel.value) ? sel.value : (localStorage.getItem('cloud_model_override') || null);
      })();
      // Collect doc attachments that were server-uploaded for context injection
      const docAttachments = sentAttachments
        .filter(a => a.isDoc && a.serverPath)
        .map(a => ({ name: a.name, path: a.serverPath, preview: a.preview || '', truncated: a.truncated || false }));
      const res = await fetch('/agent/run', {
        method: 'POST',
        // device identity selects this machine's project; without it the server refuses the run
        headers: { 'Content-Type': 'application/json', ...getDeviceHeaders() },
        body: JSON.stringify({
          messages: hist,
          mode: engineMode,
          plan: planMode,
          session_id: sessionId || null,
          temperature: getSamplingConfig().temp,
          max_tokens: (() => { const mt = getSamplingConfig().maxtok; return (isNaN(mt) || mt <= 0) ? -1 : mt; })(),
          attachments: docAttachments,
          cloud_model_override: cloudModelOverride || undefined,
          reasoning_effort: typeof getReasoningEffort === 'function' ? getReasoningEffort() : undefined,
        }),
        signal: jobCtrl.signal,
      });
      if (!res.ok) {
        const e = await res.json().catch(() => ({}));
        throw new Error(e.message || e.error || ('HTTP ' + res.status));
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
          const L = getJobAssistant();
          if (ev === 'run') {
            L.runId = d.run_id;   // routing telemetry id -> thumbs up/down feedback
          }
          else if (ev === 'step') {
            if (L._curThought) {
              L._curThought.duration_s = Math.max(1, Math.round((performance.now() - L._curThought.t0) / 1000));
              L._curThought = null;
            }
            L.acts.push({ type: 'step', ...d });
            L.statusText = d.step ? `Planning step ${d.step}...` : 'Planning next step...';
          }
          else if (ev === 'lane') {
            L.acts.push({ type: 'lane', ...d });
            L.modelDisplay = d.display || d.model;
            L.modelSource = d.source;
            L.modelProvider = d.provider;
          }
          else if (ev === 'thought') {
            if (L._curThought) {
              L._curThought.duration_s = Math.max(1, Math.round((performance.now() - L._curThought.t0) / 1000));
              L._curThought = null;
            }
            L.acts.push({ type: 'thought', duration_s: d.duration_s || 2, ...d });
            L.statusText = 'Synthesizing strategy...';
            if (typeof window.setLiveHud === 'function') {
              window.setLiveHud({ phase: 'thinking', text: 'Synthesizing strategy...' });
            }
          }
          else if (ev === 'thought_delta') {
            if (!L._curThought) {
              L._curThought = { type: 'thought', text: '', t0: performance.now(), step: d.step, model: d.model };
              L.acts.push(L._curThought);
            }
            L._curThought.text += (d.delta || '');
            L._curThought.duration_s = Math.max(1, Math.floor((performance.now() - L._curThought.t0) / 1000));
            L.reasoning = (L.reasoning || '') + (d.delta || '');
            if (typeof window.setLiveHud === 'function') {
              window.setLiveHud({ phase: 'thinking', text: 'Thinking & analyzing...' });
            }
          }
          else if (ev === 'tool_preparing') {
            if (L._curThought) {
              L._curThought.duration_s = Math.max(1, Math.round((performance.now() - L._curThought.t0) / 1000));
              L._curThought = null;
            }
            const p = d.path ? d.path.split(/[\\/]/).pop() : '';
            const actionVerb = d.name === 'write_file' ? 'Preparing to write' : (d.name === 'edit_file' ? 'Preparing to edit' : `Preparing ${d.name}`);
            const label = p ? `${actionVerb} ${p}...` : `${actionVerb}...`;
            L.statusText = label;
            if (typeof window.setLiveHud === 'function') {
              const bytesStr = d.bytes ? ` (~${Math.round(d.bytes / 4)} tokens)` : '';
              window.setLiveHud({
                phase: 'preparing',
                name: d.name,
                path: d.path,
                text: label,
                subtext: bytesStr
              });
            }
          }
          else if (ev === 'tool_call') {
            if (L._curThought) {
              L._curThought.duration_s = Math.max(1, Math.round((performance.now() - L._curThought.t0) / 1000));
              L._curThought = null;
            }
            // If the model was streaming its preamble before calling a tool, keep it as thought/reasoning or preamble
            L.acts.push({ type: 'tool_call', ...d });
            const toolLabel = (typeof formatToolStatus === 'function') ? formatToolStatus(d.name, d.args) : (`Running ${d.name}...`);
            L.statusText = toolLabel;
            const p = (d.args && (d.args.path || d.args.file || d.args.filename)) || '';
            if (typeof window.setLiveHud === 'function') {
              window.setLiveHud({
                phase: (d.name === 'write_file' || d.name === 'edit_file') ? 'writing' : 'running',
                name: d.name,
                path: p,
                text: toolLabel
              });
            }
            if (L.content && L.content.trim()) {
              if (!L.reasoning) L.reasoning = L.content.trim();
              else L.reasoning += '\n\n' + L.content.trim();
              L.content = '';
            }
          }
          else if (ev === 'tool_result') {
            L.acts.push({ type: 'tool_result', ...d });
            L.statusText = 'Crunching tool results...';
            // agent changed a file -> refresh the workspace side panel if active
            if (wsPanelOpen && (d.name === 'write_file' || d.name === 'edit_file' || d.name === 'revert')) {
              if (curSession && String(curSession.id) === String(sessionId)) {
                wsRefreshTree();
              }
            }
            if (d.name === 'write_file' || d.name === 'edit_file') {
              const p = (d.args && (d.args.path || d.args.file || d.args.filename)) || '';
              const filename = p ? p.split(/[\\/]/).pop() : 'file';
              const isSuccess = d.ok !== false;
              const verb = d.name === 'write_file' ? 'Saved' : 'Updated';
              const actions = [];
              if (p && isSuccess && typeof openFilePreview === 'function') {
                actions.push({
                  label: '👁️ Preview',
                  onClick: () => openFilePreview(p, filename)
                });
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
              if (typeof window.setLiveHud === 'function') {
                window.setLiveHud({
                  phase: isSuccess ? 'done' : 'error',
                  name: d.name,
                  path: p,
                  text: isSuccess ? `💾 ${verb} ${filename}` : `⚠️ Failed to save ${filename}`,
                  actions: actions
                });
              }
            }
          }
          else if (ev === 'verify') {
            L.acts.push({ type: 'verify', ...d });
            L.statusText = 'Verifying tool changes...';
            if (typeof window.setLiveHud === 'function') {
              window.setLiveHud({ phase: 'running', text: 'Verifying tool changes...' });
            }
          }
          else if (ev === 'permission_request') showPermModal(d.req_id, d.cmd);
          else if (ev === 'delta') {
            if (L._curThought) {
              L._curThought.duration_s = Math.max(1, Math.round((performance.now() - L._curThought.t0) / 1000));
              L._curThought = null;
            }
            if (L._resetPrev != null) {
              // after a delta_reset: keep the earlier text as reasoning only when the
              // replacement is genuinely different (a cleaned-up copy would duplicate it)
              const prev = L._resetPrev;
              L._resetPrev = null;
              if (!(d.text || '').includes(prev.slice(0, 160))) {
                L.reasoning = L.reasoning ? L.reasoning + '\n\n' + prev : prev;
              }
            }
            L.content += (d.text || '');
            L.statusText = '';
          }
          else if (ev === 'delta_replace') {
            L.content = (d.text || '');
            L.statusText = '';
          }
          else if (ev === 'delta_reset') {
            // Replacing content with a synthesized final answer: decide on the next
            // delta whether the prior streamed text is worth keeping as reasoning
            if (L.content && L.content.trim()) L._resetPrev = L.content.trim();
            L.content = '';
          }
          else if (ev === 'validated') {
            L.acts.push({ type: 'validated', ...d });
            L.statusText = 'Validating solution...';
          }
          else if (ev === 'ctx') {
            // Smart context truncation fired on the backend — surface it
            const kb = n => n >= 1000 ? (n / 1000).toFixed(1) + 'k' : String(n);
            toast(`🧹 Context truncated to fit the window: ${kb(d.before_tokens)} → ${kb(d.after_tokens)} tokens`);
          }
          else if (ev === 'plan') {
            // structured plan checklist — keep only the latest snapshot in acts
            if (!L.acts) L.acts = [];
            const planAct = { type: 'plan', items: d.items || [] };
            const pi = L.acts.findIndex(a => a.type === 'plan');
            if (pi >= 0) L.acts[pi] = planAct; else L.acts.push(planAct);
          }
          else if (ev === 'kb_blocked') {
            // Data residency: KB withheld from the cloud lane; show it as a failed KB step
            if (!L.acts) L.acts = [];
            L.acts.push({ type: 'tool_call', id: 'kb_blocked', name: 'search_knowledge_base', args: {} });
            L.acts.push({ type: 'tool_result', id: 'kb_blocked', name: 'search_knowledge_base', ok: false, result: d.message || '' });
            if (typeof toast === 'function') toast('🔒 Company knowledge base is local-only — start a local model to use it');
          }
          else if (ev === 'guard') {
            // Output sanitizer redacted part of the response
            if (!L.acts) L.acts = [];
            L.acts.push({ type: 'guard', rule: d.rule, message: d.message });
            toast('🧼 ' + (d.message || ('Response filtered by policy: ' + (d.rule || ''))));
          }
          else if (ev === 'usage') {
            // per-step prompt size; the last step's is the run's real context use
            if (d.prompt_tokens) L.promptTokens = d.prompt_tokens;
          }
          else if (ev === 'done') {
            if (L._curThought) {
              L._curThought.duration_s = Math.max(1, Math.round((performance.now() - L._curThought.t0) / 1000));
              L._curThought = null;
            }
            // run ended early (step cap or loop stop): keep why, so the bubble can offer Continue
            if (d.reason) {
              if (!L.acts) L.acts = [];
              L.acts.push({ type: 'stopped', reason: d.reason, note: d.note || '', steps: d.steps,
                            pending: d.pending || 0, plan_total: d.plan_total || 0 });
            }
          }
          else if (ev === 'error') throw new Error(d.error);

          if (curSession && String(curSession.id) === String(sessionId)) {
            renderLast();
          }
        }
      }
      const targetAssistant = getJobAssistant();
      if (targetAssistant._resetPrev != null) {
        // delta_reset with no replacement text: keep what was streamed
        if (!targetAssistant.content) targetAssistant.content = targetAssistant._resetPrev;
        targetAssistant._resetPrev = null;
      }
      if (!targetAssistant.content && targetAssistant.acts && targetAssistant.acts.length > 0) {
        targetAssistant.content = 'Task completed. See tool operations above for details.';
      }
      targetAssistant.statusText = '';
      if (typeof window.setLiveHud === 'function') {
        window.setLiveHud({ phase: 'done', text: 'Task completed' });
      }
      const dt = (performance.now() - t0) / 1000;
      const fullLen = (targetAssistant.content || '').length + (targetAssistant.reasoning || '').length;
      const ntok = Math.max(1, Math.round(fullLen / 3.5));
      targetAssistant.tps = ntok / dt; targetAssistant.ntok = ntok; targetAssistant.secs = dt;
      if (ntok > 1 && $('chip-ts') && curSession && String(curSession.id) === String(sessionId)) {
        $('chip-ts').textContent = '⚡ ' + (ntok / dt).toFixed(1) + ' t/s';
      }
      persistMsgForSession(sessionId, 'assistant', targetAssistant.content, {
        tps: targetAssistant.tps, ntok, secs: dt,
        promptTokens: targetAssistant.promptTokens || undefined,
        reasoning: targetAssistant.reasoning || undefined,
        acts: targetAssistant.acts,
        modelDisplay: targetAssistant.modelDisplay || undefined,
        modelSource: targetAssistant.modelSource || undefined,
        modelProvider: targetAssistant.modelProvider || undefined,
        runId: targetAssistant.runId || undefined,
      });
    } catch (e) {
      if (e.name !== 'AbortError') {
        const L = getJobAssistant();
        L.content += (L.content ? '\n\n' : '') + '⚠️ ' + e.message;
      }
    }

    window.bgJobs.delete(String(sessionId));
    if (ctrl === jobCtrl) ctrl = null;

    if (curSession && String(curSession.id) === String(sessionId)) {
      setGenUI(false);
      renderLast();
      updateContextChip();
      if (wsPanelOpen) wsRefreshTree(); // final state of workspace after the task
    } else {
      toast(`⚡ Agent task "${job.title}" finished`);
    }
    if (typeof updateBgIndicators === 'function') updateBgIndicators();
  })();
}
