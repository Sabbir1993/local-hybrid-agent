/* ---------------- input handling & boot ---------------- */
function submitPrompt() {
  const isCurBusy = curSession ? (window.bgJobs && window.bgJobs.has(String(curSession.id))) : generating;
  if (isCurBusy) {
    toast('Current session is already generating (stop it with ■ or switch to another chat)', true);
    return;
  }
  const input = $('input');
  const text = input ? input.value.trim() : '';
  const hasFiles = attachments && attachments.some(a => a.content != null || (a.isImage && a.b64));

  // Check if a slash command is armed (e.g. <compact>)
  if (window.getArmedCmd && window.getArmedCmd()) {
    const cmd = window.getArmedCmd();
    // commands whose argument is optional run on a bare Enter
    // (/init scans the whole project; /plan and /build just switch mode)
    if (['compact', 'init', 'plan', 'build'].includes(cmd.name)) {
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
    if (window.renderInputHighlights) window.renderInputHighlights();
    doCompact(text.replace(/^\/compact\s*/i, '').trim());
    return;
  }

  // /init [focus] — scan the project and write AGENTS.md (agent mode)
  if (/^\/init(\s|$)/i.test(text)) {
    if (input) input.value = '';
    if (window.renderInputHighlights) window.renderInputHighlights();
    if (window.runInit) window.runInit(text.replace(/^\/init\s*/i, '').trim());
    return;
  }

  // /plan and /build typed directly — Plan/Build mode switches (agent mode)
  if (/^\/(plan|build)(\s|$)/i.test(text)) {
    if (agentMode && (!curProject || !curProject.id)) { flashProjectsCard(); return; }
    if (window._setPlanMode) window._setPlanMode(/^\/plan/i.test(text));
    const rest = text.replace(/^\/(plan|build)\s*/i, '').trim();
    if (!rest) {
      if (input) input.value = '';
      if (window.renderInputHighlights) window.renderInputHighlights();
      toast(/^\/plan/i.test(text) ? '📋 Plan mode — explore & propose changes' : '🔨 Build mode — execute changes');
      return;
    }
  }

  // /<library-command> [args] — Agent Library prompt command (agent mode)
  const libM = agentMode && text.match(/^\/([a-zA-Z0-9_\-]+)(?:\s+([\s\S]*))?$/);
  if (libM && window.isLibraryCommand && window.isLibraryCommand(libM[1])) {
    if (input) input.value = '';
    if (window.renderInputHighlights) window.renderInputHighlights();
    window.runLibraryCommand(libM[1], (libM[2] || '').trim());
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
  if (!$(id)) return;
  $(id).addEventListener('blur', () => {
    if ($(id).value.trim() === '') $(id).value = CFG_INPUT_DEFAULTS[id]();
  });
});
$('btn-send').onclick = submitPrompt;
$('btn-abort').onclick = () => {
  if (curSession && window.bgJobs && window.bgJobs.has(String(curSession.id))) {
    abortSessionJob(curSession.id);
  } else if (ctrl) {
    ctrl.abort();
  }
};
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
    const sm = $('settings-modal');
    if (sm) closeSettingsModal();
    const mb = $('modal-bg');
    if (mb) mb.hidden = true;
  }
});

/* settings in-page modal to keep background tasks alive without page reload */
function openSettingsModal() {
  const m = $('settings-modal');
  const frame = $('settings-frame');
  if (m) {
    m.hidden = false;
    m.removeAttribute('hidden');
    m.style.display = 'flex';
    if (frame && (!frame.src || frame.src === 'about:blank' || frame.src.endsWith('/'))) {
      frame.src = '/settings';
    }
  } else {
    location.href = '/settings';
  }
}

function closeSettingsModal() {
  const m = $('settings-modal');
  if (m) {
    m.hidden = true;
    m.setAttribute('hidden', '');
    m.style.display = 'none';
    if (typeof loadConfig === 'function') loadConfig();
    if (typeof loadProfiles === 'function') loadProfiles();
  }
}
window.openSettingsModal = openSettingsModal;
window.closeSettingsModal = closeSettingsModal;

