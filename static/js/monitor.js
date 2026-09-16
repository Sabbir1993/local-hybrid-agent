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
