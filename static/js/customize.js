/* ---------------- customize.js ----------------
 * Customize page: browse and install skills, connectors and plugins from the
 * reviewed in-repo catalogs (routes/customize.py). Anyone signed in can browse;
 * install / remove needs capabilities.install (checked again server-side).
 */

(function () {
  const KIND_LABEL = { skills: 'skills', connectors: 'connectors', plugins: 'plugins', agents: 'agents' };
  const CAT_ICON = {
    security: '🛡️', compliance: '⚖️', engineering: '⌨️', operations: '🧰', finance: '💰',
    productivity: '✅', web: '🌐', tickets: '🎫', developer: '🧪', data: '🗃️', general: '🧩', custom: '🔧',
    design: '🎨', communication: '💬', crm: '🤝', payments: '💳', cloud: '☁️', commerce: '🛍️',
  };
  const S = { kind: 'skills', view: 'discover', q: '', category: null, data: {}, detail: null,
              registry: null, registryQ: '', busy: false,
              market: null, marketQ: '', marketUrl: '', remote: null, remoteBusy: false,
              agents: null, forkAgent: null, forkDir: '', connecting: {} };

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
    const seg = document.querySelector('#cz-box .cz-seg');
    if (seg) seg.style.display = S.kind === 'agents' ? 'none' : '';
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

  const SENS = { pii: 'Personal data', payments: 'Payments', data: 'Project data' };
  const SENS_HINT = {
    pii: 'Can reach personal or customer records. Each person signs in with their own account.',
    payments: 'Can reach payment data. Test mode first; a PCI-DSS review is needed before real data.',
    data: 'Can reach business or project data. Each person signs in with their own account.',
  };

  function connChips(it) {
    if (S.kind !== 'connectors' || !it.in_catalog) return '';
    let h = '';
    if (it.sensitivity) h += ` <span class="cz-chip bad" title="${e(SENS_HINT[it.sensitivity] || '')}">${e(SENS[it.sensitivity] || it.sensitivity)}</span>`;
    if (it.installed && it.installed_scope) h += ` <span class="cz-chip mid" title="${it.installed_scope === 'user' ? 'Only you can use its tools' : 'One shared account for everyone'}">${it.installed_scope === 'user' ? 'Just you' : 'Shared'}</span>`;
    if (it.installed && it.oauth && !it.signed_in) h += ' <span class="cz-chip mid">Sign-in needed</span>';
    if (!it.installed && !it.allowed) h += ' <span class="cz-chip mid" title="An administrator has to allow this connector first">Needs admin</span>';
    return h;
  }

  /* brand-neutral monogram tile: no third-party logos are bundled or fetched */
  function tile(it, lg) {
    if (S.kind === 'connectors' && /^#[0-9a-f]{6}$/i.test(it.color || '')) {
      const ini = String(it.title || it.id).replace(/[^A-Za-z0-9 ]/g, ' ').trim().split(/\s+/).slice(0, 2)
        .map(w => w[0]).join('').toUpperCase();
      return `<div class="cz-icon cz-mono${lg ? ' lg' : ''}" style="background:${it.color}" aria-hidden="true">${e(ini)}</div>`;
    }
    return `<div class="cz-icon${lg ? ' lg' : ''}">${catIcon(it.category)}</div>`;
  }

  const canRemove = it => it.can_uninstall && (S.kind === 'connectors' ? (it.installed_scope === 'user' || canInstall()) : canInstall());
  const canConnect = it => S.kind === 'connectors' && it.installed && it.oauth && !it.signed_in &&
                           (it.installed_scope === 'user' || canInstall());

  function card(it) {
    const noun = S.kind === 'connectors' ? 'Disconnect' : 'Remove';
    const connect = canConnect(it)
      ? (S.connecting[it.id] ? '<button class="ru-btn" disabled>Connecting…</button>'
                             : `<button class="ru-btn primary" data-connect="${e(it.id)}" title="Sign in to ${e(it.title)}">Connect account</button>`) : '';
    const action = it.installed
      ? (canRemove(it)
          ? `<span class="cz-swap"><span class="cz-plus done" title="Installed">✓</span><button class="cz-plus rm" data-remove="${e(it.id)}" title="${noun}" aria-label="${noun} ${e(it.title)}">✕</button></span>`
          : '<span class="cz-plus done" title="Installed">✓</span>')
      : ((S.kind === 'connectors' ? (it.in_catalog && (it.allowed || canInstall())) : (canInstall() && it.in_catalog))
          ? `<button class="cz-plus" data-install="${e(it.id)}" title="Install">+</button>` : '');
    return `
      <div class="cz-card" data-open="${e(it.id)}" tabindex="0">
        ${tile(it)}
        <div class="cz-main">
          <div class="cz-name">${e(it.title)}${it.in_catalog ? ' <span class="cz-verified" title="Reviewed, in-repo catalog entry">✓</span>' : ''} ${statusChip(it)}${connChips(it)}</div>
          <div class="cz-desc">${e(it.description)}</div>
          <div class="cz-by">${it.author ? 'by ' + e(it.author) : ''}${it.egress ? ' · <span class="cz-egress" title="Tool calls send data to a third party">external</span>' : ''}</div>
        </div>
        <span class="cz-acts">${connect}${action}</span>
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
    if (S.kind === 'marketplace') { renderMarketplace(body); return; }
    if (S.kind === 'agents') { renderAgents(body); return; }
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

  function marketCard(it) {
    return `
      <div class="cz-card static">
        <div class="cz-icon">${catIcon(it.category)}</div>
        <div class="cz-main">
          <div class="cz-name">${e(it.title)}${it.installed ? ' <span class="cz-chip ok">installed</span>' : ''}</div>
          <div class="cz-desc">${e(it.description)}</div>
          <div class="cz-by">${it.author ? 'by ' + e(it.author) + ' - ' : ''}${e(it.name || '')}</div>
          <div class="cz-actions" style="flex-direction:row; gap:8px; margin-top:8px;">
            ${it.manifest_url ? `<button class="btn ghost cz-act" data-review="${e(it.manifest_url)}" data-code="${e(it.code_url || '')}">Review</button>` : ''}
          </div>
        </div>
      </div>`;
  }

  async function searchMarketplace(q) {
    S.marketQ = q;
    S.market = 'loading';
    render();
    try { S.market = await api('/customize/plugins/registry?q=' + encodeURIComponent(q)); }
    catch (err) { S.market = { error: err.message, items: [] }; }
    if (S.kind === 'marketplace' && !S.detail) render();
  }

  async function reviewRemote(manifestUrl, codeUrl) {
    S.marketUrl = manifestUrl || '';
    S.remoteBusy = true; S.remote = null;
    render();
    try {
      S.remote = await api('/customize/plugins/inspect-remote', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ manifest_url: manifestUrl, code_url: codeUrl || '' }),
      });
    } catch (err) { S.remote = { error: err.message }; }
    S.remoteBusy = false;
    if (S.kind === 'marketplace') render();
  }

  async function installRemote() {
    const r = S.remote;
    if (!r || !r.manifest) return;
    if (!confirm(`Install remote plugin '${r.manifest.name}'? Only continue if you reviewed the code.`)) return;
    try {
      await api('/customize/plugins/install-remote', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ manifest_url: r.manifest_url, code_url: r.code_url || '', code_sha256: r.code_sha256 || '' }),
      });
      toast(`Installed '${r.manifest.name}'`);
      S.remote = null; S.marketUrl = '';
      S.market = null;
      render();
    } catch (err) { toast('Install failed: ' + err.message, true); }
  }

  function renderMarketplace(body) {
    let h = `<div class="cz-banner">Remote plugins run as <b>in-process Python</b>. Review the manifest and code preview before installing.</div>
      <div class="cz-sec"><span>Plugin Marketplace</span> <span class="cz-dim">external registry</span></div>
      <form class="cz-reg-form" id="cz-market-form"><input type="search" id="cz-market-q" placeholder="Search marketplace" value="${e(S.marketQ)}" maxlength="100"><button class="btn ghost" type="submit">Search</button></form>
      <form class="cz-reg-form" id="cz-remote-form"><input type="url" id="cz-remote-url" placeholder="Paste a plugin.json manifest URL to review" value="${e(S.marketUrl)}" maxlength="500"><button class="btn ghost" type="submit">Review URL</button></form>`;
    if (S.remote) h += remoteDetailHtml();
    else if (S.remoteBusy) h += '<div class="cz-empty">Fetching manifest…</div>';
    if (S.market === 'loading') h += '<div class="cz-empty">Loading marketplace…</div>';
    else if (S.market && S.market.error) h += `<div class="cz-empty">${e(S.market.error)}</div>`;
    else if (S.market) {
      const items = (S.market.items || []).filter(it => {
        if (!S.q) return true;
        const q = S.q.toLowerCase();
        return [it.name, it.title, it.description, it.author].some(v => String(v || '').toLowerCase().includes(q));
      });
      h += `<div class="cz-sec"><span>Results</span> <span class="cz-count">${items.length}</span></div>`;
      h += items.length ? '<div class="cz-grid">' + items.map(marketCard).join('') + '</div>'
        : '<div class="cz-empty">No marketplace results.</div>';
    }
    body.innerHTML = h;
  }

  function remoteDetailHtml() {
    const r = S.remote;
    if (r.error) return `<div class="cz-empty">${e(r.error)}</div>`;
    const m = r.manifest || {};
    return `
      <div class="cz-detail">
        <div class="cz-dtitle">${e(m.title || m.name)} ${r.name_taken ? '<span class="cz-chip mid">installed</span>' : ''}</div>
        <div class="cz-desc">${e(m.description || '')}</div>
        <table class="cz-meta">
          <tr><th>Manifest</th><td><code>${e(r.manifest_url || '')}</code></td></tr>
          <tr><th>Code</th><td><code>${e(r.code_url || '(none)')}</code></td></tr>
          <tr><th>Syntax</th><td>${e(r.code_syntax || '')}</td></tr>
        </table>
        <div class="cz-actions">
          ${r.code_url && !r.name_taken ? `<button class="btn accent cz-act" id="cz-remote-install">Install - I reviewed the code</button>` : ''}
          <button class="btn ghost cz-act" id="cz-remote-clear">Clear review</button>
        </div>
        <div class="cz-sec"><span>Code preview</span></div>
        <div class="cz-preview"><pre>${e(r.code_preview || '(no code)')}</pre></div>
      </div>`;
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
    if (S.kind === 'connectors' && it.in_catalog) {
      actions = connectorActions(it);
    } else if (canInstall()) {
      if (!it.installed && it.in_catalog) {
        const secrets = (it.secret_env || []).map(s => `
          <label class="cz-secret">${e(s.label || s.key)} <code>${e(s.key)}</code>
            <input type="password" data-secret="${e(s.key)}" autocomplete="off" placeholder="stored in the OS keychain"></label>`).join('');
        actions = secrets + `<button class="btn accent cz-act" id="cz-do-install">Install</button>`;
      } else if (it.installed && it.can_uninstall) {
        actions = `<button class="btn red cz-act" id="cz-do-remove">${S.kind === 'connectors' ? 'Disconnect' : 'Remove'}</button>`;
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
          ${tile(it, true)}
          <div><div class="cz-dtitle">${e(it.title)}${it.in_catalog ? ' <span class="cz-verified">✓</span>' : ''} ${statusChip(it)}${connChips(it)}</div>
               <div class="cz-desc full">${e(it.description)}</div></div>
        </div>
        ${it.egress ? '<div class="cz-banner warn">⚠ Tool calls go to a third-party service outside this server. Card numbers (PAN) are masked in tool <b>arguments</b> when the cloud-egress guard is on, and always in results. Keep other cardholder and customer data out of prompts that use it, and confirm the vendor is approved for data egress under Bangladesh Bank data-localization rules.</div>' : ''}
        ${it.sensitivity ? `<div class="cz-banner warn"><b>${e(SENS[it.sensitivity] || '')}.</b> ${e(SENS_HINT[it.sensitivity] || '')} Only you can use the tools of your own connection.</div>` : ''}
        ${it.note ? `<div class="cz-banner">${e(it.note)}</div>` : ''}
        <table class="cz-meta">${rows.map(([k, v]) => `<tr><th>${k}</th><td>${v}</td></tr>`).join('')}</table>
        <div class="cz-actions">${actions}</div>
        <div class="cz-sec"><span>${S.kind === 'connectors' ? (it.transport === 'http' ? 'Endpoint' : 'Command') : 'Source'}</span> <span class="cz-dim">read-only, review before installing</span></div>
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

  /* ---------------- connectors: install form, sign-in, admin setup ---------------- */

  function connectorActions(it) {
    const admin = canInstall();
    let h = '';
    if (!it.installed) {
      if (!it.allowed && !admin) {
        return '<span class="cz-dim">An administrator has to allow this connector before you can add it.</span>';
      }
      if (admin && !it.sensitivity) {
        const g = it.scope_default === 'global';
        h += `<div class="cz-scope" role="radiogroup" aria-label="Install for">Install for
          <label><input type="radio" name="cz-scope" value="user" ${g ? '' : 'checked'}> Just me</label>
          <label><input type="radio" name="cz-scope" value="global" ${g ? 'checked' : ''}> Everyone (one shared account)</label></div>`;
      } else {
        h += '<div class="cz-dim">Installs just for you: you sign in with your own account.</div>';
      }
      h += (it.fields || []).map(f => `
        <label class="cz-secret">${e(f.label)}
          <input type="text" data-field="${e(f.key)}" placeholder="${e(f.placeholder || '')}" autocomplete="off" spellcheck="false"></label>`).join('');
      h += (it.secret_env || []).map(s => `
        <label class="cz-secret">${e(s.label || s.key)} <code>${e(s.key)}</code>
          <input type="password" data-secret="${e(s.key)}" autocomplete="off" placeholder="stored in the OS keychain"></label>`).join('');
      if (it.needs_client && !admin) {
        h += '<div class="cz-dim">This vendor needs a one-time app setup by an administrator before anyone can sign in.</div>';
      }
      h += '<button class="btn accent cz-act" id="cz-do-install">Install</button>';
    } else {
      if (it.oauth && (it.installed_scope === 'user' || admin)) {
        h += S.connecting[it.id] ? '<button class="btn cz-act" disabled>Connecting…</button>'
          : `<button class="btn accent cz-act" id="cz-do-connect">${it.signed_in ? 'Reconnect account' : 'Connect account'}</button>`;
        if (it.signed_in) h += '<button class="btn ghost cz-act" id="cz-do-signout">Sign out</button>';
      }
      if (canRemove(it)) h += '<button class="btn red cz-act" id="cz-do-remove">Disconnect</button>';
    }
    return h + adminBlock(it);
  }

  function adminBlock(it) {
    if (!canInstall()) return '';
    let h = `<div class="cz-sec"><span>Administrator</span></div>
      <label class="cz-toggle"><input type="checkbox" id="cz-allow" ${it.allowed ? 'checked' : ''}> Users may add this connector</label>`;
    if (it.oauth && (it.client === 'preregistered' || it.client === 'auto')) {
      h += `<div class="cz-client">
        <div class="cz-note">${it.client === 'preregistered'
          ? 'This vendor does not register apps automatically.'
          : 'Sign-in tries automatic registration first; use this only if the vendor refuses it.'}
          Create an OAuth app with them, add the redirect address <code>http://127.0.0.1:&lt;port&gt;/callback</code>
          (or your <code>mcp_oauth_redirect_base</code> address when the vendor needs https), then paste its client ID and secret once.
          The secret stays in the OS keychain and users never see it.</div>
        <div class="cz-fork-row">
          <input type="text" id="cz-client-id" class="cz-search" placeholder="${it.has_client ? 'Client ID saved - paste a new one to replace' : 'Client ID'}" autocomplete="off" spellcheck="false">
          <input type="password" id="cz-client-secret" class="cz-search" placeholder="${it.has_client ? 'Secret saved (leave blank to keep)' : 'Client secret'}" autocomplete="off">
        </div>
        <div class="cz-actions"><button class="btn accent cz-act" id="cz-client-save">Save client</button>
          ${it.has_client ? '<button class="btn ghost cz-act" id="cz-client-clear">Remove client</button>' : ''}</div>
      </div>`;
    }
    return h;
  }

  async function connectAccount(id, scope) {
    if (S.connecting[id]) return;
    S.connecting[id] = true;
    render();
    try {
      const start = await api(`/mcp/servers/${encodeURIComponent(id)}/oauth/start`, {
        method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ scope }),
      });
      if (start.mode === 'redirect' && /^https:\/\//.test(start.auth_url || '')) window.open(start.auth_url, '_blank', 'noopener');
      else toast('Finish signing in in the browser window that just opened.');
      const t0 = Date.now();
      for (;;) {
        await new Promise(r => { setTimeout(r, 2000); });
        const st = await api(`/mcp/servers/${encodeURIComponent(id)}/oauth/status?scope=${encodeURIComponent(scope)}`);
        if (st.state === 'done') break;
        if (st.state === 'error') throw new Error(st.error || 'sign-in failed');
        if (Date.now() - t0 > 330000) throw new Error('timed out waiting for the sign-in');
      }
      toast(`Connected '${id}' ✓`);
    } catch (err) {
      toast('Sign-in failed: ' + err.message, true);
    } finally {
      delete S.connecting[id];
      try { await load('connectors', true); } catch (_) {}
      render();
      setTimeout(() => load('connectors', true).then(() => { if (S.kind === 'connectors') render(); }).catch(() => {}), 3000);
    }
  }

  async function signOut(id, scope) {
    try {
      await api(`/mcp/servers/${encodeURIComponent(id)}/oauth/disconnect`, {
        method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ scope }),
      });
      toast(`Signed out of '${id}'`);
      await load('connectors', true);
      render();
    } catch (err) { toast('Could not sign out: ' + err.message, true); }
  }

  async function saveClient(id) {
    const cid = $c('cz-client-id').value.trim();
    const secret = $c('cz-client-secret').value.trim();
    if (!cid && !secret) { toast('Paste the client ID and secret first', true); return; }
    const it = S.data.connectors.items.find(i => i.id === id) || {};
    if (!cid && !it.has_client) { toast('The client ID is required', true); return; }
    try {
      await api(`/customize/connectors/${encodeURIComponent(id)}/oauth-client`, {
        method: 'PUT', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ client_id: cid, client_secret: secret }),
      });
      toast('Client saved. People can now press Connect account.');
      await load('connectors', true);
      render();
    } catch (err) { toast(err.message, true); }
  }

  async function clearClient(id) {
    if (!confirm('Remove the saved client? People who are signed in stay signed in, but no one can sign in again until you add one.')) return;
    try {
      await api(`/customize/connectors/${encodeURIComponent(id)}/oauth-client`, { method: 'DELETE' });
      await load('connectors', true);
      render();
    } catch (err) { toast(err.message, true); }
  }

  async function setAllowed(id, on) {
    try {
      await api(`/customize/connectors/${encodeURIComponent(id)}/enabled`, {
        method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ enabled: on }),
      });
      toast(on ? 'Users can now add this connector' : 'Users can no longer add this connector');
      await load('connectors', true);
      render();
    } catch (err) { toast(err.message, true); }
  }

  /* ---------------- agents: starter templates and agents other users shared ---------------- */

  const canReview = () => !!(window.__user && window.__user.is_super_admin) ||
                          (window.hasPerm && window.hasPerm('custom_agents.publish'));

  async function loadAgents(force) {
    if (S.agents && !force) return S.agents;
    const all = (await api('/custom-agents')).agents || [];
    let pending = [];
    if (canReview()) { try { pending = (await api('/custom-agents/pending')).agents || []; } catch (_) {} }
    S.agents = { templates: all.filter(a => a.scope === 'template'),
                 shared: all.filter(a => a.scope === 'shared'), pending };
    return S.agents;
  }

  function agentMatches(a) {
    if (!S.q) return true;
    const q = S.q.toLowerCase();
    return [a.name, a.slug, a.description].some(v => String(v || '').toLowerCase().includes(q));
  }

  function agentCard(a, kind) {
    const tools = (a.tool_allowlist && a.tool_allowlist.length) ? a.tool_allowlist.length + ' tools' : 'all tools';
    const who = kind === 'pending' ? 'shared by ' + e(a.owner_name || 'a user') : kind === 'shared' ? 'shared by another user' : 'starter template';
    const btns = kind === 'pending'
      ? `<button class="ru-btn primary" data-agent-approve="${a.id}">Approve</button><button class="ru-btn" data-agent-reject="${a.id}">Reject</button>`
      : `<button class="ru-btn primary" data-agent-fork="${a.id}">Fork</button>`;
    return `<div class="cz-card static cz-agent">
      <div class="cz-icon">${e(a.icon || '🤖')}</div>
      <div class="cz-main">
        <div class="cz-name">${e(a.name)} <span class="cz-chip mid">${e(tools)}</span></div>
        <div class="cz-desc">${e(a.description)}</div>
        <div class="cz-by">${who} · <code>/${e(a.slug)}</code></div>
      </div>
      <div class="cz-agent-btns">${btns}</div>
    </div>`;
  }

  function agentSection(title, list, kind, empty) {
    const items = list.filter(agentMatches);
    return `<div class="cz-sec"><span>${e(title)}</span> <span class="cz-count">${items.length}</span></div>` +
      (items.length ? `<div class="cz-grid">${items.map(a => agentCard(a, kind)).join('')}</div>` : `<div class="cz-empty">${empty}</div>`);
  }

  function forkForm(a) {
    return `<button class="cz-link cz-back" id="cz-agent-back">← Back</button>
      <div class="cz-detail">
        <div class="cz-dhead"><div class="cz-icon lg">${e(a.icon || '🤖')}</div>
          <div><div class="cz-dtitle">${e(a.name)}</div><div class="cz-desc full">${e(a.description)}</div></div></div>
        <div class="cz-sec"><span>Where should it work?</span></div>
        <p class="cz-note">A copy of this agent is added to your Personal Agents. Pick the folder on your computer it works in. It can read, write and analyse files there, and nothing outside it. You can change the folder later.</p>
        <div class="cz-fork-row">
          <input type="text" id="cz-fork-dir" class="cz-search" placeholder="D:/reports/weekly" value="${e(S.forkDir)}" autocomplete="off" spellcheck="false">
          <button type="button" class="ru-btn browse" id="cz-fork-browse">📁 Browse…</button>
        </div>
        <div class="cz-actions"><button class="btn accent cz-act" id="cz-fork-go" ${S.forkDir.trim() ? '' : 'disabled'}>Add to my agents</button></div>
      </div>`;
  }

  async function renderAgents(body) {
    let d;
    try { d = await loadAgents(); }
    catch (err) { body.innerHTML = `<div class="cz-empty">Failed to load: ${e(err.message)}</div>`; return; }
    if (S.forkAgent) {
      const a = [...d.templates, ...d.shared].find(x => String(x.id) === String(S.forkAgent));
      if (a) { body.innerHTML = forkForm(a); return; }
      S.forkAgent = null;
    }
    let h = '<div class="cz-note">Fork an agent to use it. You choose the folder it works in. Create your own from the Personal Agents card in Chat.</div>';
    if (canReview()) h += agentSection('Waiting for approval', d.pending, 'pending', 'Nothing is waiting for approval.');
    h += agentSection('Shared by users', d.shared, 'shared', 'No one has shared an agent yet.');
    h += agentSection('Starter templates', d.templates, 'template', 'No templates match your search.');
    body.innerHTML = h;
  }

  async function reviewAgent(id, approve) {
    try {
      await api(`/custom-agents/${encodeURIComponent(id)}/${approve ? 'approve' : 'reject'}`, { method: 'POST' });
      toast(approve ? 'Approved: everyone can now fork it' : 'Rejected: it stays private to its owner');
      await loadAgents(true);
      render();
    } catch (err) { toast('Could not review that agent: ' + err.message, true); }
  }

  async function forkIntoMine() {
    const dir = S.forkDir.trim();
    if (!dir || S.busy) return;
    S.busy = true;
    try {
      const made = await api(`/custom-agents/${encodeURIComponent(S.forkAgent)}/fork`, {
        method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ work_dir: dir }),
      });
      toast(`Added '${made.name}' to your Personal Agents`);
      S.forkAgent = null; S.forkDir = '';
      if (window.loadCustomAgents) window.loadCustomAgents();
      render();
    } catch (err) { toast(err.message, true); }
    finally { S.busy = false; }
  }

  /* returns true when the click was an agents-tab action */
  function onAgentsClick(ev, t) {
    if (S.kind !== 'agents') return false;
    const f = t.closest('[data-agent-fork]');
    if (f) { S.forkAgent = f.dataset.agentFork; S.forkDir = ''; render(); return true; }
    const ap = t.closest('[data-agent-approve]');
    if (ap) { reviewAgent(ap.dataset.agentApprove, true); return true; }
    const rj = t.closest('[data-agent-reject]');
    if (rj) { reviewAgent(rj.dataset.agentReject, false); return true; }
    if (t.id === 'cz-agent-back') { S.forkAgent = null; render(); return true; }
    if (t.id === 'cz-fork-go') { forkIntoMine(); return true; }
    if (t.id === 'cz-fork-browse') {
      const cur = $c('cz-fork-dir').value.trim();
      (window.pickFolder ? window.pickFolder(cur) : Promise.resolve('')).then(p => {
        if (p) { S.forkDir = p; render(); }
      });
      return true;
    }
    return !!t.closest('.cz-agent');
  }

  /* ---------------- actions ---------------- */

  async function install(id, secrets, extra) {
    if (S.busy) return;
    S.busy = true;
    let installed = null;
    try {
      const res = await api(`/customize/${S.kind}/${encodeURIComponent(id)}/install`, {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ secrets: secrets || {}, ...(extra || {}) }),
      });
      installed = res.item || null;
      toast(`Installed '${id}' ✓` + (S.kind === 'connectors' ? (installed && installed.oauth ? ' - sign in next' : ' (connecting…)') : ''));
      await load(S.kind, true);
      render();
      if (S.kind === 'connectors') setTimeout(() => load('connectors', true).then(render).catch(() => {}), 4000);
    } catch (err) { toast('Install failed: ' + err.message, true); }
    finally { S.busy = false; }
    // a connector that needs a sign-in goes straight to it
    if (installed && installed.oauth && installed.installed_scope) connectAccount(id, installed.installed_scope);
  }

  async function remove(id) {
    const verb = S.kind === 'connectors' ? 'Disconnect' : 'Remove';
    if (!confirm(`${verb} '${id}'? Its tools are removed right away; you can add it again from the catalog.`)) return;
    try {
      await api(`/customize/${S.kind}/${encodeURIComponent(id)}`, { method: 'DELETE' });
      toast(S.kind === 'connectors' ? `Disconnected '${id}'` : `Removed '${id}'`);
      await load(S.kind, true);
      render();
    } catch (err) { toast('Remove failed: ' + err.message, true); }
  }

  function needsForm(id) {
    const it = (S.data[S.kind] || { items: [] }).items.find(i => i.id === id);
    if (!it) return false;
    if (S.kind === 'connectors') return !!(it.oauth || it.sensitivity || it.note || (it.fields || []).length || (it.secret_env || []).length);
    return it.secret_env && it.secret_env.length;
  }

  function onBodyClick(ev) {
    const t = ev.target;
    const rv = t.closest('[data-review]');
    if (rv) { reviewRemote(rv.dataset.review, rv.dataset.code || ''); return; }
    if (t.id === 'cz-remote-install') { installRemote(); return; }
    if (t.id === 'cz-remote-clear') { S.remote = null; S.marketUrl = ''; render(); return; }
    const rm = t.closest('[data-remove]');
    if (rm) { ev.stopPropagation(); remove(rm.dataset.remove); return; }
    if (onAgentsClick(ev, t)) return;
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
      const secrets = {}, fields = {};
      document.querySelectorAll('#cz-body [data-secret]').forEach(i => { secrets[i.dataset.secret] = i.value; });
      document.querySelectorAll('#cz-body [data-field]').forEach(i => { fields[i.dataset.field] = i.value.trim(); });
      const picked = document.querySelector('#cz-body input[name="cz-scope"]:checked');
      install(S.detail, secrets, S.kind === 'connectors' ? { fields, scope: picked ? picked.value : 'user' } : null);
      return;
    }
    if (t.id === 'cz-do-remove') { remove(S.detail); return; }
    const cn = t.closest('[data-connect]');
    if (cn) {
      ev.stopPropagation();
      const it = S.data.connectors.items.find(i => i.id === cn.dataset.connect);
      if (it) connectAccount(it.id, it.installed_scope);
      return;
    }
    const conn = (id => (S.data.connectors || { items: [] }).items.find(i => i.id === id))(S.detail);
    if (t.id === 'cz-do-connect' && conn) { connectAccount(conn.id, conn.installed_scope); return; }
    if (t.id === 'cz-do-signout' && conn) { signOut(conn.id, conn.installed_scope); return; }
    if (t.id === 'cz-client-save' && conn) { saveClient(conn.id); return; }
    if (t.id === 'cz-client-clear' && conn) { clearClient(conn.id); return; }
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
    S.market = null; S.remote = null; S.remoteBusy = false; S.marketUrl = '';
    S.agents = null; S.forkAgent = null; S.forkDir = '';
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
      S.kind = b.dataset.kind; S.category = null; S.detail = null; S.q = ''; S.forkAgent = null; render();
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
    body.addEventListener('change', ev => {
      if (ev.target.id === 'cz-allow' && S.detail) setAllowed(S.detail, ev.target.checked);
    });
    body.addEventListener('input', ev => {
      if (ev.target.id !== 'cz-fork-dir') return;
      S.forkDir = ev.target.value;
      const go = $c('cz-fork-go');
      if (go) go.disabled = !S.forkDir.trim();
    });
    body.addEventListener('keydown', ev => {
      if (ev.key === 'Enter' && ev.target.matches('.cz-card[data-open]')) { S.detail = ev.target.dataset.open; render(); }
    });
    body.addEventListener('submit', ev => {
      if (ev.target.id === 'cz-reg-form') {
        ev.preventDefault();
        searchRegistry($c('cz-reg-q').value.trim());
        return;
      }
      if (ev.target.id === 'cz-market-form') {
        ev.preventDefault();
        searchMarketplace($c('cz-market-q').value.trim());
        return;
      }
      if (ev.target.id === 'cz-remote-form') {
        ev.preventDefault();
        const u = $c('cz-remote-url').value.trim();
        if (u) reviewRemote(u, '');
        return;
      }
    });
  }
})();
