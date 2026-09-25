let chatUserScrolledUp = false;

function initChatScrollTracking() {
  const chat = $('chat');
  if (!chat || chat._scrollTrackingActive) return;
  chat._scrollTrackingActive = true;

  chat.addEventListener('scroll', () => {
    const dist = chat.scrollHeight - chat.scrollTop - chat.clientHeight;
    if (dist < 40) {
      chatUserScrolledUp = false;
      updateScrollBottomBtn(false);
    } else if (dist > 80) {
      chatUserScrolledUp = true;
      if (generating) {
        updateScrollBottomBtn(true);
      }
    }
  }, { passive: true });
}

function updateScrollBottomBtn(show) {
  const btn = $('btn-scroll-bottom');
  if (!btn) return;
  btn.style.display = show ? 'flex' : 'none';
}

function scrollToBottom() {
  const chat = $('chat');
  if (chat) {
    chatUserScrolledUp = false;
    chat.scrollTo({ top: chat.scrollHeight, behavior: 'smooth' });
    updateScrollBottomBtn(false);
  }
}
window.scrollToBottom = scrollToBottom;
document.getElementById('btn-scroll-bottom')?.addEventListener('click', scrollToBottom);

// the answer-check verdict saved with a message (no draft copy when nothing was fixed)
function _checkMeta(m) {
  const c = m && m.check;
  if (!c || c.state !== 'done') return undefined;
  return { state: 'done', mode: c.mode, verdict: c.verdict, issues: c.issues || [], checker: c.checker,
           source: c.source, fixed: c.fixed || 0, original: c.original || undefined, note: c.note || undefined };
}
window._checkMeta = _checkMeta;

function renderAll() {
  const inner = $('chat-inner');
  if (typeof mediaSyncStop === 'function') mediaSyncStop();   // Stop follows the open chat's images
  const isLoaded = (typeof mainLaneReady === 'function') ? mainLaneReady() : (curStatus && curStatus.pid);
  inner.innerHTML = messages.length ? messages.map(bubbleHtml).join('') :
    `<div id="empty">
      <div class="big">⚡</div>
      <h2>Local Agent</h2>
      <p id="empty-model" class="mono">${isLoaded && curStatus.model ? curStatus.model.split('\\').pop().split('/').pop() : 'Model unloaded'}</p>
      ${!isLoaded ? `<div class="empty-card">
        <p><b>No model is currently loaded in GPU VRAM.</b></p>
        <p class="dim" style="margin-top: 6px;">Select a model or profile from the top dropdown menu and click <b>▶ Load Model</b> to start inference.</p>
      </div>` : `<p class="dim" style="margin-top: 10px;">Type a message below to start chatting, or configure parameters in the sidebar.</p>`}
    </div>`;
  const chat = $('chat');
  if (chat) {
    chatUserScrolledUp = false;
    chat.scrollTop = chat.scrollHeight;
    initChatScrollTracking();
    updateScrollBottomBtn(false);
  }
  updateContextChip();
  if (typeof renderInlineMermaid === 'function') {
    renderInlineMermaid(inner);
  }
}

function onThinkSummaryClick(idx, ev) {
  const details = ev.currentTarget.closest('details');
  if (details && messages[idx]) {
    messages[idx]._thinkOpen = !details.open;
  }
}
window.onThinkSummaryClick = onThinkSummaryClick;

function onToggleThink(idx, isOpen) {
  if (messages[idx]) {
    messages[idx]._thinkOpen = isOpen;
  }
}
window.onToggleThink = onToggleThink;

// While a mouse button is held inside the chat (dragging a scrollbar, selecting
// text), streaming re-renders would replace the element under the pointer and
// cancel the drag. Hold them back and catch up once the button is released.
let _chatPointerHeld = false;
let _renderLastPending = false;
function _releaseChatPointer() {
  if (!_chatPointerHeld) return;
  _chatPointerHeld = false;
  if (_renderLastPending) {
    _renderLastPending = false;
    renderLast();
  }
}
document.addEventListener('pointerdown', e => {
  if (e.button === 0 && e.target.closest && e.target.closest('#chat-inner')) _chatPointerHeld = true;
}, true);
window.addEventListener('pointerup', _releaseChatPointer, true);
window.addEventListener('mouseup', _releaseChatPointer, true);
window.addEventListener('pointercancel', _releaseChatPointer, true);
window.addEventListener('blur', _releaseChatPointer);
// Chrome can skip mouseup after a native scrollbar drag; any buttonless move ends the hold
window.addEventListener('mousemove', e => { if (_chatPointerHeld && e.buttons === 0) _releaseChatPointer(); }, { passive: true });

// Streaming events can arrive far faster than the screen refreshes; each one
// used to rebuild the whole last bubble (markdown + highlighting). Coalesce
// them into at most one render per animation frame.
let _renderRaf = 0;
function scheduleRenderLast() {
  if (_renderRaf) return;
  _renderRaf = requestAnimationFrame(() => {
    _renderRaf = 0;
    renderLast();
  });
}
window.scheduleRenderLast = scheduleRenderLast;

