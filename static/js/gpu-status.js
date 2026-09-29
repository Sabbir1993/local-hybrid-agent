/* ---------------- status / gpu polling ---------------- */
function setPill(mode, s) {
  if (typeof setLoadBtn === 'function') setLoadBtn(mode);
  const p = $('pill'), txt = $('pill-text');
  if (!p || !txt) return;
  p.className = 'pill ' + mode;
  if (mode === 'on') txt.textContent = 'Loaded · ' + fmtUptime(s.uptime_s);
  else if (mode === 'cloud') txt.textContent = '';  // just the green dot below
  else if (mode === 'loading') txt.textContent = 'Loading model…';
  else if (mode === 'offline') txt.textContent = 'Manager offline';
  else txt.textContent = 'Model unloaded';
}

function updateHardwareTag(tag) {
  const el = $('logo-hw-tag');
  if (el && tag && el.textContent !== tag) {
    el.textContent = tag;
  }
}

let pollFailures = 0;
let _lastLocalPid = null;
async function pollStatus() {
  try {
    const res = await fetch('/control/status', { background: true });
    if (!res.ok) throw new Error('status not ok');
    const s = await res.json();
    pollFailures = 0;
    curStatus = s;
    if (s.hardware_tag) updateHardwareTag(s.hardware_tag);
    const cloudMain = s.cloud_main || null;
    // cloud main lane: report ☁️ instead of a misleading "Model unloaded"
    setPill(cloudMain ? 'cloud' : (s.pid ? 'on' : 'off'), s);
    const ka = $('ka');
    if (ka && !ka._user) ka.checked = !!s.keepalive;

    // Refresh model picker whenever local process load/unload state transitions
    const hasPid = !!s.pid;
    if (_lastLocalPid !== null && _lastLocalPid !== hasPid) {
      if (typeof loadProfiles === 'function') loadProfiles();
    }
    _lastLocalPid = hasPid;

    const sel = $('profile');
    const selName = (sel && sel.value) ? sel.value.split('\\').pop().split('/').pop() : 'Model';
    const m = s.model ? s.model.split('\\').pop().split('/').pop() : selName;
    if (!$('empty-model')) return;

    if (cloudMain) {
      $('empty-model').textContent =
        `${cloudMain.display}  ·  via ${cloudMain.provider}  ·  cloud (no VRAM)`;
      const ec = document.querySelector('.empty-card');
      if (ec) {
        ec.innerHTML = `<p><b>${esc(cloudMain.display)} is ready!</b></p><p class="dim" style="margin-top:6px;">Main lane is served by <b>${esc(cloudMain.provider)}</b> in the cloud — your local GPU(s) stay free. Type your message below and press Enter.</p>`;
      }
    } else if (s.model && s.pid) {
      const visionTag = s.vision_capable ? '  ·  👁 vision' : '';
      $('empty-model').textContent =
        `${m}${visionTag}  ·  split ${s.tensor_split || 'auto'}  ·  bench ${Number(s.measured_tg_tokens_per_sec || 0).toFixed(1)} t/s`;
      const ec = document.querySelector('.empty-card');
      if (ec) {
        ec.innerHTML = `<p><b>${m} is ready!</b></p><p class="dim" style="margin-top:6px;">GPU(s) loaded${s.vision_capable ? ' with <b>vision support</b> (mmproj)' : ''}. Type your message below and press Enter to chat.</p>`;
      }
    } else {
      $('empty-model').textContent = 'Model unloaded';
      const ec = document.querySelector('.empty-card');
      if (ec) {
        ec.innerHTML = `<p><b>${m} is currently unloaded.</b></p><p class="dim" style="margin-top:6px;">Click <b>▶ Load</b> in the top bar, or simply type your message below to auto-load.</p>`;
      }
    }

    // keep profile dropdown in sync if switched elsewhere (never for a cloud
    // selection — that one is owned by the main-lane binding, not by s.profile)
    const cloudSel = (typeof isCloudValue === 'function') && isCloudValue(sel && sel.value);
    if (!cloudSel && s.profile && sel.value && !sel._user) {
      const opt = [...sel.options].find(o => o.value === s.profile || o.textContent.includes(s.profile));
      if (opt && sel.value !== opt.value) sel.value = opt.value;
    }
    if (typeof renderModelPicker === 'function') renderModelPicker();

    // update context consumption chip
    if (s.context && s.context.n_ctx) {
      curCtxMax = s.context.n_ctx;
    }
    updateContextChip();
  } catch (e) {
    pollFailures++;
    if (pollFailures >= 2) {
      setPill('offline');
    }
  }
}

