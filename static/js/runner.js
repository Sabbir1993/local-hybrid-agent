/* runner.js - admin panel for the isolated code runner (settings.html #sec-runner).
 * GET /sandbox/status, POST /sandbox/pair {url, code}, POST /sandbox/selftest, PUT /sandbox/enabled, DELETE /sandbox/runner;
 * all need users.manage. The pairing code and the token are never shown or stored by this page. */
'use strict';

function runnerEsc(s) {
  return String(s == null ? '' : s).replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
}

async function loadRunnerPanel() {
  const box = document.getElementById('runner-content');
  if (!box) return;
  let st;
  try {
    const r = await fetch('/sandbox/status');
    if (!r.ok) { box.innerHTML = '<div class="mon-empty">You do not have permission to manage the code runner.</div>'; return; }
    st = await r.json();
  } catch (e) {
    box.innerHTML = '<div class="mon-empty">Could not load the code runner status.</div>';
    return;
  }
  const badge = !st.configured ? ['Not paired', 'var(--dim)']
    : !st.reachable ? ['Not reachable', 'var(--danger, #c0392b)']
    : st.ready ? ['Ready: the chat can use it', 'var(--ok, #2e8b57)']
    : !st.enabled ? ['Paired, switched off', 'var(--dim)']
    : ['Reachable but its sandbox is not working', 'var(--danger, #c0392b)'];
  const verified = st.verified_at ? new Date(st.verified_at * 1000).toLocaleString() : 'never';
  let html = `<div style="padding:10px 4px;display:flex;flex-direction:column;gap:14px;max-width:560px">
    <div style="font-size:13px"><b style="color:${badge[1]}">${runnerEsc(badge[0])}</b></div>`;
  if (st.configured) {
    html += `<div class="dim" style="font-size:12px;line-height:1.6">
      Address: ${runnerEsc(st.url)}${st.url_from_env ? ' (set by the RUNNER_URL environment variable)' : ''}<br>
      Certificate: ${runnerEsc(st.fingerprint)}<br>
      Sandbox on the runner: ${runnerEsc(st.sandbox || 'unknown')}<br>
      Last passed self-test: ${runnerEsc(verified)}</div>
      <div style="display:flex;gap:8px;flex-wrap:wrap">
        <button type="button" class="btn" id="rn-test" style="width:auto;padding:6px 14px">Test connection</button>
        <button type="button" class="btn accent" id="rn-toggle" style="width:auto;padding:6px 14px">${st.enabled ? 'Switch off' : 'Switch on'}</button>
        <button type="button" class="btn" id="rn-forget" style="width:auto;padding:6px 14px">Forget this runner</button>
      </div>`;
  } else {
    html += `<div class="dim" style="font-size:12px;line-height:1.5">
        On the runner VM run <code>python3 runner_server.py pair-code</code>: it prints a one-time code that works for 10 minutes.
        Enter the runner's address and that code. The app pins the runner's certificate and keeps its token in the server's keychain.</div>
      <label style="display:flex;flex-direction:column;gap:4px;font-size:11px;font-weight:600;color:var(--dim)">
        <span>Runner address</span>
        <input type="text" id="rn-url" placeholder="https://[RUNNER-ADDRESS]:8470" autocomplete="off" style="width:100%">
      </label>
      <label style="display:flex;flex-direction:column;gap:4px;font-size:11px;font-weight:600;color:var(--dim)">
        <span>Pairing code</span>
        <input type="text" id="rn-code" placeholder="XXXXX-XXXXX" autocomplete="off" style="width:200px">
      </label>
      <div><button type="button" class="btn accent" id="rn-pair" style="width:auto;padding:6px 14px">Pair and test</button></div>`;
  }
  html += `<div id="rn-results" style="font-size:12px;line-height:1.6"></div><div id="rn-msg" class="dim" style="font-size:12px"></div></div>`;
  box.innerHTML = html;
  const msg = box.querySelector('#rn-msg'), results = box.querySelector('#rn-results');
  const showChecks = (r) => {
    results.innerHTML = (r.checks || []).map(c =>
      `<div>${c.ok ? '✓' : '✗'} ${runnerEsc(c.name.replace(/_/g, ' '))}${c.ok ? '' : ' - ' + runnerEsc(c.detail || '')}${c.critical ? '' : ' (optional)'}</div>`).join('');
  };
  const call = async (label, url, opts) => {
    msg.textContent = label;
    try {
      const r = await fetch(url, opts);
      const d = await r.json().catch(() => ({}));
      if (!r.ok) { msg.textContent = d.detail || 'That did not work.'; return null; }
      msg.textContent = '';
      return d;
    } catch (e) {
      msg.textContent = 'Could not reach the server.';
      return null;
    }
  };
  const again = () => loadRunnerPanel();
  const pairBtn = box.querySelector('#rn-pair');
  if (pairBtn) pairBtn.addEventListener('click', async () => {
    const url = box.querySelector('#rn-url').value.trim(), code = box.querySelector('#rn-code').value.trim();
    if (!url || !code) { msg.textContent = 'Enter the runner address and the pairing code.'; return; }
    const d = await call('Pairing and running the self-test (can take a minute)…', '/sandbox/pair',
      { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ url, code }) });
    if (d) { showChecks(d.selftest || {}); msg.textContent = d.enabled ? 'Paired and ready.' : 'Paired, but the self-test did not pass: it stays off.'; setTimeout(again, 2500); }
  });
  const testBtn = box.querySelector('#rn-test');
  if (testBtn) testBtn.addEventListener('click', async () => {
    const d = await call('Running the self-test (can take a minute)…', '/sandbox/selftest', { method: 'POST' });
    if (d) { showChecks(d); msg.textContent = d.ok ? 'Self-test passed.' : 'Self-test failed.'; }
  });
  const toggle = box.querySelector('#rn-toggle');
  if (toggle) toggle.addEventListener('click', async () => {
    const d = await call('Saving…', '/sandbox/enabled',
      { method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ enabled: !st.enabled }) });
    if (d) again();
  });
  const forget = box.querySelector('#rn-forget');
  if (forget) forget.addEventListener('click', async () => {
    if (!window.confirm('Forget this runner? The chat loses its code tool until you pair a runner again.')) return;
    const d = await call('Removing…', '/sandbox/runner', { method: 'DELETE' });
    if (d) again();
  });
}
window.loadRunnerPanel = loadRunnerPanel;
