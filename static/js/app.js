"use strict";
const $ = id => document.getElementById(id);
const LUID_NAMES = {
  '0x04ac733a': 'A770 #1 · display',
  '0x04ab067c': 'A770 #2 · headless',
};
const IGNORED_LUIDS = new Set(['0x000165f7', '0x0001665a']);

let messages = [];       // {role, content, reasoning, tps?, ntok?, secs?}
let generating = false;
let ctrl = null;
let curStatus = null;
let agentMode = false;
let planMode = false;
let chatWebSearch = true;
try {
  const savedWeb = localStorage.getItem('chat_web_search');
  if (savedWeb !== null) chatWebSearch = savedWeb === '1';
} catch (e) {}

function updateWebToggleUI() {
  const btn = $('btn-web-toggle');
  if (!btn) return;
  btn.classList.toggle('active', !!chatWebSearch);
  btn.title = chatWebSearch
    ? 'Web Search is ON (model searches web & fetches URLs) — Click to turn OFF'
    : 'Web Search is OFF (offline local knowledge only) — Click to turn ON';
}

/* ---------------- theme management ---------------- */
const THEME_KEY = 'a770_theme';
const THEMES = ['slate', 'claude', 'tokyonight', 'oled', 'nord', 'classic', 'light'];
const THEME_NAMES = {
  slate: 'Slate & Indigo',
  claude: 'Claude Warm',
  tokyonight: 'Tokyo Night',
  oled: 'OLED Black',
  nord: 'Nord Arctic',
  classic: 'Classic Dark',
  light: 'Pure Light ☀️',
};

function getSavedTheme() {
  return localStorage.getItem(THEME_KEY) || 'slate';
}

function applyTheme(t) {
  if (!THEMES.includes(t)) t = 'slate';
  document.documentElement.setAttribute('data-theme', t);
  document.querySelectorAll('.theme-opt').forEach(b => {
    b.classList.toggle('active', b.getAttribute('data-t') === t);
  });
}

function setTheme(t) {
  localStorage.setItem(THEME_KEY, t);
  applyTheme(t);
  toast(`🎨 Theme: ${THEME_NAMES[t] || t}`);
}

function cycleTheme() {
  const cur = getSavedTheme();
  const nextIdx = (THEMES.indexOf(cur) + 1) % THEMES.length;
  setTheme(THEMES[nextIdx]);
}

window.setTheme = setTheme;
window.cycleTheme = cycleTheme;
applyTheme(getSavedTheme());

if ($('btn-theme')) {
  $('btn-theme').onclick = cycleTheme;
}

/* ---------------- helpers ---------------- */
function esc(s) { return s.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;'); }

function md(s) {
  // split fences BEFORE escaping: hlCode() escapes internally
  const parts = String(s).split(/```/);
  let out = '';
  for (let i = 0; i < parts.length; i++) {
    if (i % 2 === 1) { // fenced code block: ```lang\ncode
      const m = parts[i].match(/^([\w#+.-]*)\n([\s\S]*)$/);
      const lang = hlLangFor(m ? m[1] : '');
      const code = m ? m[2] : parts[i];
      out += '<pre><code>' + (lang ? hlCode(code, lang) : esc(code)) + '</code></pre>';
    } else {
      let t = esc(parts[i]);
      t = t.replace(/`([^`]+)`/g, '<code>$1</code>');
      t = t.replace(/\*\*([^*]+)\*\*/g, '<strong>$1</strong>');
      t = t.replace(/^#{1,3} (.*)$/gm, '<b>$1</b>');
      t = t.replace(/^\s*[-*] (.*)$/gm, '• $1');
      t = t.replace(/\[([^\]]+)\]\((https?:[^)]+)\)/g, '<a href="$2" target="_blank" rel="noopener">$1</a>');
      t = t.replace(/\n/g, '<br>');
      out += t;
    }
  }
  return out;
}

function toast(msg, isErr) {
  const t = $('toast');
  t.textContent = msg;
  t.className = 'show' + (isErr ? ' err' : '');
  clearTimeout(t._h);
  t._h = setTimeout(() => t.className = '', 4000);
}

function fmtUptime(s) {
  if (s == null) return '';
  s = Math.floor(s);
  const h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60), sec = s % 60;
  return h ? `${h}h ${m}m` : (m ? `${m}m ${sec}s` : `${sec}s`);
}

/* ---------------- status / gpu polling ---------------- */
function setPill(mode, s) {
  const p = $('pill'), txt = $('pill-text');
  p.className = 'pill ' + mode;
  if (typeof setLoadBtn === 'function') setLoadBtn(mode);
  if (mode === 'on') txt.textContent = 'Loaded · ' + fmtUptime(s.uptime_s);
  else if (mode === 'loading') txt.textContent = 'Loading model…';
  else if (mode === 'offline') txt.textContent = 'Manager offline';
  else txt.textContent = 'Model unloaded';
}

let pollFailures = 0;
async function pollStatus() {
  try {
    const res = await fetch('/control/status');
    if (!res.ok) throw new Error('status not ok');
    const s = await res.json();
    pollFailures = 0;
    curStatus = s;
    setPill(s.pid ? 'on' : 'off', s);
    const ka = $('ka');
    if (!ka._user) ka.checked = !!s.keepalive;

    const sel = $('profile');
    const selName = (sel && sel.value) ? sel.value.split('\\').pop().split('/').pop() : 'Model';
    const m = s.model ? s.model.split('\\').pop().split('/').pop() : selName;

    if (s.model && s.pid) {
      $('empty-model').textContent =
        `${m}  ·  split ${s.tensor_split || 'auto'}  ·  bench ${Number(s.measured_tg_tokens_per_sec || 0).toFixed(1)} t/s`;
      const ec = document.querySelector('.empty-card');
      if (ec) {
        ec.innerHTML = `<p><b>${m} is ready!</b></p><p class="dim" style="margin-top:6px;">Dual Intel Arc A770 GPUs loaded. Type your message below and press Enter to chat.</p>`;
      }
    } else {
      $('empty-model').textContent = 'Model unloaded';
      const ec = document.querySelector('.empty-card');
      if (ec) {
        ec.innerHTML = `<p><b>${m} is currently unloaded.</b></p><p class="dim" style="margin-top:6px;">Click <b>▶ Load</b> in the top bar, or simply type your message below to auto-load.</p>`;
      }
    }

    // keep profile dropdown in sync if switched elsewhere
    if (s.profile && sel.value && !sel._user) {
      const opt = [...sel.options].find(o => o.value === s.profile || o.textContent.includes(s.profile));
      if (opt && sel.value !== opt.value) sel.value = opt.value;
    }

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
  try {
    const d = await (await fetch('/control/gpu')).json();
    const comp = {};
    (d.compute || []).forEach(c => { comp[c.luid] = (comp[c.luid] || 0) + c.pct; });
    
    // Deduplicate & filter to show strictly the 2 physical Arc A770 GPUs
    let rawAds = (d.adapters || []).filter(a => !IGNORED_LUIDS.has(a.luid) && a.gb >= 1.0);
    const unique = {};
    rawAds.forEach(a => {
      if (!unique[a.luid] || a.gb > unique[a.luid].gb) {
        unique[a.luid] = a;
      }
    });
    const ads = Object.values(unique).sort((a, b) => b.gb - a.gb);

    $('gpu-list').innerHTML = ads.map((a, idx) => {
      const name = LUID_NAMES[a.luid] || (`Arc A770 #${idx + 1} (${a.luid})`);
      const pct = Math.min(100, a.gb / 16 * 100);
      const cp = Math.min(100, Math.round(comp[a.luid] || 0));
      return `<div class="gpu" title="${name}: ${a.gb.toFixed(1)} GB dedicated VRAM used, ${cp}% compute utilization">
        <div class="g-top"><b>${name}</b><span class="mono">${a.gb.toFixed(1)} / 16 GB</span></div>
        <div class="bar"><div class="fill${a.gb > 14.5 ? ' warn' : ''}" style="width:${pct}%"></div></div>
        <div class="g-bot"><span>compute engine</span><span class="mono ${cp > 5 ? 'hot' : ''}">${cp}%</span></div>
      </div>`;
    }).join('') || '<div class="dim" style="font-size:12px">No discrete GPU data</div>';
  } catch (e) { /* manager restarting */ }
}

let curCtxMax = 32768;

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

function updateContextChip() {
  const cChip = $('chip-ctx');
  if (!cChip) return;

  let promptToks = 0;
  let compToks = 0;
  let totalToks = 0;

  if (Array.isArray(messages) && messages.length > 0) {
    for (const m of messages) {
      const tok = getMsgTokens(m);
      if (m.role === 'assistant') {
        compToks += tok;
      } else {
        promptToks += tok;
      }
      totalToks += tok;
    }
  }

  const nCtx = (curStatus && curStatus.context && curStatus.context.n_ctx) ? curStatus.context.n_ctx : curCtxMax;
  const pct = Math.min(100, Number(((totalToks / Math.max(1, nCtx)) * 100).toFixed(1)));
  const fmtK = n => n >= 1000 ? (n / 1000).toFixed(1) + 'k' : n;

  cChip.textContent = `🧠 ${fmtK(totalToks)} / ${fmtK(nCtx)} (${pct}%)`;
  cChip.title = `Session Tokens: ${totalToks.toLocaleString()} / ${nCtx.toLocaleString()} tokens used (${pct}%) · Prompt: ${promptToks.toLocaleString()} · Completion: ${compToks.toLocaleString()} · ${messages.length} messages`;

  if (pct > 85) cChip.style.color = 'var(--red)';
  else if (pct > 70) cChip.style.color = 'var(--amber)';
  else cChip.style.color = 'var(--dim)';
}

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
}

function renderLast() {
  renderAll();
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
    return `<div class="msg user"><div class="bubble">${imgs}${md(displayText || '(attachment)')}${filesTag}</div></div>`;
  }
  let inner = '';
  
  // Render collapsible Antigravity agent action items & tool calls (if present in message)
  if (m.acts && m.acts.length) {
    inner += agentActsHtml(m.acts);
  }

  if (m.reasoning) {
    inner += `<details class="think"><summary>💭 Thinking</summary><div>${esc(m.reasoning).replace(/\n/g, '<br>')}</div></details>`;
  }
  const isLast = idx === messages.length - 1;
  const hasText = !!(m.content && m.content.trim());
  let body = hasText ? md(m.content) : (generating && isLast ? (m.statusText ? `<span class="dim" style="font-size:12px; font-style:italic;">${esc(m.statusText)}</span> ` : '') + '<span class="cursor">▍</span>' : '');
  
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
    const qMatch = block.match(/(?:❓\s*)?(?:#{1,4}\s*)?(?:[*_]{0,2})(Q[0-9]+|Question\s+[0-9]+|\b[0-9]+)[*_]{0,2}\s*[-–:.)]\s*([^\n]+)/i);
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
        max_tokens: parseInt($('maxtok').value) > 0 ? parseInt($('maxtok').value) : 4096,
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

/* ---------------- server controls ---------------- */
async function warmup() {
  try {
    toast('Warming up shaders (first request compile takes ~10s)…');
    await fetch('/v1/chat/completions', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ messages: [{ role: 'user', content: 'hi' }], max_tokens: 1, temperature: 0 }),
    });
  } catch (e) { /* non-fatal */ }
}

async function loadSelectedModel() {
  const sel = $('profile');
  const target = sel.value;
  if (!target) { toast('Please select a model from the dropdown first', true); return; }
  if (curStatus && curStatus.pid) {  // loaded -> unload
    if (ctrl) ctrl.abort();
    try { await fetch('/control/stop', { method: 'POST' }); } catch (e) {}
    pollStatus(); pollGpu();
    toast('Model unloaded — VRAM freed on both A770s');
    return;
  }
  setPill('loading');
  toast('Loading model into Arc A770 VRAM (~10–60s)…');
  try {
    const r = await fetch('/control/switch', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ target: target })
    });
    const j = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(j.error || ('HTTP ' + r.status));
  } catch (e) {
    toast('Load failed: ' + e.message, true);
    pollStatus();
    return;
  }
  await warmup();
  pollStatus();
  loadConfig();
  toast('Model loaded ✅');
}

$('btn-load-header').onclick = loadSelectedModel;

function setLoadBtn(mode) {
  const b = $('btn-load-header');
  b.classList.remove('unload', 'loading');
  if (mode === 'on') {
    b.innerHTML = '<span>■</span>';
    b.classList.add('unload');
    b.title = 'Unload model — terminate llama-server and free VRAM/DRAM';
  } else if (mode === 'loading') {
    b.innerHTML = '<span class="spinner"></span>';
    b.classList.add('loading');
    b.title = 'Loading model into VRAM…';
  } else {
    b.innerHTML = '<span>▶</span>';
    b.title = 'Load the selected model into GPU VRAM';
  }
}

/* clear everything */
$('m-cancel').onclick = () => { $('modal-bg').hidden = true; };
$('modal-bg').onclick = e => { if (e.target.id === 'modal-bg') $('modal-bg').hidden = true; };
$('m-ok').onclick = async () => {
  $('modal-bg').hidden = true;
  if (ctrl) ctrl.abort();
  try { await fetch('/control/stop', { method: 'POST' }); } catch (e) {}
  messages = [];
  renderAll();
  pollStatus(); pollGpu();
  toast('Cleared — model unloaded, VRAM freed, chat reset');
};

/* keepalive toggle (Model Config card) */
$('ka').onchange = async e => {
  const on = e.target.checked;
  e.target._user = true;
  try {
    const r = await fetch('/control/keepalive', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ enabled: on }),
    });
    const j = await r.json();
    toast('Keepalive ' + (j.keepalive ? 'ON — VRAM stays resident at full speed' : 'OFF'));
  } catch (err) {
    toast('Keepalive toggle failed', true);
  }
};

/* chat clear (chat only) */
if ($('btn-chatclear')) $('btn-chatclear').onclick = () => { messages = []; renderAll(); };

