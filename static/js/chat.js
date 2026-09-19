/* ---------------- chat ---------------- */
function renderAll() {
  const inner = $('chat-inner');
  const isLoaded = curStatus && curStatus.pid;
  inner.innerHTML = messages.length ? messages.map(bubbleHtml).join('') :
    `<div id="empty">
      <div class="big">⚡</div>
      <h2>A770 Dual Runtime</h2>
      <p id="empty-model" class="mono">${isLoaded && curStatus.model ? curStatus.model.split('\\').pop().split('/').pop() : 'Model unloaded'}</p>
      ${!isLoaded ? `<div class="empty-card">
        <p><b>No model is currently loaded in GPU VRAM.</b></p>
        <p class="dim" style="margin-top: 6px;">Select a model or profile from the top dropdown menu and click <b>▶ Load Model</b> to start inference.</p>
      </div>` : `<p class="dim" style="margin-top: 10px;">Type a message below to start chatting, or configure parameters in the sidebar.</p>`}
    </div>`;
  const chat = $('chat');
  chat.scrollTop = chat.scrollHeight;
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

function renderLast() {
  const inner = $('chat-inner');
  if (!inner) return;
  const lastIdx = messages.length - 1;
  if (lastIdx < 0 || inner.children.length !== messages.length || $('empty')) {
    renderAll();
    return;
  }

  const lastEl = inner.lastElementChild;
  const m = messages[lastIdx];

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
      thinkWasAtBottom = (prevDiv.scrollHeight - prevDiv.scrollTop - prevDiv.clientHeight) < 40;
    }
  }

  // Generate new HTML for the last message
  const temp = document.createElement('div');
  temp.innerHTML = bubbleHtml(m, lastIdx);
  const newEl = temp.firstElementChild;
  if (newEl) {
    inner.replaceChild(newEl, lastEl);

    // Auto-scroll thinking container to keep up with stream
    const newThink = newEl.querySelector('details.think');
    const newThinkDiv = newThink ? newThink.querySelector('.think-content, div') : null;
    if (newThinkDiv) {
      if (generating && !m.content && thinkWasAtBottom) {
        newThinkDiv.scrollTop = newThinkDiv.scrollHeight;
      } else if (thinkScrollTop >= 0) {
        newThinkDiv.scrollTop = thinkScrollTop;
      }
    }
  }

  const chat = $('chat');
  if (chat) chat.scrollTop = chat.scrollHeight;
  updateContextChip();
  if (newEl && !generating && typeof renderInlineMermaid === 'function') {
    renderInlineMermaid(newEl);
  }
}

