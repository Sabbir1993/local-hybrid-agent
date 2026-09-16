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