/* ---------------- real-time monitor ---------------- */
let monOpen = false;
let monTimer = null;

function monFmtTps(r) {
  if (r.stream && r.tps) return r.tps.toFixed(1) + ' t/s';
  if (r.tps) return r.tps.toFixed(1) + ' t/s';
  return '—';
}

// Human-readable executed-at time for a finished request
function monFmtTime(r) {
  if (!r.ended_at) return '';
  const d = new Date(r.ended_at * 1000);
  const now = new Date();
  const sameDay = d.toDateString() === now.toDateString();
  const hm = d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
  return sameDay ? hm : d.toLocaleDateString([], { month: 'short', day: 'numeric' }) + ' ' + hm;
}

// Render recent list only when it changes, so entries don't flicker/rerender on every poll
let monRecentKey = '';
function renderMonitorRecent(list) {
  const key = list.map(r => `${r.id}:${r.model || ''}:${r.status}:${r.completion_tokens}:${r.duration_s}`).join('|');
  if (key === monRecentKey) return;
  monRecentKey = key;
  const rl = $('mon-recent-list');
  if (!list || !list.length) {
    rl.innerHTML = '<div class="mon-empty">No requests yet</div>';
    return;
  }
  rl.innerHTML = list.map(r => {
    let mTag = '';
    if (r.model) {
      mTag = `<span class="mon-model-tag" title="${esc(r.model)}">${esc(r.model)}</span>`;
    } else if (r.endpoint && r.endpoint.includes('executor')) {
      mTag = `<span class="mon-model-tag" title="Qwen2.5-VL-3B">Qwen2.5-VL-3B</span>`;
    }
    return `
    <div class="mon-recent" title="Executed at ${esc(monFmtTime(r))}">
      <div class="mon-recent-left">
        <span class="ep">${esc(r.endpoint)}</span>
        ${mTag}
      </div>
      <span class="num">${r.prompt_tokens || '–'}→${r.completion_tokens || '–'} tok · ${monFmtTps(r)} · ${r.duration_s ? r.duration_s.toFixed(1) + 's' : '—'} · 🕒 ${esc(monFmtTime(r))}${r.status >= 400 ? ' ⚠ ' + r.status : ''}</span>
    </div>`;
  }).join('');
}

async function pollMonitor() {
  try {
    const d = await (await fetch('/control/monitor')).json();
    // active generations
    const al = $('mon-active-list');
    if (!d.active || !d.active.length) {
      al.innerHTML = '<div class="mon-empty">No active requests</div>';
    } else {
      al.innerHTML = d.active.map(r => {
        let mTag = '';
        if (r.model) {
          mTag = `<span class="mon-model-tag" title="${esc(r.model)}">${esc(r.model)}</span>`;
        } else if (r.endpoint && r.endpoint.includes('executor')) {
          mTag = `<span class="mon-model-tag" title="Qwen2.5-VL-3B">Qwen2.5-VL-3B</span>`;
        }
        return `
        <div class="mon-active">
          <div class="row1">
            <span class="ep">${esc(r.endpoint)} ${mTag}</span>
            <span class="tps">${(r.gen_tps || 0).toFixed(1)} t/s</span>
          </div>
          <div class="row1"><span class="dim">${r.elapsed_s}s elapsed · gen ${r.gen_tokens} tok</span><span class="dim">${r.prompt_tokens ? 'prompt ' + r.prompt_tokens + ' msgs' : ''}</span></div>
        </div>`;
      }).join('');
    }
    // recent requests (rendered only on change)
    renderMonitorRecent(d.recent || []);
  } catch (e) { /* server restarting */ }
}

function setMonitor(open) {
  monOpen = open;
  $('monitor-drawer').classList.toggle('open', open);
  if (open) {
    const sd = $('settings-drawer');
    if (sd) sd.classList.remove('open');
    monRecentKey = '';   // force re-render in case entries changed while closed
    pollMonitor();
    monTimer = setInterval(pollMonitor, 1500);
  } else if (monTimer) {
    clearInterval(monTimer);
    monTimer = null;
  }
}

$('btn-monitor').onclick = () => setMonitor(!monOpen);
$('mon-close').onclick = () => setMonitor(false);

function setSettings(open) {
  const d = $('settings-drawer');
  if (!d) return;
  const isOpen = (open === undefined) ? !d.classList.contains('open') : !!open;
  d.classList.toggle('open', isOpen);
  if (isOpen && monOpen) setMonitor(false);
  if (isOpen) {
    loadConfig();   // refresh config panel from server on open
    loadCapabilities();
  }
}

/* ---------------- capabilities panel ---------------- */
async function loadCapabilities() {
  const box = $('caps-content');
  if (!box) return;
  try {
    const d = await (await fetch('/control/capabilities')).json();
    if (d.error) { box.innerHTML = '<div class="mon-empty">' + esc(d.error) + '</div>'; return; }
    let h = `<div class="rep-cell" style="display:flex; align-items:center; justify-content:space-between;"><span><b>${d.total_tools}</b> tools registered</span></div>`;
    // Web
    h += capSection('web', '🌐 Web Browsing', d.web.enabled,
      (d.web.tools || []).map(t => `<div class="cap-item">🔗 <b>${esc(t.name)}</b> — ${esc(t.description || '')}</div>`).join(''),
      'web_fetch pulls page text; web_search queries DuckDuckGo (keyless)');

    // Skills
    h += capSection('skills', '🎯 Skills', d.skills.enabled,
      (d.skills.items || []).map(s => `<div class="cap-item">📘 <b>${esc(s.name)}</b> — ${esc(s.description || '')}</div>`).join('') || '<div class="cap-item dim">none in skills/ yet</div>',
      'Reusable instruction packs loaded from skills/*/SKILL.md');

    // MCP
    const mcpInner = (d.mcp.servers || []).length
      ? d.mcp.servers.map(s => `
          <div class="cap-item">
            <span class="cap-dot ${s.status === 'ready' ? 'on' : (s.status === 'error' ? 'err' : '')}" title="${esc(s.status)}"></span>
            <b>${esc(s.name)}</b> <span class="dim">(${esc(s.transport)}) · ${s.tools.length} tool(s)</span>
            ${s.error ? `<div class="dim" style="font-size:10px; color:var(--red);">${esc(s.error)}</div>` : ''}
            ${(s.tools || []).map(t => `<div class="dim" style="font-size:10px; padding-left:14px;">↳ ${esc(t.name)} — ${esc(t.description || '')}</div>`).join('')}
          </div>`).join('')
      : '<div class="cap-item dim">no servers configured (config.json → capabilities.mcp_servers)</div>';
    h += capSection('mcp', '🔌 MCP Servers', d.mcp.enabled, mcpInner,
      'External tool servers via Model Context Protocol (stdio / http)');

    // Plugins
    h += capSection('plugins', '🧩 Plugins', d.plugins.enabled,
      (d.plugins.items || []).map(p => `
        <div class="cap-item">⚡ <b>${esc(p.name)}</b> <span class="dim">· ${p.tools.length} tool(s), ${p.prompt_fragments} prompt frag(s)</span>
          ${(p.tools || []).map(t => `<div class="dim" style="font-size:10px; padding-left:14px;">↳ ${esc(t.split('__').pop())}</div>`).join('')}
        </div>`).join('') || '<div class="cap-item dim">none in plugins/ yet</div>',
      'Python modules loaded from plugins/*/plugin.py');

    // Shell
    const sh = d.shell || {};
    const shellInner = `
      <div class="cap-item">
        <label style="display:flex; align-items:center; gap:6px; cursor:pointer;">
          <input type="checkbox" id="shell-ask" ${sh.ask_first ? 'checked' : ''} style="accent-color:var(--green);">
          <b>Ask before running</b> <span class="dim">(permission modal; matched patterns skip asking)</span>
        </label>
      </div>
      <div class="cap-item">
        <b>Allowed patterns</b> <span class="dim">(wildcards ok, <code>*</code> = allow all)</span>
        <div id="shell-pats" style="display:flex; flex-direction:column; gap:3px; margin-top:4px;">
          ${(sh.allow_patterns || []).map((p, i) => `
            <div style="display:flex; gap:5px; align-items:center;">
              <input type="text" class="shell-pat" data-i="${i}" value="${esc(p)}" style="flex:1; background:var(--bg-input); color:var(--text); border:1px solid var(--border); border-radius:5px; padding:3px 7px; font-size:10.5px; font-family:monospace;">
              <button class="btn ghost shell-pat-del" data-i="${i}" style="width:auto; margin:0; padding:2px 7px; font-size:10px; color:var(--red);">✕</button>
            </div>`).join('')}
        </div>
        <div style="display:flex; gap:5px; margin-top:5px;">
          <input type="text" id="shell-pat-new" placeholder="e.g. git * or npx skills * or *" style="flex:1; background:var(--bg-input); color:var(--text); border:1px solid var(--border); border-radius:5px; padding:3px 7px; font-size:10.5px; font-family:monospace;">
          <button class="btn ghost" id="shell-pat-add" style="width:auto; margin:0; padding:3px 10px; font-size:10.5px;">+ Add</button>
        </div>
        <button class="btn accent" id="shell-pats-save" style="width:auto; margin:6px 0 0; padding:4px 12px; font-size:10.5px;">💾 Save patterns</button>
      </div>`;
    h += capSection('shell', '⌨️ Shell Execution', sh.enabled, shellInner,
      'run_shell tool — agent runs commands like "npx skills add …" in the workspace');

    box.innerHTML = h;
    box.querySelectorAll('.cap-toggle').forEach(t => {
      t.onclick = async () => {
        const section = t.dataset.section;
        const enable = !t.classList.contains('on');
        try {
          const r = await fetch('/control/capabilities', {
            method: 'POST', headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ section, enabled: enable }),
          });
          const j = await r.json();
          if (!r.ok) throw new Error(j.error || r.status);
          toast(`Capability '${section}' ${enable ? 'enabled' : 'disabled'} ✓`);
          loadCapabilities();
        } catch (e) { toast('Toggle failed: ' + e.message, true); }
      };
    });

    // shell section controls
    const askEl = box.querySelector('#shell-ask');
    if (askEl) askEl.onchange = async () => {
      try {
        const r = await fetch('/control/shell_settings', {
          method: 'POST', headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ ask_first: askEl.checked }),
        });
        if (!r.ok) throw new Error('save failed');
        toast(askEl.checked ? 'Shell: will ask before unmatched commands' : 'Shell: no permission prompts');
      } catch (e) { toast('Save failed: ' + e.message, true); }
    };
    const addBtn = box.querySelector('#shell-pat-add');
    const newPat = box.querySelector('#shell-pat-new');
    if (addBtn && newPat) addBtn.onclick = () => {
      if (!newPat.value.trim()) return;
      const wrap = box.querySelector('#shell-pats');
      const i = wrap.querySelectorAll('.shell-pat').length;
      const row = document.createElement('div');
      row.style.cssText = 'display:flex; gap:5px; align-items:center;';
      row.innerHTML = `<input type="text" class="shell-pat" value="${esc(newPat.value.trim())}" style="flex:1; background:var(--bg-input); color:var(--text); border:1px solid var(--border); border-radius:5px; padding:3px 7px; font-size:10.5px; font-family:monospace;">
        <button class="btn ghost shell-pat-del" style="width:auto; margin:0; padding:2px 7px; font-size:10px; color:var(--red);">✕</button>`;
      wrap.appendChild(row);
      newPat.value = '';
      wirePatDel(row.querySelector('.shell-pat-del'));
    };
    const saveBtn = box.querySelector('#shell-pats-save');
    if (saveBtn) saveBtn.onclick = async () => {
      const pats = [...box.querySelectorAll('.shell-pat')].map(i => i.value.trim()).filter(Boolean);
      try {
        const r = await fetch('/control/shell_settings', {
          method: 'POST', headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ allow_patterns: pats }),
        });
        if (!r.ok) throw new Error('save failed');
        toast(`Saved ${pats.length} shell pattern(s) ✓`);
      } catch (e) { toast('Save failed: ' + e.message, true); }
    };
    function wirePatDel(btn) {
      btn.onclick = () => btn.closest('div[style]').remove();
    }
    box.querySelectorAll('.shell-pat-del').forEach(wirePatDel);
  } catch (e) {
    box.innerHTML = '<div class="mon-empty">Failed: ' + esc(e.message) + '</div>';
  }
}

function capSection(id, title, enabled, innerHtml, note) {
  return `<div class="cap-group">
    <div class="cap-head">
      <span>${title}</span>
      <button class="cap-toggle ${enabled ? 'on' : ''}" data-section="${id}" title="Enable/disable this capability">${enabled ? 'ON' : 'OFF'}</button>
    </div>
    <div class="cap-body" style="${enabled ? '' : 'opacity:0.45;'}">${innerHtml || ''}${note ? `<div class="dim" style="font-size:9.5px; margin-top:4px;">${esc(note)}</div>` : ''}</div>
  </div>`;
}

$('btn-theme').onclick = () => cycleTheme();
$('btn-settings').onclick = () => setSettings();
$('settings-close').onclick = () => setSettings(false);