function bubbleHtml(m, idx) {
  if (m.role === 'user') {
    const imgs = (m.images || []).map(u =>
      `<img src="${u}" class="chat-img-thumb" alt="Attachment" title="Click to enlarge" onclick="openImageModal(this.src, 'Image attachment')" style="max-width:240px; max-height:180px; border-radius:8px; display:block; margin:6px 0; border:1px solid rgba(255,255,255,0.15); box-shadow:0 2px 8px rgba(0,0,0,0.3); cursor:zoom-in;">`).join('');
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
      + `${md((m.content || '').replace(/^\[COMPACTED CONTEXT SUMMARY\]\n?/, ''))}</div></div>`;
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
  const hasText = !!(m.content && m.content.trim());

  if (m.reasoning) {
    const isGeneratingThis = generating && isLast;
    const isActivelyThinking = isGeneratingThis && !hasText;
    // Auto-expand while thinking if user hasn't explicitly toggled it
    const isOpen = m._thinkOpen !== undefined ? m._thinkOpen : isActivelyThinking;
    const statusLabel = isActivelyThinking ? '💭 Thinking...' : '💭 Thinking';
    const thinkBody = esc(m.reasoning).replace(/\n/g, '<br>') + (isActivelyThinking ? '<span class="cursor">▍</span>' : '');
    inner += `<details class="think" ${isOpen ? 'open' : ''} ontoggle="onToggleThink(${idx}, this.open)"><summary onclick="onThinkSummaryClick(${idx}, event)">${statusLabel}</summary><div class="think-content">${thinkBody}</div></details>`;
  }

  let body = '';
  if (hasText) {
    body = md(m.content);
  } else if (generating && isLast) {
    if (m.statusText) {
      body = `<span class="dim" style="font-size:12px; font-style:italic;">${esc(m.statusText)}</span> <span class="cursor">▍</span>`;
    } else if (!m.reasoning) {
      body = '<span class="cursor">▍</span>';
    }
  }
  
  // Render interactive grill-me / ask_question choice cards if options or question frontiers are present
  if (hasText && !generating) {
    const qCards = renderInteractiveQuestions(m.content, idx);
    if (qCards) {
      body += qCards;
    }
  }

  if (body) {
    inner += `<div class="bubble">${body}${generating && isLast && hasText ? '<span class="cursor">▍</span>' : ''}</div>`;
  }
  if (m.tps) inner += `<div class="meta">${m.ntok} tok · ${m.tps.toFixed(1)} t/s · ${m.secs.toFixed(1)}s</div>`;
  return `<div class="msg bot">${inner}</div>`;
}

// Global state for multi-select question answers: { [msgIdx]: { [qId]: Set(options) } }
window._grillSelected = window._grillSelected || {};

function toggleGrillOption(idx, qId, val, isMulti) {
  window._grillSelected[idx] = window._grillSelected[idx] || {};
  if (!isMulti) {
    window._grillSelected[idx][qId] = new Set([val]);
  } else {
    window._grillSelected[idx][qId] = window._grillSelected[idx][qId] || new Set();
    if (window._grillSelected[idx][qId].has(val)) {
      window._grillSelected[idx][qId].delete(val);
    } else {
      window._grillSelected[idx][qId].add(val);
    }
  }
  // Update DOM classes for selected pills
  const container = $(`grill-q-${idx}-${qId}`);
  if (container) {
    container.querySelectorAll('.grill-pill').forEach(pill => {
      const pVal = pill.getAttribute('data-val');
      const isSel = window._grillSelected[idx][qId].has(pVal);
      pill.classList.toggle('active', isSel);
    });
  }
}

// Track submitted question cards so previous rounds show submitted status and do not get re-submitted
window._grillSubmitted = window._grillSubmitted || new Set();

function submitGrillAnswers(idx) {
  if (window._grillSubmitted.has(idx)) return;
  const qState = window._grillSelected[idx] || {};
  const lines = [];
  
  // Collect from pills
  for (const [qId, setVals] of Object.entries(qState)) {
    const chosen = Array.from(setVals);
    const customInp = $(`grill-custom-${idx}-${qId}`);
    if (customInp && customInp.value.trim()) {
      chosen.push(customInp.value.trim());
    }
    if (chosen.length > 0) {
      lines.push(`${qId}: ${chosen.join(', ')}`);
    }
  }
  
  // Check any standalone inputs where no pill was clicked
  const card = $(`grill-card-${idx}`);
  if (card) {
    card.querySelectorAll('.grill-custom-input').forEach(inp => {
      const qId = inp.getAttribute('data-qid');
      if (!qState[qId] || qState[qId].size === 0) {
        if (inp.value.trim()) {
          lines.push(`${qId}: ${inp.value.trim()}`);
        }
      }
    });
  }

  if (lines.length === 0) {
    toast('Please select an option or write an answer first', true);
    return;
  }

  // Mark as submitted
  window._grillSubmitted.add(idx);
  const btn = card ? card.querySelector('.grill-submit-btn') : null;
  if (btn) {
    btn.disabled = true;
    btn.textContent = '✓ Answer Submitted';
  }
  
  const text = lines.join('\n');
  if (agentMode) {
    runAgentSSE(text);
  } else {
    send(text);
  }
}

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
    const lines = block.split('\n');
    for (const line of lines) {
      const optMatch = line.match(/^\s*(?:[-*•]|\([a-zA-Z0-9]+\)|[a-zA-Z0-9]+[.)])\s*(?:\[[ x]\]\s*)?([^\n]+)/);
      if (optMatch) {
        let optText = optMatch[1].replace(/[*_]/g, '').trim();
        if (!optText.startsWith('❓') && !optText.startsWith('➡️') && !optText.toLowerCase().startsWith('recommend') && optText.length > 1 && optText.length < 150) {
          if (/^(?:\(Recommended\)|\(Rec\)|\bRecommended:?\b)/i.test(optText)) {
            optText = optText.replace(/^(?:\(Recommended\)|\(Rec\)|\bRecommended:?\b)/i, '').trim();
            if (!recommendation) recommendation = optText;
          }
          if (optText && !options.includes(optText)) {
            options.push(optText);
          }
        }
      }
    }

    if (recommendation) {
      const cleanRec = recommendation.replace(/^(?:\(Recommended\)|\(Rec\)|\bRecommended:?\b)/i, '').trim();
      if (cleanRec && !options.some(o => o.toLowerCase().includes(cleanRec.toLowerCase()) || cleanRec.toLowerCase().includes(o.toLowerCase()))) {
        options.unshift(cleanRec);
      }
    }

    if (options.length > 0 || recommendation) {
      parsedQuestions.push({ qId, qTitle, recommendation, options });
    }
  }

  if (parsedQuestions.length === 0) return '';

  const isSubmitted = window._grillSubmitted.has(idx);
  let html = `<div class="grill-interactive-box" id="grill-card-${idx}">`;
  html += `<div class="grill-box-header"><span>🎯 Decision Options</span><span class="grill-box-sub">${isSubmitted ? 'Answers submitted' : 'Click an option or type custom input'}</span></div>`;

  for (const q of parsedQuestions) {
    html += `<div class="grill-q-block" id="grill-q-${idx}-${q.qId}">`;
    html += `<div class="grill-q-label"><strong>${esc(q.qId)}</strong>: ${esc(q.qTitle)}</div>`;
    html += `<div class="grill-options-grid">`;

    for (const opt of q.options) {
      const isRec = q.recommendation && (opt === q.recommendation || opt.includes(q.recommendation) || q.recommendation.includes(opt));
      const cleanVal = opt.replace(/^(?:\(Recommended\)|\(Rec\)|\bRecommended:?\b)/i, '').trim();
      const isSelected = window._grillSelected[idx] && window._grillSelected[idx][q.qId] && window._grillSelected[idx][q.qId].has(cleanVal);
      html += `<button type="button" class="grill-pill ${isRec ? 'recommended' : ''} ${isSelected ? 'active' : ''}" data-val="${esc(cleanVal)}" ${isSubmitted ? 'disabled' : ''} onclick="toggleGrillOption(${idx}, '${esc(q.qId)}', '${esc(cleanVal).replace(/'/g, "\\'")}', true)">`;
      if (isRec) html += `<span class="grill-pill-badge">★ Recommended</span>`;
      html += `<span>${esc(cleanVal)}</span>`;
      html += `</button>`;
    }

    html += `</div>`;
    if (!isSubmitted) {
      html += `<div class="grill-custom-row">`;
      html += `<input type="text" id="grill-custom-${idx}-${q.qId}" data-qid="${esc(q.qId)}" class="grill-custom-input" placeholder="Or write custom answer for ${esc(q.qId)}..." onkeydown="if(event.key==='Enter'){event.preventDefault(); submitGrillAnswers(${idx});}">`;
      html += `</div>`;
    }
    html += `</div>`;
  }

  html += `<div class="grill-footer">`;
  html += `<button type="button" class="btn primary grill-submit-btn" ${isSubmitted ? 'disabled' : ''} onclick="submitGrillAnswers(${idx})">${isSubmitted ? '✓ Answer Submitted' : '✓ Submit Decisions'}</button>`;
  html += `</div>`;
  html += `</div>`;

  return html;
}