async function pollGpu() {
  // don't call an endpoint we know will 403 (each denial is audit-logged)
  if (window.__perms && !(window.__user && window.__user.is_super_admin)
      && !window.__perms.has('settings.runtime.view')) return;
  try {
    const r = await fetch('/control/gpu');
    if (!r.ok) return;
    const d = await r.json();
    const comp = {};
    (d.compute || []).forEach(c => { comp[c.luid] = (comp[c.luid] || 0) + c.pct; });
    
    // Deduplicate & filter to show strictly the physical discrete GPUs
    let rawAds = (d.adapters || []).filter(a => !IGNORED_LUIDS.has(a.luid) && a.gb >= 1.0);
    const unique = {};
    rawAds.forEach(a => {
      if (!unique[a.luid] || a.gb > unique[a.luid].gb) {
        unique[a.luid] = a;
      }
    });
    const ads = Object.values(unique).sort((a, b) => b.gb - a.gb);
    const totals = d.vram_totals_gb || [];  // sorted largest-first, positional match to `ads`

    if (d.hardware_tag) {
      updateHardwareTag(d.hardware_tag);
    } else {
      const count = ads.length;
      let gpuLabel = 'CPU';
      if (count === 2) gpuLabel = 'DUAL GPU';
      else if (count === 1) gpuLabel = 'SINGLE GPU';
      else if (count > 2) gpuLabel = `${count}x GPU`;
      const eng = (d.engine || 'VULKAN').toUpperCase();
      updateHardwareTag(`${gpuLabel} · ${eng}`);
    }

    const glist = $('gpu-list');
    if (glist) {
      glist.innerHTML = ads.map((a, idx) => {
        const name = LUID_NAMES[a.luid] || (`GPU #${idx + 1} (${a.luid})`);
        const cap = totals[idx] || 16;  // fall back to 16GB if capacity is unknown
        const pct = Math.min(100, a.gb / cap * 100);
        const cp = Math.min(100, Math.round(comp[a.luid] || 0));
        return `<div class="gpu" title="${name}: ${a.gb.toFixed(1)} GB dedicated VRAM used, ${cp}% compute utilization">
          <div class="g-top"><b>${name}</b><span class="mono">${a.gb.toFixed(1)} / ${cap.toFixed(0)} GB</span></div>
          <div class="bar"><div class="fill${a.gb > cap - 1.5 ? ' warn' : ''}" style="width:${pct}%"></div></div>
          <div class="g-bot"><span>compute engine</span><span class="mono ${cp > 5 ? 'hot' : ''}">${cp}%</span></div>
        </div>`;
      }).join('') || '<div class="dim" style="font-size:12px">No discrete GPU data</div>';
    }
    return d;
  } catch (e) { /* manager restarting */ }
}

let curCtxMax = 32768;

/* True when the MAIN lane can answer right now: a local llama-server is up, or
   the main lane is bound to a cloud model (nothing to load into VRAM). */
function mainLaneReady() {
  return !!(curStatus && (curStatus.pid || curStatus.cloud_main));
}

function getMsgTokens(m) {
  if (!m) return 0;
  if (typeof m.ntok === 'number' && m.ntok > 0) return m.ntok;
  if (m.meta && typeof m.meta.ntok === 'number' && m.meta.ntok > 0) return m.meta.ntok;
  let textLen = (m.content || '').length + (m.reasoning || '').length;
  if (Array.isArray(m.images) && m.images.length > 0) {
    textLen += m.images.length * 576 * 3.5;
  }
  return Math.max(1, Math.round(textLen / 3.5));
}

/* ---------------- shared context accounting ----------------
   ONE estimator for the chip, the /compact bubble and the auto-compact trigger.
   They used to disagree badly: the chip reported 32k/261k while the compact
   bubble claimed ~4.9k, because it filtered to user|assistant only (dropping
   every tool message - the bulk of an agent turn) and never looked at `acts`
   (where tool calls live). Chip and bubble now cannot drift apart. */

/* One chars-per-token divisor, replacing the /3.0 vs /3.5 split. */
const CTX_CHARS_PER_TOK = 3.5;