function renderLast() {
  if (_renderRaf) {   // a direct render supersedes the pending frame
    cancelAnimationFrame(_renderRaf);
    _renderRaf = 0;
  }
  const inner = $('chat-inner');
  if (!inner) return;
  if (_chatPointerHeld) { _renderLastPending = true; return; }
  const lastIdx = messages.length - 1;
  if (lastIdx < 0 || inner.children.length !== messages.length || $('empty')) {
    renderAll();
    return;
  }

  const lastEl = inner.lastElementChild;
  const m = messages[lastIdx];
  const chat = $('chat');
  initChatScrollTracking();

  const chatWasAtBottom = chat ? (chat.scrollHeight - chat.scrollTop - chat.clientHeight < 50) : true;

  // Preserve existing details state and scroll position if user interacted with it
  const prevThink = lastEl.querySelector('details.think');
  let thinkScrollTop = -1;
  let thinkWasAtBottom = true;
  if (prevThink) {
    if (m._thinkOpen === undefined) {
      m._thinkOpen = prevThink.open;
    }
    const prevDiv = prevThink.querySelector('.think-content, div');
    if (prevDiv) {
      thinkScrollTop = prevDiv.scrollTop;
      thinkWasAtBottom = (prevDiv.scrollHeight - prevDiv.scrollTop - prevDiv.clientHeight) < 15;
    }
  }

  // Fast in-place update for streaming reasoning without replacing DOM
  const existingThink = lastEl.querySelector('details.think');
  const existingThinkDiv = existingThink ? existingThink.querySelector('.think-content') : null;
  const isOnlyStreamingReasoning = generating && m.reasoning && !m.content && existingThinkDiv;

  if (isOnlyStreamingReasoning) {
    const job = (curSession && window.bgJobs) ? window.bgJobs.get(String(curSession.id)) : null;
    const t0 = (job && job.t0) ? job.t0 : _genStartTime;
    const elapsedSec = Math.max(1, Math.floor((performance.now() - t0) / 1000));
    const thinkSummary = existingThink.querySelector('summary');
    if (thinkSummary) {
      thinkSummary.innerHTML = `<span class="codex-thought-icon">🧠</span> <span class="codex-thought-title">Thinking (${elapsedSec}s)...</span><span class="codex-chevron">▾</span>`;
    }

    const thinkBody = esc(m.reasoning).replace(/\n/g, '<br>') + '<span class="cursor">▍</span>';
    existingThinkDiv.innerHTML = thinkBody;
    if (thinkWasAtBottom && !m._thinkUserScrolled) {
      existingThinkDiv.scrollTop = existingThinkDiv.scrollHeight;
    } else if (thinkScrollTop >= 0) {
      existingThinkDiv.scrollTop = thinkScrollTop;
    }

    const workingBox = lastEl.querySelector('.claude-working-box');
    if (workingBox) {
      const textEl = workingBox.querySelector('.claude-working-text');
      const timerEl = workingBox.querySelector('.claude-working-timer');
      const msg = getClaudeWorkingPhrase(m, elapsedSec);
      if (textEl && textEl.textContent !== msg) textEl.textContent = msg;
      if (timerEl && timerEl.textContent !== `${elapsedSec}s`) timerEl.textContent = `${elapsedSec}s`;
    }
  } else {
    // Agent step list + tool output blocks are re-created below; remember their scroll
    // so the fixed-height panels stay scrollable while the task streams
    const SCROLLERS = '.agy-steps-list, .agy-detail-code, .agy-diff';
    const prevScroll = Array.from(lastEl.querySelectorAll(SCROLLERS)).map(el => ({
      top: el.scrollTop,
      atBottom: el.scrollHeight - el.scrollTop - el.clientHeight < 15,
    }));
    // Generate new HTML for the last message
    const temp = document.createElement('div');
    temp.innerHTML = bubbleHtml(m, lastIdx);
    const newEl = temp.firstElementChild;
    if (newEl) {
      inner.replaceChild(newEl, lastEl);
      newEl.querySelectorAll(SCROLLERS).forEach((el, i) => {
        const p = prevScroll[i];
        const follow = el.classList.contains('agy-steps-list') && generating && (!p || p.atBottom);
        el.scrollTop = follow ? el.scrollHeight : (p ? p.top : 0);
      });

      // Auto-scroll thinking container to keep up with stream
      const newThink = newEl.querySelector('details.think');
      const newThinkDiv = newThink ? newThink.querySelector('.think-content, div') : null;
      if (newThinkDiv) {
        newThinkDiv.addEventListener('scroll', () => {
          const d = newThinkDiv.scrollHeight - newThinkDiv.scrollTop - newThinkDiv.clientHeight;
          m._thinkUserScrolled = d > 15;
        }, { passive: true });

        if (generating && !m.content && thinkWasAtBottom && !m._thinkUserScrolled) {
          newThinkDiv.scrollTop = newThinkDiv.scrollHeight;
        } else if (thinkScrollTop >= 0) {
          newThinkDiv.scrollTop = thinkScrollTop;
        }
      }
    }
  }

  // Only auto-scroll the main chat container if user has NOT scrolled up and was at the bottom
  if (chat && !chatUserScrolledUp && chatWasAtBottom) {
    chat.scrollTop = chat.scrollHeight;
    updateScrollBottomBtn(false);
  } else if (generating && chatUserScrolledUp) {
    updateScrollBottomBtn(true);
  }

  updateContextChip();
  if (inner.lastElementChild && !generating && typeof renderInlineMermaid === 'function') {
    renderInlineMermaid(inner.lastElementChild);
  }
}

/* ---------------- Claude-Style Dynamic Working State ---------------- */
function formatToolStatus(name, args) {
  args = args || {};
  let p = args.path || args.file || args.filename || '';
  if (typeof p === 'string' && p) {
    p = p.split('\\').pop().split('/').pop();
  }
  switch (name) {
    case 'read_file':
      return p ? `Reading ${p}...` : 'Reading file...';
    case 'write_file':
      return p ? `Writing ${p}...` : 'Writing file...';
    case 'edit_file':
      return p ? `Editing ${p}...` : 'Editing file...';
    case 'run_python':
      return 'Running Python script...';
    case 'grep': {
      const q = args.query || args.pattern || '';
      return q ? `Searching for "${q.length > 25 ? q.slice(0, 25) + '…' : q}"...` : 'Searching workspace...';
    }
    case 'list_files':
      return p ? `Scanning ${p}...` : 'Listing workspace files...';
    case 'web_search': {
      const q = args.query || '';
      return q ? `Searching web for "${q.length > 25 ? q.slice(0, 25) + '…' : q}"...` : 'Searching the web...';
    }
    case 'web_fetch':
      return 'Fetching webpage content...';
    case 'search_memory':
      return 'Searching past memory...';
    case 'search_knowledge_base':
      return 'Searching knowledge base...';
    case 'analyze_image':
      return 'Analyzing image with vision model...';
    case 'create_plan':
      return 'Formulating execution plan...';
    case 'update_plan_item':
      return 'Updating task plan...';
    case 'list_diff':
      return 'Reviewing workspace changes...';
    case 'revert':
      return p ? `Reverting ${p}...` : 'Reverting file...';
    default:
      return `Executing ${name}...`;
  }
}
window.formatToolStatus = formatToolStatus;

const CLAUDE_WORKING_PHRASES = [
  'Working...',
  'Crunching...',
  'Thinking...',
  'Connecting the dots...',
  'Synthesizing ideas...',
  'Pondering...',
  'Formulating response...',
  'Brewing thoughts...',
  'Analyzing context...',
  'Piecing it together...',
  'Ruminating...',
  'Drafting response...',
  'Weaving insights...',
  'Polishing thoughts...',
  'Simmering...'
];