/* ---------------- usage report ---------------- */
async function loadReport(days) {
  const box = $('rep-content');
  box.innerHTML = '<div class="mon-empty">Loading…</div>';
  try {
    const d = await (await fetch('/control/report?days=' + days)).json();
    const fmt = n => n == null ? '0' : n.toLocaleString();
    let html = `
      <div class="rep-grid">
        <div class="rep-cell"><b>${fmt(d.total_tokens)}</b><span>Total Tokens</span></div>
        <div class="rep-cell"><b>${fmt(d.prompt_tokens)}</b><span>Prompt Tokens</span></div>
        <div class="rep-cell"><b>${fmt(d.completion_tokens)}</b><span>Generated</span></div>
        <div class="rep-cell" style="border-color: rgba(56,189,248,0.35); background: rgba(56,189,248,0.06);" title="Total tokens read directly from prompt and output cache">
          <b style="color: #38bdf8;">${fmt(d.total_cached_tokens)}</b>
          <span>Total Read From Cache (${d.cache_hit_rate || 0}% hit)</span>
        </div>
        <div class="rep-cell" title="Input tokens read from prompt / KV cache without re-evaluation">
          <b class="rep-cache-val">${fmt(d.prompt_cached_tokens)}</b>
          <span>Input Cache Read</span>
        </div>
        <div class="rep-cell" title="Output tokens read from speculative draft / accepted cache">
          <b class="rep-gen-val">${fmt(d.completion_cached_tokens)}</b>
          <span>Output Cache Read</span>
        </div>
        <div class="rep-cell" title="Total calls and tokens executed by orchestrator models (executor, vision, embedder, router)">
          <b style="color: #f59e0b;">⚡ ${fmt(d.orchestrator_requests)} <small style="font-size:11px; font-weight:normal; color:var(--dim);">(${fmt(d.orchestrator_total_tokens)} tok)</small></b>
          <span>Orchestrator Models</span>
        </div>
        <div class="rep-cell"><b>${fmt(d.requests)}</b><span>Total Requests</span></div>
        <div class="rep-cell"><b>${d.avg_tps} t/s</b><span>Avg Speed (${d.avg_duration_s}s)</span></div>
      </div>`;

    if (d.by_model && d.by_model.length) {
      html += `
        <div class="rep-sub">
          <span>By Model</span>
          <span style="font-size:9.5px; font-weight:normal; text-transform:none; color:var(--dim);">Includes main &amp; orchestrator lanes</span>
        </div>
        <table class="rep-table">
          <tr>
            <th>Model</th>
            <th>Role</th>
            <th>Req</th>
            <th>Prompt</th>
            <th title="Input tokens read from cache">In Cache</th>
            <th>Gen</th>
            <th title="Output tokens read from cache">Out Cache</th>
            <th>Total</th>
          </tr>`;
      d.by_model.forEach(m => {
        const isOrch = !!m.is_orchestrator;
        const roleBadge = isOrch
          ? `<span class="badge-orch" title="Orchestrator sub-model">⚡ Orch</span>`
          : `<span class="badge-main" title="Main model">Main</span>`;
        const rawName = (m.model || 'unknown').split('\\').pop().split('/').pop();
        html += `
          <tr>
            <td title="${esc(m.model || '')}">${esc(rawName)}</td>
            <td>${roleBadge}</td>
            <td>${fmt(m.requests)}</td>
            <td>${fmt(m.prompt_tokens)}</td>
            <td class="rep-cache-val">${fmt(m.prompt_cached_tokens)}</td>
            <td>${fmt(m.completion_tokens)}</td>
            <td class="rep-gen-val">${fmt(m.completion_cached_tokens)}</td>
            <td><b>${fmt(m.total_tokens)}</b></td>
          </tr>`;
      });
      html += `</table>`;
    }

    if (d.by_day && d.by_day.length) {
      html += `
        <div class="rep-sub">
          <span>By Day</span>
          <span style="font-size:9.5px; font-weight:normal; text-transform:none; color:var(--dim);">Daily usage &amp; cache hits</span>
        </div>
        <table class="rep-table">
          <tr>
            <th>Day</th>
            <th>Req</th>
            <th title="Orchestrator requests">Orch</th>
            <th>Prompt</th>
            <th title="Input tokens read from cache">In Cache</th>
            <th>Gen</th>
            <th title="Output tokens read from cache">Out Cache</th>
            <th>Total</th>
          </tr>`;
      d.by_day.forEach(x => {
        html += `
          <tr>
            <td>${x.day}</td>
            <td>${fmt(x.requests)}</td>
            <td style="color:#f59e0b;">${fmt(x.orchestrator_requests)}</td>
            <td>${fmt(x.prompt_tokens)}</td>
            <td class="rep-cache-val">${fmt(x.prompt_cached_tokens)}</td>
            <td>${fmt(x.completion_tokens)}</td>
            <td class="rep-gen-val">${fmt(x.completion_cached_tokens)}</td>
            <td><b>${fmt(x.total_tokens)}</b></td>
          </tr>`;
      });
      html += `</table>`;
    }
    box.innerHTML = html;
  } catch (e) {
    box.innerHTML = '<div class="mon-empty">Report failed: ' + esc(e.message) + '</div>';
  }
}

$('btn-report').onclick = () => {
  $('report-modal').hidden = false;
  loadReport(30);
};
$('rep-close').onclick = () => { $('report-modal').hidden = true; };
$('report-modal').onclick = e => { if (e.target.id === 'report-modal') $('report-modal').hidden = true; };

/* ---------------- agent docs modal ---------------- */
$('btn-docs').onclick = () => {
  // fill in the currently loaded model's exact ID (what agents must use)
  const mid = $('doc-model-id');
  if (curStatus && curStatus.pid && curStatus.model) {
    mid.textContent = curStatus.model;
  } else if (curStatus && curStatus.model) {
    mid.textContent = curStatus.model;
  } else {
    mid.textContent = '(load a model first — its path becomes the Model ID)';
  }
  // also update the example snippet with the real id
  const ex = $('doc-example');
  const selected = $('profile') && $('profile').value ? $('profile').value.split('\\').pop().split('/').pop() : null;
  if (curStatus && curStatus.model) {
    ex.textContent = ex.textContent.replace(/model="[^"]*"/, `model="${curStatus.model}"`);
  } else if (selected) {
    ex.textContent = ex.textContent.replace(/model="[^"]*"/, `model="E:\\AI\\Models\\${selected}"`);
  }
  $('docs-modal').hidden = false;
};
$('docs-close').onclick = () => { $('docs-modal').hidden = true; };
$('docs-modal').onclick = e => { if (e.target.id === 'docs-modal') $('docs-modal').hidden = true; };
document.querySelectorAll('.doc-copy-btn').forEach(b => {
  b.onclick = () => {
    const txt = $(b.dataset.copy).textContent;
    if (!txt || txt.startsWith('(')) { toast('Nothing to copy yet', true); return; }
    navigator.clipboard.writeText(txt).then(
      () => { b.textContent = '✓ Copied'; setTimeout(() => b.textContent = '📋 Copy', 1200); toast('Copied to clipboard'); },
      () => toast('Copy failed', true)
    );
  };
});
document.querySelectorAll('.rep-day').forEach(b => {
  b.onclick = () => {
    document.querySelectorAll('.rep-day').forEach(x => x.className = 'btn ghost rep-day');
    b.className = 'btn blue rep-day';
    loadReport(b.dataset.days);
  };
});

    /* dynamic model & profile list */
    const CFG_DEFAULTS = {
      ctx: 32768, ngl: 999, threads: 0, tb: 0, batch: 2048, ubatch: 512,
      np: 1, kai: 25, ts: '9,11', sm: 'layer', fa: 'auto', ct: 'f16',
    };
    async function loadProfiles() {
      try {
        const d = await (await fetch('/control/profiles')).json();
        const sel = $('profile');
        sel.innerHTML = '';

        // Group GGUF models by family (text before first '-')
        const groups = {};
        (d.models || []).forEach(m => {
          const fam = (m.family || 'Other').trim() || 'Other';
          (groups[fam] = groups[fam] || []).push(m);
        });
        const famNames = Object.keys(groups).sort();

        famNames.forEach(fam => {
          const g = document.createElement('optgroup');
          g.label = '❒ ' + fam;
          groups[fam].forEach(m => {
            const o = document.createElement('option');
            o.value = m.path;
            const name = (m.name || '').trim();
            const shortName = name.length > 20 ? (name.slice(0, 20) + '...') : name;
            const size = m.size_gb != null ? ` · ${m.size_gb}GB` : '';
            const mtp = m.mtp_available ? ' ⚡MTP' : '';
            o.textContent = `${shortName}${size}${mtp}`;
            o.title = `${m.name}${size}${mtp}`;
            o.dataset.mtp = m.mtp_available ? '1' : '';
            o.dataset.mtpPath = m.mtp_draft_path || '';
            g.appendChild(o);
          });
          sel.appendChild(g);
        });

        if (!sel.options.length) {
          const o = document.createElement('option');
          o.value = '';
          o.textContent = 'No models found';
          sel.appendChild(o);
        }

        // restore last selection across page refreshes
        try {
          const saved = localStorage.getItem('app_model');
          if (saved && [...sel.options].some(o => o.value === saved)) {
            sel.value = saved;
          }
        } catch (e) {}
        loadConfig();   // dropdown is ready — fetch its saved config
      } catch (e) {}
    }

$('profile').onchange = e => {
  e.target._user = true;
  try { localStorage.setItem('app_model', e.target.value); } catch (err) {}
  loadConfig();   // show the newly selected model's saved config
};

/* ---------------- launch config ---------------- */
function restoreDefault(id, val) {
  // if user deleted a value, snap the field back to its default
  if ($(id).value.trim() === '') { $(id).value = val; return true; }
  return false;
}

async function loadConfig() {
  try {
    // The drawer edits the model selected in the dropdown. Pass it always
    // (?model=) so values shown/saved target it whether loaded or not.
    const sel = $('profile');
    const target = (sel && sel.value) ? sel.value : (curStatus && curStatus.model);
    let url = '/control/config';
    if (target) url += '?model=' + encodeURIComponent(target);
    const c = await (await fetch(url)).json();
    if (c.error) return;
    fillConfigForm(c);
  } catch (e) {}
}

function fillConfigForm(c) {
    $('cfg-ctx').value = c.context_size ?? CFG_DEFAULTS.ctx;
    // Keep the context chip n_ctx in sync as soon as we know the model's window size
    if (c.context_size && c.context_size > 0) {
      curCtxMax = c.context_size;
      updateContextChip();
    }
    $('cfg-ngl').value = c.n_gpu_layers ?? CFG_DEFAULTS.ngl;
    $('cfg-threads').value = c.threads ?? CFG_DEFAULTS.threads;
    $('cfg-tb').value = c.threads_batch ?? CFG_DEFAULTS.tb;
    $('cfg-batch').value = c.batch_size ?? CFG_DEFAULTS.batch;
    $('cfg-ubatch').value = c.ubatch_size ?? CFG_DEFAULTS.ubatch;
    $('cfg-np').value = c.n_slots ?? CFG_DEFAULTS.np;
    $('cfg-kai').value = c.keepalive_interval_s ?? CFG_DEFAULTS.kai;
    $('cfg-ts').value = c.tensor_split ?? CFG_DEFAULTS.ts;
    updateGpuDependentFields();
    $('cfg-sm').value = c.split_mode || CFG_DEFAULTS.sm;
    $('cfg-fa').value = c.flash_attn || CFG_DEFAULTS.fa;
    $('cfg-ct').value = c.kv_cache_type || CFG_DEFAULTS.ct;
    // MTP section
    const mtpAvail = !!c.mtp_available;
    const mtpRow = $('mtp-row');
    if (mtpRow) {
      mtpRow.style.display = mtpAvail ? '' : 'none';
      $('mtp-file').textContent = mtpAvail && c.mtp_draft_path ? c.mtp_draft_path.split('\\').pop().split('/').pop() : '';
    }
    if ($('cfg-mtp')) $('cfg-mtp').checked = !!c.mtp_enabled;
    if ($('mtp-nmax')) $('mtp-nmax').value = c.mtp_draft_n_max ?? 3;
}

/* Tensor split drives GPU selection: 0,1 = GPU 2 only, 1,0 = GPU 1 only, 9,11 = dual */
function parseTensorSplit(v) {
  return String(v || '').split(',').map(s => s.trim()).filter(s => s !== '');
}

function activeGpuCount() {
  const shares = parseTensorSplit($('cfg-ts') ? $('cfg-ts').value : '');
  if (shares.length > 1) return shares.filter(s => parseInt(s) > 0).length;
  return 2;   // default dual setup
}

function updateGpuDependentFields() {
  const multi = activeGpuCount() > 1;
  const ts = $('cfg-ts'), sm = $('cfg-sm');
  if (sm) { sm.disabled = !multi; sm.style.opacity = multi ? '' : '0.45'; }
}

if ($('cfg-ts')) $('cfg-ts').addEventListener('input', updateGpuDependentFields);

$('btn-apply').onclick = async () => {
  // restore defaults for any field the user cleared
  restoreDefault('cfg-ctx', CFG_DEFAULTS.ctx);
  restoreDefault('cfg-ts', CFG_DEFAULTS.ts);
  const updates = {
    context_size: parseInt($('cfg-ctx').value) || CFG_DEFAULTS.ctx,
    n_gpu_layers: parseInt($('cfg-ngl').value) ?? CFG_DEFAULTS.ngl,
    threads: parseInt($('cfg-threads').value) || 0,
    threads_batch: parseInt($('cfg-tb').value) || 0,
    batch_size: parseInt($('cfg-batch').value) || 0,
    ubatch_size: parseInt($('cfg-ubatch').value) || 0,
    n_slots: parseInt($('cfg-np').value) || 0,
    keepalive_interval_s: parseInt($('cfg-kai').value) || CFG_DEFAULTS.kai,
    tensor_split: $('cfg-ts').value.trim() || CFG_DEFAULTS.ts,
    split_mode: $('cfg-sm').value,
    flash_attn: $('cfg-fa').value,
    kv_cache_type: $('cfg-ct').value,
    mtp_enabled: $('cfg-mtp') && $('cfg-mtp').checked,
    mtp_draft_n_max: parseInt($('mtp-nmax') && $('mtp-nmax').value) || 3,
  };
  const wasLoaded = !!(curStatus && curStatus.pid);
  toast('Saving config…');
  try {
    // persist only - no restart; user unloads/loads to apply.
    // Always target the dropdown-selected model.
    const sel = $('profile');
    const target = (sel && sel.value) ? sel.value : (curStatus && curStatus.model);
    let url = '/control/config';
    if (target) url += '?model=' + encodeURIComponent(target);
    const r = await fetch(url, {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ updates, persist: true, restart: false }),
    });
    const j = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(j.error || ('HTTP ' + r.status));
    // reflect server-side clamping (e.g. context 200K -> 262144 max) in the form
    if (j.config) fillConfigForm(j.config);
  } catch (e) {
    toast('Config save failed: ' + e.message, true);
    return;
  }
  if (wasLoaded) {
    toast('Config saved ✓ Unload the model (■), then Load (▶) again to apply');
  } else {
    toast('Config saved ✓ It will be used on next load');
  }
};

