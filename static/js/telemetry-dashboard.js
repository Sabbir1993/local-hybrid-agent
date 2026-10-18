/* ================= telemetry-dashboard.js =================
 * Real-time inference & hardware telemetry dashboard controller
 */

let tmTimer = null;
let tmIsActive = false;

function generateSparklineSvg(values, width = 240, height = 36, color = '#38bdf8') {
  if (!values || values.length < 2) {
    const yMid = height / 2;
    return `
      <svg class="tm-sparkline-svg" viewBox="0 0 ${width} ${height}" preserveAspectRatio="none">
        <line x1="0" y1="${yMid}" x2="${width}" y2="${yMid}" stroke="${color}" stroke-width="1.5" stroke-dasharray="3 3" opacity="0.4" />
      </svg>`;
  }

  const minVal = Math.min(...values);
  const maxVal = Math.max(...values);
  const range = (maxVal - minVal) || 1.0;
  const padding = 3;
  const availHeight = height - (padding * 2);

  const pts = values.map((v, i) => {
    const x = (i / (values.length - 1)) * width;
    const y = height - padding - (((v - minVal) / range) * availHeight);
    return { x: Number(x.toFixed(1)), y: Number(y.toFixed(1)) };
  });

  // Build smooth bezier curve
  let linePath = `M ${pts[0].x},${pts[0].y}`;
  for (let i = 0; i < pts.length - 1; i++) {
    const p0 = pts[i];
    const p1 = pts[i + 1];
    const cx = (p0.x + p1.x) / 2;
    linePath += ` C ${cx},${p0.y} ${cx},${p1.y} ${p1.x},${p1.y}`;
  }

  const lastPt = pts[pts.length - 1];
  const areaPath = `${linePath} L ${lastPt.x},${height} L 0,${height} Z`;

  const gradId = `tm-grad-${Math.random().toString(36).substring(2, 8)}`;

  return `
    <svg class="tm-sparkline-svg" viewBox="0 0 ${width} ${height}" preserveAspectRatio="none">
      <defs>
        <linearGradient id="${gradId}" x1="0" y1="0" x2="0" y2="1">
          <stop offset="0%" stop-color="${color}" stop-opacity="0.35" />
          <stop offset="100%" stop-color="${color}" stop-opacity="0.02" />
        </linearGradient>
      </defs>
      <path d="${areaPath}" fill="url(#${gradId})" />
      <path d="${linePath}" fill="none" stroke="${color}" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" />
    </svg>`;
}

function fmtTokens(n) {
  if (n === null || n === undefined) return '0';
  if (n >= 1000000) return (n / 1000000).toFixed(1) + 'M';
  if (n >= 1000) return (n / 1000).toFixed(1) + 'k';
  return String(n);
}

