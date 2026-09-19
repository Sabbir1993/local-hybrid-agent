/* ---------------- input handling & boot ---------------- */
function submitPrompt() {
  if (generating) return;
  const input = $('input');
  const text = input ? input.value.trim() : '';
  const hasFiles = attachments && attachments.some(a => a.content != null || (a.isImage && a.b64));

  // Check if a slash command is armed (e.g. <compact>)
  if (window.getArmedCmd && window.getArmedCmd()) {
    const cmd = window.getArmedCmd();
    if (cmd.name === 'compact') {
      if (input) input.value = '';
      window.runArmedCmd(cmd, text);
      return;
    }
    if (!text && !hasFiles) return;
    if (input) input.value = '';
    window.runArmedCmd(cmd, text);
    return;
  }

  if (!text && !hasFiles) return;

  // /compact [extra instructions] — context compaction (both chat & agent mode)
  if (/^\/compact(\s|$)/i.test(text)) {
    if (input) input.value = '';
    doCompact(text.replace(/^\/compact\s*/i, '').trim());
    return;
  }

  // /plan and /build typed directly — Plan/Build mode switches (agent mode)
  if (/^\/(plan|build)(\s|$)/i.test(text)) {
    if (agentMode && (!curProject || !curProject.id)) { flashProjectsCard(); return; }
    if (window._setPlanMode) window._setPlanMode(/^\/plan/i.test(text));
    if (input) input.value = '';
    toast(/^\/plan/i.test(text) ? '📋 Plan mode — explore & propose changes' : '🔨 Build mode — execute changes');
    return;
  }

  // Project gate: In agent mode, require an active project before sending any prompt!
  if (agentMode && (!curProject || !curProject.id)) {
    flashProjectsCard();
    return;
  }

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
    if (img) {
      img.src = '';
      img.style.transform = '';
    }
  }
}

// Ctrl + Wheel Zoom for Image Modal
let imgModalZoom = 1.0;
const imgBody = $('img-body');
if (imgBody) {
  imgBody.addEventListener('wheel', e => {
    if (e.ctrlKey) {
      e.preventDefault();
      const img = $('img-full');
      if (!img) return;
      const delta = e.deltaY < 0 ? 0.15 : -0.15;
      imgModalZoom = Math.min(4.0, Math.max(0.4, imgModalZoom + delta));
      img.style.transform = `scale(${imgModalZoom})`;
      img.style.transformOrigin = 'center center';
      img.style.transition = 'transform 0.12s ease';
    }
  }, { passive: false });
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

const prevModal = $('preview-modal');
if (prevModal) {
  prevModal.addEventListener('click', e => {
    if (e.target.id === 'preview-modal') {
      if (typeof closeFilePreview === 'function') closeFilePreview();
    }
  });
}
const prevClose = $('preview-close');
if (prevClose) {
  prevClose.addEventListener('click', () => {
    if (typeof closeFilePreview === 'function') closeFilePreview();
  });
}

window.addEventListener('keydown', e => {
  if (e.key === 'Escape') {
    closeImageModal();
    if (typeof closeFilePreview === 'function') closeFilePreview();
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
loadProjects(true);

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
