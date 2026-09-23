/* ---------------- audit log (top-bar menu) ----------------
   Server-side filtered + paginated view of /admin/audit_log (audit.view).
   Filter dropdowns are filled from /admin/audit_log/facets so they only
   offer users/actions/results that actually appear in the trail. */
const _audit = { page: 1, pages: 1, total: 0, timer: null, reqSeq: 0, facetsLoaded: false };

function _auditParams() {
  const p = new URLSearchParams();
  const range = $('af-range').value;
  if (range === 'custom') {
    const from = $('af-from').value, to = $('af-to').value;
    if (from) p.set('since', String(new Date(from).getTime() / 1000));
    if (to) p.set('until', String(new Date(to).getTime() / 1000));
  } else if (range !== 'all') {
    p.set('since', String(Date.now() / 1000 - Number(range) * 86400));
  }
  const user = $('af-user').value;
  if (user !== '') p.set('user_id', user);
  const set = (k, id) => { const v = $(id).value.trim(); if (v) p.set(k, v); };
  set('action', 'af-action');
  set('result', 'af-result');
  set('ip', 'af-ip');
  set('q', 'af-q');
  return p;
}

async function loadAuditFacets() {
  try {
    const r = await fetch('/admin/audit_log/facets');
    if (!r.ok) return;
    const f = await r.json();
    const keep = id => $(id).value;
    const u = keep('af-user'), a = keep('af-action'), res = keep('af-result');
    $('af-user').innerHTML = '<option value="">All users</option>' + (f.users || []).map(x =>
      `<option value="${x.user_id == null ? 0 : x.user_id}">${esc(x.username || 'system')} (${x.n})</option>`).join('');
    // group actions by prefix ("users.manage" -> "users.*") so a whole area can be picked at once
    const prefixes = {};
    (f.actions || []).forEach(x => {
      const dot = x.action.indexOf('.');
      if (dot > 0) { const k = x.action.slice(0, dot); prefixes[k] = (prefixes[k] || 0) + x.n; }
    });
    const prefixOpts = Object.entries(prefixes).filter(([, n]) => n > 0)
      .map(([k, n]) => `<option value="${esc(k)}.*">${esc(k)}.* — all (${n})</option>`).join('');
    $('af-action').innerHTML = '<option value="">All actions</option>' +
      (prefixOpts ? `<optgroup label="Area">${prefixOpts}</optgroup>` : '') +
      '<optgroup label="Action">' + (f.actions || []).map(x =>
        `<option value="${esc(x.action)}">${esc(x.action)} (${x.n})</option>`).join('') + '</optgroup>';
    $('af-result').innerHTML = '<option value="">Any result</option>' + (f.results || []).map(x =>
      `<option value="${esc(x)}">${esc(x)}</option>`).join('');
    $('af-user').value = u; $('af-action').value = a; $('af-result').value = res;
    _audit.facetsLoaded = true;
  } catch (_) { /* dropdowns stay at "All" */ }
}

function _auditDetail(raw) {
  if (!raw) return '';
  try { return JSON.stringify(JSON.parse(raw), null, 2); } catch (_) { return String(raw); }
}