function updateTelemetryUi(data) {
  if (!data) return;

  // 1. Model State Bar
  const mState = data.model_state || {};
  const currState = (mState.state || 'idle').toLowerCase();
  
  document.querySelectorAll('.tm-state-pill').forEach(pill => {
    pill.classList.remove('active');
  });
  const activePill = document.querySelector(`.tm-state-pill.state-${currState}`);
  if (activePill) activePill.classList.add('active');

  const subEl = document.getElementById('tm-state-subtitle');
  if (subEl) subEl.textContent = mState.desc || 'Waiting for a request';

  const sumEl = document.getElementById('tm-last-summary');
  if (sumEl) sumEl.textContent = mState.last_summary || 'No requests yet';

  // 2. Metric Cards
  const cards = data.cards || {};

  // Speed Card
  if (cards.speed) {
    const dVal = document.getElementById('tm-speed-decode');
    const pVal = document.getElementById('tm-speed-prefill');
    const spWrap = document.getElementById('tm-spark-speed');
    if (dVal) dVal.textContent = (cards.speed.decode_tps || 0).toFixed(1);
    if (pVal) pVal.textContent = Math.round(cards.speed.prefill_tps || 0);
    if (spWrap) spWrap.innerHTML = generateSparklineSvg(cards.speed.history, 240, 36, '#38bdf8');
  }

  // GPU Load Card
  if (cards.gpu_load) {
    const lVal = document.getElementById('tm-gpu-load');
    const nVal = document.getElementById('tm-gpu-name');
    const spWrap = document.getElementById('tm-spark-gpu');
    if (lVal) lVal.textContent = Math.round(cards.gpu_load.pct || 0);
    if (nVal) nVal.textContent = cards.gpu_load.name || 'Discrete GPUs';
    if (spWrap) spWrap.innerHTML = generateSparklineSvg(cards.gpu_load.history, 240, 36, '#38bdf8');
  }

  // VRAM Card
  if (cards.vram) {
    const vVal = document.getElementById('tm-vram-val');
    const dVal = document.getElementById('tm-vram-detail');
    const spWrap = document.getElementById('tm-spark-vram');
    if (vVal) vVal.innerHTML = `${cards.vram.used_gb} <span class="tm-card-unit">/ ${cards.vram.total_gb} GB</span>`;
    if (dVal) dVal.textContent = cards.vram.detail || `${cards.vram.pct}% allocated`;
    if (spWrap) spWrap.innerHTML = generateSparklineSvg(cards.vram.history, 240, 36, '#34d399');
  }

  // GPU Temp Card
  if (cards.gpu_temp) {
    const tVal = document.getElementById('tm-gpu-temp');
    const spWrap = document.getElementById('tm-spark-temp');
    if (tVal) tVal.textContent = Math.round(cards.gpu_temp.temp_c || 45);
    if (spWrap) spWrap.innerHTML = generateSparklineSvg(cards.gpu_temp.history, 240, 36, '#fb923c');
  }

  // Power Card
  if (cards.power) {
    const pVal = document.getElementById('tm-power-val');
    const lVal = document.getElementById('tm-power-lim');
    const spWrap = document.getElementById('tm-spark-power');
    if (pVal) pVal.textContent = Math.round(cards.power.watts || 55);
    if (lVal) lVal.textContent = `of ${Math.round(cards.power.limit_watts || 450)} W limit`;
    if (spWrap) spWrap.innerHTML = generateSparklineSvg(cards.power.history, 240, 36, '#38bdf8');
  }

  // PCIe Card
  if (cards.pcie) {
    const sVal = document.getElementById('tm-pcie-spec');
    const rVal = document.getElementById('tm-pcie-rate');
    const spWrap = document.getElementById('tm-spark-pcie');
    if (sVal) sVal.textContent = cards.pcie.spec || 'Gen4 x16';
    if (rVal) rVal.textContent = `to GPU ${cards.pcie.rate_mb_s} MB/s`;
    if (spWrap) spWrap.innerHTML = generateSparklineSvg(cards.pcie.history, 240, 36, '#38bdf8');
  }

  // CPU Card
  if (cards.cpu) {
    const cVal = document.getElementById('tm-cpu-val');
    const tVal = document.getElementById('tm-cpu-threads');
    const spWrap = document.getElementById('tm-spark-cpu');
    if (cVal) cVal.textContent = Math.round(cards.cpu.pct || 0);
    if (tVal) tVal.textContent = `${cards.cpu.cores} cores · ${cards.cpu.threads} threads`;
    if (spWrap) spWrap.innerHTML = generateSparklineSvg(cards.cpu.history, 240, 36, '#2dd4bf');
  }

  // Disk Card
  if (cards.disk) {
    const rVal = document.getElementById('tm-disk-read');
    const wVal = document.getElementById('tm-disk-write');
    const spWrap = document.getElementById('tm-spark-disk');
    if (rVal) rVal.textContent = (cards.disk.read_mb_s || 0).toFixed(1);
    if (wVal) wVal.textContent = `write ${(cards.disk.write_mb_s || 0).toFixed(1)} MB/s`;
    if (spWrap) spWrap.innerHTML = generateSparklineSvg(cards.disk.history, 240, 36, '#60a5fa');
  }

  // 3. Hardware & Context Allocation Bars
  const ctx = data.context || {};
  const ctxPct = Math.min(100, Math.max(0, ctx.pct || 0));
  const ctxBar = document.getElementById('tm-bar-ctx');
  const ctxVal = document.getElementById('tm-bar-ctx-val');
  if (ctxBar && ctxVal) {
    ctxBar.style.width = `${ctxPct}%`;
    ctxVal.textContent = `${fmtTokens(ctx.n_past || 0)} / ${fmtTokens(ctx.n_ctx || 32768)} (${ctxPct}%)`;
  }

  // Hardware Bars
  const hw = data.hardware || {};
  const vramBar = document.getElementById('tm-bar-vram');
  const vramVal = document.getElementById('tm-bar-vram-val');
  if (vramBar && vramVal) {
    const vramPct = Math.min(100, (hw.used_vram_gb / Math.max(1, hw.total_vram_gb)) * 100);
    vramBar.style.width = `${vramPct}%`;
    vramVal.textContent = `${hw.used_vram_gb} / ${hw.total_vram_gb} GB`;
  }

  const ramBar = document.getElementById('tm-bar-ram');
  const ramVal = document.getElementById('tm-bar-ram-val');
  if (ramBar && ramVal) {
    ramBar.style.width = `${hw.system_ram_pct}%`;
    ramVal.textContent = `${hw.system_ram_used_gb} / ${hw.system_ram_total_gb} GB`;
  }

  const tempBar = document.getElementById('tm-bar-temp');
  const tempVal = document.getElementById('tm-bar-temp-val');
  if (tempBar && tempVal) {
    const tempPct = Math.min(100, (hw.gpu_temp_c / 100) * 100);
    tempBar.style.width = `${tempPct}%`;
    tempVal.textContent = `${Math.round(hw.gpu_temp_c)} °C`;
  }

  // 4. Recent Requests Table
  const tbody = document.getElementById('tm-recent-tbody');
  const requests = data.recent_requests || [];
  if (tbody) {
    if (!requests.length) {
      tbody.innerHTML = `<tr><td colspan="9" style="text-align:center; padding:18px; color:var(--dim);">No recent requests recorded</td></tr>`;
    } else {
      tbody.innerHTML = requests.map(r => {
        const statusClass = r.status.toLowerCase().startsWith('err') ? 'error' : 'done';
        return `
          <tr>
            <td>${r.time}</td>
            <td><span class="tm-req-status ${statusClass}">● ${r.status}</span></td>
            <td>${(r.prompt || 0).toLocaleString()}</td>
            <td>${(r.reused || 0).toLocaleString()}</td>
            <td>${(r.output || 0).toLocaleString()}</td>
            <td>${r.prefill_tps > 0 ? r.prefill_tps : '—'}</td>
            <td>${r.decode_tps > 0 ? r.decode_tps : '—'}</td>
            <td>${r.vram_hit_rate > 0 ? r.vram_hit_rate + '%' : '0.0%'}</td>
            <td>${r.duration}s</td>
          </tr>`;
      }).join('');
    }
  }

  // Table footer summary
  const summaryEl = document.getElementById('tm-table-summary');
  const cs = data.cache_summary || {};
  if (summaryEl) {
    if (cs.total_count > 0) {
      summaryEl.textContent = `Recent window: ${cs.total_count} requests · ${(cs.total_prompt || 0).toLocaleString()} prompt tokens read at ${cs.avg_prefill_tps} tok/s (${(cs.total_reused || 0).toLocaleString()} reused) · ${(cs.total_output || 0).toLocaleString()} written at ${cs.avg_decode_tps} tok/s`;
    } else {
      summaryEl.textContent = `Waiting for inference traffic…`;
    }
  }

  // 5. Conversation Cache Stats
  const cacheTag = document.getElementById('tm-cache-tag');
  const cacheLast = document.getElementById('tm-cache-last');
  const cacheTotal = document.getElementById('tm-cache-total');
  if (cacheTag) cacheTag.textContent = `${cs.reused_count || 0} of ${cs.total_count || 0} requests reused part of their prompt`;
  if (cacheLast) cacheLast.textContent = `${(cs.last_reused || 0).toLocaleString()} of ${(cs.last_prompt || 0).toLocaleString()} prompt tokens reused`;
  if (cacheTotal) cacheTotal.textContent = `${(cs.total_reused || 0).toLocaleString()} tokens (${cs.cache_percentage || 0}% of all prompt tokens)`;
}

