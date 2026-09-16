/* ---------------- input handling & boot ---------------- */
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
