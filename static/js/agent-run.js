/* ---------------- agent SSE runner ---------------- */
async function runAgentSSE(text) {
  let ctxToastShown = false;   // the context-trim toast shows once per run
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
    // Pass the real message objects. This used to map to {role, content}, which
    // dropped acts/ntok/images and under-counted an agent turn several-fold.
    await autoCompactIfNeeded(buildContextMessages());
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
      const histFull = ctxMsgs.slice(0, -1).map(m => ({ role: m.role, content: m.content }));
      // earlier turns are sent shortened (attached file text, long answers): static/js/history-clip.js
      const hist = typeof clipHistoryForRun === 'function' ? clipHistoryForRun(histFull) : histFull;
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
          plan: agentMode ? planMode : false,
          session_id: sessionId || null,
          temperature: window.customAgentRequestOverrides ? window.customAgentRequestOverrides().temperature : getSamplingConfig().temp,
          max_tokens: (() => { const mt = getSamplingConfig().maxtok; return (isNaN(mt) || mt <= 0) ? -1 : mt; })(),
          top_p: getSamplingConfig().topp,
          min_p: getSamplingConfig().minp,
          repeat_penalty: getSamplingConfig().rep,
          presence_penalty: getSamplingConfig().presence,
          top_k: getSamplingConfig().topk,
          ...samplingExtraBody(),
          system_prompt: (getSamplingConfig().sysprompt || '').trim() || undefined,
          attachments: docAttachments,
          cloud_model_override: cloudModelOverride || undefined,
          reasoning_effort: window.customAgentRequestOverrides ? window.customAgentRequestOverrides().reasoning_effort : (typeof getReasoningEffort === 'function' ? getReasoningEffort() : undefined),
          verify: typeof answerCheckBegin === 'function' ? answerCheckBegin(job.assistantMsg) : undefined,
          custom_agent_id: typeof getActiveCustomAgentId === 'function' ? getActiveCustomAgentId() : undefined,
          personal: !agentMode || undefined,   // Personal Agent run from Chat: server confines writes to the common folder
        }),
        signal: jobCtrl.signal,
      });
      if (!res.ok) {
        const e = await res.json().catch(() => ({}));
        if (e.error === 'custom_agent_not_found' && typeof clearActiveCustomAgent === 'function') clearActiveCustomAgent();
        throw new Error(typeof apiErrorText === 'function' ? apiErrorText(e, res.status) : (e.message || e.error || ('HTTP ' + res.status)));
      }
      const sseEnd = await readSSE(res, (ev, d) => {
        const L = getJobAssistant();
        if (typeof sseAnswerCheck === 'function' && sseAnswerCheck(L, ev, d)) {
          if (typeof scheduleRenderLast === 'function' && curSession && String(curSession.id) === String(sessionId)) scheduleRenderLast();
          return;
        }
        if (ev === 'custom_agent') {
          L.customAgent = d;
        }
        else if (ev === 'run') {
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
          // remember which lane is driving, so the context budget is measured
          // against that lane's window (executor is much smaller than main)
          if (d.lane) window.lastAgentLane = d.lane;
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
          if (wsPanelOpen && ['write_file', 'edit_file', 'append_file', 'insert_at_line', 'revert'].includes(d.name)) {
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
        else if (ev === 'permission_request') showPermModal(d.req_id, d.cmd, d.kind, d.saveable);
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
          // a checked-and-fixed answer replaces the draft (kept in check.original)
          if (L.content && L.content.trim() && !(L.check && L.check.state === 'checking')) L._resetPrev = L.content.trim();
          L.content = '';
        }
        else if (ev === 'validated') {
          L.acts.push({ type: 'validated', ...d });
          L.statusText = 'Validating solution...';
        }
        else if (ev === 'ctx') {
          // Smart context truncation fired on the backend — surface it
          // once per run: a long run trims a little on every step, and a toast each time is noise
          if (!ctxToastShown) {
            ctxToastShown = true;
            const kb = n => n >= 1000 ? (n / 1000).toFixed(1) + 'k' : String(n);
            const who = d.lane === 'executor' ? 'Helper model' : 'Main model';
            toast(`🧹 ${who}: older steps summarised to fit its window (${kb(d.before_tokens)} → ${kb(d.after_tokens)} tokens)`);
          }
        }
        else if (ev === 'plan') {
          // structured plan checklist — keep only the latest snapshot in acts
          if (!L.acts) L.acts = [];
          const planAct = { type: 'plan', items: d.items || [] };
          const pi = L.acts.findIndex(a => a.type === 'plan');
          if (pi >= 0) L.acts[pi] = planAct; else L.acts.push(planAct);
        }
        else if (ev === 'kb_blocked') sseKbBlocked(L, d);
        else if (ev === 'lane_warning') sseLaneWarning(L, d);
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
          // how the run ended: completed | stopped | failed | cancelled (older servers send none: a reason means stopped)
          L.runState = d.state || (d.reason ? 'stopped' : 'completed');
          if (typeof answerCheckEnd === 'function') answerCheckEnd(L);
          // run ended early (step cap or loop stop): keep why, so the bubble can offer Continue
          if (d.reason) {
            if (!L.acts) L.acts = [];
            L.acts.push({ type: 'stopped', reason: d.reason, note: d.note || '', detail: d.detail || '', steps: d.steps,
                          pending: d.pending || 0, plan_total: d.plan_total || 0,
                          plan_done: d.plan_done || 0, plan_failed: d.plan_failed || 0,
                          elapsed_s: d.elapsed_s || 0 });
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
      // A stream that ends without its `done` event did not finish (server stopped, connection dropped, proxy cut it):
      // that is an interruption, never a success.
      if (!(sseEnd && sseEnd.terminal)) {
        sseInterrupt(targetAssistant, 'the connection to the server ended before the run finished', 'interrupted');
      }
      const leaked = sseApplyThinkSplit(targetAssistant);
      if (leaked) (targetAssistant.acts = targetAssistant.acts || []).push({ type: 'thought', text: leaked, duration_s: 1 });
      const okEnd = targetAssistant.runState === 'completed';
      if (!targetAssistant.content && okEnd && targetAssistant.acts && targetAssistant.acts.length > 0) {
        targetAssistant.content = 'Task completed. See tool operations above for details.';
      }
      targetAssistant.statusText = '';
      if (typeof window.setLiveHud === 'function') {
        window.setLiveHud(okEnd ? { phase: 'done', text: 'Task completed' }
          : { phase: 'error', text: targetAssistant.runState === 'stopped' ? 'Run stopped' : 'Run interrupted' });
      }
      finishAgentMessage(sessionId, targetAssistant, t0);
    } catch (e) {
      const L = getJobAssistant();
      const started = !!(L.runId || (L.acts && L.acts.length));      // the server had accepted the run
      if (e.name !== 'AbortError') L.content += (L.content ? '\n\n' : '') + '⚠️ ' + e.message;
      if (started) {
        sseInterrupt(L, e.name === 'AbortError' ? 'you stopped the run' : e.message,
                     e.name === 'AbortError' ? 'cancelled' : 'interrupted');
        L.statusText = '';
        if (typeof window.setLiveHud === 'function') window.setLiveHud({ phase: 'error', text: e.name === 'AbortError' ? 'Run stopped' : 'Run interrupted' });
        finishAgentMessage(sessionId, L, t0);   // keep what the run did, with its final state, so a reload shows the same thing
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

/* The tail of an agent run: timing, the t/s chip, and saving the message with the state the run ended in. */
function finishAgentMessage(sessionId, L, t0) {
  const dt = Math.max(0.001, (performance.now() - t0) / 1000);
  const fullLen = (L.content || '').length + (L.reasoning || '').length;
  const ntok = Math.max(1, Math.round(fullLen / 3.5));
  L.tps = ntok / dt; L.ntok = ntok; L.secs = dt;
  if (ntok > 1 && $('chip-ts') && curSession && String(curSession.id) === String(sessionId)) {
    $('chip-ts').textContent = '⚡ ' + (ntok / dt).toFixed(1) + ' t/s';
  }
  if (typeof answerCheckEnd === 'function') answerCheckEnd(L);
  const cut = L.runState === 'interrupted' || L.runState === 'cancelled' || L.runState === 'failed';
  persistMsgForSession(sessionId, 'assistant', L.content || (cut ? '(run interrupted)' : ''), {
    tps: L.tps, ntok, secs: dt,
    promptTokens: L.promptTokens || undefined,
    reasoning: L.reasoning || undefined,
    // screenshots stay in this browser tab: never written to the server's session history
    acts: (L.acts || []).map(a => (a && a.image) ? { ...a, image: undefined } : a),
    modelDisplay: L.modelDisplay || undefined,
    modelSource: L.modelSource || undefined,
    modelProvider: L.modelProvider || undefined,
    runId: L.runId || undefined,
    runState: L.runState || undefined,
    check: typeof _checkMeta === 'function' ? _checkMeta(L) : undefined,
  });
}