function getClaudeWorkingPhrase(m, elapsedSec) {
  if (m && m.statusText) {
    return m.statusText;
  }
  const isAgent = (m && m.acts && m.acts.length > 0) || (typeof agentMode !== 'undefined' && agentMode);
  if (isAgent) {
    const AGENT_PHRASES = [
      'Working on task...',
      'Crunching...',
      'Planning next action...',
      'Analyzing workspace...',
      'Connecting the dots...',
      'Synthesizing findings...',
      'Drafting response...',
      'Refining solution...',
      'Polishing output...',
      'Almost there...'
    ];
    const idx = Math.floor(elapsedSec / 2.5) % AGENT_PHRASES.length;
    return AGENT_PHRASES[idx];
  }
  const idx = Math.floor(elapsedSec / 2.5) % CLAUDE_WORKING_PHRASES.length;
  return CLAUDE_WORKING_PHRASES[idx];
}

function renderClaudeWorkingStateHtml(msg, elapsedSec) {
  return `<div class="claude-working-box">`
    + `<span class="claude-sparkle-icon">✦</span>`
    + `<span class="claude-working-text">${esc(msg)}</span>`
    + `<span class="claude-bouncing-dots"><span class="dot"></span><span class="dot"></span><span class="dot"></span></span>`
    + `<span class="claude-working-timer mono">${elapsedSec}s</span>`
    + `</div>`;
}

let _genTicker = null;
let _genStartTime = 0;

function startClaudeWorkingTicker(t0) {
  _genStartTime = t0 || performance.now();
  if (_genTicker) clearInterval(_genTicker);
  _genTicker = setInterval(onClaudeWorkingTick, 500);
}

function stopClaudeWorkingTicker() {
  if (_genTicker) {
    clearInterval(_genTicker);
    _genTicker = null;
  }
}

function onClaudeWorkingTick() {
  if (!generating || !messages || !messages.length) {
    stopClaudeWorkingTicker();
    return;
  }
  const lastIdx = messages.length - 1;
  const m = messages[lastIdx];
  if (!m || m.role !== 'assistant') return;

  const job = (curSession && window.bgJobs) ? window.bgJobs.get(String(curSession.id)) : null;
  const t0 = (job && job.t0) ? job.t0 : _genStartTime;
  const elapsedSec = Math.max(1, Math.floor((performance.now() - t0) / 1000));
  const hasText = !!(m.content && m.content.trim());

  const inner = $('chat-inner');
  if (!inner) return;
  const lastEl = inner.lastElementChild;
  if (!lastEl) return;

  // 1. Update think summary timer if model is actively thinking
  const isGeneratingThis = generating && (lastIdx === messages.length - 1);
  const isActivelyThinking = isGeneratingThis && !hasText;
  const thinkSummary = lastEl.querySelector('details.think > summary');
  if (thinkSummary && isActivelyThinking) {
    thinkSummary.innerHTML = `<span class="codex-thought-icon">🧠</span> <span class="codex-thought-title">Thinking (${elapsedSec}s)...</span><span class="codex-chevron">▾</span>`;
  }

  // 2. If content text hasn't streamed in yet, update or create the working pill
  if (!hasText) {
    const workingBox = lastEl.querySelector('.claude-working-box');
    const msg = getClaudeWorkingPhrase(m, elapsedSec);
    if (workingBox) {
      const textEl = workingBox.querySelector('.claude-working-text');
      const timerEl = workingBox.querySelector('.claude-working-timer');
      if (textEl && textEl.textContent !== msg) textEl.textContent = msg;
      if (timerEl) timerEl.textContent = `${elapsedSec}s`;
    } else {
      scheduleRenderLast();
    }
  }
}