/* Tool results are truncated server-side; don't let a 2 MB read_file response
   dominate the estimate. Mirrors how the prompt is actually assembled. */
const CTX_TOOL_RESULT_CHARS = 4000;

/* Tokens charged per tool call, for the name + argument envelope. */
function actTokens(a) {
  if (!a) return 0;
  if (a.type === 'tool_call' || a.type === 'tool_result') {
    const name = a.name || '';
    let args = '';
    try { args = a.args ? JSON.stringify(a.args) : ''; } catch (_) { args = String(a.args || ''); }
    let body = '';
    if (a.type === 'tool_call') body = args;
    else {
      const r = a.result;
      body = (r && typeof r === 'object') ? (r.text || JSON.stringify(r) || '') : String(r == null ? '' : r);
      if (body.length > CTX_TOOL_RESULT_CHARS) body = body.slice(0, CTX_TOOL_RESULT_CHARS);
    }
    return Math.max(1, Math.round((name.length + body.length) / CTX_CHARS_PER_TOK));
  }
  if (a.type === 'thought' || a.type === 'reasoning') return 0;  // already in reasoning
  return 0;
}

/* Split one message's tokens into prompt-side and completion-side.
   Tool RESULTS are prompt-side: they are sent back to the model on the next
   turn, so charging them to "completion" would understate the real prompt and
   make the chip's breakdown misleading. Tool calls stay completion-side. */
function messageTokensSplit(m) {
  if (!m) return { prompt: 0, completion: 0, total: 0 };
  const own = getMsgTokens(m);
  let actPrompt = 0, actCompletion = 0;
  if (Array.isArray(m.acts)) {
    for (const a of m.acts) {
      if (a && a.type === 'tool_result') actPrompt += actTokens(a);
      else actCompletion += actTokens(a);
    }
  }
  const isAssistant = m.role === 'assistant';
  return {
    prompt: own + (isAssistant ? actPrompt : actCompletion),
    completion: isAssistant ? own + actCompletion : 0,
    total: own + actPrompt + actCompletion,
  };
}

/* Total tokens one message contributes, including its tool calls. */
function messageTokens(m) {
  return messageTokensSplit(m).total;
}

/* The single source of truth. Covers every role (user/assistant/tool/system) and
   counts tool activity, so agent turns are no longer under-counted.

   Honours the compact-marker boundary: if the caller passes the raw transcript
   and it opens with a compaction marker, only the marker + kept tail + what
   follows are counted. Without this, everything the summary already replaced
   would be counted a second time. */
function estimateSessionTokens(msgs) {
  if (!Array.isArray(msgs) || !msgs.length) return { total: 0, prompt: 0, completion: 0, msgs };

  let list = msgs;
  let markerAt = -1;
  for (let i = msgs.length - 1; i >= 0; i--) {
    if (msgs[i] && msgs[i].compact) { markerAt = i; break; }
  }
  if (markerAt >= 0) {
    // the summary replaces everything before it, except the `kept` messages
    // immediately preceding it, which survive verbatim
    const kept = msgs[markerAt].compactKept || 0;
    list = msgs.slice(Math.max(0, markerAt - kept), markerAt + 1)
      .concat(msgs.slice(markerAt + 1));
  }

  let prompt = 0, completion = 0;
  for (const m of list) {
    const s = messageTokensSplit(m);
    prompt += s.prompt;
    completion += s.completion;
  }
  return { total: prompt + completion, prompt, completion, msgs: list };
}

const CTX_WARN_PCT = 70;
const CTX_CRIT_PCT = 85;
const CTX_COMPACT_PCT = 65;   // compact *before* the chip turns amber

/* The context window of the lane that will actually answer the next step.
   In agent mode a step may run on the executor, whose window is a fraction of
   main's; budgeting against main's 261k would let an executor step overflow
   without a single warning. Falls back to the main window when unknown. */
