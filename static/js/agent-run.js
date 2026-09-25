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
  // End the thought card that is still streaming, keeping how long it ran
  const closeThought = (L) => {
    if (!L._curThought) return;
    L._curThought.duration_s = Math.max(1, Math.round((performance.now() - L._curThought.t0) / 1000));
    L._curThought = null;
  };

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
      await readSSE(res, (ev, d) => {
        const L = getJobAssistant();
        if (ev === 'run') {
          L.runId = d.run_id;   // routing telemetry id -> thumbs up/down feedback
        }
        else if (ev === 'step') {
          closeThought(L);
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
          closeThought(L);
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
        else if (ev === 'delta_to_thought') {
          const moved = sseDeltaToThought(L);
          if (moved) {
            if (!L._curThought) {
              L._curThought = { type: 'thought', text: '', t0: performance.now(), step: d.step, model: d.model };
              L.acts.push(L._curThought);
            }
            L._curThought.text += moved;
          }
        }
        else if (ev === 'tool_preparing') {
          closeThought(L);
          sseToolPreparing(L, d);
        }
        else if (ev === 'tool_call') {
          closeThought(L);
          sseToolCall(L, d);
        }
        else if (ev === 'tool_result') {
          sseToolResult(L, d);
          // agent changed a file -> refresh the workspace side panel if active
          if (wsPanelOpen && (d.name === 'write_file' || d.name === 'edit_file' || d.name === 'revert')) {
            if (curSession && String(curSession.id) === String(sessionId)) {
              wsRefreshTree();
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
          closeThought(L);
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
        else if (ev === 'kb_blocked') sseKbBlocked(L, d);
        else if (ev === 'guard') {
          if (!L.acts) L.acts = [];
          L.acts.push({ type: 'guard', rule: d.rule, message: d.message });
          sseGuardToast(d);
        }
        else if (ev === 'usage') {
          // per-step prompt size; the last step's is the run's real context use
          if (d.prompt_tokens) L.promptTokens = d.prompt_tokens;
        }
        else if (ev === 'done') {
          closeThought(L);
          // run ended early (step cap or loop stop): keep why, so the bubble can offer Continue
          if (d.reason) {
            if (!L.acts) L.acts = [];
            L.acts.push({ type: 'stopped', reason: d.reason, note: d.note || '', steps: d.steps,
                          pending: d.pending || 0, plan_total: d.plan_total || 0 });
          }
        }
        else if (ev === 'error') throw new Error(d.error);

        if (curSession && String(curSession.id) === String(sessionId)) {
          scheduleRenderLast();
        }
      });
      const targetAssistant = getJobAssistant();
      if (targetAssistant._resetPrev != null) {
        // delta_reset with no replacement text: keep what was streamed
        if (!targetAssistant.content) targetAssistant.content = targetAssistant._resetPrev;
        targetAssistant._resetPrev = null;
      }
      const leaked = sseApplyThinkSplit(targetAssistant);
      if (leaked) (targetAssistant.acts = targetAssistant.acts || []).push({ type: 'thought', text: leaked, duration_s: 1 });
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
