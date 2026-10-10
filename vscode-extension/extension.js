// A770 agent for VS Code: ask about the selection / run a task, watch it work, answer approval cards.
// No autocomplete, by design. The agent works on your machine through the SSL Local Agent (Companion); the
// server holds no workspace. The API token lives in VS Code's SecretStorage, never in settings.json.
'use strict';

const vscode = require('vscode');
const lib = require('./lib');

const TOKEN_KEY = 'a770.apiToken';
let output;

function crypto16() {
  return require('crypto').randomBytes(12).toString('base64url');
}

async function getToken(context) {
  const token = await context.secrets.get(TOKEN_KEY);
  if (token) return token;
  return setToken(context);
}

async function setToken(context) {
  const value = await vscode.window.showInputBox({
    title: 'A770 API token', prompt: 'Paste the API token an administrator issued you (stored in VS Code SecretStorage)',
    password: true, ignoreFocusOut: true, placeHolder: 'a770_pat_...',
  });
  if (!value) return undefined;
  if (!value.trim().startsWith('a770_pat_')) {
    vscode.window.showErrorMessage('That does not look like an A770 API token (it starts with a770_pat_).');
    return undefined;
  }
  await context.secrets.store(TOKEN_KEY, value.trim());
  return value.trim();
}

async function api(context, path, init = {}) {
  const base = lib.normalizeBase(vscode.workspace.getConfiguration('a770').get('baseUrl'));
  const token = await getToken(context);
  if (!token) throw new Error('no API token set');
  const headers = { Authorization: `Bearer ${token}`, 'User-Agent': 'A770NativeApp/1.0', ...(init.headers || {}) };
  const device = vscode.workspace.getConfiguration('a770').get('deviceId') || context.workspaceState.get('a770.device');
  if (device) headers['X-Device-Id'] = device;
  return fetch(base + path, { ...init, headers });
}

async function discoverDevice(context) {
  if (vscode.workspace.getConfiguration('a770').get('deviceId')) return;
  try {
    const r = await api(context, '/control/companion/status');
    const info = r.ok ? await r.json() : {};
    if (info.connected && info.device_id) context.workspaceState.update('a770.device', info.device_id);
  } catch (e) { /* reported by the run itself */ }
}

async function answerCard(context, data) {
  const choices = lib.cardChoices(String(data.kind || 'shell'));
  const picked = await vscode.window.showWarningMessage(lib.cardMessage(data), { modal: true },
    ...choices.map((c) => c.label));
  const choice = choices.find((c) => c.label === picked);
  const decision = choice ? choice.decision : 'deny';
  await api(context, '/agent/permission', {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ req_id: data.req_id, decision }),
  });
  output.appendLine(`  -> ${decision === 'deny' ? 'denied' : 'allowed'}`);
}

async function runTask(context, prompt) {
  output.show(true);
  output.appendLine(`\n=== ${lib.oneLine(prompt, 120)}`);
  const runId = 'vsc-' + crypto16();
  const permissionMode = vscode.workspace.getConfiguration('a770').get('permissionMode') || 'auto';
  await vscode.window.withProgress({ location: vscode.ProgressLocation.Notification, title: 'A770 agent', cancellable: true },
    async (progress, cancelToken) => {
      const controller = new AbortController();
      cancelToken.onCancellationRequested(() => {
        controller.abort();
        api(context, `/agent/run/${runId}/cancel`, { method: 'POST' }).catch(() => {});
        output.appendLine('\n[stopped: the run was cancelled on the server]');
      });
      let resp;
      try {
        resp = await api(context, '/agent/run', {
          method: 'POST', signal: controller.signal, headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ messages: [{ role: 'user', content: prompt }], mode: 'auto',
            permission_mode: permissionMode, client_run_id: runId }),
        });
      } catch (e) {
        output.appendLine(`error: ${lib.oneLine(e.message)}`);
        return;
      }
      if (!resp.ok) {
        output.appendLine(`error: HTTP ${resp.status}: ${lib.oneLine(await resp.text(), 300)}`);
        return;
      }
      const parser = new lib.SseParser();
      const renderer = new lib.Renderer();
      const decoder = new TextDecoder();
      try {
        for await (const chunk of resp.body) {
          for (const ev of parser.push(decoder.decode(chunk, { stream: true }))) {
            if (ev.event === 'permission_request') {
              await answerCard(context, ev.data);
              continue;
            }
            if (ev.event === 'tool_call') progress.report({ message: lib.clean(ev.data.name) });
            for (const text of renderer.feed(ev.event, ev.data)) output.append(text);
          }
        }
      } catch (e) {
        if (e.name !== 'AbortError') output.appendLine(`\nerror: ${lib.oneLine(e.message)}`);
      }
    });
}

function editorContext() {
  const ed = vscode.window.activeTextEditor;
  if (!ed) return {};
  const rel = vscode.workspace.asRelativePath(ed.document.uri, false);
  const sel = ed.selection;
  return {
    file: rel, languageId: ed.document.languageId,
    selection: sel.isEmpty ? '' : ed.document.getText(sel),
    startLine: sel.isEmpty ? 0 : sel.start.line + 1, endLine: sel.isEmpty ? 0 : sel.end.line + 1,
  };
}

function activate(context) {
  output = vscode.window.createOutputChannel('A770 agent');
  context.subscriptions.push(output);
  context.subscriptions.push(
    vscode.commands.registerCommand('a770.setToken', () => setToken(context)),
    vscode.commands.registerCommand('a770.runTask', async () => {
      const question = await vscode.window.showInputBox({ title: 'A770: run a task', prompt: 'What should the agent do?',
        ignoreFocusOut: true });
      if (!question) return;
      await discoverDevice(context);
      await runTask(context, lib.buildPrompt({ question, ...editorContext() }));
    }),
    vscode.commands.registerCommand('a770.askAboutSelection', async () => {
      const ctx = editorContext();
      if (!ctx.selection) { vscode.window.showInformationMessage('Select some code first.'); return; }
      const question = await vscode.window.showInputBox({ title: 'A770: ask about the selection',
        prompt: 'Question or change to make', ignoreFocusOut: true });
      if (!question) return;
      await discoverDevice(context);
      await runTask(context, lib.buildPrompt({ question, ...ctx }));
    }),
  );
}

function deactivate() {}

module.exports = { activate, deactivate };