let tmSocket = null;
let tmHttpFallbackTimer = null;
let tmReconnectTimer = null;

function isTelemetryPermitted() {
  if (window.__user && window.__user.is_super_admin) return true;
  if (typeof window.hasPerm === 'function') {
    return window.hasPerm('monitor.view');
  }
  return false;
}

async function pollTelemetryOnce() {
  if (!tmIsActive) return;

  // Only call if user is logged in and permitted
  if (window.__sessionReady) {
    try {
      const session = await window.__sessionReady;
      if (!session || !isTelemetryPermitted()) {
        stopTelemetryMonitoring();
        return;
      }
    } catch (_) {
      stopTelemetryMonitoring();
      return;
    }
  } else if (!isTelemetryPermitted()) {
    stopTelemetryMonitoring();
    return;
  }

  // Only poll if panel is present and active
  const panel = document.getElementById('sec-live-monitor');
  if (!panel || !panel.classList.contains('active')) {
    stopTelemetryMonitoring();
    return;
  }

  try {
    const res = await fetch('/control/telemetry/live', { background: true });
    if (res.status === 401 || res.status === 403) {
      stopTelemetryMonitoring();
      return;
    }
    if (res.ok) {
      const data = await res.json();
      updateTelemetryUi(data);
    }
  } catch (err) {
    // server cycling or unreachable
  }
}