function setGenUI(on) {
  generating = on;
  $('btn-send').style.display = on ? 'none' : '';
  $('btn-abort').style.display = on ? '' : 'none';
  $('input').focus();
}

async function send(inputText) {
  const input = $('input');
  const text = (inputText !== undefined ? inputText : (input ? input.value : '')).trim();
  const hasFiles = attachments && attachments.some(a => a.content != null || (a.isImage && a.b64));
  if ((!text && !hasFiles) || generating) return;
  if (!curStatus || !curStatus.pid) {
    const sel = $('profile');
    if (!sel || !sel.value) {
      toast('Please select a model from the top dropdown first', true);
      return;
    }
    const mName = sel.value.split('\\').pop().split('/').pop();
    toast(`⏳ Loading ${mName} into GPU VRAM before sending...`);
    await loadSelectedModel();
    await pollStatus();
    if (!curStatus || !curStatus.pid) {
      toast('Model loading failed or still in progress. Please wait a moment and try again.', true);
      return;
    }
  }
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
    console.warn('Chat prompt build warning:', err);
  }
  assistantMsg.statusText = '';
  renderLast();

  const userMeta = {
    images: sentImages.length ? sentImages : undefined,
    files: sentFiles || undefined,
    displayContent: text || undefined
  };
  ensureSession(text ? text.slice(0, 60) : (sentFiles ? `📎 ${sentFiles}` : 'Files session')).then(() => persistMsg('user', fullPrompt, userMeta));

  const sys = $('sysprompt').value.trim();
  const msgs = [];
  if (sys) msgs.push({ role: 'system', content: sys });
  for (const m of messages.slice(0, -1)) msgs.push({ role: m.role, content: m.content });
  const t0 = performance.now();
  const last = () => messages[messages.length - 1];
  try {
    const res = await fetch('/chat/run', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        messages: msgs,
        web_search: !!chatWebSearch,
        system_prompt: sys || undefined,
        temperature: parseFloat($('temp').value),
        max_tokens: (isNaN(parseInt($('maxtok').value)) || parseInt($('maxtok').value) <= 0) ? -1 : parseInt($('maxtok').value),
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
        const ev = evM[1];
        let d = {};
        try { d = JSON.parse(dtM[1]); } catch (e) {}
        const L = last();
        if (ev === 'delta') {
          L.content += (d.text || '');
        } else if (ev === 'thought_delta') {
          L.reasoning = (L.reasoning || '') + (d.delta || '');
        } else if (ev === 'thought') {
          L.reasoning = (L.reasoning ? L.reasoning + '\n\n' : '') + (d.text || '');
        } else if (ev === 'delta_reset') {
          if (L.content && L.content.trim()) {
            L.reasoning = (L.reasoning ? L.reasoning + '\n\n' : '') + L.content.trim();
          }
          L.content = '';
        } else if (ev === 'tool_call') {
          if (!L.acts) L.acts = [];
          L.acts.push({ type: 'tool_call', ...d });
          if (L.content && L.content.trim()) {
            L.reasoning = (L.reasoning ? L.reasoning + '\n\n' : '') + L.content.trim();
            L.content = '';
          }
        } else if (ev === 'tool_result') {
          if (!L.acts) L.acts = [];
          L.acts.push({ type: 'tool_result', ...d });
        } else if (ev === 'done') {
          if (d && (d.completion_tokens || d.total_tokens)) {
            if (d.completion_tokens) L.ntok = d.completion_tokens;
            if (d.prompt_tokens && messages.length >= 2) {
              const uMsg = messages[messages.length - 2];
              if (uMsg && uMsg.role === 'user') {
                uMsg.ntok = d.prompt_tokens;
              }
            }
          }
        } else if (ev === 'error') {
          throw new Error(d.error || 'Chat execution error');
        }
        renderLast();
      }
    }
  } catch (e) {
    if (e.name !== 'AbortError') {
      last().content += (last().content ? '\n\n' : '') + '⚠️ ' + e.message;
    }
  }
  
  const m = last().content.match(/^\s*<think>([\s\S]*?)<\/think>/);
  if (m) {
    last().reasoning = (last().reasoning || '') + m[1];
    last().content = last().content.slice(m[0].length).trim();
  }
  const dt = (performance.now() - t0) / 1000;
  const fullLen = (last().content || '').length + (last().reasoning || '').length;
  const ntok = last().ntok || Math.max(1, Math.round(fullLen / 3.5));
  last().tps = ntok / dt; last().ntok = ntok; last().secs = dt;
  if (ntok > 1) $('chip-ts').textContent = '⚡ ' + (ntok / dt).toFixed(1) + ' t/s';
  persistMsg('assistant', last().content, {
    tps: last().tps,
    ntok,
    secs: dt,
    reasoning: last().reasoning || undefined,
    acts: (last().acts && last().acts.length) ? last().acts : undefined
  });
  ctrl = null;
  setGenUI(false);
  renderLast();
  updateContextChip();
}