async function loadAuditPage(page) {
  const box = $('audit-content');
  const seq = ++_audit.reqSeq;
  const p = _auditParams();
  p.set('page', String(page || 1));
  p.set('page_size', $('af-size').value);
  box.classList.add('loading');
  try {
    const r = await fetch('/admin/audit_log?' + p.toString());
    if (seq !== _audit.reqSeq) return; // a newer filter change already fired
    if (!r.ok) {
      box.innerHTML = '<div class="mon-empty">You do not have access to the audit log.</div>';
      return;
    }
    const d = await r.json();
    _audit.page = d.page;
    _audit.total = d.total;
    _audit.pages = Math.max(1, Math.ceil(d.total / d.page_size));
    const rows = d.entries || [];
    if (!rows.length) {
      box.innerHTML = '<div class="mon-empty">No entries match these filters.</div>';
    } else {
      box.innerHTML = `<table class="audit-table">
        <thead><tr><th>Time</th><th>User</th><th>Action</th><th>Resource</th><th>Permission</th><th>Result</th><th>IP</th><th>Detail</th></tr></thead>
        <tbody>${rows.map(e => {
          const det = _auditDetail(e.detail);
          const res = String(e.result || '');
          const cls = res === 'deny' ? 'deny' : (res === 'allow' ? 'allow' : 'other');
          return `<tr class="audit-row${det ? ' has-detail' : ''}" data-id="${e.id}">
            <td class="mono nowrap" title="${esc(new Date(e.ts * 1000).toISOString())}">${esc(new Date((e.ts || 0) * 1000).toLocaleString())}</td>
            <td>${e.username ? esc(e.username) : '<span class="dim">system</span>'}</td>
            <td class="mono">${esc(e.action || '')}</td>
            <td class="audit-trunc" title="${esc(e.resource || '')}">${esc(e.resource || '')}</td>
            <td class="mono dim">${esc(e.permission_key || '')}</td>
            <td><span class="audit-res ${cls}">${esc(res)}</span></td>
            <td class="mono dim nowrap">${esc(e.ip || '')}</td>
            <td class="audit-trunc dim">${det ? esc(det.replace(/\s+/g, ' ')) : ''}</td>
          </tr>${det ? `<tr class="audit-detail" hidden><td colspan="8"><pre>${esc(det)}</pre></td></tr>` : ''}`;
        }).join('')}</tbody></table>`;
    }
    const start = d.total ? (d.page - 1) * d.page_size + 1 : 0;
    const end = Math.min(d.total, d.page * d.page_size);
    $('audit-count').textContent = d.total
      ? `Showing ${start.toLocaleString()}–${end.toLocaleString()} of ${d.total.toLocaleString()}`
      : '0 entries';
    $('ap-label').textContent = `Page ${d.page} / ${_audit.pages}`;
    $('ap-first').disabled = $('ap-prev').disabled = d.page <= 1;
    $('ap-next').disabled = $('ap-last').disabled = d.page >= _audit.pages;
  } catch (e) {
    if (seq === _audit.reqSeq) box.innerHTML = '<div class="mon-empty">Failed to load: ' + esc(e.message) + '</div>';
  } finally {
    if (seq === _audit.reqSeq) box.classList.remove('loading');
  }
}

function _auditRefilter(debounce) {
  clearTimeout(_audit.timer);
  if (debounce) _audit.timer = setTimeout(() => loadAuditPage(1), 300);
  else loadAuditPage(1);
}

function openAuditModal() {
  $('audit-modal').hidden = false;
  loadAuditFacets();
  loadAuditPage(1);
}

function closeAuditModal() { $('audit-modal').hidden = true; }

if ($('btn-audit')) {
  $('btn-audit').onclick = openAuditModal;
  $('audit-close').onclick = closeAuditModal;
  $('audit-modal').addEventListener('click', e => { if (e.target.id === 'audit-modal') closeAuditModal(); });
  document.addEventListener('keydown', e => {
    if (e.key === 'Escape' && !$('audit-modal').hidden) closeAuditModal();
  });

  $('audit-filters').addEventListener('submit', e => { e.preventDefault(); _auditRefilter(false); });
  $('af-range').addEventListener('change', () => {
    const custom = $('af-range').value === 'custom';
    document.querySelectorAll('#audit-filters .af-custom').forEach(el => { el.hidden = !custom; });
    _auditRefilter(false);
  });
  ['af-from', 'af-to', 'af-user', 'af-action', 'af-result', 'af-size'].forEach(id =>
    $(id).addEventListener('change', () => _auditRefilter(false)));
  ['af-ip', 'af-q'].forEach(id => $(id).addEventListener('input', () => _auditRefilter(true)));

  $('af-reset').onclick = () => {
    $('audit-filters').reset();
    document.querySelectorAll('#audit-filters .af-custom').forEach(el => { el.hidden = true; });
    _auditRefilter(false);
  };
  $('af-export').onclick = () => {
    // same filters as the table, all pages (server caps at 50,000 rows)
    window.location.href = '/admin/audit_log/export?' + _auditParams().toString();
  };

  $('ap-first').onclick = () => loadAuditPage(1);
  $('ap-prev').onclick = () => loadAuditPage(Math.max(1, _audit.page - 1));
  $('ap-next').onclick = () => loadAuditPage(Math.min(_audit.pages, _audit.page + 1));
  $('ap-last').onclick = () => loadAuditPage(_audit.pages);

  // click a row to expand its full detail JSON
  $('audit-content').addEventListener('click', e => {
    const row = e.target.closest('.audit-row.has-detail');
    if (!row) return;
    const det = row.nextElementSibling;
    if (det && det.classList.contains('audit-detail')) {
      det.hidden = !det.hidden;
      row.classList.toggle('open', !det.hidden);
    }
  });
}