function bubbleHtml(m, idx) {
  if (m.role === 'user') {
    const imgs = (m.images || []).map(u =>
      `<img src="${esc(u)}" class="chat-img-thumb" alt="Attachment" title="Click to enlarge" data-click="open-image" style="max-width:240px; max-height:180px; border-radius:8px; display:block; margin:6px 0; border:1px solid rgba(255,255,255,0.15); box-shadow:0 2px 8px rgba(0,0,0,0.3); cursor:zoom-in;">`).join('');
    const filesTag = m.files ? `<div class="dim" style="font-size:10.5px; margin-top:4px;">📎 ${esc(m.files)}</div>` : '';
    // Display clean user text, strip any injected vision/file tags from the bubble UI
    let displayText = m.displayContent || m.content || '';
    if (displayText.includes('--- IMAGE:')) {
      displayText = displayText.replace(/--- IMAGE:[\s\S]*?--- END [^\n]+ ---/g, '').trim();
    }
    if (displayText.includes('--- FILE:')) {
      displayText = displayText.replace(/--- FILE:[\s\S]*?--- END [^\n]+ ---/g, '').trim();
    }
    if (displayText.includes('[Attached Files]')) {
      displayText = displayText.replace(/\[Attached Files\][\s\S]*?--- END [^\n]+ ---(\s*\[NOTE:.*\])?/g, '').trim();
    }
    return `<div class="msg user"><div class="bubble">${imgs}${md(displayText || '(attachment)')}${filesTag}</div></div>`;
  }
  // pending compaction indicator
  if (m.compactPending) {
    const bTok = m.beforeToks || 0;
    const bTokStr = bTok ? ` (~${bTok >= 1000 ? (bTok / 1000).toFixed(1) + 'k' : bTok} tokens)` : '';
    return `<div class="msg bot"><div class="bubble" style="border:1px dashed var(--accent, #6366f1); background:rgba(99,102,241,0.06); padding:12px 14px;">`
      + `<div style="display:flex; align-items:center; gap:8px; font-size:12.5px; font-weight:600; color:var(--accent, #6366f1); margin-bottom:4px;">`
      + `<span>🧹</span> <span>Compacting conversation${bTokStr}…</span> <span class="cursor">▍</span></div>`
      + `<div class="dim" style="font-size:11px; line-height:1.4;">Distilling conversation context, key decisions, files, and next steps into a concise summary (~70–85% reduction expected).`
      + (m.content ? `<br><span style="color:var(--text); font-style:italic;">${esc(m.content)}</span>` : '')
      + `</div></div></div>`;
  }
  // compacted-context summary bubble (from /compact)
  if (m.compact) {
    const bTok = m.compactBefore || 0;
    const aTok = m.compactAfter || m.ntok || 0;
    const pct = (typeof m.reductionPct === 'number' && m.reductionPct > 0)
      ? m.reductionPct
      : ((bTok > 0 && aTok > 0 && bTok > aTok) ? Math.round((1 - aTok / bTok) * 100) : 0);
    const wasTok = bTok ? ` · was ${bTok >= 1000 ? (bTok / 1000).toFixed(1) + 'k' : bTok} tok` : '';
    const nowTok = aTok ? ` → now ${aTok >= 1000 ? (aTok / 1000).toFixed(1) + 'k' : aTok} tok` : '';
    const keptInfo = m.compactKept ? ` (${m.compactKept} recent preserved)` : '';
    const pctBadge = pct > 0
      ? `<span style="background:rgba(34,197,94,0.15); color:#4ade80; border:1px solid rgba(34,197,94,0.35); border-radius:999px; padding:1px 8px; font-size:11px; font-weight:700; display:inline-flex; align-items:center; gap:3px;">-${pct}% reduction</span>`
      : '';
    return `<div class="msg bot"><div class="bubble" style="border:1px dashed rgba(99,102,241,0.45); background:rgba(99,102,241,0.04); padding:12px 14px;">`
      + `<div style="display:flex; justify-content:space-between; align-items:center; margin-bottom:8px; border-bottom:1px solid rgba(255,255,255,0.08); padding-bottom:6px; flex-wrap:wrap; gap:6px;">`
      + `  <div style="font-size:12px; font-weight:700; color:var(--accent, #6366f1); display:flex; align-items:center; gap:8px;">`
      + `    <span>🧹 Compacted Context Summary</span>`
      + `    ${pctBadge}`
      + `  </div>`
      + `  <div class="mono dim" style="font-size:10.5px;">${wasTok}${nowTok}${keptInfo}</div>`
      + `</div>`
      + `${md((m.content || '').replace(/^\[COMPACTED CONTEXT SUMMARY\]\n?/, ''))}`
      + `<div class="dim" style="font-size:10.5px; margin-top:8px; padding-top:6px; border-top:1px dashed rgba(255,255,255,0.08);">Earlier messages are still saved above — only replies from here on use this summary as context.</div>`
      + `</div></div>`;
  }
  let inner = '';
  
  // Structured plan checklist (create_plan / update_plan_item tracking)
  if (m.acts && m.acts.length) {
    inner += planPanelHtml(m.acts);
  }

  // Render collapsible Antigravity agent action items & tool calls (if present in message)
  if (m.acts && m.acts.length) {
    inner += agentActsHtml(m.acts);
  }

  const isLast = idx === messages.length - 1;
  // answers saved with a leaked think block (bare </think>) -> reasoning card
  if (!(generating && isLast) && m.content && m.content.includes('</think>')) {
    sseApplyThinkSplit(m);
  }
  if (m.acts && m.acts.length && typeof agentStoppedHtml === 'function') {
    inner += agentStoppedHtml(m.acts, isLast && !generating);
  }
  const hasText = !!(m.content && m.content.trim());

  const actsHasThoughts = m.acts && m.acts.some(a => a.type === 'thought' || a.type === 'reasoning');
  if (m.reasoning && !actsHasThoughts) {
    const isGeneratingThis = generating && isLast;
    const isActivelyThinking = isGeneratingThis && !hasText;
    const job = (curSession && window.bgJobs) ? window.bgJobs.get(String(curSession.id)) : null;
    const t0 = (job && job.t0) ? job.t0 : _genStartTime;
    const elapsedSec = isActivelyThinking ? Math.max(1, Math.floor((performance.now() - t0) / 1000)) : 0;
    if (isActivelyThinking) {
      m._lastThinkSec = elapsedSec;
    }
    const finalSec = m._lastThinkSec || (m.secs ? Math.max(1, Math.round(m.secs)) : null);
    // Auto-expand while thinking if user hasn't explicitly toggled it
    const isOpen = m._thinkOpen !== undefined ? m._thinkOpen : isActivelyThinking;
    const statusLabel = isActivelyThinking
      ? `Thinking (${elapsedSec}s)...`
      : (finalSec ? `Thought for ${finalSec}s` : 'Thought');
    const thinkBody = esc(m.reasoning).replace(/\n/g, '<br>') + (isActivelyThinking ? '<span class="cursor">▍</span>' : '');
    inner += `<details class="think codex-thought-card" ${isOpen ? 'open' : ''} data-think-idx="${idx}">
      <summary class="codex-thought-head" data-click="think-summary" data-arg="${idx}">
        <span class="codex-thought-icon">🧠</span>
        <span class="codex-thought-title">${statusLabel}</span>
        <span class="codex-chevron">▾</span>
      </summary>
      <div class="think-content codex-thought-body">${thinkBody}</div>
    </details>`;
  }

  let body = '';
  const acHidden = typeof answerCheckHides === 'function' && answerCheckHides(m);
  const mediaLive = typeof mediaBusy === 'function' && mediaBusy(m);
  if (mediaLive) {
    body = mediaCardHtml(m, idx);          // /image or /video still running / asking / failed
  } else if (acHidden) {
    body = answerCheckGateHtml(m);
  } else if (hasText) {
    body = md(m.content);
  } else if (!generating && m.reasoning && m.reasoning.trim()) {
    // If generation completed and content was empty, render reasoning so user is never left with a blank message
    body = md(m.reasoning);
  } else if (generating && isLast) {
    const job = (curSession && window.bgJobs) ? window.bgJobs.get(String(curSession.id)) : null;
    const t0 = (job && job.t0) ? job.t0 : _genStartTime;
    const elapsedSec = Math.max(1, Math.floor((performance.now() - t0) / 1000));
    const msg = getClaudeWorkingPhrase(m, elapsedSec);
    body = renderClaudeWorkingStateHtml(msg, elapsedSec);
  }
  
  // Render interactive grill-me / ask_question choice cards if options or question frontiers are present
  if (hasText && !generating && !acHidden) {
    const qCards = renderInteractiveQuestions(m.content, idx);
    if (qCards) {
      body += qCards;
    }
  }

  // Render dedicated media preview section for images and videos found in assistant response
  if (hasText && !m.media && typeof renderMediaPreviewSection === 'function') {
    const mediaPreview = renderMediaPreviewSection(m.content);
    if (mediaPreview) {
      body += mediaPreview;
    }
  }

  if (body || m.errorAlert) {
    const isWorkingPill = generating && isLast && !hasText && !m.errorAlert;
    const bubbleClass = isWorkingPill ? 'bubble claude-working-container' : 'bubble';
    const alertHtml = m.errorAlert ? `<div class="chat-alert-box error"><svg class="ui-icon" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" style="flex-shrink:0; margin-top:2px;"><circle cx="12" cy="12" r="10"/><line x1="12" y1="8" x2="12" y2="12"/><line x1="12" y1="16" x2="12.01" y2="16"/></svg><div><strong>Service Notice</strong><div style="font-size:12px; margin-top:2px; opacity:0.9;">${esc(m.errorAlert)}</div></div></div>` : '';
    inner += `<div class="${bubbleClass}">${body}${alertHtml}</div>`;
  }
  if (typeof answerCheckBadgeHtml === 'function') inner += answerCheckBadgeHtml(m, idx);
  if (m.media && typeof mediaActionsHtml === 'function') inner += mediaActionsHtml(m, idx);
  if (m.tps) {
    const modelTag = m.modelDisplay
      ? `${m.modelSource === 'cloud' ? '☁ ' : ''}${esc(m.modelDisplay)} · `
      : '';
    inner += `<div class="meta">${modelTag}${m.ntok} tok · ${m.tps.toFixed(1)} t/s · ${m.secs.toFixed(1)}s</div>`;
  }
  return `<div class="msg bot">${inner}</div>`;
}

