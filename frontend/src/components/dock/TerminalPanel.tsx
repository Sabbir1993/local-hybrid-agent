import React, { useEffect, useMemo, useRef, useState } from 'react';
import { apiFetch } from '../../services/api';
import { useAppStore, type TermEntry } from '../../stores/useAppStore';

const COLORS: Record<number, string> = {
  30: '#6b7280', 31: '#f87171', 32: '#4ade80', 33: '#fbbf24', 34: '#60a5fa', 35: '#c084fc', 36: '#22d3ee', 37: '#e5e7eb',
  90: '#9ca3af', 91: '#fca5a5', 92: '#86efac', 93: '#fde047', 94: '#93c5fd', 95: '#d8b4fe', 96: '#67e8f9', 97: '#ffffff',
};

/** SGR colours as React spans. Text stays text, so output can never inject markup. */
function ansi(text: string): React.ReactNode[] {
  let t = String(text ?? '').replace(/\r\n/g, '\n');
  // progress bars: keep the last redraw of a line
  t = t.split('\n').map((l) => (l.includes('\r') ? l.split('\r').filter(Boolean).pop() || '' : l)).join('\n');
  t = t.replace(/\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)/g, '');   // window-title and other OSC sequences
  t = t.replace(/\x1b\[[0-9;?]*[A-HJKSTfhlsu]/g, '');
  const out: React.ReactNode[] = [];
  const re = /\x1b\[([0-9;]*)m/g;
  let last = 0;
  let fg = '';
  let bold = false;
  let m: RegExpExecArray | null;
  const push = (chunk: string, key: number) => {
    if (!chunk) return;
    const clean = chunk.replace(/\x1b/g, '');
    out.push(fg || bold ? <span key={key} style={{ color: fg || undefined, fontWeight: bold ? 700 : undefined }}>{clean}</span> : <React.Fragment key={key}>{clean}</React.Fragment>);
  };
  while ((m = re.exec(t))) {
    push(t.slice(last, m.index), out.length);
    last = re.lastIndex;
    (m[1] === '' ? ['0'] : m[1].split(';')).forEach((c) => {
      const n = parseInt(c, 10);
      if (n === 0) { fg = ''; bold = false; }
      else if (n === 1) bold = true;
      else if (n === 22) bold = false;
      else if (n === 39) fg = '';
      else if (COLORS[n]) fg = COLORS[n];
    });
  }
  push(t.slice(last), out.length);
  return out;
}

const HISTORY_KEY = 'term_history';   // kept for this browser session only: commands can contain secrets

export const TerminalPanel: React.FC = () => {
  const { appMode, activeProjectId, projects, activeCustomAgent, termEntries, addTermEntry, updateTermEntry, clearTerm } = useAppStore();
  const [shell, setShell] = useState(() => localStorage.getItem('default_terminal_shell') || 'powershell');
  const [cmd, setCmd] = useState('');
  const [running, setRunning] = useState(false);
  const [cwd, setCwd] = useState('');
  const [online, setOnline] = useState<{ connected: boolean; name?: string } | null>(null);
  const ctl = useRef<AbortController | null>(null);
  const outRef = useRef<HTMLDivElement>(null);
  const history = useRef<string[]>((() => { try { return JSON.parse(sessionStorage.getItem(HISTORY_KEY) || '[]'); } catch (_) { return []; } })());
  const histIdx = useRef(-1);
  const draft = useRef('');
  // System (administrator) terminal: asked every time, never remembered, Windows only
  const [sys, setSys] = useState(false);
  const [askAdmin, setAskAdmin] = useState(false);
  const [adminBusy, setAdminBusy] = useState(false);
  const [adminErr, setAdminErr] = useState('');
  const [adminAllowed, setAdminAllowed] = useState(false);
  const isWindows = /windows/i.test(navigator.userAgent);
  const sysRef = useRef(false);
  sysRef.current = sys;

  const endSystem = (note?: string) => {
    if (!sysRef.current) return;
    setSys(false);
    setCwd(target.ok ? target.dir! : '');
    apiFetch('/control/companion/admin-terminal/stop', { method: 'POST', background: true }).catch(() => {});
    if (note) addTermEntry({ id: 't' + Date.now(), cmd: 'System terminal', source: 'system', status: 'done', stderr: note, exitCode: 0, time: Date.now() });
  };

  const startSystem = async () => {
    setAdminBusy(true);
    setAdminErr('');
    try {
      const r = await apiFetch('/control/companion/admin-terminal/start', { method: 'POST' });
      const d = await r.json().catch(() => ({}));
      if (!r.ok || !d.ok) {
        setAdminErr(typeof d.error === 'string' ? d.error : typeof d.detail === 'string' ? d.detail : `Could not start (HTTP ${r.status}).`);
      } else {
        setSys(true);
        setAskAdmin(false);
        setCwd(target.ok ? target.dir! : 'C:\\');
        if (shell !== 'powershell' && shell !== 'cmd') setShell('powershell');
      }
    } catch (e: any) {
      setAdminErr('Could not start: ' + (e?.message || e));
    } finally {
      setAdminBusy(false);
    }
  };

  useEffect(() => {
    apiFetch('/control/companion/admin-terminal', { background: true }).then((r) => r.json()).then((d) => setAdminAllowed(Boolean(d?.allowed))).catch(() => {});
  }, []);

  // while System is on: follow the server (idle expiry) and fall back to a regular terminal when it ends
  useEffect(() => {
    if (!sys) return;
    const t = window.setInterval(() => {
      apiFetch('/control/companion/admin-terminal', { background: true }).then((r) => r.json()).then((d) => {
        if (sysRef.current && d && d.active === false) { setSys(false); setCwd(target.ok ? target.dir! : ''); addTermEntry({ id: 't' + Date.now(), cmd: 'System terminal', source: 'system', status: 'done', stderr: 'The administrator terminal timed out and was switched off. This is a regular terminal again.', exitCode: 0, time: Date.now() }); }
      }).catch(() => {});
    }, 20000);
    return () => window.clearInterval(t);
  }, [sys]);

  // leaving the panel, or changing project/agent, always ends administrator rights
  useEffect(() => () => { if (sysRef.current) apiFetch('/control/companion/admin-terminal/stop', { method: 'POST', background: true }).catch(() => {}); }, []);
  useEffect(() => { if (sysRef.current) endSystem('Switched to a regular terminal.'); }, [appMode, activeProjectId, activeCustomAgent?.id]);

  // Where commands run: the project folder (Code) or the agent's work folder (Agent)
  const target = useMemo(() => {
    if (appMode === 'code') {
      const p = projects.find((x) => x.id === activeProjectId);
      return p?.workspace_dir ? { ok: true, dir: p.workspace_dir, name: p.name } : { ok: false, reason: 'No project selected. Select a project in Code mode to use the terminal.' };
    }
    if (appMode === 'agent') {
      if (activeCustomAgent?.work_dir) return { ok: true, dir: activeCustomAgent.work_dir as string, name: activeCustomAgent.name as string };
      if (activeCustomAgent) return { ok: false, reason: `Agent "${activeCustomAgent.name}" has no workspace folder. Fork or edit it to set one.` };
      return { ok: false, reason: 'No custom agent selected. Pick an agent with a workspace folder to use the terminal.' };
    }
    return { ok: false, reason: 'The terminal needs a project (Code mode) or a custom agent with a workspace folder (Agent mode).' };
  }, [appMode, activeProjectId, projects, activeCustomAgent]);

  useEffect(() => {
    if (target.ok) setCwd(target.dir!);
  }, [target.ok, target.ok ? target.dir : '']);

  useEffect(() => {
    let alive = true;
    const poll = () => apiFetch('/control/companion/status', { background: true })
      .then((r) => r.json())
      .then((d) => { if (alive) setOnline({ connected: Boolean(d?.connected), name: d?.device_name || d?.device_id }); })
      .catch(() => {});
    poll();
    const t = window.setInterval(poll, 15000);
    return () => { alive = false; window.clearInterval(t); };
  }, []);

  useEffect(() => {
    const el = outRef.current;
    if (el) el.scrollTop = el.scrollHeight;
  }, [termEntries]);

  const promptText = () => {
    let dir = cwd;
    if (dir.length > 38) dir = '…' + dir.slice(-37);
    return shell === 'bash' ? `${dir} $` : shell === 'cmd' ? `${dir}>` : `PS ${dir}>`;
  };

  const run = async (command: string) => {
    if (!target.ok && !sys) {
      addTermEntry({ id: 't' + Date.now(), cmd: command, source: 'system', status: 'done', stderr: (target as any).reason, exitCode: 1, time: Date.now() });
      return;
    }
    const id = 'term_' + Math.random().toString(36).slice(2, 9);
    addTermEntry({ id, cmd: command, source: 'user', status: 'running', time: Date.now() });
    setRunning(true);
    ctl.current = new AbortController();
    // a fresh process per command: `cd` also prints the new folder so the prompt can follow it
    let send = command;
    const cd = command.trim().match(/^cd(?:\s+(.*))?$/i);
    if (cd) {
      const to = (cd[1] || '').trim();
      send = shell === 'cmd' ? (to ? `cd /d ${to} && cd` : 'cd') : shell === 'bash' ? (to ? `cd ${to} && pwd` : 'cd ~ && pwd') : (to ? `cd ${to}; (Get-Location).Path` : 'cd ~; (Get-Location).Path');
    }
    try {
      const r = await apiFetch('/control/companion/shell', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ command: send, cwd: cwd || (target.ok ? target.dir : 'C:\\'), shell, ...(sys ? { elevated: true } : {}) }),
        signal: ctl.current.signal,
      });
      const d = await r.json().catch(() => ({ ok: false, error: 'Non-JSON server response' }));
      const code = typeof d.exit_code === 'number' ? d.exit_code : d.ok && r.ok ? 0 : 1;
      let stdout = d.stdout || '';
      if (cd && code === 0 && stdout) {
        const lines = stdout.trim().split(/\r?\n/).map((l: string) => l.trim()).filter(Boolean);
        if (lines.length) { setCwd(lines[lines.length - 1]); stdout = ''; }   // the folder line is the new prompt, not output
      }
      if (d.admin_required && sys) { setSys(false); setCwd(target.ok ? target.dir! : ''); }
      const err = d.stderr || d.error || d.detail || (!r.ok ? `HTTP Error ${r.status}` : '');
      updateTermEntry(id, { status: 'done', stdout, stderr: typeof err === 'string' ? err : JSON.stringify(err), exitCode: code });
    } catch (e: any) {
      updateTermEntry(id, {
        status: 'done',
        stderr: e?.name === 'AbortError' ? '^C stopped waiting. The command may still finish on the device (60s limit).' : 'Execution failed: ' + e.message,
        exitCode: e?.name === 'AbortError' ? 130 : 1,
      });
    } finally {
      setRunning(false);
      ctl.current = null;
    }
  };

  const submit = async (e: React.FormEvent) => {
    e.preventDefault();
    const c = cmd.trim();
    if (!c || running) return;
    setCmd('');
    history.current.push(c);
    if (history.current.length > 200) history.current.shift();
    try { sessionStorage.setItem(HISTORY_KEY, JSON.stringify(history.current)); } catch (_) {}
    histIdx.current = -1;
    if (/^(clear|cls)$/i.test(c)) { clearTerm(); return; }
    await run(c);
  };

  const onKey = (e: React.KeyboardEvent<HTMLInputElement>) => {
    const h = history.current;
    if ((e.key === 'ArrowUp' || e.key === 'ArrowDown') && h.length) {
      e.preventDefault();
      if (histIdx.current === -1) { draft.current = cmd; histIdx.current = h.length; }
      histIdx.current += e.key === 'ArrowUp' ? -1 : 1;
      if (histIdx.current < 0) histIdx.current = 0;
      if (histIdx.current >= h.length) { histIdx.current = -1; setCmd(draft.current); } else setCmd(h[histIdx.current]);
    } else if (e.ctrlKey && e.key.toLowerCase() === 'l') {
      e.preventDefault();
      clearTerm();
    } else if (e.ctrlKey && e.key.toLowerCase() === 'c' && !String(window.getSelection() || '')) {
      e.preventDefault();
      if (running) ctl.current?.abort();
      else if (cmd) { addTermEntry({ id: 't' + Date.now(), cmd: cmd + '^C', source: 'user', status: 'done', time: Date.now() }); setCmd(''); }
    }
  };

  const fmt = (t: number) => new Date(t).toLocaleTimeString([], { hour12: false, hour: '2-digit', minute: '2-digit', second: '2-digit' });

  return (
    <>
      <div className="term-toolbar">
        <div className="term-status-wrap">
          <span className="term-indicator" id="term-status-dot" style={{ display: 'inline-block', width: 8, height: 8, borderRadius: '50%', marginRight: 6, background: online?.connected ? 'var(--green, #3fb950)' : 'var(--dim)' }} />
          <span className="term-title" id="term-status-text" style={{ fontSize: 11, fontWeight: 600 }}>
            {online?.connected ? `Companion Online (${online.name || 'paired'})` : 'Local Shell'}
          </span>
          <span className="term-cwd" id="term-cwd-display" title={target.ok ? `${target.name}: ${target.dir}` : (target as any).reason} style={{ fontSize: '10.5px', marginLeft: 8, color: target.ok ? 'var(--dim)' : 'var(--amber, #f59e0b)' }}>
            {target.ok ? `📁 ${cwd}` : '🔒 (No workspace selected)'}
          </span>
        </div>
        <div className="term-toolbar-actions" style={{ display: 'flex', alignItems: 'center', gap: 4 }}>
          <select
            id="term-mode-select" title={isWindows ? (adminAllowed ? 'Project: regular rights. System: administrator rights (asks for confirmation)' : 'System (administrator) needs the terminal.admin permission') : 'The administrator terminal is available on Windows only'}
            value={sys ? 'system' : 'project'} disabled={adminBusy}
            onChange={(e) => { if (e.target.value === 'system') { setAdminErr(''); setAskAdmin(true); } else endSystem('Switched to a regular terminal.'); }}
            style={{ padding: '2px 6px', fontSize: 11, background: sys ? '#7f1d1d' : 'var(--panel2)', border: '1px solid var(--border)', borderRadius: 4, color: 'inherit' }}
          >
            <option value="project">{appMode === 'agent' ? 'Agent' : 'Project'}</option>
            <option value="system" disabled={!isWindows || !adminAllowed}>System (admin)</option>
          </select>
          <select
            id="term-shell-select" className="term-shell-select" title="Choose shell interpreter" value={shell}
            onChange={(e) => { setShell(e.target.value); try { localStorage.setItem('default_terminal_shell', e.target.value); } catch (_) {} }}
            style={{ padding: '2px 6px', fontSize: 11, background: 'var(--panel2)', border: '1px solid var(--border)', borderRadius: 4, color: 'inherit' }}
          >
            <option value="powershell">PowerShell</option>
            <option value="cmd">CMD</option>
            <option value="bash" disabled={sys}>Bash</option>
            <option value="pwsh">PWSH</option>
          </select>
          <button className="btn ghost ln-small" id="term-btn-clear" title="Clear console (Ctrl+L)" style={{ width: 'auto', margin: 0, padding: '2px 8px', fontSize: 11 }} onClick={clearTerm}>Clear</button>
        </div>
      </div>

      {sys && (
        <div role="alert" style={{ background: '#7f1d1d', color: '#fff', fontSize: 11, padding: '4px 10px', display: 'flex', alignItems: 'center', gap: 8 }}>
          <strong>ADMINISTRATOR</strong>
          <span style={{ flex: 1 }}>Commands you type here run with full rights on your PC. Ends after inactivity.</span>
          <button className="btn ghost ln-small" style={{ width: 'auto', margin: 0, padding: '1px 8px', fontSize: 11, color: '#fff', borderColor: '#fca5a5' }} onClick={() => endSystem('Switched to a regular terminal.')}>Switch to regular</button>
        </div>
      )}
      {askAdmin && (
        <div role="dialog" aria-modal="true" aria-label="Enable administrator terminal" style={{ position: 'fixed', inset: 0, background: 'rgba(0,0,0,.55)', zIndex: 1000, display: 'flex', alignItems: 'center', justifyContent: 'center' }}>
          <div style={{ background: 'var(--panel, #161b22)', border: '1px solid var(--border)', borderRadius: 10, padding: 18, maxWidth: 440, fontSize: 13 }}>
            <div style={{ fontWeight: 700, fontSize: 15, marginBottom: 8 }}>Turn on the administrator terminal?</div>
            <p style={{ margin: '0 0 8px' }}>Commands you type will run with <strong>administrator rights</strong> on your PC, so they can read and change system files and settings. Use it for system analysis only.</p>
            <ul style={{ margin: '0 0 8px', paddingLeft: 18, color: 'var(--dim)' }}>
              <li>You will be asked again in a window on your PC, then by Windows (UAC).</li>
              <li>Only commands you type here are elevated. The agent never is.</li>
              <li>Each command is recorded in the audit log. It is not remembered: it ends when you switch back, close this panel, or after inactivity.</li>
            </ul>
            {adminErr && <div style={{ color: '#f87171', marginBottom: 8 }}>{adminErr}</div>}
            <div style={{ display: 'flex', justifyContent: 'flex-end', gap: 8 }}>
              <button className="btn ghost" style={{ width: 'auto', margin: 0 }} disabled={adminBusy} onClick={() => setAskAdmin(false)}>Cancel</button>
              <button className="btn" style={{ width: 'auto', margin: 0, background: '#b91c1c' }} disabled={adminBusy} onClick={startSystem}>{adminBusy ? 'Waiting for confirmation on your PC…' : 'Continue'}</button>
            </div>
          </div>
        </div>
      )}

      <div className="term-body" id="term-output" ref={outRef} tabIndex={-1} style={{ flex: 1, overflowY: 'auto', padding: 10, fontFamily: 'monospace', fontSize: '11.5px', background: 'var(--bg-code, #0d1117)' }}>
        {termEntries.length === 0 && (
          <div style={{ color: '#8b949e' }}>
            Console. Runs on your device through the companion. Commands the agent runs show here too.<br />
            Up and Down for history, Ctrl+L to clear, Ctrl+C to stop waiting.
          </div>
        )}
        {termEntries.map((en: TermEntry) => (
          <div key={en.id} className="term-entry">
            <div className="term-cmd-line">
              <span className={`term-prompt-tag${en.source === 'agent' ? ' agent' : ''}`}>{en.source === 'agent' ? '⚙' : promptText()}</span>{' '}
              <span className="term-cmd-text">{en.source !== 'user' && <span className="term-src">[{en.source}] </span>}{en.cmd}</span>{' '}
              <span className="term-meta">{fmt(en.time)}</span>
            </div>
            {en.status === 'running' && <div className="term-stdout term-run-indicator">running</div>}
            {en.stdout ? <div className="term-stdout" style={{ whiteSpace: 'pre-wrap' }}>{ansi(en.stdout)}</div> : null}
            {en.stderr ? <div className="term-stderr" style={{ whiteSpace: 'pre-wrap' }}>{ansi(en.stderr)}</div> : null}
            {typeof en.exitCode === 'number' && en.exitCode !== 0 && <div className="term-exit-badge term-exit-err">exit {en.exitCode}</div>}
          </div>
        ))}
      </div>

      <form className="term-input-bar" id="term-input-form" onSubmit={submit} style={{ display: 'flex', borderTop: '1px solid var(--border)', padding: '6px 8px', background: 'var(--panel2)' }}>
        <span className="term-prompt-char" id="term-prompt" title={cwd} style={{ marginRight: 6, color: '#58a6ff', fontWeight: 700, whiteSpace: 'nowrap' }}>{target.ok ? promptText() : '$'}</span>
        <input
          type="text" id="term-input" className="term-cmd-input" value={cmd} onChange={(e) => setCmd(e.target.value)} onKeyDown={onKey}
          placeholder={running ? 'running… Ctrl+C to stop waiting' : target.ok ? '' : (target as any).reason}
          autoComplete="off" spellCheck={false}
          style={{ flex: 1, background: 'transparent', border: 'none', outline: 'none', color: 'inherit', fontSize: 12, fontFamily: 'monospace' }}
        />
        <button type="submit" className="term-run-btn" id="term-btn-run" disabled={running} style={{ marginLeft: 6, padding: '3px 10px', fontSize: 11, background: 'var(--accent)', color: '#fff', border: 'none', borderRadius: 4, cursor: 'pointer' }}>Run</button>
      </form>
    </>
  );
};