/* attachments: text files get appended to the prompt on send */
const attachments = [];

function fmtBytes(n) {
  if (n < 1024) return n + ' B';
  if (n < 1048576) return (n / 1024).toFixed(1) + ' KB';
  return (n / 1048576).toFixed(1) + ' MB';
}

function refreshAttachUI() {
  const b = $('btn-attach');
  const info = $('attach-info');
  const previewBar = $('attach-preview-bar');
  b.classList.toggle('has-file', attachments.length > 0);
  b.querySelectorAll('.badge').forEach(x => x.remove());

  if (previewBar) {
    if (!attachments.length) {
      previewBar.innerHTML = '';
    } else {
      previewBar.innerHTML = attachments.map((a, i) => {
        const thumb = a.isImage && a.dataUrl ?
          `<img src="${a.dataUrl}" alt="${esc(a.name)}" class="chat-img-thumb" title="Click to enlarge">` :
          `<span style="font-size:20px; line-height:1;">📄</span>`;
        return `<div class="attach-card">
          ${thumb}
          <div class="attach-card-info">
            <span class="attach-card-name" title="${esc(a.name)}">${esc(a.name)}</span>
            <span class="attach-card-size">${fmtBytes(a.size || 0)}</span>
          </div>
          <span class="attach-card-remove" onclick="removeAttachment(${i})" title="Remove attachment">✕</span>
        </div>`;
      }).join('');
    }
  }

  if (attachments.length) {
    const badge = document.createElement('span');
    badge.className = 'badge';
    badge.textContent = attachments.length;
    b.appendChild(badge);
    const names = attachments.map(a => (a.isImage ? '🖼 ' : '') + a.name).join(', ');
    info.textContent = `${attachments.length} attachment${attachments.length > 1 ? 's' : ''} (${names.slice(0, 45)}…) · Click to clear all`;
  } else {
    info.textContent = 'Enter to send · Shift+Enter for newline';
  }
}

function removeAttachment(idx) {
  if (idx >= 0 && idx < attachments.length) {
    const removed = attachments.splice(idx, 1)[0];
    refreshAttachUI();
    toast(`Removed ${removed.name || 'attachment'}`);
  }
}

function preloadAttachmentVision(att) {
  if (!att || !att.isImage || !att.b64 || att.visionPromise) return;
  att.visionPromise = (async () => {
    try {
      const r = await fetch('/agent/vision', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          image_b64: att.b64,
          mime: att.mime || 'image/png',
          question: 'Describe this image in detail for a coding agent. Include any visible text, errors, or UI elements.'
        })
      });
      const j = await r.json();
      if (!r.ok) throw new Error(j.error || 'HTTP ' + r.status);
      att.visionDescription = j.description || '';
      return att.visionDescription;
    } catch (err) {
      att.visionDescription = `(vision unavailable: ${err.message})`;
      return att.visionDescription;
    }
  })();
}

$('btn-attach').onclick = () => $('file-input').click();
const IMAGE_RE = /\.(png|jpe?g|webp|gif|bmp|svg)$/i;

function addAttachmentFile(f, namePrefix = 'screenshot') {
  const isImage = IMAGE_RE.test(f.name || '') || (f.type && f.type.startsWith('image/'));
  const maxBytes = isImage ? (15 * 1024 * 1024) : (512 * 1024);
  if (f.size > maxBytes) {
    toast(`"${f.name || 'File'}" is ${fmtBytes(f.size)} — max size is ${fmtBytes(maxBytes)}`, true);
    return;
  }
  const timeStr = new Date().toTimeString().split(' ')[0].replace(/:/g, '');
  const fname = f.name && !f.name.startsWith('image.') ? f.name : `${namePrefix}_${timeStr}.png`;
  const att = { name: fname, size: f.size, isImage, truncated: false };
  attachments.push(att);
  const idx = attachments.length - 1;
  const reader = new FileReader();
  reader.onload = ev => {
    const url = String(ev.target.result || '');
    if (isImage) {
      attachments[idx].dataUrl = url;
      attachments[idx].b64 = (url.split(',')[1] || '');
      attachments[idx].mime = f.type || 'image/png';
      preloadAttachmentVision(attachments[idx]);
    } else {
      attachments[idx].content = url;
    }
    refreshAttachUI();
  };
  reader[isImage ? 'readAsDataURL' : 'readAsText'](f);
  refreshAttachUI();
}

$('file-input').onchange = e => {
  [...e.target.files].forEach(f => addAttachmentFile(f, 'upload'));
  e.target.value = '';
};

// Clipboard paste listener: paste screenshots directly anywhere on screen
window.addEventListener('paste', e => {
  const cd = e.clipboardData || window.clipboardData;
  if (!cd) return;
  let hasImage = false;
  const items = cd.items;
  if (items && items.length) {
    for (let i = 0; i < items.length; i++) {
      const it = items[i];
      if (it.type && it.type.startsWith('image/')) {
        const file = it.getAsFile();
        if (file) {
          hasImage = true;
          addAttachmentFile(file, 'clipboard_screenshot');
        }
      }
    }
  } else if (cd.files && cd.files.length) {
    for (let i = 0; i < cd.files.length; i++) {
      const file = cd.files[i];
      if (file.type && file.type.startsWith('image/')) {
        hasImage = true;
        addAttachmentFile(file, 'clipboard_screenshot');
      }
    }
  }
  if (hasImage) {
    e.preventDefault();
  }
});

// Drag & drop files or images anywhere on the window
window.addEventListener('dragover', e => {
  e.preventDefault();
});
window.addEventListener('drop', e => {
  e.preventDefault();
  if (e.dataTransfer && e.dataTransfer.files && e.dataTransfer.files.length) {
    [...e.dataTransfer.files].forEach(f => addAttachmentFile(f, 'dropped_file'));
  }
});

// Clicking attach-info clears attachments
$('attach-info').style.cursor = 'pointer';
$('attach-info').title = 'Click to clear attachments';
$('attach-info').onclick = () => {
  if (attachments.length) {
    clearAttachments();
    toast('Attachments cleared');
  }
};

async function buildPromptText(text, attList = null, signal = null) {
  const list = (attList && attList.length) ? attList : attachments;
  const textParts = list.filter(a => a.content != null).map(a =>
    `--- FILE: ${a.name} ---\n${a.content}\n--- END ${a.name} ---`);
  const imgParts = [];
  for (const a of list.filter(x => x.isImage && (x.b64 || x.visionPromise || x.visionDescription))) {
    try {
      let desc = a.visionDescription;
      if (!desc && a.visionPromise) {
        desc = await a.visionPromise;
      } else if (!desc && a.b64) {
        const r = await fetch('/agent/vision', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({
            image_b64: a.b64,
            mime: a.mime || 'image/png',
            question: 'Describe this image in detail for a coding agent. Include any visible text, errors, or UI elements.'
          }),
          signal: signal || undefined
        });
        const j = await r.json();
        if (!r.ok) throw new Error(j.error || 'HTTP ' + r.status);
        desc = j.description || '';
      }
      imgParts.push(`--- IMAGE: ${a.name} ---\n${desc}\n--- END ${a.name} ---`);
    } catch (err) {
      if (err.name === 'AbortError') throw err;
      imgParts.push(`--- IMAGE: ${a.name} ---\n(vision unavailable: ${err.message})\n--- END ${a.name} ---`);
    }
  }
  const parts = [...textParts, ...imgParts];
  const base = (text ? text + '\n\n' : '') + parts.join('\n\n');
  if (!agentMode) return base;
  return await expandAtTags(base);
}

function clearAttachments() {
  attachments.length = 0;
  refreshAttachUI();
}

/* ---------------- agent workspace panel (right side) ---------------- */
let wsPanelOpen = false;
const wsExpandedDirs = new Set();   // remember expanded folders while panel is used

/* per-extension colored file glyphs (Claude Code style) */
function wsFileIcon(name) {
  const e = name.split('.').pop().toLowerCase();
  const map = {
    py: '🐍', js: '⚡', mjs: '⚡', jsx: '⚡', ts: '⚡', tsx: '⚡',
    html: '🌐', htm: '🌐', css: '🎨', scss: '🎨', json: '🧩',
    md: '📝', txt: '📝', sh: '⚙️', ps1: '⚙️', bat: '⚙️',
    php: '🐘', sql: '🗄️', yml: '🔧', yaml: '🔧', toml: '🔧',
    png: '🖼️', jpg: '🖼️', jpeg: '🖼️', webp: '🖼️', svg: '🖼️',
    gguf: '🧠', db: '🗄️',
  };
  return map[e] || '📄';
}

function fmtSize(bytes) {
  if (bytes == null) return '';
  if (bytes < 1024) return bytes + ' B';
  if (bytes < 1024 * 1024) return (bytes / 1024).toFixed(1) + ' KB';
  return (bytes / (1024 * 1024)).toFixed(2) + ' MB';
}

async function wsLoadTree(dirPath, targetEl, indent) {
  try {
    const r = await fetch('/agent/ws/tree?path=' + encodeURIComponent(dirPath || ''));
    const d = await r.json();
    if (!r.ok || d.error) { targetEl.innerHTML = `<div class="ws-empty">${esc(d.error || 'failed')}</div>`; return; }
    if (!indent) {
      const rp = $('ws-root-path');
      if (rp) rp.textContent = '📁 ' + (d.root || '');
      const tn = $('ws-title-name');
      if (tn) tn.textContent = d.project || 'Workspace';
      renderWsChanges(d.changes || []);
    }
    if (!d.nodes || !d.nodes.length) {
      targetEl.innerHTML = '<div class="ws-empty" style="padding:6px 0 0 14px;">(empty)</div>';
      return;
    }
    const wrap = document.createElement('div');
    wrap.className = 'ws-children';
    d.nodes.forEach(n => {
      const row = document.createElement('div');
      row.className = 'ws-node' + (n.dir ? ' dir' : '') + (n.changed ? ' changed' : '');
      row.dataset.path = (n.path || '').toLowerCase();
      const sizeStr = (!n.dir && n.size != null) ? fmtSize(n.size) : '';
      row.title = n.path + (sizeStr ? ` · ${sizeStr}` : '') + (n.changed ? ` · ${n.status || 'modified'} this session` : '');
      const caret = n.dir ? '<span class="caret">▶</span>' : '<span class="caret" style="visibility:hidden;">▶</span>';
      const sizeTag = sizeStr ? `<span class="ws-size">${sizeStr}</span>` : '';
      row.innerHTML = `${caret}<span>${n.dir ? '📁' : wsFileIcon(n.name)}</span><span class="nm">${esc(n.name)}</span>${sizeTag}`;
      if (n.dir) {
        const kids = document.createElement('div');
        kids.className = 'ws-children';
        kids.style.display = 'none';
        row.appendChild(kids);
        row.onclick = () => {
          const open = kids.style.display === 'none';
          kids.style.display = open ? 'block' : 'none';
          row.classList.toggle('expanded', open);
          if (open) {
            if (!kids.childElementCount) wsLoadTree(n.path, kids, true);
            wsExpandedDirs.add(n.path);
          } else {
            wsExpandedDirs.delete(n.path);
          }
          wsApplyFilter();
        };
      } else {
        row.onclick = () => wsShowFile(n.path);
      }
      wrap.appendChild(row);
      if (n.dir && wsExpandedDirs.has(n.path)) {
        row.click();
      }
    });
    targetEl.appendChild(wrap);
    wsApplyFilter();
  } catch (e) {
    targetEl.innerHTML = `<div class="ws-empty">${esc(e.message)}</div>`;
  }
}


/* client-side filter box */
let wsFilterT;
if ($('ws-filter')) {
  $('ws-filter').oninput = () => {
    clearTimeout(wsFilterT);
    wsFilterT = setTimeout(wsApplyFilter, 120);
  };
}
function wsApplyFilter() {
  const q = ($('ws-filter') && $('ws-filter').value || '').toLowerCase().trim();
  const tree = $('ws-tree');
  if (!tree) return;
  tree.querySelectorAll('.ws-node').forEach(row => {
    if (!q) { row.style.display = ''; return; }
    row.style.display = (row.dataset.path || '').includes(q) ? '' : 'none';
  });
  tree.querySelectorAll('.ws-children').forEach(grp => {
    if (!q) return;
    // keep a group visible if any child row matched
    const any = [...grp.querySelectorAll('.ws-node')].some(r => r.style.display !== 'none');
    const parentRow = grp.previousElementSibling;
    if (parentRow && parentRow.classList.contains('ws-node')) {
      parentRow.style.display = any ? '' : 'none';
    }
    grp.style.display = any ? grp.style.display : 'none';
  });
}
if ($('ws-refresh')) $('ws-refresh').onclick = () => wsRefreshTree();