// Interactive question cards (Claude/Codex-style): one option per line, radio for
// single-choice, checkbox for multi-select, plus an "Other" row with free text.
// Handlers are wired by event delegation off data-* indexes — never interpolate
// option text into inline JS (apostrophes in options used to break onclick).
// State: _grillMeta[msgIdx] = parsed questions; _grillSelected[msgIdx][qi] = Set(optIdx | 'other');
// _grillCustom[msgIdx][qi] = "Other" text (survives re-renders).
window._grillMeta = window._grillMeta || {};
window._grillSelected = window._grillSelected || {};
window._grillCustom = window._grillCustom || {};
// Track submitted question cards so previous rounds show submitted status and do not get re-submitted
window._grillSubmitted = window._grillSubmitted || new Set();

function _grillSel(idx, qi) {
  window._grillSelected[idx] = window._grillSelected[idx] || {};
  window._grillSelected[idx][qi] = window._grillSelected[idx][qi] || new Set();
  return window._grillSelected[idx][qi];
}

function _grillSyncDom(idx, qi) {
  const block = $(`grill-q-${idx}-${qi}`);
  if (!block) return;
  const sel = _grillSel(idx, qi);
  block.querySelectorAll('.grill-opt').forEach(row => {
    const key = row.dataset.opt === 'other' ? 'other' : Number(row.dataset.opt);
    const on = sel.has(key);
    row.classList.toggle('active', on);
    row.setAttribute('aria-checked', on ? 'true' : 'false');
  });
}

function toggleGrillOption(idx, qi, key) {
  if (window._grillSubmitted.has(idx)) return;
  const q = (window._grillMeta[idx] || [])[qi];
  if (!q) return;
  const sel = _grillSel(idx, qi);
  if (q.multi) {
    if (sel.has(key)) sel.delete(key); else sel.add(key);
  } else {
    sel.clear();
    sel.add(key);
  }
  _grillSyncDom(idx, qi);
  if (key === 'other' && sel.has('other')) {
    const inp = $(`grill-custom-${idx}-${qi}`);
    if (inp) inp.focus();
  }
}

function _grillRowFromEvent(e) {
  const row = e.target.closest && e.target.closest('.grill-opt');
  if (!row) return null;
  const card = row.closest('.grill-interactive-box');
  const block = row.closest('.grill-q-block');
  if (!card || !block) return null;
  return {
    row,
    idx: Number(card.dataset.idx),
    qi: Number(block.dataset.qi),
    key: row.dataset.opt === 'other' ? 'other' : Number(row.dataset.opt),
  };
}

document.addEventListener('click', (e) => {
  const hit = _grillRowFromEvent(e);
  if (!hit) return;
  // Clicking into the "Other" text box selects it but never toggles it off
  if (e.target.closest('.grill-custom-input')) {
    if (!_grillSel(hit.idx, hit.qi).has('other')) toggleGrillOption(hit.idx, hit.qi, 'other');
    return;
  }
  toggleGrillOption(hit.idx, hit.qi, hit.key);
});

document.addEventListener('keydown', (e) => {
  const inp = e.target.closest && e.target.closest('.grill-custom-input');
  if (inp) {
    if (e.key === 'Enter') {
      e.preventDefault();
      submitGrillAnswers(Number(inp.closest('.grill-interactive-box').dataset.idx));
    }
    return;
  }
  const hit = _grillRowFromEvent(e);
  if (hit && (e.key === ' ' || e.key === 'Enter')) {
    e.preventDefault();
    toggleGrillOption(hit.idx, hit.qi, hit.key);
  }
});

document.addEventListener('input', (e) => {
  const inp = e.target.closest && e.target.closest('.grill-custom-input');
  if (!inp) return;
  const idx = Number(inp.closest('.grill-interactive-box').dataset.idx);
  const qi = Number(inp.closest('.grill-q-block').dataset.qi);
  window._grillCustom[idx] = window._grillCustom[idx] || {};
  window._grillCustom[idx][qi] = inp.value;
  // Typing in "Other" selects it (and deselects others for single-choice)
  if (inp.value.trim() && !_grillSel(idx, qi).has('other')) toggleGrillOption(idx, qi, 'other');
});

function submitGrillAnswers(idx) {
  if (window._grillSubmitted.has(idx)) return;
  const meta = window._grillMeta[idx] || [];
  const lines = [];

  meta.forEach((q, qi) => {
    const sel = _grillSel(idx, qi);
    const custom = ((window._grillCustom[idx] || {})[qi] || '').trim();
    const chosen = q.options.filter((_, oi) => sel.has(oi));
    if (custom && sel.has('other')) chosen.push(custom);
    if (chosen.length > 0) lines.push(`${q.qId}: ${chosen.join(', ')}`);
  });

  if (lines.length === 0) {
    toast('Please select an option or write an answer first', true);
    return;
  }

  // Mark as submitted
  window._grillSubmitted.add(idx);
  const card = $(`grill-card-${idx}`);
  if (card) {
    card.classList.add('submitted');
    card.querySelectorAll('.grill-opt').forEach(r => { r.setAttribute('aria-disabled', 'true'); r.tabIndex = -1; });
    card.querySelectorAll('.grill-custom-input').forEach(i => { i.disabled = true; });
    const sub = card.querySelector('.grill-box-sub');
    if (sub) sub.textContent = 'Answers submitted';
    const btn = card.querySelector('.grill-submit-btn');
    if (btn) {
      btn.disabled = true;
      btn.textContent = '✓ Answer Submitted';
    }
  }

  const text = lines.join('\n');
  if (agentMode) {
    runAgentSSE(text);
  } else {
    send(text);
  }
}