/* header buttons */
if ($('btn-theme')) $('btn-theme').onclick = () => cycleTheme();
if ($('btn-chat-cfg')) $('btn-chat-cfg').onclick = () => setSettings();
if ($('settings-close')) $('settings-close').onclick = () => setSettings(false);
if ($('btn-settings')) $('btn-settings').onclick = () => openSettingsModal();
if ($('settings-modal-close')) $('settings-modal-close').onclick = () => closeSettingsModal();

/* boot */
try {
  const isNative = typeof isNativeAppClient === 'function' ? isNativeAppClient() : false;
  const savedMode = localStorage.getItem('app_mode');
  setAppMode(isNative && savedMode === 'agent', false);
} catch (_) {}
loadProfiles();   // loadConfig() runs inside once the dropdown is ready
loadProjects(true);

async function checkCompanionStatus() {
  let connected = false, hostname = null;
  try {
    const d = await (await fetch('/control/companion/status')).json();
    connected = !!d.connected;
    hostname = d.hostname;
  } catch (e) {}
  updateAgentModeAvailability(connected, hostname);
  return connected;
}

/* Populate + manage the secondary cloud model selector (#cloud-model-sel).
   Shown when the engine mode needs a cloud executor lane (e.g. Main Local · Rest Cloud). */
async function _populateCloudModelSel() {
  const sel = $('cloud-model-sel');
  if (!sel) return;
  try {
    const res = await fetch('/control/profiles');
    if (!res.ok) return;
    const data = await res.json();
    const models = (data.cloud || []);
    const prev = sel.value || localStorage.getItem('cloud_model_override') || '';
    sel.innerHTML = '<option value="">☁ Pick cloud model…</option>' +
      models.map(m => `<option value="${m.key}">${m.display || m.name} (${m.provider_name || m.provider || ''})</option>`).join('');
    if (prev && [...sel.options].some(o => o.value === prev)) sel.value = prev;
  } catch (e) {}
}

function _updateCloudModelSelVisibility() {
  const engineSel = $('agent-engine');
  const cloudSel = $('cloud-model-sel');
  if (!engineSel || !cloudSel) return;
  const mode = engineSel.value;
  // Show the cloud model picker when there is a cloud executor lane
  const needsCloudExec = (mode === 'main-local-rest-cloud');
  cloudSel.style.display = (needsCloudExec && agentMode) ? 'inline-block' : 'none';
}

(async () => {
  await checkCompanionStatus();

  // Restore saved agent engine preference across page refreshes
  try {
    const savedEngine = localStorage.getItem('agent_engine');
    const engineSel = $('agent-engine');
    if (savedEngine && engineSel) {
      // legacy values: main -> all-local, tiered -> main-local-rest-cloud, all-cloud -> no-orchestration
      const legacy = {
        main: 'all-local',
        tiered: 'main-local-rest-cloud',
        agent: 'all-local',
        'all-cloud': 'no-orchestration',
      };
      engineSel.value = legacy[savedEngine] || savedEngine;
      if (!engineSel.value) engineSel.value = 'all-local';
    }
    if (engineSel) {
      engineSel.addEventListener('change', () => {
        try { localStorage.setItem('agent_engine', engineSel.value); } catch (e) {}
        _updateCloudModelSelVisibility();
      });
    }
  } catch (e) {}

  // Cloud model selector
  const cloudSel = $('cloud-model-sel');
  if (cloudSel) {
    try {
      const savedOverride = localStorage.getItem('cloud_model_override');
      if (savedOverride) cloudSel.value = savedOverride;
    } catch (e) {}
    cloudSel.addEventListener('change', () => {
      const val = cloudSel.value;
      try { localStorage.setItem('cloud_model_override', val); } catch (e) {}
      if (val) {
        // Also persist to backend user lane bindings so executor & vision lanes use this cloud model
        fetch('/control/cloud/lanes', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({
            set: { executor: val, vision: val },
            routing_mode: 'custom'
          })
        }).catch(() => {});
      }
    });
    await _populateCloudModelSel();
    _updateCloudModelSelVisibility();
  }
})();

// The chat page has no GPU panel: the logo's hardware tag comes from
// /control/status. /control/gpu needs settings.runtime.view, so polling it here
// 403'd every 5s for other users and flooded the audit log with denials.
pollStatus();
setInterval(pollStatus, 4000);