async function wsShowFile(path) {
  try {
    const r = await fetch('/agent/ws/file?path=' + encodeURIComponent(path));
    const d = await r.json();
    if (!r.ok || d.error) { toast('File view failed: ' + (d.error || r.status), true); return; }
    $('ws-tree-view').style.display = 'none';
    const fv = $('ws-file-view');
    fv.style.display = 'flex';
    $('ws-file-path').textContent = path;
    $('ws-file-path').title = path;

    const badge = $('ws-file-badge');
    if (d.changed) {
      badge.textContent = '● ' + (d.status === 'created' ? 'created this session' : 'modified this session');
      badge.className = d.status === 'created' ? 'created' : 'modified';
    } else {
      badge.textContent = 'unchanged';
      badge.className = 'unchanged';
    }

    const pre = $('ws-file-code');
    const lang = hlLangFor(path);
    if (d.changed && d.diff) {
      // highlighted unified diff: green +, red -, dim context
      const marker = { '+': 'wsdiff-add', '-': 'wsdiff-del', ' ': 'wsdiff-ctx' };
      pre.innerHTML = d.diff.map(l => {
        const sign = l.t === ' ' ? '  ' : l.t + ' ';
        return `<span class="${marker[l.t] || 'wsdiff-ctx'}">${sign}${hlCode(l.s, lang)}</span>`;
      }).join('');
    } else {
      pre.innerHTML = hlCode(d.content || '(empty file)', lang);
    }
    pre.scrollLeft = 0; pre.scrollTop = 0;
  } catch (e) {
    toast('File view failed: ' + e.message, true);
  }
}

/* shift+wheel -> horizontal scroll in file viewer */
(function initWsHScroll() {
  const pre = $('ws-file-code');
  if (!pre) return;
  pre.addEventListener('wheel', e => {
    if (e.shiftKey && e.deltaY) {
      e.preventDefault();
      pre.scrollLeft += e.deltaY;
    }
  }, { passive: false });
})();

function wsShowTree() {
  $('ws-file-view').style.display = 'none';
  $('ws-tree-view').style.display = 'block';
}

function wsRefreshTree() {
  const tree = $('ws-tree');
  if (!tree) return;
  tree.innerHTML = '';
  wsExpandedDirs.clear();
  wsLoadTree('', tree, false);
  // if a file is open, refresh its diff too
  if ($('ws-file-view').style.display !== 'none') {
    const p = $('ws-file-path').textContent;
    if (p) wsShowFile(p);
  }
}

function updateWsRail() {
  const rail = $('ws-rail');
  if (!rail) return;
  // workspace panel only makes sense with an active project workspace
  rail.style.display = (agentMode && curProject && !wsPanelOpen) ? 'block' : 'none';
}

function setWsPanel(open) {
  wsPanelOpen = open;
  const panel = $('ws-panel');
  panel.style.right = '';
  panel.classList.toggle('open', open);
  updateWsRail();
  if (open) wsRefreshTree();
}

$('ws-rail').onclick = () => setWsPanel(true);
$('ws-close').onclick = () => setWsPanel(false);
$('ws-back').onclick = wsShowTree;
window.addEventListener('keydown', e => {
  if (e.key === 'Escape' && wsPanelOpen) setWsPanel(false);
});

/* ---------------- shell permission modal ---------------- */
function showPermModal(reqId, cmd) {
  const m = $('perm-modal');
  if (!m) return;
  m.dataset.reqId = reqId;
  $('perm-cmd').textContent = cmd;
  // suggest the leading command word as an allow pattern, * for everything
  const firstWord = (cmd.trim().split(/\s+/)[0] || '*') + ' *';
  const projName = curProject ? (curProject.name || 'this project') : 'active project';
  $('perm-note').innerHTML = `Permission scopes for <code>${esc(firstWord)}</code>:<br>` +
    `• <b>Just once:</b> Run this command now without saving.<br>` +
    `• <b>For this project:</b> Automatically permit in <i>${esc(projName)}</i>.<br>` +
    `• <b>Always:</b> Allow globally across all projects in <code>config.json</code>.`;
  m.dataset.pattern = firstWord;
  m.hidden = false;
}

async function answerPermission(decision) {
  const m = $('perm-modal');
  const reqId = m.dataset.reqId;
  m.hidden = true;
  if (!reqId) return;
  const pattern = (decision === 'always' || decision === 'project') ? m.dataset.pattern : null;
  const projectId = (curProject && curProject.id) ? curProject.id : null;
  try {
    await fetch('/agent/permission', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        req_id: reqId,
        decision,
        pattern,
        project_id: projectId
      }),
    });
  } catch (e) { /* stream may have ended */ }

  if (decision === 'always') {
    toast(`✓ Pattern "${m.dataset.pattern}" allow-listed globally`);
  } else if (decision === 'project') {
    const pName = curProject ? curProject.name : 'project';
    toast(`✓ Pattern "${m.dataset.pattern}" allowed for ${pName}`);
  } else if (decision === 'allow') {
    toast('▶ Shell command approved once');
  } else if (decision === 'deny') {
    toast('✕ Shell command denied');
  }
}

if ($('perm-allow')) $('perm-allow').onclick = () => answerPermission('allow');
if ($('perm-project')) $('perm-project').onclick = () => answerPermission('project');
if ($('perm-always')) $('perm-always').onclick = () => answerPermission('always');
if ($('perm-deny')) $('perm-deny').onclick = () => answerPermission('deny');
if ($('perm-close-x')) $('perm-close-x').onclick = () => answerPermission('deny');

/* drag the left-edge grip to resize the workspace panel width */
(function initWsGrip() {
  const grip = $('ws-grip');
  const panel = $('ws-panel');
  if (!grip || !panel) return;

  // restore persisted width
  try {
    const saved = parseInt(localStorage.getItem('ws_panel_w'));
    if (saved >= 280) panel.style.width = saved + 'px';
  } catch (e) {}

  let dragging = false;
  grip.addEventListener('mousedown', e => {
    dragging = true;
    grip.classList.add('dragging');
    document.body.classList.add('ws-resizing');
    e.preventDefault();
  });
  window.addEventListener('mousemove', e => {
    if (!dragging) return;
    const vw = window.innerWidth;
    const w = Math.min(Math.max(280, vw - e.clientX), Math.floor(vw * 0.9));
    panel.style.width = w + 'px';
    panel.style.maxWidth = 'none';   // allow drag beyond the 92vw default cap
  });
  window.addEventListener('mouseup', () => {
    if (!dragging) return;
    dragging = false;
    grip.classList.remove('dragging');
    document.body.classList.remove('ws-resizing');
    try { localStorage.setItem('ws_panel_w', parseInt(panel.style.width) || 360); } catch (e) {}
  });
})();

/* Plan/Build dropdown selector (default Build) */
(function initPlanSeg() {
  const planSel = $('agent-plan-sel');
  if (!planSel) return;
  planMode = localStorage.getItem('agent_plan') === 'plan';
  planSel.value = planMode ? 'plan' : 'build';
  function apply(silent) {
    planSel.value = planMode ? 'plan' : 'build';
    const input = $('input');
    if (input && agentMode) {
      input.placeholder = planMode
        ? 'Plan mode — agent explores read-only and proposes a plan…'
        : 'Describe a coding task (e.g. "Find and fix bug in main.py")…';
    }
    if (!silent) toast(planMode ? '📋 Plan — agent proposes, nothing is written' : '🔨 Build — agent executes changes');
  }
  planSel.onchange = () => {
    planMode = planSel.value === 'plan';
    try { localStorage.setItem('agent_plan', planMode ? 'plan' : 'build'); } catch (e) {}
    apply();
  };
  apply(true);
  window._setPlanMode = (on) => {
    planMode = on;
    try { localStorage.setItem('agent_plan', on ? 'plan' : 'build'); } catch (e) {}
    apply(true);
  };
})();

/* ---------------- @ file tagging & / commands (agent mode) ---------------- */
let cmdMenu = { open: false, kind: null, items: [], sel: 0, tokenStart: -1 };

function cmdMenuClose() {
  cmdMenu.open = false; cmdMenu.kind = null; cmdMenu.items = []; cmdMenu.sel = 0; cmdMenu.tokenStart = -1;
  const m = $('cmd-menu');
  if (m) { m.style.display = 'none'; m.innerHTML = ''; }
}

function cmdMenuRender() {
  const m = $('cmd-menu');
  if (!m || !cmdMenu.open || !cmdMenu.items.length) { cmdMenuClose(); return; }
  m.innerHTML = cmdMenu.items.map((it, i) => `
    <div class="cmd-item ${i === cmdMenu.sel ? 'sel' : ''}" data-i="${i}">
      <span class="cmd-icon">${it.icon || ''}</span>
      <span class="cmd-name">${esc(it.name)}</span>
      ${it.desc ? `<span class="cmd-desc">${esc(it.desc)}</span>` : ''}
    </div>`).join('');
  m.style.display = 'block';
  m.querySelectorAll('.cmd-item').forEach(el => {
    el.onclick = () => cmdMenuPick(parseInt(el.dataset.i));
  });
}

async function cmdMenuOpen(kind, query) {
  cmdMenu.open = true; cmdMenu.kind = kind; cmdMenu.sel = 0;
  const m = $('cmd-menu');
  if (kind === 'files') {
    // fetch the workspace flat file list once per open (agent mode, project set)
    try {
      const d = await (await fetch('/agent/workspace')).json();
      const q = query.toLowerCase();
      cmdMenu.items = (d.files || [])
        .filter(f => !q || f.path.toLowerCase().includes(q))
        .slice(0, 12)
        .map(f => ({ icon: wsFileIcon(f.path), name: f.path, desc: `${(f.size / 1024).toFixed(1)} KB`, value: f.path }));
    } catch (e) { cmdMenuClose(); return; }
  } else if (kind === 'slash') {
    const all = [
      { icon: '📋', name: 'plan', desc: 'switch to Plan mode (read-only, propose)' },
      { icon: '🔨', name: 'build', desc: 'switch to Build mode (execute)' },
    ];
    try {
      const d = await (await fetch('/control/capabilities')).json();
      (d.skills && d.skills.items || []).forEach(s => {
        all.push({ icon: '🎯', name: s.name, desc: s.description || '', isSkill: true });
      });
    } catch (e) {}
    const q = query.toLowerCase();
    cmdMenu.items = all.filter(i => !q || i.name.toLowerCase().includes(q)).slice(0, 14);
    if (!cmdMenu.items.length) { cmdMenuClose(); return; }
  }
  cmdMenuRender();
}

function cmdMenuPick(i) {
  const it = cmdMenu.items[i];
  if (!it) return;
  const input = $('input');
  const before = input.value.slice(0, cmdMenu.tokenStart);
  const afterStart = cmdMenu.tokenStart + currentToken(input).length;
  const after = input.value.slice(afterStart);
  if (cmdMenu.kind === 'files') {
    input.value = before + '@' + it.value + ' ' + after.replace(/^\s?/, '');
  } else if (cmdMenu.kind === 'slash') {
    if (it.name === 'plan') { window._setPlanMode && window._setPlanMode(true); }
    else if (it.name === 'build') { window._setPlanMode && window._setPlanMode(false); }
    else if (it.isSkill) {
      input.value = before + '/' + it.name + ' ' + after;
    }
    else { input.value = before + '/' + it.name + ' ' + after; }
  }
  cmdMenuClose();
  input.focus();
}

function currentToken(input) {
  // text from the last @ or / up to the caret (or end of the current word)
  const pos = input.selectionStart != null ? input.selectionStart : input.value.length;
  const upto = input.value.slice(0, pos);
  const m = upto.match(/(?:^|\s)([@\/])([^@\/\s]*)$/);
  return m ? { ch: m[1], text: m[2], start: pos - m[2].length - 1 } : null;
}

(function initCmdMenu() {
  const input = $('input');
  if (!input) return;
  input.addEventListener('input', () => {
    if (!agentMode) { cmdMenuClose(); return; }
    const tok = currentToken(input);
    if (tok && tok.ch === '@') {
      if (!cmdMenu.open || cmdMenu.kind !== 'files') cmdMenu.tokenStart = tok.start;
      cmdMenuOpen('files', tok.text);
    } else if (tok && tok.ch === '/' && tok.start === 0) {
      if (!cmdMenu.open || cmdMenu.kind !== 'slash') cmdMenu.tokenStart = tok.start;
      cmdMenuOpen('slash', tok.text);
    } else {
      cmdMenuClose();
    }
  });
  input.addEventListener('keydown', e => {
    if (!cmdMenu.open || !cmdMenu.items.length) return;
    if (e.key === 'ArrowDown') { e.preventDefault(); cmdMenu.sel = (cmdMenu.sel + 1) % cmdMenu.items.length; cmdMenuRender(); }
    else if (e.key === 'ArrowUp') { e.preventDefault(); cmdMenu.sel = (cmdMenu.sel - 1 + cmdMenu.items.length) % cmdMenu.items.length; cmdMenuRender(); }
    else if (e.key === 'Enter' || e.key === 'Tab') { e.preventDefault(); e.stopPropagation(); cmdMenuPick(cmdMenu.sel); }
    else if (e.key === 'Escape') { cmdMenuClose(); }
  }, true);   // capture: run before the global Enter-to-send handler
})();

/* @tagged files get expanded into the prompt on send (agent mode) */
async function expandAtTags(text) {
  if (!text.includes('@')) return text;
  // @path tokens: @rel/path.ext (letters, digits, _ - . / \ space-free)
  const tags = [...text.matchAll(/@([\w\-./\\]+\.[\w]+)/g)].map(m => m[1]);
  if (!tags.length) return text;
  let out = text;
  for (const t of tags.slice(0, 5)) {
    try {
      const r = await fetch('/agent/ws/file?path=' + encodeURIComponent(t));
      const d = await r.json();
      if (!r.ok || d.error) continue;
      const clipped = d.content.length > 12000
        ? d.content.slice(0, 12000) + '\n... (truncated, ' + d.content.length + ' chars)' : d.content;
      out = out.split('@' + t).join('see file "' + t + '":\n' + clipped);
    } catch (e) { /* leave the tag as-is */ }
  }
  return out;
}