const _GRILL_MULTI_RE = /\b(?:select|choose|pick|check|tick)\s+(?:all|any|one or more|multiple|several)\b|\ball that apply\b|\bmulti-?select\b|\bmultiple (?:choices|answers|options)\b/i;
const _GRILL_OTHER_RE = /^(?:other|something else|none of (?:the )?above|custom(?: answer)?)\b/i;

function renderInteractiveQuestions(rawText, idx) {
  // Check if text matches grill-me format or question frontiers
  // Look for ❓, Q1/Q2, Question 1, or ➡️ / recommended markers
  const hasTrigger = rawText.includes('❓') ||
                     /\b(?:Q[0-9]+|Question\s+[0-9]+)\b/i.test(rawText) ||
                     rawText.includes('➡️') ||
                     /(?:^|\n)\s*(?:[-*•]|\([a-zA-Z0-9]+\)|[0-9]+\))\s*\[[ x]\]/i.test(rawText);
  if (!hasTrigger) return '';

  // Split into question chunks: match starting with ❓, Q1:, Question 1:, etc.
  const blocks = rawText.split(/(?:^|\n)(?=(?:❓|#{1,4}\s*Q[0-9]+|#{1,4}\s*Question\s+[0-9]+|\bQ[0-9]+|\bQuestion\s+[0-9]+|\*\*[0-9]+\.\*\*)[.:\s-])/gi).filter(b => b.trim());
  const parsedQuestions = [];

  for (const block of blocks) {
    // Match Q identifier and title
    const qMatch = block.match(/(?:❓\s*)?(?:#{1,4}\s*)?(?:[*_]{0,2})(Q[0-9]+|Question\s+[0-9]+|\b[0-9]+)[*_]{0,2}\s*[-–:.)]?\s*([^\n]+)/i);
    if (!qMatch) continue;
    let rawQId = qMatch[1].trim();
    if (!rawQId.toLowerCase().startsWith('q')) {
      const numOnly = rawQId.replace(/\D/g, '');
      rawQId = numOnly ? `Q${numOnly}` : rawQId;
    } else {
      rawQId = rawQId.replace(/Question\s*/i, 'Q').toUpperCase();
    }
    const qId = rawQId;
    const qTitle = qMatch[2].replace(/[*_#]/g, '').trim();

    // Look for recommended answer (➡️ ... or Recommendation: ... or (Recommended: ...))
    let recommendation = '';
    const recMatch = block.match(/(?:➡️|Recommendation:?|Recommended:?)\s*([^\n]+)/i);
    if (recMatch) {
      recommendation = recMatch[1].replace(/[*_]/g, '').trim();
    }

    // Look for options like: - [ ] Option or - Option A or (A) Option or A) Option
    const options = [];
    let sawCheckbox = false;
    const lines = block.split('\n');
    for (const line of lines) {
      const optMatch = line.match(/^\s*(?:[-*•]|\([a-zA-Z0-9]+\)|[a-zA-Z0-9]+[.)])\s*(\[[ x]\]\s*)?([^\n]+)/i);
      if (!optMatch) continue;
      let optText = optMatch[2].replace(/[*_`]/g, '').trim();
      if (optText.startsWith('❓') || optText.startsWith('➡️') || optText.toLowerCase().startsWith('recommend')) continue;
      if (optText.length <= 1 || optText.length >= 200) continue;
      // Lead-in lines ("However, here are a few things I can do:") are headers, not choices
      if (/:\s*$/.test(optText)) continue;
      if (optMatch[1]) sawCheckbox = true;
      if (/^(?:\(Recommended\)|\(Rec\)|\bRecommended:?\b)/i.test(optText)) {
        optText = optText.replace(/^(?:\(Recommended\)|\(Rec\)|\bRecommended:?\b)/i, '').trim();
        if (!recommendation) recommendation = optText;
      }
      // The card always renders its own "Other" row
      if (_GRILL_OTHER_RE.test(optText)) continue;
      if (optText && !options.includes(optText)) {
        options.push(optText);
      }
    }

    if (recommendation) {
      const cleanRec = recommendation.replace(/^(?:\(Recommended\)|\(Rec\)|\bRecommended:?\b)/i, '').trim();
      if (cleanRec && !options.some(o => o.toLowerCase().includes(cleanRec.toLowerCase()) || cleanRec.toLowerCase().includes(o.toLowerCase()))) {
        options.unshift(cleanRec);
      }
    }

    if (options.length > 0 || recommendation) {
      const multi = sawCheckbox || _GRILL_MULTI_RE.test(block);
      parsedQuestions.push({ qId, qTitle, recommendation, options, multi });
    }
  }

  if (parsedQuestions.length === 0) return '';

  window._grillMeta[idx] = parsedQuestions;
  const isSubmitted = window._grillSubmitted.has(idx);
  const customState = window._grillCustom[idx] || {};
  const tab = isSubmitted ? '-1' : '0';
  const disabledAttr = isSubmitted ? ' aria-disabled="true"' : '';
  let html = `<div class="grill-interactive-box${isSubmitted ? ' submitted' : ''}" id="grill-card-${idx}" data-idx="${idx}">`;
  html += `<div class="grill-box-header"><span>🎯 Decision Options</span><span class="grill-box-sub">${isSubmitted ? 'Answers submitted' : 'Select an option or write your own'}</span></div>`;

  parsedQuestions.forEach((q, qi) => {
    const sel = _grillSel(idx, qi);
    const role = q.multi ? 'checkbox' : 'radio';
    const kind = q.multi ? 'multi' : 'single';
    html += `<div class="grill-q-block" id="grill-q-${idx}-${qi}" data-qi="${qi}">`;
    html += `<div class="grill-q-label"><strong>${esc(q.qId)}</strong> ${esc(q.qTitle)}`;
    html += `<span class="grill-q-mode">${q.multi ? 'Select all that apply' : 'Select one'}</span></div>`;
    html += `<div class="grill-options-list" role="${q.multi ? 'group' : 'radiogroup'}">`;

    q.options.forEach((opt, oi) => {
      const isRec = q.recommendation && (opt === q.recommendation || opt.includes(q.recommendation) || q.recommendation.includes(opt));
      const on = sel.has(oi);
      html += `<div class="grill-opt ${kind}${isRec ? ' recommended' : ''}${on ? ' active' : ''}" role="${role}" tabindex="${tab}" aria-checked="${on}"${disabledAttr} data-opt="${oi}">`;
      html += `<span class="grill-opt-mark" aria-hidden="true"></span>`;
      html += `<span class="grill-opt-num">${oi + 1}.</span>`;
      html += `<span class="grill-opt-text">${esc(opt)}</span>`;
      if (isRec) html += `<span class="grill-pill-badge">★ Recommended</span>`;
      html += `</div>`;
    });

    // "Other" row: free-text answer
    const otherOn = sel.has('other');
    html += `<div class="grill-opt other ${kind}${otherOn ? ' active' : ''}" role="${role}" tabindex="${tab}" aria-checked="${otherOn}"${disabledAttr} data-opt="other">`;
    html += `<span class="grill-opt-mark" aria-hidden="true"></span>`;
    html += `<span class="grill-opt-num">${q.options.length + 1}.</span>`;
    html += `<input type="text" id="grill-custom-${idx}-${qi}" class="grill-custom-input" value="${esc(customState[qi] || '')}" placeholder="Other — type your own answer…" ${isSubmitted ? 'disabled' : ''}>`;
    html += `</div>`;

    html += `</div></div>`;
  });

  html += `<div class="grill-footer">`;
  html += `<button type="button" class="btn primary grill-submit-btn" ${isSubmitted ? 'disabled' : ''} data-click="submit-grill" data-arg="${idx}">${isSubmitted ? '✓ Answer Submitted' : '✓ Submit Decisions'}</button>`;
  html += `</div>`;
  html += `</div>`;

  return html;
}

function setGenUI(on) {
  generating = on;
  $('btn-send').style.display = on ? 'none' : '';
  $('btn-abort').style.display = on ? '' : 'none';
  if (on) {
    const job = (curSession && window.bgJobs) ? window.bgJobs.get(String(curSession.id)) : null;
    startClaudeWorkingTicker(job ? job.t0 : performance.now());
  } else {
    stopClaudeWorkingTicker();
    if (typeof mediaSyncStop === 'function') mediaSyncStop();   // an image may still be in progress
  }
  $('input').focus();
}

// Previous answers' large code blocks (e.g. a whole HTML report) are resent on
// every follow-up and pull the model back to stale content; replace them with
// a short stub. [DOWNLOAD: ...] tags stay so the model knows the file exists.
function omitBulkyCode(text, maxChars = 1500) {
  if (!text || text.indexOf('```') === -1) return text;
  return text.replace(/```([\w+-]*)[^\n]*\n([\s\S]*?)```/g, (whole, lang, body) => {
    if (whole.length <= maxChars) return whole;
    const lines = body.split('\n').length;
    return `[previous ${lang || 'code'} block, ${lines} lines — omitted]`;
  });
}

async function send(inputText) {
  const input = $('input');
  const text = (inputText !== undefined ? inputText : (input ? input.value : '')).trim();
  const hasFiles = attachments && attachments.some(a => a.content != null || (a.isImage && a.b64));
  if ((!text && !hasFiles) || generating) return;
  if (!mainLaneReady()) {
    const sel = $('profile');
    if (!sel || !sel.value) {
      toast('Please select a model from the top dropdown first', true);
      return;
    }
    const mName = sel.value.split('\\').pop().split('/').pop();
    toast(`⏳ Loading ${mName} into GPU VRAM before sending...`);
    await loadSelectedModel();
    await pollStatus();
    if (!mainLaneReady()) {
      toast('Model loading failed or still in progress. Please wait a moment and try again.', true);
      return;
    }
  }
  const sentAttachments = attachments.slice();
  const sentImages = sentAttachments.filter(a => a.isImage && a.dataUrl).map(a => a.dataUrl);
  const sentFiles = sentAttachments.map(a => a.name).join(', ');
  const nFiles = sentAttachments.filter(a => a.content != null).length;
  if (input) input.value = '';
  if (window.renderInputHighlights) window.renderInputHighlights();
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
    console.warn('Chat prompt build warning:', err);
  }
  if (chatWebSearch) assistantMsg.statusText = '🌐 Searching web & analyzing...';
  renderLast();

  const userMeta = {
    images: sentImages.length ? sentImages : undefined,
    files: sentFiles || undefined,
    displayContent: text || undefined
  };
  const session = await ensureSession(text ? text.slice(0, 60) : (sentFiles ? `📎 ${sentFiles}` : 'Files session'));
  const sessionId = session ? session.id : (curSession ? curSession.id : 0);
  const sessionTitle = session ? session.title : (curSession ? curSession.title : (text || 'Chat').slice(0, 40));
  persistMsgForSession(sessionId, 'user', fullPrompt, userMeta);

  // Register into background jobs
  const jobCtrl = ctrl;
  const job = {
    id: sessionId,
    title: sessionTitle,
    mode: 'chat',
    ctrl: jobCtrl,
    messages: messages,
    assistantMsg: assistantMsg,
    t0: performance.now(),
  };
  window.bgJobs.set(String(sessionId), job);
  if (typeof updateBgIndicators === 'function') updateBgIndicators();

  const samplingCfg = getSamplingConfig();
  const sys = (samplingCfg.sysprompt || '').trim();
  // The system prompt goes only via `system_prompt`; the server composes the
  // system message (pushing it here too sent it twice).
  const msgs = [];
  const ctxMsgs = typeof buildContextMessages === 'function' ? buildContextMessages() : messages;
  for (const m of ctxMsgs.slice(0, -1)) {
    msgs.push({ role: m.role, content: m.role === 'assistant' ? omitBulkyCode(m.content) : m.content });
  }
  const t0 = performance.now();
  const getJobAssistant = () => job.assistantMsg;

  try {
    const res = await fetch('/chat/run', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        messages: msgs,
        web_search: !!chatWebSearch,
        deep_mode: !!chatDeepMode,
        reasoning_effort: typeof getReasoningEffort === 'function' ? getReasoningEffort() : undefined,
        verify: typeof answerCheckBegin === 'function' ? answerCheckBegin(job.assistantMsg) : undefined,
        system_prompt: sys || undefined,
        temperature: samplingCfg.temp,
        max_tokens: (isNaN(samplingCfg.maxtok) || samplingCfg.maxtok <= 0) ? -1 : samplingCfg.maxtok,
      }),
      signal: jobCtrl.signal,
    });
    if (!res.ok) {
      const e = await res.json().catch(() => ({}));
      throw new Error(e.error || ('HTTP ' + res.status));
    }
    await readSSE(res, (ev, d) => {
      const L = getJobAssistant();
      if (typeof sseAnswerCheck === 'function' && sseAnswerCheck(L, ev, d)) {
        // verify_start / verify_result (answer-check.js)
      } else if (ev === 'lane') {
        L.modelDisplay = d.display || d.model;
        L.modelSource = d.source;
        L.modelProvider = d.provider;
      } else if (ev === 'queued') {
        // all local model slots busy: routes/common.py admission gate
        L.statusText = '⏳ Waiting for a free model slot' + (d.position ? ` (#${d.position} in queue)` : '') + '...';
      } else if (ev === 'delta') {
        L.content += (d.text || '');
        L.statusText = '';
      } else if (ev === 'thought_delta') {
        L.reasoning = (L.reasoning || '') + (d.delta || '');
        if (typeof window.setLiveHud === 'function') {
          window.setLiveHud({ phase: 'thinking', text: 'Thinking & analyzing...' });
        }
      } else if (ev === 'delta_to_thought') {
        sseDeltaToThought(L);
      } else if (ev === 'tool_preparing') {
        sseToolPreparing(L, d);
      } else if (ev === 'thought') {
        L.reasoning = (L.reasoning ? L.reasoning + '\n\n' : '') + (d.text || '');
      } else if (ev === 'delta_replace') {
        L.content = (d.text || '');
        L.statusText = '';
      } else if (ev === 'delta_reset') {
        // a checked-and-fixed answer replaces the draft (kept in check.original)
        if (L.content && L.content.trim() && !(L.check && L.check.state === 'checking')) {
          L.reasoning = (L.reasoning ? L.reasoning + '\n\n' : '') + L.content.trim();
        }
        L.content = '';
      } else if (ev === 'tool_call') {
        sseToolCall(L, d);
      } else if (ev === 'tool_result') {
        sseToolResult(L, d);
      } else if (ev === 'tool_progress') {
        sseToolProgress(L, d);
      } else if (ev === 'done') {
        L.statusText = '';
        if (typeof answerCheckEnd === 'function') answerCheckEnd(L);
        if (typeof window.setLiveHud === 'function') {
          window.setLiveHud({ phase: 'done', text: 'Response complete' });
        }
        if (d && (d.completion_tokens || d.total_tokens)) {
          if (d.completion_tokens) L.ntok = d.completion_tokens;
          // Real prompt size (system prompt + tools + history + tool results);
          // persisted so the context chip survives a reload.
          if (d.prompt_tokens) L.promptTokens = d.prompt_tokens;
        }
      } else if (ev === 'kb_blocked') {
        sseKbBlocked(L, d);
      } else if (ev === 'guard') {
        sseGuardToast(d);
      } else if (ev === 'error') {
        throw new Error(d.error || 'Chat execution error');
      }
      if (curSession && String(curSession.id) === String(sessionId)) {
        scheduleRenderLast();
      }
    });
  } catch (e) {
    if (e.name !== 'AbortError') {
      const L = getJobAssistant();
      let errText = e.message || 'Request failed';
      try {
        const jsonMatch = errText.match(/\{[\s\S]*\}/);
        if (jsonMatch) {
          const parsed = JSON.parse(jsonMatch[0]);
          if (parsed.error && parsed.error.message) {
            errText = parsed.error.message;
          } else if (parsed.message) {
            errText = parsed.message;
          }
        }
      } catch (_) {}
      const cleanMsg = errText.replace(/^Chat error\s*/i, '').replace(/^upstream\s+\d+:\s*/i, '');
      L.errorAlert = cleanMsg;
    }
  }

  const targetAssistant = getJobAssistant();
  sseApplyThinkSplit(targetAssistant);
  // Recovery: if content is empty and no error alert, promote reasoning or latest tool result so message never terminates blank
  if (!targetAssistant.content.trim() && !targetAssistant.errorAlert) {
    if (targetAssistant.reasoning && targetAssistant.reasoning.trim()) {
      targetAssistant.content = targetAssistant.reasoning.trim();
    } else if (targetAssistant.acts && targetAssistant.acts.length) {
      const lastRes = targetAssistant.acts.slice().reverse().find(a => a.type === 'tool_result' && a.result);
      if (lastRes && typeof lastRes.result === 'string' && lastRes.result.trim()) {
        targetAssistant.content = `I found the following information:\n\n${lastRes.result.trim().slice(0, 1000)}`;
      } else {
        targetAssistant.content = 'Task completed successfully.';
      }
    }
  }
  const dt = (performance.now() - t0) / 1000;
  const fullLen = (targetAssistant.content || '').length + (targetAssistant.reasoning || '').length;
  const ntok = targetAssistant.ntok || Math.max(1, Math.round(fullLen / 3.5));
  targetAssistant.tps = ntok / dt; targetAssistant.ntok = ntok; targetAssistant.secs = dt;
  if (ntok > 1 && $('chip-ts')) $('chip-ts').textContent = '⚡ ' + (ntok / dt).toFixed(1) + ' t/s';

  if (typeof answerCheckEnd === 'function') answerCheckEnd(targetAssistant);
  persistMsgForSession(sessionId, 'assistant', targetAssistant.content, {
    tps: targetAssistant.tps,
    ntok,
    promptTokens: targetAssistant.promptTokens || undefined,
    secs: dt,
    reasoning: targetAssistant.reasoning || undefined,
    acts: (targetAssistant.acts && targetAssistant.acts.length) ? targetAssistant.acts : undefined,
    modelDisplay: targetAssistant.modelDisplay || undefined,
    modelSource: targetAssistant.modelSource || undefined,
    modelProvider: targetAssistant.modelProvider || undefined,
    check: _checkMeta(targetAssistant),
  });

  window.bgJobs.delete(String(sessionId));
  if (ctrl === jobCtrl) ctrl = null;

  if (curSession && String(curSession.id) === String(sessionId)) {
    setGenUI(false);
    renderLast();
    updateContextChip();
  } else {
    toast(`⚡ Chat "${job.title}" finished generating`);
  }
  if (typeof updateBgIndicators === 'function') updateBgIndicators();
}