function startHttpFallback() {
  if (!tmIsActive || tmHttpFallbackTimer) return;
  pollTelemetryOnce();
  tmHttpFallbackTimer = setInterval(pollTelemetryOnce, 1000);
}

function stopHttpFallback() {
  if (tmHttpFallbackTimer) {
    clearInterval(tmHttpFallbackTimer);
    tmHttpFallbackTimer = null;
  }
}

function connectTelemetryWebSocket() {
  if (!tmIsActive) return;

  if (typeof window.hasPerm === 'function' && !isTelemetryPermitted()) {
    stopTelemetryMonitoring();
    return;
  }

  const panel = document.getElementById('sec-live-monitor');
  if (!panel || !panel.classList.contains('active')) {
    stopTelemetryMonitoring();
    return;
  }

  const wsProto = location.protocol === 'https:' ? 'wss:' : 'ws:';
  const wsUrl = `${wsProto}//${location.host}/ws/telemetry`;

  try {
    tmSocket = new WebSocket(wsUrl);

    tmSocket.onopen = () => {
      stopHttpFallback();
      if (tmReconnectTimer) {
        clearTimeout(tmReconnectTimer);
        tmReconnectTimer = null;
      }
    };

    tmSocket.onmessage = (event) => {
      try {
        const data = JSON.parse(event.data);
        updateTelemetryUi(data);
      } catch (_) {}
    };

    tmSocket.onerror = () => {
      if (tmIsActive) startHttpFallback();
    };

    tmSocket.onclose = (event) => {
      tmSocket = null;
      if (event.code === 4401 || event.code === 4403) {
        stopTelemetryMonitoring();
        return;
      }
      if (tmIsActive) {
        startHttpFallback();
        if (!tmReconnectTimer) {
          tmReconnectTimer = setTimeout(() => {
            tmReconnectTimer = null;
            if (tmIsActive) connectTelemetryWebSocket();
          }, 2000);
        }
      }
    };
  } catch (err) {
    startHttpFallback();
  }
}

function startTelemetryMonitoring() {
  if (tmIsActive) return;
  if (typeof window.hasPerm === 'function' && !isTelemetryPermitted()) {
    return;
  }
  tmIsActive = true;
  connectTelemetryWebSocket();
}

function stopTelemetryMonitoring() {
  tmIsActive = false;
  if (tmReconnectTimer) {
    clearTimeout(tmReconnectTimer);
    tmReconnectTimer = null;
  }
  if (tmSocket) {
    try {
      tmSocket.onopen = null;
      tmSocket.onmessage = null;
      tmSocket.onerror = null;
      tmSocket.onclose = null;
      tmSocket.close();
    } catch (_) {}
    tmSocket = null;
  }
  stopHttpFallback();
}

window.isTelemetryPermitted = isTelemetryPermitted;
window.startTelemetryMonitoring = startTelemetryMonitoring;
window.stopTelemetryMonitoring = stopTelemetryMonitoring;