/* mode switcher: Chat vs Agent */
function setAppMode(isAgent, isUserSwitch = false) {
  const prevMode = agentMode;
  agentMode = !!isAgent;
  try { localStorage.setItem('app_mode', agentMode ? 'agent' : 'chat'); } catch (e) {}

  const btnChat = $('mode-chat');
  const btnAgent = $('mode-agent');
  if (btnChat) btnChat.classList.toggle('active', !agentMode);
  if (btnAgent) btnAgent.classList.toggle('active', agentMode);
  const banner = $('agent-banner');
  if (banner) banner.style.display = 'none';
  const projCard = $('projects-card');
  if (projCard) projCard.style.display = agentMode ? 'block' : 'none';
  // plan/build select + engine select only in agent mode
  const planSel = $('agent-plan-sel');
  if (planSel) planSel.style.display = agentMode ? 'inline-block' : 'none';
  const eng = $('agent-engine');
  if (eng) eng.style.display = agentMode ? 'inline-block' : 'none';
  const webToggle = $('btn-web-toggle');
  if (webToggle) webToggle.style.display = agentMode ? 'none' : 'flex';
  if (agentMode && window._setPlanMode) window._setPlanMode(planMode);   // refresh placeholder
  // workspace side panel needs agent mode + an active project
  if (!agentMode && wsPanelOpen) setWsPanel(false);
  updateWsRail();
  const sTitle = $('session-title');
  if (sTitle) {
    sTitle.textContent = agentMode ? 'Project Tasks' : 'Recent Chats';
    sTitle.title = agentMode ? 'Project-scoped task sessions' : 'Generic chat sessions';
  }
  const sel = $('profile');
  const mName = (sel && sel.value) ? sel.value.split('\\').pop().split('/').pop() : 'model';
  if (agentMode) {
    $('input').placeholder = 'Describe a coding task (e.g. "Find and fix bug in main.py", "Refactor the database queries")...';
    if (isUserSwitch) toast(curProject ? `Agent Task mode active (workspace: ${curProject.name || curProject})` : 'Agent Task mode active');
  } else {
    $('input').placeholder = `Message ${mName}... (Enter to send, Shift+Enter for newline)`;
    if (isUserSwitch) toast('Chat mode active — direct conversation with LLM');
  }

  // Switching between modes opens a new fresh chat window and loads relevant session list
  if (isUserSwitch && prevMode !== agentMode) {
    if (ctrl) {
      try { ctrl.abort(); } catch (e) {}
      ctrl = null;
      setGenUI(false);
    }
    curSession = null;
    messages = [];
    renderAll();
    loadSessions();
    if ($('input')) {
      $('input').value = '';
      $('input').focus();
    }
    clearAttachments();
  } else {
    loadSessions();
  }
}

$('mode-chat').onclick = () => setAppMode(false, true);
$('mode-agent').onclick = () => setAppMode(true, true);

/* ---------------- projects & sessions ---------------- */
let curProject = null;      // {id, name}
let curSession = null;      // {id, title}

async function loadProjects() {
  try {
    const d = await (await fetch('/control/projects')).json();
    const list = $('projects-list');
    if (d.active) {
      const p = (d.projects || []).find(x => x.name === d.active);
      if (p) curProject = p;
    } else curProject = null;

    if (list) {
      list.innerHTML = '';
      const userProjects = (d.projects || []).filter(p => p.id !== 0 && p.name !== 'scratch' && p.name !== 'default');

      if (!userProjects.length) {
        list.innerHTML = `
          <div class="project-empty-state">
            <span style="font-size:11px; color:var(--dim);">No custom projects yet</span>
            <button class="btn ghost" style="font-size:10.5px; padding:3px 8px; margin-top:4px;" onclick="$('btn-newproject').click()">+ Create Project</button>
          </div>`;
      } else {
        userProjects.forEach(p => {
          const ws = p.workspace_dir || `${d.workspace_root || 'E:\\AI\\workspace'}\\${p.name}`;
          const isCur = curProject && curProject.id === p.id;
          const row = document.createElement('div');
          row.className = 'project-row' + (isCur ? ' cur' : '');
          row.title = `Project: ${p.name}\nDirectory: ${ws}`;
          row.innerHTML = `
            <div class="proj-icon-wrapper">
              <svg class="proj-dir-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round">
                <path d="M22 19a2 2 0 0 1-2 2H4a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h5l2 3h9a2 2 0 0 1 2 2z"></path>
              </svg>
            </div>
            <div class="proj-meta">
              <div class="proj-name">${esc(p.name)}</div>
              <div class="proj-path" title="${esc(ws)}">${esc(ws)}</div>
            </div>
            <span class="p-del" title="Remove project registration (files preserved)">✕</span>
          `;
          row.onclick = e => {
            if (e.target.classList.contains('p-del')) {
              e.stopPropagation();
              deleteProject(p.id, p.name);
              return;
            }
            activateProject(p.id);
          };
          list.appendChild(row);
        });
      }
    }

    // Populate registered projects in manage modal if open
    const mList = $('proj-manage-list');
    if (mList) {
      if (!d.projects || d.projects.length === 0) {
        mList.innerHTML = '<div style="color:var(--dim); font-size:11px; padding:6px; text-align:center;">No projects registered.</div>';
      } else {
        mList.innerHTML = '';
        d.projects.forEach(p => {
          const ws = p.workspace_dir || `${d.workspace_root || 'E:\\AI\\workspace'}\\${p.name}`;
          const item = document.createElement('div');
          item.style.cssText = 'display:flex; align-items:center; justify-content:space-between; background:var(--bg); border:1px solid var(--border); border-radius:6px; padding:5px 8px; font-size:11.5px;';
          item.innerHTML = `
            <div style="min-width:0; flex:1; margin-right:8px;">
              <div style="font-weight:600; color:var(--text); overflow:hidden; text-overflow:ellipsis; white-space:nowrap;">📁 ${esc(p.name)}</div>
              <div style="font-size:9.5px; color:var(--dim); font-family:monospace; overflow:hidden; text-overflow:ellipsis; white-space:nowrap;" title="${esc(ws)}">${esc(ws)}</div>
            </div>
            <button class="btn ghost btn-del-proj-row" style="width:auto; margin:0; padding:2px 7px; font-size:10px; color:var(--red); border-color:rgba(239,68,68,0.3);" title="Remove project from agent (files on disk are NOT deleted)">🗑️ Remove</button>
          `;
          item.querySelector('.btn-del-proj-row').onclick = () => deleteProject(p.id, p.name);
          mList.appendChild(item);
        });
      }
    }

    updateWsRail();
    loadSessions();
  } catch (e) {}
}

async function deleteProject(pid, pname) {
  if (!confirm(`Remove project "${pname}" from agent?\n\nNOTE: Only the project registration and sessions are removed from the agent.\nWorkspace files on your disk will NOT be touched or deleted.`)) {
    return;
  }
  try {
    const r = await fetch(`/control/projects/${pid}`, { method: 'DELETE' });
    const d = await r.json();
    if (!r.ok || !d.ok) throw new Error(d.error || ('HTTP ' + r.status));
    toast(`Project "${pname}" removed from agent (disk files preserved)`);
    if (curProject && curProject.id === pid) {
      curProject = null;
      curSession = null;
      messages = [];
      renderAll();
    }
    await loadProjects();
  } catch (e) {
    toast('Delete project failed: ' + e.message, true);
  }
}

async function loadSessions() {
  const list = $('session-list');
  const url = agentMode
    ? (curProject ? `/control/projects/${curProject.id}/sessions` : null)
    : `/control/projects/0/sessions`;

  if (agentMode && !curProject) {
    list.innerHTML = '<div class="dim" style="font-size:11.5px; padding:8px 4px;">No project selected.<br>Pick or create a project above for agent tasks.</div>';
    return;
  }
  try {
    const d = await (await fetch(url)).json();
    if (!d.sessions || !d.sessions.length) {
      const emptyMsg = agentMode
        ? 'No task sessions in this project.<br>Type a prompt to start an agent task.'
        : 'No chats yet.<br>Type a message to start chatting.';
      list.innerHTML = `<div class="dim" style="font-size:11.5px; padding:8px 4px;">${emptyMsg}</div>`;
      return;
    }
    list.innerHTML = '';
    d.sessions.forEach(s => {
      const row = document.createElement('div');
      row.className = 'session-row' + (curSession && curSession.id === s.id ? ' cur' : '');
      const icon = agentMode ? '🤖' : '💬';
      row.innerHTML = `<span style="flex:1; overflow:hidden; text-overflow:ellipsis; white-space:nowrap;" title="${esc(s.title)}">${icon} ${esc(s.title)}</span><span class="s-del" title="Delete session">✕</span>`;
      row.onclick = e => {
        if (e.target.classList.contains('s-del')) { deleteSession(s.id); return; }
        openSession(s);
      };
      list.appendChild(row);
    });

    // Auto-restore active session on initial load / refresh
    if (!curSession && d.sessions && d.sessions.length) {
      const savedSid = localStorage.getItem(agentMode ? 'active_agent_session_id' : 'active_chat_session_id');
      const target = (savedSid && d.sessions.find(x => String(x.id) === String(savedSid))) || d.sessions[0];
      if (target) {
        openSession(target);
      }
    }
  } catch (e) {}
}

async function openSession(s) {
  try {
    // Fetch messages and config in parallel so curCtxMax is ready before rendering.
    // This eliminates the race where openSession() finishes before loadProfiles() or
    // pollStatus() has had a chance to set curCtxMax from the actual model window size.
    const sel = $('profile');
    const modelTarget = (sel && sel.value) ? sel.value : (curStatus && curStatus.model);
    const cfgUrl = '/control/config' + (modelTarget ? '?model=' + encodeURIComponent(modelTarget) : '');
    const [msgRes, cfgRes] = await Promise.allSettled([
      fetch(`/control/sessions/${s.id}/messages`).then(r => r.json()),
      fetch(cfgUrl).then(r => r.json()).catch(() => null),
    ]);

    const d = (msgRes.status === 'fulfilled' ? msgRes.value : null) || {};
    const cfg = cfgRes.status === 'fulfilled' ? cfgRes.value : null;

    // Immediately seed curCtxMax from the config response (no need to wait for loadProfiles)
    if (cfg && cfg.context_size > 0) curCtxMax = cfg.context_size;

    messages = (d.messages || []).map(m => {
      const meta = m.meta || {};
      const textLen = (m.content || '').length + ((meta.reasoning) || '').length;
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
      };
    });
    curSession = s;
    try {
      localStorage.setItem(agentMode ? 'active_agent_session_id' : 'active_chat_session_id', String(s.id));
    } catch (e) {}
    renderAll();
    loadSessions();
  } catch (e) { toast('Failed to load session', true); }
}

async function deleteSession(sid) {
  try { await fetch(`/control/sessions/${sid}`, { method: 'DELETE' }); } catch (e) {}
  if (curSession && curSession.id === sid) {
    curSession = null;
    messages = [];
    try {
      localStorage.removeItem(agentMode ? 'active_agent_session_id' : 'active_chat_session_id');
    } catch (e) {}
    renderAll();
  }
  loadSessions();
}

function persistMsg(role, content, meta) {
  if (!curSession) return;
  fetch(`/control/sessions/${curSession.id}/messages`, {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ role, content, meta }),
  }).catch(() => {});
}

function formatSessionTitleFromPrompt(text) {
  if (!text) return agentMode ? 'New agent task' : 'New chat';
  let clean = text.replace(/--- (?:FILE|IMAGE):[\s\S]*?--- END [^\n]+ ---/g, '').trim();
  clean = clean.replace(/\s+/g, ' ');
  if (!clean) return agentMode ? 'Task session' : 'Chat session';
  return clean.length > 50 ? clean.slice(0, 48) + '…' : clean;
}

function ensureSession(promptText) {
  const desiredTitle = formatSessionTitleFromPrompt(promptText);
  if (curSession) {
    // If curSession has a generic default title and we now have a real prompt, auto-rename it
    if ((curSession.title === 'New chat' || curSession.title === 'New agent task' || curSession.title === 'Chat session' || curSession.title === 'Agent Task') && promptText) {
      curSession.title = desiredTitle;
      fetch(`/control/sessions/${curSession.id}`, {
        method: 'PATCH',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ title: desiredTitle })
      }).then(() => loadSessions()).catch(() => {});
    }
    return Promise.resolve(curSession);
  }
  const pid = (agentMode && curProject) ? curProject.id : 0;
  return fetch(`/control/projects/${pid}/sessions`, {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ title: desiredTitle }),
  })
    .then(r => r.json())
    .then(j => {
      curSession = j.session;
      if (curSession) {
        try {
          localStorage.setItem(agentMode ? 'active_agent_session_id' : 'active_chat_session_id', String(curSession.id));
        } catch (e) {}
      }
      loadSessions();
      return curSession;
    })
    .catch(() => null);
}

$('btn-newchat').onclick = () => {
  if (generating) { toast('Generation in progress', true); return; }
  curSession = null;
  messages = [];
  try {
    localStorage.removeItem(agentMode ? 'active_agent_session_id' : 'active_chat_session_id');
  } catch (e) {}
  renderAll();
  loadSessions();
  $('input').focus();
  toast(agentMode ? 'New agent task started' : 'New chat started');
};

const btnNewSession = $('btn-newsession');
if (btnNewSession) {
  btnNewSession.onclick = () => $('btn-newchat').click();
}

// Project modal handling
$('btn-newproject').onclick = () => {
  $('new-proj-name').value = '';
  $('new-proj-dir').value = '';
  $('proj-modal').hidden = false;
  setTimeout(() => $('new-proj-name').focus(), 50);
};

const closeProjModal = () => { $('proj-modal').hidden = true; };
$('btn-proj-cancel').onclick = closeProjModal;
$('proj-modal-close').onclick = closeProjModal;
if ($('btn-delproject')) $('btn-delproject').onclick = () => { if (curProject) deleteProject(curProject.id, curProject.name); };
if ($('btn-delproject-icon')) $('btn-delproject-icon').onclick = () => { if (curProject) deleteProject(curProject.id, curProject.name); };

