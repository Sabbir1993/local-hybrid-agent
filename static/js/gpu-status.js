/* ---------------- status / gpu polling ---------------- */
function setPill(mode, s) {
  const p = $('pill'), txt = $('pill-text');
  p.className = 'pill ' + mode;
  if (typeof setLoadBtn === 'function') setLoadBtn(mode);
  if (mode === 'on') txt.textContent = 'Loaded · ' + fmtUptime(s.uptime_s);
  else if (mode === 'cloud') txt.textContent = '';  // just the green dot below
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
    const cloudMain = s.cloud_main || null;
    // cloud main lane: report ☁️ instead of a misleading "Model unloaded"
    setPill(cloudMain ? 'cloud' : (s.pid ? 'on' : 'off'), s);
    const ka = $('ka');
    if (!ka._user) ka.checked = !!s.keepalive;

    const sel = $('profile');
    const selName = (sel && sel.value) ? sel.value.split('\\').pop().split('/').pop() : 'Model';
    const m = s.model ? s.model.split('\\').pop().split('/').pop() : selName;

    if (cloudMain) {
      $('empty-model').textContent =
        `${cloudMain.display}  ·  via ${cloudMain.provider}  ·  cloud (no VRAM)`;
      const ec = document.querySelector('.empty-card');
      if (ec) {
        ec.innerHTML = `<p><b>${cloudMain.display} is ready!</b></p><p class="dim" style="margin-top:6px;">Main lane is served by <b>${cloudMain.provider}</b> in the cloud — both Arc A770s stay free. Type your message below and press Enter.</p>`;
      }
    } else if (s.model && s.pid) {
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

    // keep profile dropdown in sync if switched elsewhere (never for a cloud
    // selection — that one is owned by the main-lane binding, not by s.profile)
    const cloudSel = (typeof isCloudValue === 'function') && isCloudValue(sel && sel.value);
    if (!cloudSel && s.profile && sel.value && !sel._user) {
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

  if (Array.isArray(ctxMsgs) && ctxMsgs.length > 0) {
    for (const m of ctxMsgs) {
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
  cChip.title = `Session Tokens: ${totalToks.toLocaleString()} / ${nCtx.toLocaleString()} tokens used (${pct}%) · Prompt: ${promptToks.toLocaleString()} · Completion: ${compToks.toLocaleString()} · ${ctxMsgs.length} messages`;

  if (pct > 85) cChip.style.color = 'var(--red)';
  else if (pct > 70) cChip.style.color = 'var(--amber)';
  else cChip.style.color = 'var(--dim)';
}