function activeLaneCtxMax() {
  try {
    // last lane the run reported, e.g. "executor" / "main"
    const lane = (typeof lastAgentLane !== 'undefined' && lastAgentLane) ? lastAgentLane : 'main';
    if (lane !== 'executor') {
      return (curStatus && curStatus.context && curStatus.context.n_ctx) ? curStatus.context.n_ctx : curCtxMax;
    }
    const ec = (typeof APP_MODELS !== 'undefined' && APP_MODELS.executor) ? APP_MODELS.executor.ctx : 0;
    if (ec > 0) return ec;
    const cfg = (typeof APP_MODELS !== 'undefined') ? APP_MODELS : null;
    if (cfg && cfg.executor) {
      const m = Object.values(cfg.executor.models || {})[0];
      if (m && m.ctx > 0) return m.ctx;
    }
  } catch (_) { /* fall through */ }
  return (curStatus && curStatus.context && curStatus.context.n_ctx) ? curStatus.context.n_ctx : curCtxMax;
}


function updateContextChip() {
  const cChip = $('chip-ctx');
  if (!cChip) return;

  let promptToks = 0;
  let compToks = 0;
  let totalToks = 0;

  // Use the same reduced set actually sent to the model (marker + kept tail +
  // everything after) so the chip reflects real usage post-/compact, not the
  // full append-only visible transcript.
  const ctxMsgs = (typeof buildContextMessages === 'function') ? buildContextMessages() : messages;

  // Anchor on the latest assistant turn that has a server-reported prompt size:
  // that figure already covers the system prompt, tools, tool results and all
  // prior history, so only its completion + later messages are added on top.
  // Turns in a compacted marker's kept tail predate the compaction — skip them.
  const minIdx = (ctxMsgs[0] && ctxMsgs[0].compact) ? 1 + (ctxMsgs[0].compactKept || 0) : 0;
  let anchor = -1;
  for (let i = ctxMsgs.length - 1; i >= minIdx; i--) {
    if (ctxMsgs[i].role === 'assistant' && ctxMsgs[i].promptTokens > 0) { anchor = i; break; }
  }
  if (anchor >= 0) {
    promptToks = ctxMsgs[anchor].promptTokens;
    compToks = messageTokens(ctxMsgs[anchor]);
    totalToks = promptToks + compToks;
    for (const m of ctxMsgs.slice(anchor + 1)) {
      const tok = messageTokens(m);
      if (m.role === 'assistant') compToks += tok;
      else promptToks += tok;
      totalToks += tok;
    }
  } else {
    // no server anchor yet (fresh session, or before the first reply): use the
    // shared estimator, which counts tool calls/results so agent turns aren't
    // under-counted the way the old prose-only fallback was
    const est = estimateSessionTokens(ctxMsgs);
    promptToks = est.prompt;
    compToks = est.completion;
    totalToks = est.total;
  }

  const nCtx = (curStatus && curStatus.context && curStatus.context.n_ctx) ? curStatus.context.n_ctx : curCtxMax;
  const pct = Math.min(100, Number(((totalToks / Math.max(1, nCtx)) * 100).toFixed(1)));
  const fmtK = n => n >= 1000 ? (n / 1000).toFixed(1) + 'k' : n;

  cChip.innerHTML = `<svg class="ui-icon" width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M9.5 2A2.5 2.5 0 0 1 12 4.5v15a2.5 2.5 0 0 1-4.96.44 2.5 2.5 0 0 1-2.96-3.08 3 3 0 0 1-.34-5.58 2.5 2.5 0 0 1 1.32-4.24 2.5 2.5 0 0 1 4.44-2.04Z"/><path d="M14.5 2A2.5 2.5 0 0 0 12 4.5v15a2.5 2.5 0 0 0 4.96.44 2.5 2.5 0 0 0 2.96-3.08 3 3 0 0 0 .34-5.58 2.5 2.5 0 0 0-1.32-4.24 2.5 2.5 0 0 0-4.44-2.04Z"/></svg> <span>${fmtK(totalToks)} / ${fmtK(nCtx)} (${pct}%)</span>`;
  cChip.title = `Session Tokens: ${totalToks.toLocaleString()} / ${nCtx.toLocaleString()} tokens used (${pct}%)`
    + ` · Prompt: ${promptToks.toLocaleString()} · Completion: ${compToks.toLocaleString()}`
    + ` · ${ctxMsgs.length} messages`
    + (anchor >= 0 ? '\nAnchored on the last server-reported prompt size.'
                   : '\nEstimated locally (no server figure yet; tool calls counted).');

  if (pct > CTX_CRIT_PCT) cChip.style.color = 'var(--red)';
  else if (pct > CTX_WARN_PCT) cChip.style.color = 'var(--amber)';
  else cChip.style.color = 'var(--dim)';
}