async function submitNewProject() {
  const name = $('new-proj-name').value.trim();
  const workspace_dir = $('new-proj-dir').value.trim() || null;
  if (!name) { toast('Project name is required', true); return; }
  try {
    const r = await fetch('/control/projects', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ name, workspace_dir }),
    });
    const j = await r.json();
    if (!r.ok) throw new Error(j.error || ('HTTP ' + r.status));
    closeProjModal();
    await loadProjects();
    await activateProject(j.project.id);
    toast(`Project "${j.project.name}" created & activated`);
  } catch (e) {
    toast('Project create failed: ' + e.message, true);
  }
}

$('btn-proj-submit').onclick = submitNewProject;
$('new-proj-name').onkeydown = e => { if (e.key === 'Enter') submitNewProject(); };
$('new-proj-dir').onkeydown = e => { if (e.key === 'Enter') submitNewProject(); };

// Workspace directory browsing & picker handling
$('btn-browse-dir').onclick = async () => {
  const btn = $('btn-browse-dir');
  const txt = $('btn-browse-dir-text');
  const icon = $('btn-browse-dir-icon');
  const oldText = 'Browse…';
  txt.textContent = 'Choosing…';
  icon.textContent = '⏳';
  btn.disabled = true;
  try {
    const curVal = $('new-proj-dir').value.trim();
    const r = await fetch('/control/browse_folder', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ initial_dir: curVal }),
    });
    if (!r.ok) {
      let errDetail = `HTTP ${r.status}`;
      try {
        const errJson = await r.json();
        errDetail = (errJson && (errJson.error || errJson.detail)) || errDetail;
        if (typeof errDetail === 'object') errDetail = JSON.stringify(errDetail);
      } catch (_) {}
      if (r.status === 404) errDetail += ' (Please restart server_manager.py to activate folder picker)';
      throw new Error(errDetail);
    }
    const d = await r.json();
    if (d.ok && d.path) {
      $('new-proj-dir').value = d.path;
      const nameInput = $('new-proj-name');
      if (!nameInput.value.trim()) {
        const parts = d.path.replace(/\\/g, '/').split('/').filter(Boolean);
        if (parts.length) nameInput.value = parts[parts.length - 1];
      }
      toast(`Selected: ${d.path}`);
    } else if (d.cancelled) {
      // User cancelled dialog
    }
  } catch (e) {
    toast('Directory chooser error: ' + (e.message || String(e)), true);
  } finally {
    txt.textContent = oldText;
    icon.textContent = '📁';
    btn.disabled = false;
  }
};

document.querySelectorAll('.btn-quick-dir').forEach(b => {
  b.onclick = () => {
    const d = b.getAttribute('data-dir');
    $('new-proj-dir').value = d === 'default' ? '' : d;
    const nameInput = $('new-proj-name');
    if (!nameInput.value.trim() && d !== 'default') {
      const parts = d.replace(/\\/g, '/').split('/').filter(Boolean);
      if (parts.length) nameInput.value = parts[parts.length - 1];
    }
  };
});

// In-App Directory Picker Modal
let pickerCurrentPath = '';
let pickerSelectedPath = '';

const closeDirPicker = () => { $('dir-picker-modal').hidden = true; };
$('dir-picker-close').onclick = closeDirPicker;
$('btn-dir-picker-cancel').onclick = closeDirPicker;

async function navigateDirPicker(path) {
  const listEl = $('dir-picker-list');
  listEl.innerHTML = '<div style="color:var(--dim); font-size:11px; padding:12px; text-align:center;">Loading directories…</div>';
  try {
    const q = path ? `?path=${encodeURIComponent(path)}` : '';
    const r = await fetch(`/control/fs/browse${q}`);
    let d = null;
    try { d = await r.json(); } catch (_) {}
    if (!r.ok || !d || !d.ok) {
      let errStr = (d && (d.error || d.detail)) || `HTTP ${r.status}`;
      if (typeof errStr === 'object') errStr = JSON.stringify(errStr);
      if (r.status === 404) errStr += ' (Please restart server_manager.py to activate file browser)';
      throw new Error(errStr);
    }
    pickerCurrentPath = d.current;
    pickerSelectedPath = d.current;
    $('dir-picker-path').textContent = d.current;
    $('btn-dir-picker-up').disabled = !d.parent;
    $('btn-dir-picker-up').onclick = () => { if (d.parent) navigateDirPicker(d.parent); };

    // Render drives
    const drivesEl = $('dir-picker-drives');
    drivesEl.innerHTML = '';
    (d.drives || []).forEach(drv => {
      const db = document.createElement('button');
      db.className = 'btn ' + (d.current.toUpperCase().startsWith(drv.toUpperCase()) ? 'blue' : 'ghost');
      db.style.cssText = 'width:auto; margin:0; padding:2px 8px; font-size:10.5px;';
      db.textContent = drv;
      db.onclick = () => navigateDirPicker(drv);
      drivesEl.appendChild(db);
    });

    // Render subdirectories
    listEl.innerHTML = '';
    if (!d.subdirs || d.subdirs.length === 0) {
      listEl.innerHTML = '<div style="color:var(--dim); font-size:11px; padding:14px; text-align:center;">(Empty or no accessible subdirectories)</div>';
      return;
    }

    d.subdirs.forEach(name => {
      const item = document.createElement('div');
      item.className = 'dir-item';
      item.innerHTML = `<span style="font-size:13px;">📁</span> <span style="flex:1; overflow:hidden; text-overflow:ellipsis; white-space:nowrap;">${name}</span>`;
      
      const getFullPath = () => {
        const sep = d.current.includes('\\') ? '\\' : '/';
        return d.current.endsWith(sep) ? (d.current + name) : (d.current + sep + name);
      };

      item.onclick = () => {
        document.querySelectorAll('#dir-picker-list .dir-item').forEach(el => el.classList.remove('selected'));
        item.classList.add('selected');
        pickerSelectedPath = getFullPath();
      };
      item.ondblclick = () => {
        navigateDirPicker(getFullPath());
      };
      listEl.appendChild(item);
    });
  } catch (e) {
    const errMsg = (e && e.message) ? e.message : String(e);
    listEl.innerHTML = `<div style="color:var(--red); font-size:11px; padding:10px;">Error: ${esc(errMsg)}</div>`;
  }
}

$('btn-open-dir-picker').onclick = () => {
  $('dir-picker-modal').hidden = false;
  const initial = $('new-proj-dir').value.trim();
  navigateDirPicker(initial);
};

$('btn-dir-picker-select').onclick = () => {
  if (pickerSelectedPath) {
    $('new-proj-dir').value = pickerSelectedPath;
    const nameInput = $('new-proj-name');
    if (!nameInput.value.trim()) {
      const parts = pickerSelectedPath.replace(/\\/g, '/').split('/').filter(Boolean);
      if (parts.length) nameInput.value = parts[parts.length - 1];
    }
  }
  closeDirPicker();
};

$('btn-dir-picker-mkdir').onclick = async () => {
  const name = $('dir-picker-new-name').value.trim();
  if (!name) return;
  try {
    const r = await fetch('/control/fs/mkdir', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ path: pickerCurrentPath, name }),
    });
    const d = await r.json();
    if (d.ok) {
      $('dir-picker-new-name').value = '';
      await navigateDirPicker(pickerCurrentPath);
      toast(`Created folder "${name}"`);
    } else {
      toast('Failed to create folder: ' + (d.error || 'unknown'), true);
    }
  } catch (e) {
    toast('Create folder error: ' + e.message, true);
  }
};

async function activateProject(pid) {
  try {
    const r = await fetch(`/control/projects/${pid}/activate`, { method: 'POST' });
    const d = await r.json();
    curProject = d.project || null;
    curSession = null;
    messages = [];
    renderAll();
    await loadProjects();
    if (curProject) {
      const ws = curProject.workspace_dir || `E:\\AI\\workspace\\${curProject.name}`;
      toast(`Active project: ${curProject.name} (${ws})`);
    } else {
      toast('No active project');
    }
    updateWsRail();
    if (wsPanelOpen) wsRefreshTree();   // tree now shows the new project's files
  } catch (e) { toast('Activate failed', true); }
}

if ($('project-sel')) {
  $('project-sel').onchange = e => {
    const v = e.target.value;
    if (!v) { setNoProject(); return; }
    activateProject(parseInt(v));
  };
}

async function setNoProject() {
  try { await fetch('/control/projects/0/activate', { method: 'POST' }); } catch (e) {}
  curProject = null; curSession = null;
  messages = [];
  renderAll();
  loadProjects();
  if (wsPanelOpen) setWsPanel(false);   // no project -> no workspace to show
  updateWsRail();
}

function toolIcon(name) {
  switch (name) {
    case 'list_files': return '📁';
    case 'read_file': return '📄';
    case 'grep': return '🔍';
    case 'write_file': return '💾';
    case 'edit_file': return '✏️';
    case 'run_python': return '🐍';
    case 'list_diff': return '📊';
    case 'revert': return '↩️';
    case 'analyze_image': return '🖼️';
    case 'search_memory': return '🧠';
    default: return '🛠️';
  }
}

function parseStepsFromActs(acts) {
  if (!acts || !acts.length) return [];
  const steps = [];
  let curStep = null;
  let curTool = null;

  acts.forEach(a => {
    if (a.type === 'step') {
      curStep = {
        step: a.step,
        total: a.total,
        lane: null,
        thought: '',
        tools: [],
      };
      steps.push(curStep);
      curTool = null;
    } else {
      if (!curStep) {
        curStep = { step: 1, total: 1, lane: null, thought: '', tools: [] };
        steps.push(curStep);
      }
      if (a.type === 'lane') {
        curStep.lane = a.lane;
      } else if (a.type === 'thought' || a.type === 'reasoning') {
        curStep.thought += (curStep.thought ? '\n\n' : '') + (a.text || '');
      } else if (a.type === 'tool_call') {
        const laneInfo = (typeof curStep.lane === 'object' && curStep.lane) ? curStep.lane : (curStep.lane ? { model: curStep.lane, display: curStep.lane } : {});
        curTool = {
          id: a.id,
          name: a.name,
          args: a.args || {},
          model: a.model || laneInfo.display || laneInfo.model || '',
          device: a.device || laneInfo.device || '',
          verify: null,
          result: null,
          ok: true
        };
        curStep.tools.push(curTool);
      } else if (a.type === 'verify') {
        if (curTool && (curTool.name === a.name || (a.id && curTool.id === a.id))) {
          curTool.verify = a;
        } else {
          const match = curStep.tools.slice().reverse().find(t => t.name === a.name);
          if (match) match.verify = a;
        }
      } else if (a.type === 'tool_result') {
        if (curTool && (curTool.name === a.name || (a.id && curTool.id === a.id)) && curTool.result === null) {
          curTool.result = a.result;
          curTool.ok = a.ok !== false;
        } else {
          const match = curStep.tools.slice().reverse().find(t => t.name === a.name && t.result === null);
          if (match) {
            match.result = a.result;
            match.ok = a.ok !== false;
          } else {
            curStep.tools.push({ id: a.id, name: a.name, args: {}, verify: null, result: a.result, ok: a.ok !== false });
          }
        }
      }
    }
  });
  return steps;
}

function formatToolArgs(name, args) {
  if (!args || typeof args !== 'object') return String(args || '');
  let out = '';
  for (const [k, v] of Object.entries(args)) {
    if (typeof v === 'string' && (v.includes('\n') || v.length > 50)) {
      out += `--- ${k} ---\n${v}\n\n`;
    } else {
      out += `${k}: ${typeof v === 'object' ? JSON.stringify(v) : v}\n`;
    }
  }
  return out.trim() || JSON.stringify(args, null, 2);
}

function quickArgPreview(name, args) {
  if (!args || typeof args !== 'object') return '';
  const p = args.path || args.file || args.filename;
  if (p) return esc(p);
  if (args.pattern) return `pattern: "${esc(args.pattern)}"`;
  if (args.code) {
    const clean = args.code.replace(/\s+/g, ' ').trim();
    return `py: ${esc(clean.slice(0, 45))}${clean.length > 45 ? '…' : ''}`;
  }
  if (args.query) return `"${esc(args.query)}"`;
  const k = Object.keys(args);
  if (k.length) return `${k[0]}: ${esc(String(args[k[0]]).slice(0, 40))}`;
  return '';
}

function toolMeta(name) {
  switch (name) {
    case 'write_file': return { icon: '📄', label: 'write_file', verb: 'Saved', cls: 'write' };
    case 'edit_file': return { icon: '✏️', label: 'edit_file', verb: 'Edited', cls: 'edit' };
    case 'read_file': return { icon: '📖', label: 'read_file', verb: 'Read', cls: 'read' };
    case 'run_python': return { icon: '⚡', label: 'run_python', verb: 'Executed', cls: 'run' };
    case 'list_files': return { icon: '📁', label: 'list_files', verb: 'Listed', cls: 'list' };
    case 'grep': return { icon: '🔍', label: 'grep', verb: 'Searched', cls: 'grep' };
    case 'revert': return { icon: '↩️', label: 'revert', verb: 'Reverted', cls: 'revert' };
    default: return { icon: '🛠️', label: name, verb: 'Done', cls: 'default' };
  }
}

function toggleAllCodex(btn) {
  const container = btn.closest('.codex-container');
  if (!container) return;
  const cards = container.querySelectorAll('.codex-action-card, .codex-thought-card');
  const isExpanding = btn.textContent.includes('Expand');
  cards.forEach(c => { c.open = isExpanding; });
  btn.textContent = isExpanding ? '⤡ Collapse All' : '⤢ Expand All';
}
const toggleAllSteps = toggleAllCodex;

function copyCodexCode(btn) {
  const container = btn.closest('.codex-action-body') || btn.closest('.codex-thought-card') || btn.parentElement;
  if (!container) return;
  let target = btn.parentElement ? btn.parentElement.nextElementSibling : null;
  if (!target || target.tagName !== 'PRE') {
    target = container.querySelector('.codex-code-content') || container.querySelector('.codex-code-block') || container.querySelector('.codex-thought-output') || container.querySelector('.codex-result-block');
  }
  if (!target) return;
  const text = target.innerText || target.textContent;
  navigator.clipboard.writeText(text).then(() => {
    const orig = btn.textContent;
    btn.textContent = '✓ Copied!';
    setTimeout(() => { btn.textContent = orig; }, 1500);
  });
}
const copyToolResult = copyCodexCode;

