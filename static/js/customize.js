/* ---------------- customize.js ----------------
 * Customize page: browse and install skills, connectors and plugins from the
 * reviewed in-repo catalogs (routes/customize.py). Anyone signed in can browse;
 * install / remove needs capabilities.install (checked again server-side).
 */

(function () {
  const KIND_LABEL = { skills: 'skills', connectors: 'connectors', plugins: 'plugins' };
  const CAT_ICON = {
    security: '🛡️', compliance: '⚖️', engineering: '⌨️', operations: '🧰', finance: '💰',
    productivity: '✅', web: '🌐', tickets: '🎫', developer: '🧪', data: '🗃️', general: '🧩', custom: '🔧',
  };
  const S = { kind: 'skills', view: 'discover', q: '', category: null, data: {}, detail: null,
              registry: null, registryQ: '', busy: false };

  const $c = id => document.getElementById(id);
  const e = s => esc(String(s == null ? '' : s));
  const canInstall = () => !!(window.__user && window.__user.is_super_admin) ||
                           (window.hasPerm && window.hasPerm('capabilities.install'));
  const catIcon = c => CAT_ICON[c] || CAT_ICON.general;
  const catLabel = c => (c || 'general').replace(/[-_]/g, ' ').replace(/\b\w/g, m => m.toUpperCase());

  async function api(url, opts) {
    const r = await fetch(url, opts);
    const j = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(j.error || j.detail || ('HTTP ' + r.status));
    return j;
  }

  async function load(kind, force) {
    if (S.data[kind] && !force) return S.data[kind];
    S.data[kind] = await api('/customize/' + kind);
    return S.data[kind];
  }

  /* ---------------- rendering ---------------- */

  function syncBar() {
    document.querySelectorAll('#cz-box .cz-tab').forEach(b => b.classList.toggle('on', b.dataset.kind === S.kind));
    document.querySelectorAll('#cz-box .cz-view').forEach(b => b.classList.toggle('on', b.dataset.view === S.view));
    $c('cz-q').placeholder = 'Search ' + KIND_LABEL[S.kind];
    $c('cz-q').value = S.q;
  }

  function matches(it) {
    if (S.category && it.category !== S.category) return false;
    if (!S.q) return true;
    const q = S.q.toLowerCase();
    return [it.id, it.title, it.description, it.author, it.category].some(v => String(v || '').toLowerCase().includes(q));
  }

  function statusChip(it) {
    if (S.kind === 'connectors' && it.installed) {
      const st = it.status || 'configured';
      const cls = st === 'ready' ? 'ok' : (st === 'error' ? 'bad' : 'mid');
      return `<span class="cz-chip ${cls}">${e(st)}</span>`;
    }
    if (S.kind === 'plugins' && it.installed) {
      if (it.error) return '<span class="cz-chip bad">error</span>';
      if (!it.enabled) return '<span class="cz-chip mid">disabled</span>';
    }
    if (it.modified) return '<span class="cz-chip mid">modified</span>';
    return it.installed ? '<span class="cz-chip ok">installed</span>' : '';
  }

  function card(it) {
    const action = it.installed
      ? '<span class="cz-plus done" title="Installed">✓</span>'
      : (canInstall() && it.in_catalog
          ? `<button class="cz-plus" data-install="${e(it.id)}" title="Install">+</button>` : '');
    return `
      <div class="cz-card" data-open="${e(it.id)}" tabindex="0">
        <div class="cz-icon">${catIcon(it.category)}</div>
        <div class="cz-main">
          <div class="cz-name">${e(it.title)}${it.in_catalog ? ' <span class="cz-verified" title="Reviewed, in-repo catalog entry">✓</span>' : ''} ${statusChip(it)}</div>
          <div class="cz-desc">${e(it.description)}</div>
          <div class="cz-by">${it.author ? 'by ' + e(it.author) : ''}${it.egress ? ' · <span class="cz-egress" title="Tool calls send data to a third party">external</span>' : ''}</div>
        </div>
        ${action}
      </div>`;
  }

  function grid(items, empty) {
    return items.length ? `<div class="cz-grid">${items.map(card).join('')}</div>`
                        : `<div class="cz-empty">${empty}</div>`;
  }

  function capBanner(d) {
    if (d.enabled) return '';
    const name = { skills: 'Skills', plugins: 'Plugins', connectors: 'MCP' }[S.kind];
    return `<div class="cz-banner">The <b>${name}</b> capability is off, so installed ${KIND_LABEL[S.kind]} aren't used by the agent yet. Turn it on in Settings → Capabilities.</div>`;
  }

  async function render() {
    syncBar();
    const body = $c('cz-body');
    if (S.detail) return renderDetail();
    let d;
    try { d = await load(S.kind); }
    catch (err) { body.innerHTML = `<div class="cz-empty">Failed to load: ${e(err.message)}</div>`; return; }

    let h = capBanner(d);
    if (S.view === 'yours') {
      const mine = d.items.filter(it => it.installed && matches(it));
      h += `<div class="cz-sec"><span>Installed ${KIND_LABEL[S.kind]}</span> <span class="cz-count">${mine.length}</span></div>`;
      h += grid(mine, S.q ? 'Nothing installed matches your search.' : 'Nothing installed yet. Switch to <b>Discover</b> to browse the catalog.');
      if (S.kind === 'connectors') h += '<div class="cz-note">Hand-added servers are edited in Settings → Capabilities → MCP.</div>';
    } else {
      const cat = d.items.filter(it => it.in_catalog && matches(it));
      const title = S.category ? catLabel(S.category) : (S.q ? 'Results' : 'Catalog');
      h += `<div class="cz-sec"><span>${e(title)}</span> <span class="cz-count">${cat.length}</span>
              ${S.category ? '<button class="cz-link" id="cz-clear-cat">Show all →</button>' : ''}</div>`;
      h += grid(cat, 'No catalog entries match.');
      const cats = Object.entries(d.categories || {}).sort((a, b) => b[1] - a[1]);
      if (!S.category && cats.length > 1) {
        h += '<div class="cz-sec"><span>Categories</span></div><div class="cz-cats">' +
          cats.map(([c, n]) => `<button class="cz-cat" data-cat="${e(c)}"><span class="cz-cat-ic">${catIcon(c)}</span><span>${e(catLabel(c))}</span><span class="cz-count">${n}</span></button>`).join('') +
          '</div>';
      }
      if (S.kind === 'connectors') h += registrySection();
      h += `<div class="cz-note">Only reviewed entries from the in-repo catalog can be installed. New ${KIND_LABEL[S.kind]} are added to the catalog through code review.</div>`;
    }
    body.innerHTML = h;
  }

  function registrySection() {
    let h = `<div class="cz-sec"><span>Public MCP Registry</span> <span class="cz-dim">browse only · not installable</span></div>
      <form class="cz-reg-form" id="cz-reg-form"><input type="search" id="cz-reg-q" placeholder="Search registry.modelcontextprotocol.io" value="${e(S.registryQ)}" maxlength="100"><button class="btn ghost" type="submit">Search</button></form>`;
    if (S.registry === 'loading') h += '<div class="cz-empty">Searching…</div>';
    else if (S.registry && S.registry.error) h += `<div class="cz-empty">${e(S.registry.error)}</div>`;
    else if (S.registry) {
      h += S.registry.servers.length ? '<div class="cz-grid">' + S.registry.servers.map(s => `
        <div class="cz-card static">
          <div class="cz-icon">🌐</div>
          <div class="cz-main">
            <div class="cz-name">${e(s.title)} <span class="cz-dim">${e(s.version)}</span></div>
            <div class="cz-desc">${e(s.description)}</div>
            <div class="cz-by">${e(s.name)}${s.url && /^https:\/\//.test(s.url) ? ` · <a href="${e(s.url)}" target="_blank" rel="noopener noreferrer">source ↗</a>` : ''}</div>
          </div>
        </div>`).join('') + '</div>' : '<div class="cz-empty">No registry results.</div>';
    }
    return h;
  }

  async function renderDetail() {
    const d = S.data[S.kind];
    const it = d && d.items.find(i => i.id === S.detail);
    const body = $c('cz-body');
    if (!it) { S.detail = null; return render(); }
    const rows = [];
    if (it.author) rows.push(['Author', e(it.author)]);
    if (it.version) rows.push(['Version', e(it.version)]);
    rows.push(['Category', e(catLabel(it.category))]);
    rows.push(['Source', it.in_catalog ? 'Reviewed catalog' : 'Local (hand-written)']);
    if (it.triggers && it.triggers.length) rows.push(['Triggers', it.triggers.map(t => `<code>${e(t)}</code>`).join(' ')]);
    if (it.tools && it.tools.length) rows.push(['Tools', it.tools.map(t => `<code>${e(t)}</code>`).join(' ')]);
    if (it.command) rows.push(['Runs', `<code>${e(it.command)}</code>`]);
    if (it.homepage && /^https:\/\//.test(it.homepage)) rows.push(['Docs', `<a href="${e(it.homepage)}" target="_blank" rel="noopener noreferrer">${e(it.homepage)} ↗</a>`]);
    if (it.error) rows.push(['Error', `<span class="cz-err">${e(it.error)}</span>`]);

    let actions = '';
    if (canInstall()) {
      if (!it.installed && it.in_catalog) {
        const secrets = (it.secret_env || []).map(s => `
          <label class="cz-secret">${e(s.label || s.key)} <code>${e(s.key)}</code>
            <input type="password" data-secret="${e(s.key)}" autocomplete="off" placeholder="stored in the OS keychain"></label>`).join('');
        actions = secrets + `<button class="btn accent cz-act" id="cz-do-install">Install</button>`;
      } else if (it.installed && it.can_uninstall) {
        actions = `<button class="btn red cz-act" id="cz-do-remove">Remove</button>`;
      } else if (it.installed) {
        actions = `<span class="cz-dim">${it.modified ? 'Locally modified: remove it from disk manually.' : 'Hand-written: managed outside this page.'}</span>`;
      }
    } else if (!it.installed) {
      actions = '<span class="cz-dim">Ask an administrator to install this (needs the <code>capabilities.install</code> permission).</span>';
    }

    body.innerHTML = `
      <button class="cz-link cz-back" id="cz-back">← Back</button>
      <div class="cz-detail">
        <div class="cz-dhead">
          <div class="cz-icon lg">${catIcon(it.category)}</div>
          <div><div class="cz-dtitle">${e(it.title)}${it.in_catalog ? ' <span class="cz-verified">✓</span>' : ''} ${statusChip(it)}</div>
               <div class="cz-desc full">${e(it.description)}</div></div>
        </div>
        ${it.egress ? '<div class="cz-banner warn">⚠ This connector sends tool-call data to a third-party service outside this server. Tool <b>arguments</b> are not PAN-masked (results are), so keep cardholder data out of prompts that use it and confirm the vendor is approved for data egress under Bangladesh Bank data-localization rules.</div>' : ''}
        <table class="cz-meta">${rows.map(([k, v]) => `<tr><th>${k}</th><td>${v}</td></tr>`).join('')}</table>
        <div class="cz-actions">${actions}</div>
        <div class="cz-sec"><span>${S.kind === 'connectors' ? 'Command' : 'Source'}</span> <span class="cz-dim">read-only, review before installing</span></div>
        <div id="cz-preview" class="cz-preview"><div class="cz-empty">Loading…</div></div>
      </div>`;
    try {
      const p = await api(`/customize/${S.kind}/${encodeURIComponent(it.id)}/preview`);
      const box = $c('cz-preview');
      if (!box || S.detail !== it.id) return;
      box.innerHTML = p.format === 'markdown' ? `<div class="cz-md">${md(p.text)}</div>` : `<pre>${e(p.text)}</pre>`;
    } catch (err) {
      const box = $c('cz-preview');
      if (box) box.innerHTML = `<div class="cz-empty">${e(err.message)}</div>`;
    }
  }

  /* ---------------- actions ---------------- */

  async function install(id, secrets) {
    if (S.busy) return;
    S.busy = true;
    try {
      await api(`/customize/${S.kind}/${encodeURIComponent(id)}/install`, {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ secrets: secrets || {} }),
      });
      toast(`Installed '${id}' ✓` + (S.kind === 'connectors' ? ' (connecting…)' : ''));
      await load(S.kind, true);
      render();
      if (S.kind === 'connectors') setTimeout(() => load('connectors', true).then(render).catch(() => {}), 4000);
    } catch (err) { toast('Install failed: ' + err.message, true); }
    finally { S.busy = false; }
  }

  async function remove(id) {
    if (!confirm(`Remove '${id}'? Its tools are removed right away; you can reinstall it from the catalog.`)) return;
    try {
      await api(`/customize/${S.kind}/${encodeURIComponent(id)}`, { method: 'DELETE' });
      toast(`Removed '${id}'`);
      await load(S.kind, true);
      render();
    } catch (err) { toast('Remove failed: ' + err.message, true); }
  }

  function needsForm(id) {
    const it = (S.data[S.kind] || { items: [] }).items.find(i => i.id === id);
    return it && it.secret_env && it.secret_env.length;
  }

  function onBodyClick(ev) {
    const t = ev.target;
    const inst = t.closest('[data-install]');
    if (inst) {
      ev.stopPropagation();
      const id = inst.dataset.install;
      if (needsForm(id)) { S.detail = id; render(); }   // secrets are entered on the detail view
      else install(id);
      return;
    }
    if (t.closest('a')) return;
    const cat = t.closest('[data-cat]');
    if (cat) { S.category = cat.dataset.cat; render(); return; }
    if (t.id === 'cz-clear-cat') { S.category = null; render(); return; }
    if (t.id === 'cz-back') { S.detail = null; render(); return; }
    if (t.id === 'cz-do-install') {
      const secrets = {};
      document.querySelectorAll('#cz-body [data-secret]').forEach(i => { secrets[i.dataset.secret] = i.value; });
      install(S.detail, secrets);
      return;
    }
    if (t.id === 'cz-do-remove') { remove(S.detail); return; }
    const c = t.closest('[data-open]');
    if (c) { S.detail = c.dataset.open; render(); }
  }

  async function searchRegistry(q) {
    S.registryQ = q;
    S.registry = 'loading';
    render();
    try { S.registry = await api('/customize/connectors/registry?q=' + encodeURIComponent(q)); }
    catch (err) { S.registry = { error: err.message, servers: [] }; }
    if (S.kind === 'connectors' && S.view === 'discover' && !S.detail) render();
  }

  function open() {
    $c('cz-modal').hidden = false;
    S.data = {};            // always show fresh install state
    S.detail = null;
    render();
    $c('cz-q').focus();
  }

  function close() { $c('cz-modal').hidden = true; }

  if ($c('btn-customize')) {
    $c('btn-customize').onclick = open;
    $c('cz-close').onclick = close;
    $c('cz-modal').addEventListener('click', ev => { if (ev.target.id === 'cz-modal') close(); });
    document.addEventListener('keydown', ev => {
      if (ev.key !== 'Escape' || $c('cz-modal').hidden) return;
      if (S.detail) { S.detail = null; render(); } else close();
    });
    document.querySelectorAll('#cz-box .cz-tab').forEach(b => b.onclick = () => {
      S.kind = b.dataset.kind; S.category = null; S.detail = null; S.q = ''; render();
    });
    document.querySelectorAll('#cz-box .cz-view').forEach(b => b.onclick = () => {
      S.view = b.dataset.view; S.category = null; S.detail = null; render();
    });
    let timer;
    $c('cz-q').addEventListener('input', ev => {
      clearTimeout(timer);
      timer = setTimeout(() => { S.q = ev.target.value.trim(); S.detail = null; render(); }, 150);
    });
    const body = $c('cz-body');
    body.addEventListener('click', onBodyClick);
    body.addEventListener('keydown', ev => {
      if (ev.key === 'Enter' && ev.target.matches('.cz-card[data-open]')) { S.detail = ev.target.dataset.open; render(); }
    });
    body.addEventListener('submit', ev => {
      if (ev.target.id !== 'cz-reg-form') return;
      ev.preventDefault();
      searchRegistry($c('cz-reg-q').value.trim());
    });
  }
})();