function agentActsHtml(acts) {
  if (!acts || !acts.length) return '';
  const steps = parseStepsFromActs(acts);
  if (!steps.length) return '';

  const allTools = [];
  const thoughts = [];
  steps.forEach(s => {
    if (s.thought && s.thought.trim()) thoughts.push(s.thought.trim());
    s.tools.forEach(t => allTools.push({
      ...t,
      step: s.step,
      lane: s.lane,
      thought: s.thought ? s.thought.trim() : ''
    }));
  });

  if (allTools.length === 0 && thoughts.length === 0) return '';

  const totalOps = allTools.length;
  const isAllDone = totalOps > 0 && allTools.every(t => t.result !== null);
  const hasFailed = allTools.some(t => t.result !== null && !t.ok);

  // Count files & searches for header summary like: "Exploring 14 files, 3 searches"
  const fileOps = allTools.filter(t => ['read_file', 'write_file', 'edit_file'].includes(t.name));
  const webOps = allTools.filter(t => ['web_search', 'web_fetch'].includes(t.name));
  const searchOps = allTools.filter(t => ['grep', 'list_files'].includes(t.name));
  const otherOps = allTools.filter(t => !['read_file', 'write_file', 'edit_file', 'grep', 'list_files', 'web_search', 'web_fetch'].includes(t.name));

  const summaryParts = [];
  if (webOps.length > 0) summaryParts.push(`${webOps.length} web search${webOps.length !== 1 ? 'es' : ''}`);
  if (fileOps.length > 0) summaryParts.push(`${fileOps.length} file${fileOps.length !== 1 ? 's' : ''}`);
  if (searchOps.length > 0) summaryParts.push(`${searchOps.length} search${searchOps.length !== 1 ? 'es' : ''}`);
  if (otherOps.length > 0) summaryParts.push(`${otherOps.length} action${otherOps.length !== 1 ? 's' : ''}`);
  if (summaryParts.length === 0) summaryParts.push(`${totalOps} step${totalOps !== 1 ? 's' : ''}`);

  const summaryTitle = isAllDone ? `Completed ${summaryParts.join(', ')}` : `Running ${summaryParts.join(', ')}`;

  let h = '<div class="agy-agent-container">';
  h += `<details class="agy-agent-drawer" open>
    <summary class="agy-agent-summary">
      <div class="agy-summary-left">
        <span class="agy-summary-pulse ${hasFailed ? 'err' : (isAllDone ? 'done' : 'active')}"></span>
        <span>${esc(summaryTitle)}</span>
      </div>
      <span class="agy-summary-chevron">▼</span>
    </summary>
    <div class="agy-steps-list">`;

  // Render individual action items in the Antigravity list format
  allTools.forEach(t => {
    let p = t.args.path || t.args.file || t.args.filename;
    if (!p && t.args.raw) {
      const m = t.args.raw.match(/"(?:path|file|filename)"\s*:\s*"([^"]+)"/);
      if (m) p = m[1];
    }

    let verb = 'Analyzed';
    let iconClass = 'file';
    let iconSymbol = '📄';
    let label = esc(p || t.name);
    let extra = '';

    if (t.name === 'read_file') {
      verb = 'Analyzed';
      iconClass = 'python';
      iconSymbol = p && p.endsWith('.py') ? '🐍' : '📄';
      if (t.args.start_line != null && t.args.end_line != null) {
        extra = `<span class="agy-step-lines">#L${t.args.start_line}-${t.args.end_line}</span>`;
      }
    } else if (t.name === 'write_file') {
      verb = 'Created';
      iconClass = 'edit';
      iconSymbol = '💾';
      if (typeof t.args.content === 'string') {
        const lines = t.args.content.split('\n').length;
        extra = `<span class="agy-step-lines">(${lines} lines)</span>`;
      }
    } else if (t.name === 'edit_file') {
      verb = 'Edited';
      iconClass = 'edit';
      iconSymbol = '✏️';
    } else if (t.name === 'grep') {
      verb = 'Searched';
      iconClass = 'search';
      iconSymbol = '🔍';
      label = esc(t.args.query || t.args.pattern || 'pattern');
      if (t.result) {
        const matches = (t.result.match(/\\n/g) || []).length + 1;
        extra = `<span class="agy-step-count">${matches} result${matches !== 1 ? 's' : ''}</span>`;
      }
    } else if (t.name === 'list_files') {
      verb = 'Listed';
      iconClass = 'file';
      iconSymbol = '📁';
      label = esc(t.args.path || 'workspace');
    } else if (t.name === 'run_python') {
      verb = 'Executed';
      iconClass = 'python';
      iconSymbol = '⚡';
      label = esc(t.args.file || (t.args.code ? t.args.code.slice(0, 30) + '…' : 'python code'));
    } else if (t.name === 'web_search') {
      verb = 'Searched web';
      iconClass = 'search';
      iconSymbol = '🌐';
      label = esc(t.args.query || t.args.q || 'web query');
      if (t.result) {
        const matches = (t.result.match(/https?:\/\//g) || []).length;
        if (matches > 0) extra = `<span class="agy-step-count">${matches} source${matches !== 1 ? 's' : ''}</span>`;
      }
    } else if (t.name === 'web_fetch') {
      verb = 'Fetched page';
      iconClass = 'file';
      iconSymbol = '🔗';
      label = esc(t.args.url || 'web page');
    }

    const isRunning = t.result === null;

    h += `<details class="agy-step-detail">
      <summary class="agy-step-row">
        <span class="agy-step-verb">${verb}</span>
        <span class="agy-step-icon ${iconClass}">${iconSymbol}</span>
        <span class="agy-step-file">${label}</span>
        ${extra}
        <span class="agy-step-spacer"></span>
        ${isRunning ? '<span class="agy-summary-pulse active" style="width:6px; height:6px;"></span>' : (t.ok ? '' : '<span style="color:var(--red); font-size:11px;">⚠</span>')}
      </summary>
      <div class="agy-detail-body">
        <div class="agy-detail-bar">
          <span>${esc(t.name)} ${p ? '· ' + esc(p) : ''}</span>
          ${t.model ? `<span style="font-family:monospace; opacity:0.8;">${esc(t.model)}</span>` : ''}
        </div>`;

    if (t.thought) {
      h += `<div style="font-size:11px; color:var(--dim); margin-bottom:6px; font-style:italic;">💭 ${esc(t.thought)}</div>`;
    }

    if (t.name === 'write_file' && typeof t.args.content === 'string') {
      h += `<pre class="agy-detail-code"><code>${esc(t.args.content)}</code></pre>`;
    } else if (t.name === 'edit_file' && (t.args.old_string || t.args.new_string)) {
      h += `<div class="agy-detail-code">
        <div style="color:#fca5a5;">- ${esc(t.args.old_string || '')}</div>
        <div style="color:#86efac;">+ ${esc(t.args.new_string || '')}</div>
      </div>`;
    } else if (t.args && Object.keys(t.args).length > 0) {
      h += `<pre class="agy-detail-code"><code>${esc(JSON.stringify(t.args, null, 2))}</code></pre>`;
    }

    if (t.result !== null) {
      h += `<div style="font-size:10px; font-weight:700; color:var(--dim); margin:6px 0 4px; text-transform:uppercase;">Result</div>
      <pre class="agy-detail-code" style="color:${t.ok ? 'var(--dim)' : 'var(--red)'};"><code>${esc(t.result || '(empty)')}</code></pre>`;
    }

    h += `</div></details>`;
  });

  if (!isAllDone) {
    h += `<div class="agy-working-bar"><span class="agy-summary-pulse active" style="width:6px; height:6px;"></span> Working…</div>`;
  }

  h += `</div></details></div>`;
  return h;
}

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
  ensureSession((text || (sentFiles ? `📎 ${sentFiles}` : 'Agent task')).slice(0, 60)).then(() => persistMsg('user', fullPrompt, userMeta));
  const last = () => messages[messages.length - 1];
  (async () => {
    let usage = null;
    const t0 = performance.now();
    try {
      const hist = messages.slice(0, -1).map(m => ({ role: m.role, content: m.content }));
      const engineMode = $('agent-engine') ? $('agent-engine').value : 'tiered';
      const res = await fetch('/agent/run', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          messages: hist,
          mode: engineMode,
          plan: planMode,
          temperature: parseFloat($('temp').value),
          max_tokens: parseInt($('maxtok').value) > 0 ? parseInt($('maxtok').value) : 4096,
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

/* input handling */
function submitPrompt() {
  const input = $('input');
  const text = input ? input.value.trim() : '';
  const hasFiles = attachments && attachments.some(a => a.content != null || (a.isImage && a.b64));
  if ((!text && !hasFiles) || generating) return;
  if (agentMode) runAgentSSE(text); else send(text);
}
$('input').addEventListener('keydown', e => {
  if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); submitPrompt(); }
});
// snap cleared config fields back to defaults
const CFG_INPUT_DEFAULTS = {
  'cfg-ctx': () => CFG_DEFAULTS.ctx, 'cfg-ngl': () => CFG_DEFAULTS.ngl,
  'cfg-threads': () => CFG_DEFAULTS.threads, 'cfg-tb': () => CFG_DEFAULTS.tb,
  'cfg-batch': () => CFG_DEFAULTS.batch, 'cfg-ubatch': () => CFG_DEFAULTS.ubatch,
  'cfg-np': () => CFG_DEFAULTS.np, 'cfg-kai': () => CFG_DEFAULTS.kai,
  'cfg-ts': () => CFG_DEFAULTS.ts,
};
Object.keys(CFG_INPUT_DEFAULTS).forEach(id => {
  $(id).addEventListener('blur', () => {
    if ($(id).value.trim() === '') $(id).value = CFG_INPUT_DEFAULTS[id]();
  });
});
$('btn-send').onclick = submitPrompt;
$('btn-abort').onclick = () => { if (ctrl) ctrl.abort(); };
const btnWebToggle = $('btn-web-toggle');
if (btnWebToggle) {
  btnWebToggle.onclick = () => {
    chatWebSearch = !chatWebSearch;
    try { localStorage.setItem('chat_web_search', chatWebSearch ? '1' : '0'); } catch (e) {}
    updateWebToggleUI();
    toast(chatWebSearch ? '🌐 Web search enabled for chat' : '🌐 Web search disabled (offline mode)');
  };
  updateWebToggleUI();
}
$('temp').oninput = e => { $('tempv').textContent = parseFloat(e.target.value).toFixed(2); };
$('topp').oninput = e => { $('toppv').textContent = parseFloat(e.target.value).toFixed(2); };
$('minp').oninput = e => { $('minpv').textContent = parseFloat(e.target.value).toFixed(3); };
$('rep').oninput = e => { $('repv').textContent = parseFloat(e.target.value).toFixed(2); };
$('presence').oninput = e => { $('presencev').textContent = parseFloat(e.target.value).toFixed(2); };

/* image preview lightbox modal */
function openImageModal(src, title = 'Image attachment') {
  const m = $('img-modal');
  const img = $('img-full');
  const t = $('img-title');
  const dl = $('img-dl');
  if (!m || !img || !src) return;
  img.src = src;
  if (t) t.textContent = title;
  if (dl) dl.href = src;
  m.hidden = false;
  m.removeAttribute('hidden');
  m.style.display = 'flex';
}

function closeImageModal() {
  const m = $('img-modal');
  if (m) {
    m.hidden = true;
    m.setAttribute('hidden', '');
    m.style.display = 'none';
    const img = $('img-full');
    if (img) img.src = '';
  }
}

// Global click handler to expand images on popup
document.addEventListener('click', e => {
  const img = e.target.closest('.msg .bubble img, .attach-preview-bar .attach-card img, .chat-img-thumb');
  if (img && !e.target.closest('.attach-card-remove')) {
    e.stopPropagation();
    openImageModal(img.src, img.alt || img.title || 'Image preview');
  }
});

const imgModal = $('img-modal');
if (imgModal) {
  imgModal.addEventListener('click', e => {
    if (e.target.id === 'img-modal' || e.target.id === 'img-body') {
      closeImageModal();
    }
  });
}
const imgClose = $('img-close');
if (imgClose) {
  imgClose.addEventListener('click', () => closeImageModal());
}

window.addEventListener('keydown', e => {
  if (e.key === 'Escape') {
    closeImageModal();
    const sd = $('settings-drawer');
    if (sd) sd.classList.remove('open');
    const md = $('monitor-drawer');
    if (md) md.classList.remove('open');
    const pm = $('proj-modal');
    if (pm) pm.hidden = true;
    const dpm = $('dir-picker-modal');
    if (dpm) dpm.hidden = true;
    const rm = $('report-modal');
    if (rm) rm.hidden = true;
    const dm = $('docs-modal');
    if (dm) dm.hidden = true;
    const mb = $('modal-bg');
    if (mb) mb.hidden = true;
  }
});

/* boot */
loadProfiles();   // loadConfig() runs inside once the dropdown is ready
loadProjects();

// Restore saved mode and agent engine preference across page refreshes
try {
  const savedMode = localStorage.getItem('app_mode');
  if (savedMode === 'agent') {
    setAppMode(true, false);
  } else {
    setAppMode(false, false);
  }
  const savedEngine = localStorage.getItem('agent_engine');
  const engineSel = $('agent-engine');
  if (savedEngine && engineSel) {
    engineSel.value = savedEngine;
  }
  if (engineSel) {
    engineSel.addEventListener('change', () => {
      try { localStorage.setItem('agent_engine', engineSel.value); } catch (e) {}
    });
  }
} catch (e) {}

pollStatus();
pollGpu();
setInterval(pollStatus, 4000);
setInterval(pollGpu, 5000);
