/* ==========================================================================
   CLAUDE DOCK & SLOT UI CONTROLLER (v1.0)
   Manages Right Split Pane, 3-Dots Menu, Web Preview, 12-Step Plan, Slot Selector
   ========================================================================== */
"use strict";

(function() {
  // ---------------- 1. RIGHT DOCK CONTROLLER ----------------
  const RightDock = {
    activeTab: 'plan', // 'plan' | 'files' | 'preview' | 'terminal'
    isOpen: false, // Default collapsed
    wakeLock: null,
    previewMode: 'preview', // 'preview' | 'code'
    isMobileView: false,
    currentCwd: '',

    init() {
      this.dockEl = $('right-dock');
      this.planView = $('dock-view-plan');
      this.filesView = $('dock-view-files');
      this.previewView = $('dock-view-preview');
      this.terminalView = $('dock-view-terminal');
      this.tasksView = $('dock-view-tasks');
      this.keepAwakeToggle = $('toggle-keep-awake');

      if (!this.dockEl) return;

      // Start collapsed by default
      this.close();

      // Restore saved width
      try {
        const savedW = parseInt(localStorage.getItem('claude_dock_w'), 10);
        if (savedW >= 300 && savedW <= window.innerWidth * 0.75) {
          this.dockEl.style.width = savedW + 'px';
        }
      } catch (_) {}

      // Tab visibility based on Chat vs Agent / Code mode
      this.syncTabVisibility();

      // Wire header tab buttons
      $('dock-btn-plan')?.addEventListener('click', () => this.toggle('plan'));
      $('dock-btn-files')?.addEventListener('click', () => this.toggle('files'));
      $('dock-btn-preview')?.addEventListener('click', () => this.toggle('preview'));
      $('dock-btn-terminal')?.addEventListener('click', () => this.toggle('terminal'));
      $('dock-btn-tasks')?.addEventListener('click', () => this.toggle('terminal'));
      $('dock-btn-close')?.addEventListener('click', () => this.close());
      $('dock-btn-expand')?.addEventListener('click', () => this.toggleExpand());

      // Wire header right dock toggle button
      $('btn-dock-toggle')?.addEventListener('click', () => {
        if (this.isOpen) this.close();
        else this.open(this.activeTab || 'plan');
      });

      // Wire sidebar shortcuts
      $('btn-sidebar-search')?.addEventListener('click', () => {
        const inp = $('input');
        if (inp) { inp.focus(); inp.value = '/'; }
      });

      // Keep Computer Awake toggle
      if (this.keepAwakeToggle) {
        this.keepAwakeToggle.addEventListener('change', () => {
          this.setKeepAwake(this.keepAwakeToggle.checked);
        });
      }

      // Drag to resize right dock
      this.initResizeGrip();

      // Initialize Console Terminal
      this.initTerminal();

      // Keyboard shortcuts
      window.addEventListener('keydown', (e) => {
        // Ctrl+Shift+F for files
        if ((e.ctrlKey || e.metaKey) && e.shiftKey && e.key.toLowerCase() === 'f') {
          e.preventDefault();
          this.open('files');
        } else if ((e.ctrlKey || e.metaKey) && e.key === '`') {
          e.preventDefault();
          this.toggle('terminal');
        }
      });

      // Default initial view: right dock collapsed by default
      this.close();
    },

    syncTabVisibility(isAgentOrCode) {
      const mode = (typeof window.appMode !== 'undefined' ? window.appMode : (typeof appMode !== 'undefined' ? appMode : 'chat'));
      const hasCustomAgent = !!(window.activeCustomAgent || (window.customAgents && window.customAgents.getActive && window.customAgents.getActive()));
      let showExtra = (mode === 'agent' || mode === 'code' || hasCustomAgent);
      if (typeof isAgentOrCode === 'boolean') {
        showExtra = isAgentOrCode;
      }
      if (mode === 'chat' && !hasCustomAgent) {
        showExtra = false;
      }

      const codeBtn = $('dock-btn-files');
      const termBtn = $('dock-btn-terminal');

      if (codeBtn) codeBtn.style.setProperty('display', showExtra ? '' : 'none', 'important');
      if (termBtn) termBtn.style.setProperty('display', showExtra ? '' : 'none', 'important');

      if (!showExtra && (this.activeTab === 'files' || this.activeTab === 'terminal')) {
        this.open('plan');
      }
    },

    initSidebarResizeGrip() {
      // Left sidebar is fixed (not draggable to expand or shrink)
      try {
        localStorage.removeItem('claude_sidebar_w');
        const sidebar = $('sidebar');
        if (sidebar) sidebar.style.width = '';
      } catch (_) {}
    },

    setTerminalCwd(cwd, label) {
      this.currentCwd = cwd || '';
      const cwdEl = $('term-cwd-display');
      if (cwdEl) {
        cwdEl.textContent = label ? `📁 ${label}` : (cwd ? `📁 ${cwd}` : '📁 (workspace)');
        cwdEl.title = cwd || 'Active workspace directory';
      }
      this.logTerminal({
        cmd: `cd "${cwd || '.'}"`,
        source: 'system',
        stdout: `Working directory changed to: ${cwd || 'default'}`
      });
    },

    open(tab) {
      this.activeTab = tab;
      this.isOpen = true;
      if (this.dockEl) {
        this.dockEl.classList.remove('dock-collapsed');
      }

      // Update Tab Buttons
      $('dock-btn-plan')?.classList.toggle('active', tab === 'plan');
      $('dock-btn-files')?.classList.toggle('active', tab === 'files');
      $('dock-btn-preview')?.classList.toggle('active', tab === 'preview');
      $('dock-btn-terminal')?.classList.toggle('active', tab === 'terminal');

      // Activate corresponding view container
      [this.planView, this.filesView, this.previewView, this.terminalView, this.tasksView].forEach(v => {
        if (v) v.classList.remove('active');
      });

      if (tab === 'plan' && this.planView) this.planView.classList.add('active');
      if (tab === 'files' && this.filesView) {
        this.filesView.classList.add('active');
        if (typeof wsRefreshTree === 'function') wsRefreshTree();
      }
      if (tab === 'preview' && this.previewView) this.previewView.classList.add('active');
      if (tab === 'terminal' && this.terminalView) {
        this.terminalView.classList.add('active');
        const inp = $('term-input');
        if (inp) { inp.placeholder = ''; inp.focus(); }
      }
    },

    close() {
      this.isOpen = false;
      if (this.dockEl) this.dockEl.classList.add('dock-collapsed');
    },

    toggle(tab) {
      if (this.isOpen && this.activeTab === tab) {
        this.close();
      } else {
        this.open(tab);
      }
    },

    toggleExpand() {
      if (!this.dockEl) return;
      const curW = this.dockEl.offsetWidth;
      if (curW > 600) {
        this.dockEl.style.width = '480px';
      } else {
        this.dockEl.style.width = '720px';
      }
      try { localStorage.setItem('claude_dock_w', parseInt(this.dockEl.style.width, 10)); } catch (_) {}
    },

    // ---------------- CONSOLE TERMINAL CONTROLLER ----------------
    // Not a PTY: every command is one request to /control/companion/shell (companion `shell.run`, a fresh
    // process each time). The prompt, history, cwd tracking (`cd`), colours and Ctrl+C below are the client side.
    termHistory: [],
    termHistIdx: -1,
    termDraft: '',

    initTerminal() {
      const clearBtn = $('term-btn-clear');
      const form = $('term-input-form');
      const input = $('term-input');
      const shellSel = $('term-shell-select');
      const body = $('term-output');

      try { this.termHistory = JSON.parse(localStorage.getItem('term_history') || '[]').slice(-200); } catch (_) { this.termHistory = []; }

      if (shellSel) {
        shellSel.value = localStorage.getItem('default_terminal_shell') || 'powershell';
        shellSel.addEventListener('change', () => {
          localStorage.setItem('default_terminal_shell', shellSel.value);
          this.updatePrompt();
          input?.focus();
        });
      }

      clearBtn?.addEventListener('click', () => { this.clearTerminal(); input?.focus(); });

      // a real terminal takes focus wherever you click, unless you are selecting text
      body?.addEventListener('mouseup', () => {
        if (!String(window.getSelection() || '')) input?.focus();
      });

      form?.addEventListener('submit', async (e) => {
        e.preventDefault();
        if (this._termRunning) return;
        const cmd = (input?.value || '').trim();
        if (!cmd) return;
        input.value = '';
        this.termHistory.push(cmd);
        if (this.termHistory.length > 200) this.termHistory.shift();
        try { localStorage.setItem('term_history', JSON.stringify(this.termHistory)); } catch (_) {}
        this.termHistIdx = -1;
        if (/^(clear|cls)$/i.test(cmd)) { this.clearTerminal(); return; }
        await this.runTerminalCommand(cmd);
      });

      input?.addEventListener('keydown', (e) => {
        if (e.key === 'ArrowUp' || e.key === 'ArrowDown') {
          if (!this.termHistory.length) return;
          e.preventDefault();
          if (this.termHistIdx === -1) { this.termDraft = input.value; this.termHistIdx = this.termHistory.length; }
          this.termHistIdx += e.key === 'ArrowUp' ? -1 : 1;
          if (this.termHistIdx < 0) this.termHistIdx = 0;
          if (this.termHistIdx >= this.termHistory.length) { this.termHistIdx = -1; input.value = this.termDraft; }
          else input.value = this.termHistory[this.termHistIdx];
          requestAnimationFrame(() => input.setSelectionRange(input.value.length, input.value.length));
        } else if (e.ctrlKey && e.key.toLowerCase() === 'l') {
          e.preventDefault();
          this.clearTerminal();
        } else if (e.ctrlKey && e.key.toLowerCase() === 'c' && !String(window.getSelection() || '')) {
          e.preventDefault();
          if (this._termRunning && this._termCtl) this._termCtl.abort();
          else if (input.value) { this.logTerminal({ cmd: input.value + '^C', source: 'user', stdout: '' }); input.value = ''; }
        }
      });

      this.updateTerminalStatus();
      // the project / agent is often not loaded yet on refresh, and can change later: follow it
      setInterval(() => {
        const t = this.getValidTerminalTarget();
        const key = (t.valid ? t.dir : '') + '|' + t.valid;
        if (key !== this._termTargetKey) { this._termTargetKey = key; this.updateTerminalStatus(); }
      }, 1000);
    },

    clearTerminal() {
      const out = $('term-output');
      if (out) out.innerHTML = '';
    },

    // "PS E:\proj>" for PowerShell, "E:\proj>" for cmd, "~/proj $" for bash
    promptText() {
      const shell = $('term-shell-select')?.value || localStorage.getItem('default_terminal_shell') || 'powershell';
      let dir = this.currentCwd || '';
      if (dir.length > 38) dir = '…' + dir.slice(-37);
      if (shell === 'bash') return `${dir} $`;
      if (shell === 'cmd') return `${dir}>`;
      return `PS ${dir}>`;
    },

    updatePrompt() {
      const el = $('term-prompt');
      if (el) { el.textContent = this.promptText(); el.title = this.currentCwd || ''; }
    },

    // SGR colours -> spans. Text is HTML-escaped first, so output can never inject markup.
    ansiToHtml(text) {
      const COLORS = { 30: '#6b7280', 31: '#f87171', 32: '#4ade80', 33: '#fbbf24', 34: '#60a5fa', 35: '#c084fc', 36: '#22d3ee', 37: '#e5e7eb',
                       90: '#9ca3af', 91: '#fca5a5', 92: '#86efac', 93: '#fde047', 94: '#93c5fd', 95: '#d8b4fe', 96: '#67e8f9', 97: '#ffffff' };
      let t = String(text == null ? '' : text).replace(/\r\n/g, '\n');
      t = t.split('\n').map(l => (l.includes('\r') ? l.split('\r').filter(Boolean).pop() || '' : l)).join('\n');   // progress bars: keep the last redraw
      t = esc(t).replace(/\x1b\[[0-9;?]*[A-HJKSTfhlsu]/g, '');
      let open = false, out = '', last = 0, fg = '', bold = false;
      const re = /\x1b\[([0-9;]*)m/g;
      let m;
      const flush = () => { if (open) { out += '</span>'; open = false; } };
      while ((m = re.exec(t))) {
        out += t.slice(last, m.index);
        last = re.lastIndex;
        flush();
        (m[1] === '' ? ['0'] : m[1].split(';')).forEach(c => {
          const n = parseInt(c, 10);
          if (n === 0) { fg = ''; bold = false; }
          else if (n === 1) bold = true;
          else if (n === 22) bold = false;
          else if (n === 39) fg = '';
          else if (COLORS[n]) fg = COLORS[n];
        });
        if (fg || bold) { out += `<span${fg ? ` style="color:${fg}"` : ''}${bold ? ' class="term-ansi-bold"' : ''}>`; open = true; }
      }
      out += t.slice(last);
      flush();
      return out.replace(/\x1b/g, '');
    },

    getValidTerminalTarget() {
      const mode = window.appMode || (typeof appMode !== 'undefined' ? appMode : 'chat');
      if (mode === 'code') {
        if (window.curProject && window.curProject.workspace_dir) {
          return {
            type: 'project',
            name: window.curProject.name || 'Project',
            dir: window.curProject.workspace_dir,
            valid: true
          };
        }
        return {
          type: 'project',
          valid: false,
          reason: 'No project selected. Select a project in Code mode to use terminal.'
        };
      }
      if (mode === 'agent') {
        const agent = (typeof window.getActiveCustomAgent === 'function' ? window.getActiveCustomAgent() : window.activeCustomAgent);
        if (agent && agent.work_dir) {
          return {
            type: 'agent',
            name: agent.name || 'Custom Agent',
            dir: agent.work_dir,
            valid: true
          };
        }
        if (agent) {
          return {
            type: 'agent',
            valid: false,
            reason: `Agent "${agent.name}" has no workspace path bound. Fork or edit it to bind a folder.`
          };
        }
        return {
          type: 'agent',
          valid: false,
          reason: 'No custom agent selected. Select a custom agent with a workspace folder to use terminal.'
        };
      }
      return {
        type: 'chat',
        valid: false,
        reason: 'Terminal requires selecting a valid Project (Code mode) or Custom Agent with workspace (Agent mode).'
      };
    },

    async updateTerminalStatus() {
      const target = this.getValidTerminalTarget();
      const input = $('term-input');
      const runBtn = $('term-btn-run');
      const cwdEl = $('term-cwd-display');

      if (!target.valid) {
        // stay typeable: Enter explains what is missing instead of the field being locked
        if (input) {
          input.disabled = false;
          input.placeholder = target.reason;
        }
        if (runBtn) runBtn.disabled = false;
        if (cwdEl) {
          cwdEl.textContent = '🔒 (No workspace selected)';
          cwdEl.title = target.reason;
          cwdEl.style.color = 'var(--amber, #f59e0b)';
        }
        return;
      }

      // Valid workspace selected!
      if (input) {
        input.disabled = false;
        input.placeholder = this._termRunning ? 'running… Ctrl+C to stop waiting' : '';
      }
      if (runBtn) runBtn.disabled = false;
      // `cd` moves the prompt; only a different project / agent folder resets it
      if (this._cwdFor !== target.dir) { this._cwdFor = target.dir; this.currentCwd = target.dir; }
      this.updatePrompt();
      if (cwdEl) {
        cwdEl.textContent = `📁 ${target.dir}`;
        cwdEl.title = `${target.name}: ${target.dir}`;
        cwdEl.style.color = '';
      }

      try {
        const r = await fetch('/control/companion/status');
        const d = await r.json();
        const dot = $('term-status-dot');
        const txt = $('term-status-text');

        if (d && d.connected) {
          if (dot) { dot.className = 'term-indicator'; dot.style.background = 'var(--green)'; }
          if (txt) txt.textContent = `Companion Online (${d.device_name || d.device_id || 'paired'})`;
        } else {
          if (dot) { dot.className = 'term-indicator disconnected'; dot.style.background = 'var(--dim)'; }
          if (txt) txt.textContent = 'Local Shell';
        }
      } catch (_) {}
    },

    logTerminal(data) {
      const out = $('term-output');
      if (!out) return;

      const timeStr = new Date().toLocaleTimeString([], { hour12: false, hour: '2-digit', minute: '2-digit', second: '2-digit' });
      const entry = document.createElement('div');
      entry.className = 'term-entry';
      if (data.id) entry.dataset.id = data.id;

      const isAgent = data.source === 'agent';
      const srcTag = isAgent ? '<span class="term-src">[agent]</span>'
        : data.source === 'companion' ? '<span class="term-src">[companion]</span>'
        : data.source === 'system' ? '<span class="term-src">[system]</span>' : '';
      const prompt = isAgent ? '⚙' : this.promptText();

      let html = `
        <div class="term-cmd-line">
          <span class="term-prompt-tag${isAgent ? ' agent' : ''}">${esc(prompt)}</span>
          <span class="term-cmd-text">${srcTag}${esc(data.cmd || '')}</span>
          <span class="term-meta">${timeStr}</span>
        </div>`;

      if (data.status === 'running') {
        html += `<div class="term-stdout term-run-indicator" id="term-run-${data.id || 'curr'}">running</div>`;
      } else {
        if (data.stdout) html += `<div class="term-stdout">${this.ansiToHtml(data.stdout)}</div>`;
        if (data.stderr) html += `<div class="term-stderr">${this.ansiToHtml(data.stderr)}</div>`;
        if (typeof data.exit_code === 'number' && data.exit_code !== 0) {
          html += `<div class="term-exit-badge term-exit-err">exit ${data.exit_code}</div>`;
        }
      }

      entry.innerHTML = html;
      out.appendChild(entry);
      out.scrollTop = out.scrollHeight;
    },

    // append stdout / stderr / exit code to a finished entry
    _termFinish(entry, stdout, stderr, code) {
      entry.querySelector('.term-run-indicator')?.remove();
      if (stdout) {
        const o = document.createElement('div');
        o.className = 'term-stdout';
        o.innerHTML = this.ansiToHtml(stdout);
        entry.appendChild(o);
      }
      if (stderr) {
        const e = document.createElement('div');
        e.className = 'term-stderr';
        e.innerHTML = this.ansiToHtml(stderr);
        entry.appendChild(e);
      }
      if (code !== 0) {
        const badge = document.createElement('div');
        badge.className = 'term-exit-badge term-exit-err';
        badge.textContent = `exit ${code}`;
        entry.appendChild(badge);
      }
      const out = $('term-output');
      if (out) out.scrollTop = out.scrollHeight;
    },

    logTerminalResult(data) {
      const out = $('term-output');
      if (!out) return;

      const el = data.id ? out.querySelector(`[data-id="${data.id}"]`) : null;
      if (el) {
        const raw = String(data.result || '');
        let stdout = raw, stderr = '', exitCode = data.ok ? 0 : 1;

        if (raw.includes('--- stdout ---')) {
          const parts = raw.split('--- stdout ---');
          if (parts[1].includes('--- stderr ---')) {
            const sub = parts[1].split('--- stderr ---');
            stdout = sub[0].trim();
            stderr = sub[1].trim();
          } else {
            stdout = parts[1].trim();
          }
        }
        const m = raw.match(/exit code (-?\d+)/);
        if (m) exitCode = parseInt(m[1], 10);
        this._termFinish(el, stdout, stderr, exitCode);
      } else {
        this.logTerminal({
          cmd: 'shell.run result',
          source: 'companion',
          stdout: typeof data.result === 'string' ? data.result : JSON.stringify(data.result),
          exit_code: data.ok ? 0 : 1
        });
      }
    },

    async runTerminalCommand(cmd) {
      const target = this.getValidTerminalTarget();
      if (!target.valid) {
        this.logTerminal({ cmd, source: 'system', stderr: target.reason, exit_code: 1 });
        return;
      }

      this._termRunning = true;
      this._termCtl = new AbortController();
      const input = $('term-input');
      if (input) input.placeholder = 'running… Ctrl+C to stop waiting';

      const runId = 'term_' + Math.random().toString(36).substring(2, 9);
      this.logTerminal({ cmd, source: 'user', status: 'running', id: runId });
      const entry = () => $('term-output')?.querySelector(`[data-id="${runId}"]`);

      try {
        const cwd = this.currentCwd || target.dir;
        const shell = $('term-shell-select')?.value || localStorage.getItem('default_terminal_shell') || 'powershell';
        const headers = { 'Content-Type': 'application/json' };
        if (typeof getDeviceHeaders === 'function') {
          try { Object.assign(headers, getDeviceHeaders()); } catch (_) {}
        }

        // a fresh process per command: `cd` is run together with a print of the new folder so the prompt can follow it
        let sendCmd = cmd;
        const cdMatch = cmd.trim().match(/^cd(?:\s+(.*))?$/i);
        if (cdMatch) {
          const rawTarget = (cdMatch[1] || '').trim();
          if (shell === 'cmd') {
            sendCmd = rawTarget ? `cd /d ${rawTarget} && cd` : `cd`;
          } else if (shell === 'bash') {
            sendCmd = rawTarget ? `cd ${rawTarget} && pwd` : `cd ~ && pwd`;
          } else {
            sendCmd = rawTarget ? `cd ${rawTarget}; (Get-Location).Path` : `cd ~; (Get-Location).Path`;
          }
        }

        const r = await fetch('/control/companion/shell', {
          method: 'POST',
          headers,
          body: JSON.stringify({ command: sendCmd, cwd, shell }),
          signal: this._termCtl.signal,
        });
        const d = await r.json().catch(() => ({ ok: false, error: 'Non-JSON server response' }));

        const el = entry();
        if (el) {
          const code = typeof d.exit_code === 'number' ? d.exit_code : ((d.ok && r.ok) ? 0 : 1);
          let stdout = d.stdout || '';
          if (cdMatch && code === 0 && stdout) {
            // the folder line is the prompt's new cwd, not output to show
            const lines = stdout.trim().split(/\r?\n/).map(l => l.trim()).filter(Boolean);
            const newPath = lines[lines.length - 1];
            if (newPath) {
              this.currentCwd = newPath;
              this.updatePrompt();
              stdout = '';
            }
          }
          const err = d.stderr || d.error || d.detail || (!r.ok ? `HTTP Error ${r.status}` : '');
          this._termFinish(el, stdout, typeof err === 'string' ? err : JSON.stringify(err), code);
        }
      } catch (err) {
        const el = entry();
        if (el) {
          if (err && err.name === 'AbortError') {
            this._termFinish(el, '', '^C — stopped waiting. The command may still finish on the device (60s limit).', 130);
          } else {
            this._termFinish(el, '', 'Execution failed: ' + err.message, 1);
          }
        }
      } finally {
        this._termRunning = false;
        this._termCtl = null;
        this.updateTerminalStatus();
        $('term-input')?.focus();
      }
    },

    initResizeGrip() {
      const grip = $('dock-grip');
      if (!grip || !this.dockEl) return;

      let isDragging = false;
      const onMove = (e) => {
        if (!isDragging) return;
        const newW = window.innerWidth - e.clientX;
        if (newW >= 300 && newW <= window.innerWidth * 0.85) {
          this.dockEl.style.width = newW + 'px';
        }
      };
      const onUp = () => {
        if (!isDragging) return;
        isDragging = false;
        grip.classList.remove('dragging');
        document.body.classList.remove('ws-resizing');
        window.removeEventListener('mousemove', onMove);
        window.removeEventListener('mouseup', onUp);
        try { localStorage.setItem('claude_dock_w', parseInt(this.dockEl.style.width, 10)); } catch (_) {}
      };

      grip.addEventListener('mousedown', (e) => {
        if (e.button !== 0) return;
        e.preventDefault();
        isDragging = true;
        grip.classList.add('dragging');
        document.body.classList.add('ws-resizing');
        window.addEventListener('mousemove', onMove);
        window.addEventListener('mouseup', onUp);
      });
    },

    // Screen Wake Lock API ("Keep computer awake")
    async setKeepAwake(on) {
      if (on) {
        try {
          if ('wakeLock' in navigator) {
            this.wakeLock = await navigator.wakeLock.request('screen');
            toast('⚡ Keep awake active: Computer will not sleep');
          } else {
            // Fallback: ping server keepalive
            fetch('/control/keepalive', { method: 'POST', headers: {'Content-Type':'application/json'}, body: JSON.stringify({enabled: true}) });
            toast('⚡ Server keepalive ping enabled');
          }
        } catch (err) {
          console.warn('WakeLock error:', err);
          toast('⚠️ Could not acquire wake lock');
        }
      } else {
        if (this.wakeLock) {
          try { await this.wakeLock.release(); } catch (_) {}
          this.wakeLock = null;
        }
        toast('Computer sleep restored to normal');
      }
    },

    // ---------------- 12-STEP PLAN RENDERING ----------------
    updatePlanFromActs(acts) {
      if (!acts || !acts.length) return;
      const plans = acts.filter(a => a.type === 'plan' && Array.isArray(a.items) && a.items.length);
      if (!plans.length) return;
      const lastPlan = plans[plans.length - 1];
      const items = lastPlan.items;
      this.renderPlanView(items);
    },

    renderPlanView(items) {
      if (!this.planView) return;
      if (!items || !items.length) {
        this.planView.innerHTML = `
          <div style="padding:32px 16px; text-align:center; color:var(--dim); font-size:12.5px;">
            <div style="font-size:24px; margin-bottom:8px;">📋</div>
            <b>No active plan</b>
            <p style="margin-top:6px; font-size:11.5px;">When the agent starts a multi-step task, the 12-step plan will be tracked live here.</p>
          </div>`;
        return;
      }

      const total = items.length;
      const done = items.filter(i => i.status === 'done').length;
      const failed = items.filter(i => i.status === 'failed').length;
      const cur = items.find(i => i.status === 'in_progress');
      const pct = total ? Math.round(((done + failed) / total) * 100) : 0;

      let html = `
        <div class="plan-doc-header">
          <div class="plan-doc-title">Plan: Multi-Step Task Execution</div>
          <div style="font-size:12px; color:var(--dim);">
            ${cur ? `Step ${cur.ord || (items.indexOf(cur) + 1)} of ${total} · ` : ''}
            <b>${done}/${total}</b> completed ${failed ? `· <span style="color:var(--red);">${failed} failed</span>` : ''}
          </div>
          <div class="plan-doc-progress-bar">
            <div class="plan-doc-progress-fill" style="width:${pct}%; ${failed ? 'background:var(--amber);' : ''}"></div>
          </div>
        </div>

        <div class="plan-doc-section-title">Checklist & Execution Steps</div>
        <div class="plan-doc-steps-list">`;

      items.forEach(it => {
        const st = it.status || 'pending';
        const mark = st === 'done' ? '✅' : st === 'failed' ? '❌' : st === 'in_progress' ? '⏳' : '☐';
        html += `
          <div class="plan-doc-step-item st-${st}">
            <span class="plan-doc-step-mark">${mark}</span>
            <div style="flex:1;">
              <span style="font-weight:${st === 'in_progress' ? '600' : 'normal'};">${esc(it.text || '')}</span>
              ${it.note ? `<div style="font-size:11px; color:var(--dim); margin-top:2px;">↳ ${esc(it.note)}</div>` : ''}
            </div>
          </div>`;
      });

      html += `</div>`;

      // Context & Guidance footer
      html += `
        <div class="plan-doc-section-title">Execution Mode</div>
        <div style="font-size:11.5px; color:var(--dim); line-height:1.5; background:var(--panel2); padding:10px 12px; border-radius:6px; border:1px solid var(--border-subtle);">
          Each step is executed atomically following the 12-step plan protocol. Tools verify outputs and save diff snapshots after every file edit.
        </div>`;

      this.planView.innerHTML = html;
    },

    resetPlan() {
      if (!this.planView) return;
      this.planView.innerHTML = `
        <div style="padding:32px 16px; text-align:center; color:var(--dim); font-size:12.5px;">
          <div style="font-size:24px; margin-bottom:8px;">📋</div>
          <b>No active plan</b>
          <p style="margin-top:6px; font-size:11.5px;">When the agent starts a multi-step task, the 12-step plan will be tracked live here.</p>
        </div>`;
    },

    // ---------------- CLAUDE-STYLE WEB PREVIEW (🌐) ----------------
    setWebPreview(title, content, rawUrl, ext = 'html') {
      if (!this.previewView) return;
      this.open('preview');

      const isHtml = ext === 'html' || ext === 'htm';
      let html = `
        <div class="preview-tab-controls">
          <div style="display:flex; align-items:center; gap:8px;">
            <b>${esc(title || 'Web Preview')}</b>
            <span class="chip mono" style="font-size:10px; padding:1px 6px;">.${esc(ext)}</span>
          </div>
          <div style="display:flex; align-items:center; gap:6px;">
            <div class="preview-toggle-btns">
              <button class="preview-toggle-btn ${this.previewMode === 'preview' ? 'active' : ''}" id="btn-prev-tab-view">Preview</button>
              <button class="preview-toggle-btn ${this.previewMode === 'code' ? 'active' : ''}" id="btn-prev-tab-code">Code</button>
            </div>
            <button class="dock-icon-btn" id="btn-prev-mobile" title="Toggle Mobile / Desktop view">📱</button>
            <button class="dock-icon-btn" id="btn-prev-reload" title="Reload preview">⟳</button>
            ${rawUrl ? `<a href="${rawUrl}" target="_blank" rel="noopener" class="dock-icon-btn" title="Open in new tab" style="text-decoration:none;">↗</a>` : ''}
          </div>
        </div>

        <div class="preview-iframe-wrapper ${this.isMobileView ? 'mobile-frame' : ''}" id="prev-frame-wrap" style="display:${this.previewMode === 'preview' ? 'flex' : 'none'};">
          <iframe id="prev-dock-iframe" class="preview-live-iframe" sandbox="allow-scripts allow-forms allow-same-origin allow-modals" src="${rawUrl || 'about:blank'}"></iframe>
        </div>

        <pre class="preview-code-view" id="prev-code-wrap" style="display:${this.previewMode === 'code' ? 'block' : 'none'};">${esc(content || '')}</pre>
      `;

      this.previewView.innerHTML = html;

      // Wire controls
      $('btn-prev-tab-view')?.addEventListener('click', () => {
        this.previewMode = 'preview';
        $('prev-frame-wrap').style.display = 'flex';
        $('prev-code-wrap').style.display = 'none';
        $('btn-prev-tab-view').classList.add('active');
        $('btn-prev-tab-code').classList.remove('active');
      });

      $('btn-prev-tab-code')?.addEventListener('click', () => {
        this.previewMode = 'code';
        $('prev-frame-wrap').style.display = 'none';
        $('prev-code-wrap').style.display = 'block';
        $('btn-prev-tab-code').classList.add('active');
        $('btn-prev-tab-view').classList.remove('active');
      });

      $('btn-prev-mobile')?.addEventListener('click', () => {
        this.isMobileView = !this.isMobileView;
        $('prev-frame-wrap')?.classList.toggle('mobile-frame', this.isMobileView);
      });

      $('btn-prev-reload')?.addEventListener('click', () => {
        const frame = $('prev-dock-iframe');
        if (frame) {
          if (rawUrl) frame.src = rawUrl;
          else frame.contentWindow?.location.reload();
        }
      });
    },

    // Background Tasks view
    async refreshTasks() {
      if (!this.tasksView) return;
      this.tasksView.innerHTML = `
        <div style="padding:16px; display:flex; flex-direction:column; gap:12px;">
          <div style="display:flex; justify-content:space-between; align-items:center;">
            <b>⚡ Background Tasks & Monitor</b>
            <button class="btn ghost ln-small" id="btn-tasks-refresh">⟳ Refresh</button>
          </div>
          <div id="tasks-active-container" style="background:var(--panel2); border-radius:8px; padding:10px; border:1px solid var(--border);">
            <div style="color:var(--dim); font-size:12px;">Checking tasks...</div>
          </div>
          <div style="display:flex; gap:8px;">
            <button class="btn ghost ln-small" onclick="loadMonitor(); $('monitor-drawer').classList.add('open');">📡 Full Monitor</button>
            <button class="btn ghost ln-small" onclick="setReportModal(true);">📊 Token Report</button>
          </div>
        </div>`;

      $('btn-tasks-refresh')?.addEventListener('click', () => this.refreshTasks());

      try {
        const r = await fetch('/control/status');
        const d = await r.json();
        const box = $('tasks-active-container');
        if (box) {
          box.innerHTML = `
            <div style="display:flex; flex-direction:column; gap:6px; font-size:12px;">
              <div><b>Loaded Model:</b> <code class="mono">${esc(d.model || 'None')}</code></div>
              <div><b>Vulkan Runtime:</b> ${d.pid ? `<span style="color:var(--green);">PID ${d.pid} (Online)</span>` : '<span style="color:var(--dim);">Idle</span>'}</div>
              <div><b>VRAM Keepalive:</b> ${d.keepalive_running ? '🟢 Active (25s ping)' : '⚪ Off'}</div>
            </div>`;
        }
      } catch (_) {}
    }
  };

  // ---------------- 2. THE SIMPLISTIC "SLOT MACHINE" SELECTOR ----------------
  const SlotMachine = {
    init() {
      this.slotWrap = $('simplistic-slot-machine');
      this.reelMode = $('slot-reel-mode');
      this.reelEngine = $('slot-reel-engine');
      this.reelModel = $('slot-reel-model');

      if (!this.slotWrap) return;

      // Reel 1: Mode (Chat ↔ Agent Task)
      this.reelMode?.addEventListener('click', () => {
        this.spinMode();
      });

      // Reel 2: Engine (Local ↔ Balanced ↔ Cloud)
      this.reelEngine?.addEventListener('click', () => {
        this.spinEngine();
      });

      // Reel 3: Model Picker
      this.reelModel?.addEventListener('click', () => {
        $('model-picker-btn')?.click();
      });

      this.syncFromState();
    },

    syncFromState() {
      // Sync Mode
      if (this.reelMode) {
        this.reelMode.innerHTML = agentMode
          ? '🔨 Agent <span class="slot-caret">▾</span>'
          : '💬 Chat <span class="slot-caret">▾</span>';
      }

      // Sync Engine
      const engSel = $('agent-engine');
      if (this.reelEngine && engSel) {
        const val = engSel.value || 'all-local';
        const labels = {
          'all-local': '🔒 Local',
          'main-local-rest-cloud': '⚖ Balanced',
          'main-cloud-rest-local': '✨ Cloud',
          'no-orchestration': '🎯 Main'
        };
        this.reelEngine.innerHTML = `${labels[val] || '🔒 Local'} <span class="slot-caret">▾</span>`;
      }

      // Sync Model
      const modelLabel = $('model-picker-label');
      if (this.reelModel && modelLabel) {
        const text = modelLabel.textContent || 'Model';
        this.reelModel.innerHTML = `${esc(text)} <span class="slot-caret">▾</span>`;
      }
    },

    spinMode() {
      const targetBtn = agentMode ? $('mode-chat') : $('mode-agent');
      targetBtn?.click();
      this.syncFromState();
    },

    spinEngine() {
      const engSel = $('agent-engine');
      if (!engSel) return;
      const opts = ['all-local', 'main-local-rest-cloud', 'main-cloud-rest-local', 'no-orchestration'];
      const curIdx = opts.indexOf(engSel.value);
      const nextVal = opts[(curIdx + 1) % opts.length];
      engSel.value = nextVal;
      engSel.dispatchEvent(new Event('change'));
      this.syncFromState();
    }
  };

  // ---------------- 3. CLAUDE GIT STATUS STRIP ----------------
  const GitStrip = {
    async init() {
      this.stripEl = $('claude-git-status-strip');
      this.branchEl = $('claude-git-branch');
      this.diffsEl = $('claude-git-diffs');
      this.actionBtn = $('claude-git-action-btn');
      this.closeBtn = $('claude-git-dismiss-btn');

      if (!this.stripEl) return;

      this.actionBtn?.addEventListener('click', () => {
        if (typeof setGitModal === 'function') setGitModal(true);
      });

      this.closeBtn?.addEventListener('click', () => {
        this.stripEl.style.display = 'none';
      });

      this.refresh();
      // Refresh periodically when idle
      setInterval(() => this.refresh(), 20000);
    },

    async refresh() {
      if (!this.stripEl) return;
      // Only display Git status strip in Agent Task mode with an active project
      if (typeof agentMode === 'undefined' || !agentMode || !window.curProject || !window.curProject.name || window.curProject.name === 'scratch' || window.curProject.name === 'default') {
        this.stripEl.style.display = 'none';
        return;
      }
      try {
        const r = await fetch('/git/status');
        const d = await r.json();
        if (d.error || !d.branch) {
          this.stripEl.style.display = 'none';
          return;
        }

        this.stripEl.style.display = 'flex';
        const projName = window.curProject.name;
        if (this.branchEl) {
          this.branchEl.textContent = `${projName} · ${d.branch}`;
        }

        const changes = (d.files || []).length;
        if (this.diffsEl) {
          this.diffsEl.innerHTML = changes > 0
            ? `<span class="claude-diff-add">+${changes} modified</span>`
            : `<span style="color:var(--dim);">clean</span>`;
        }
      } catch (_) {
        this.stripEl.style.display = 'none';
      }
    }
  };

  // ---------------- 4. CLAUDE CHAT BAR CONTROLLER ----------------
  const ClaudeChatBar = {
    activeVoiceMode: 'transcribe', // 'transcribe' | 'voice'
    activeModeKey: 'auto',

    init() {
      const enterBtn = $('btn-input-enter');
      const sendBtn = $('btn-send');
      const modelBtn = $('btn-claude-model');
      const input = $('input');

      // 1. Enter button in input box triggers send or cancel
      enterBtn?.addEventListener('click', () => {
        if (typeof generating !== 'undefined' && generating) {
          $('btn-abort')?.click();
        } else {
          sendBtn?.click();
        }
      });

      // 2. Mode menu dropdown (Image 1)
      this.initModeMenu();

      // 3. Voice split pill & menu (Image 2)
      this.initVoiceControls();

      // 4. Model button opens model picker positioned above it
      modelBtn?.addEventListener('click', (e) => {
        e.stopPropagation();
        this.openModelPicker();
      });

      // 5. Context Pie Chart hover/click tooltip (Image 4)
      const ctxBtn = $('btn-claude-ctx');
      const ctxPopup = $('claude-ctx-popup');
      if (ctxBtn && ctxPopup) {
        ctxBtn.addEventListener('mouseenter', () => { ctxPopup.style.display = 'block'; });
        ctxBtn.addEventListener('mouseleave', () => { ctxPopup.style.display = 'none'; });
      }

      // 6. Synchronize model name
      this.syncModel();
      $('profile')?.addEventListener('change', () => this.syncModel());
      setInterval(() => this.syncModel(), 1000);

      // 7. Textarea typing scroll within 85px max-height box
      if (input) {
        input.addEventListener('input', () => {
          input.style.height = '60px';
        });
      }
    },

    syncModeVisibility() {
      const curMode = window.appMode || (typeof appMode !== 'undefined' ? appMode : 'chat');
      const wrap = $('claude-mode-wrap');
      if (wrap) wrap.style.setProperty('display', (curMode === 'chat') ? 'none' : 'inline-flex', 'important');
      const bypassItem = $('claude-mode-menu')?.querySelector('[data-mode-key="bypass"]');
      if (bypassItem) bypassItem.style.display = (curMode === 'chat') ? 'none' : '';
    },

    initModeMenu() {
      const modeBtn = $('btn-claude-mode');
      const modeMenu = $('claude-mode-menu');
      if (!modeBtn || !modeMenu) return;

      const restore = () => {
        let k = 'auto';
        try { k = localStorage.getItem(this.modeStoreKey()) || 'auto'; } catch (_) {}
        this.activeModeKey = k;
        this.updateModeUI(k);
        // restoring Plan must also turn the real plan switch on (and anything else turns it off)
        this._syncing = true;
        try { if (window._setPlanMode) window._setPlanMode(k === 'plan'); } finally { this._syncing = false; }
      };
      restore();
      this.syncModeVisibility();
      if (window.__sessionReady) window.__sessionReady.then(restore);   // the user id is known only after sign-in check

      modeBtn.addEventListener('click', (e) => {
        e.stopPropagation();
        const isOpen = modeMenu.style.display === 'block';
        const vMenu = $('claude-voice-menu');
        if (vMenu) vMenu.style.display = 'none';
        $('claude-mic-split-pill')?.classList.remove('open');
        modeMenu.style.display = isOpen ? 'none' : 'block';
        modeBtn.setAttribute('aria-expanded', !isOpen ? 'true' : 'false');
      });

      // Click on menu items
      modeMenu.querySelectorAll('.claude-menu-item').forEach(item => {
        item.addEventListener('click', (e) => {
          e.stopPropagation();
          const key = item.dataset.modeKey;
          this.setMode(key);
          modeMenu.style.display = 'none';
          modeBtn.setAttribute('aria-expanded', 'false');
        });
      });

      // Hotkey numbers 1-5 when menu is open or global hotkeys
      window.addEventListener('keydown', (e) => {
        if (modeMenu.style.display === 'block') {
          if (e.key === 'Escape') {
            modeMenu.style.display = 'none';
            modeBtn.setAttribute('aria-expanded', 'false');
            return;
          }
          const num = parseInt(e.key, 10);
          if (num >= 1 && num <= 5) {
            e.preventDefault();
            const target = modeMenu.querySelector(`.claude-menu-item[data-hotkey="${num}"]`);
            if (target) {
              const key = target.dataset.modeKey;
              this.setMode(key);
              modeMenu.style.display = 'none';
              modeBtn.setAttribute('aria-expanded', 'false');
            }
          }
        }
      });

      // Click outside closes menu
      document.addEventListener('click', (e) => {
        if (!e.target.closest('#claude-mode-wrap')) {
          modeMenu.style.display = 'none';
          modeBtn.setAttribute('aria-expanded', 'false');
        }
      });
    },

    // the choice is remembered per signed-in user, so Bypass for one person never carries over to another
    modeStoreKey() {
      const u = window.__user;
      return 'claude_selected_mode' + (u && u.id != null ? ':' + u.id : '');
    },

    setMode(key) {
      const keys = ['auto', 'manual', 'accept_edits', 'plan', 'bypass'];
      this.activeModeKey = keys.includes(key) ? key : 'auto';
      try { localStorage.setItem(this.modeStoreKey(), this.activeModeKey); } catch (_) {}
      this.updateModeUI(this.activeModeKey);
      // Plan is the same read-only run as the /plan switch; every other mode turns it off
      this._syncing = true;
      try { if (window._setPlanMode) window._setPlanMode(this.activeModeKey === 'plan'); } finally { this._syncing = false; }
    },

    // /plan and /build from the composer. /build leaves Plan for Auto, any other mode is kept.
    applySlashMode(name) {
      if (name === 'plan') this.setMode('plan');
      else this.setMode(this.activeModeKey === 'plan' ? 'auto' : this.activeModeKey);
    },

    // called when plan mode is switched from outside the dropdown (hidden select, restore on load)
    syncFromPlan(on) {
      if (this._syncing) return;
      if (on && this.activeModeKey !== 'plan') { this.activeModeKey = 'plan'; this.updateModeUI('plan'); }
      else if (!on && this.activeModeKey === 'plan') { this.activeModeKey = 'auto'; this.updateModeUI('auto'); }
      try { localStorage.setItem(this.modeStoreKey(), this.activeModeKey); } catch (_) {}
    },

    // end of a Plan-mode run: approve it (leave Plan for Auto or Accept edits) or keep planning
    showPlanApproval() {
      const host = $('chat-inner');
      if (!host || host.querySelector('.plan-approval')) return;
      const card = document.createElement('div');
      card.className = 'plan-approval';
      card.style.cssText = 'margin:12px 0;padding:12px 14px;border:1px solid var(--border,#444);border-radius:10px;display:flex;gap:8px;flex-wrap:wrap;align-items:center';
      card.innerHTML = '<b style="flex:1 1 100%">Plan ready. Switch to Build mode and start?</b>'
        + '<button class="btn" data-m="auto">Build it (Auto)</button>'
        + '<button class="btn" data-m="accept_edits">Build it (Accept edits)</button>'
        + '<button class="btn" data-m="keep">Keep planning</button>';
      card.addEventListener('click', (e) => {
        const m = e.target && e.target.dataset && e.target.dataset.m;
        if (!m) return;
        card.remove();
        if (m === 'keep') return;
        this.setMode(m);
        if (typeof dispatchPrompt === 'function') dispatchPrompt('Proceed with the plan.');
      });
      host.appendChild(card);
      card.scrollIntoView({ block: 'nearest' });
    },

    updateModeUI(key) {
      const modeTxt = $('claude-mode-text');
      const modeMenu = $('claude-mode-menu');
      const labels = {
        'auto': 'Auto',
        'manual': 'Manual',
        'accept_edits': 'Accept edits',
        'plan': 'Plan',
        'bypass': 'Bypass'
      };
      if (modeTxt) modeTxt.textContent = labels[key] || 'Auto';

      if (modeMenu) {
        modeMenu.querySelectorAll('.claude-menu-item').forEach(it => {
          const isTarget = it.dataset.modeKey === key;
          it.classList.toggle('active', isTarget);
          const chk = it.querySelector('.claude-menu-check');
          if (chk) chk.textContent = isTarget ? '✓' : '';
        });
      }
      this.syncModeVisibility();
    },

    initVoiceControls() {
      const micPill = $('claude-mic-split-pill');
      const micBtn = $('btn-mic');
      const arrowBtn = $('btn-mic-menu-toggle');
      const voiceMenu = $('claude-voice-menu');

      if (!micBtn || !voiceMenu) return;

      this.activeVoiceMode = localStorage.getItem('claude_voice_mode') || 'transcribe';
      this.updateVoiceUI(this.activeVoiceMode);

      // Arrow button toggles popup menu
      arrowBtn?.addEventListener('click', (e) => {
        e.stopPropagation();
        const isOpen = voiceMenu.style.display === 'block';
        const mMenu = $('claude-mode-menu');
        if (mMenu) mMenu.style.display = 'none';
        $('btn-claude-mode')?.setAttribute('aria-expanded', 'false');

        voiceMenu.style.display = isOpen ? 'none' : 'block';
        micPill?.classList.toggle('open', !isOpen);
        arrowBtn.setAttribute('aria-expanded', !isOpen ? 'true' : 'false');
      });

      // Menu items selection
      voiceMenu.querySelectorAll('.claude-menu-item').forEach(it => {
        it.addEventListener('click', (e) => {
          e.stopPropagation();
          const mode = it.dataset.voiceMode;
          this.setVoiceMode(mode);
          voiceMenu.style.display = 'none';
          micPill?.classList.remove('open');
          arrowBtn?.setAttribute('aria-expanded', 'false');

          // Trigger the chosen voice feature
          this.triggerVoiceAction(mode);
        });
      });

      // Direct click on the mic icon
      micBtn.addEventListener('click', (e) => {
        // If mode is voice to voice, prevent default dictation and trigger voice call
        if (this.activeVoiceMode === 'voice') {
          e.stopPropagation();
          this.triggerVoiceAction('voice');
        }
      });

      // Click outside closes menu
      document.addEventListener('click', (e) => {
        if (!e.target.closest('#claude-mic-split-wrap')) {
          voiceMenu.style.display = 'none';
          micPill?.classList.remove('open');
          arrowBtn?.setAttribute('aria-expanded', 'false');
        }
      });
    },

    setVoiceMode(mode) {
      this.activeVoiceMode = mode || 'transcribe';
      try { localStorage.setItem('claude_voice_mode', this.activeVoiceMode); } catch (_) {}
      this.updateVoiceUI(this.activeVoiceMode);
    },

    updateVoiceUI(mode) {
      const micBtn = $('btn-mic');
      const voiceMenu = $('claude-voice-menu');

      if (micBtn) {
        if (mode === 'voice') {
          micBtn.title = 'Real-time Voice to Voice Chat';
          micBtn.setAttribute('aria-label', 'Real-time Voice to Voice Chat');
        } else {
          micBtn.title = 'Dictate voice (Transcribe into prompt)';
          micBtn.setAttribute('aria-label', 'Dictate voice (Transcribe)');
        }
      }

      if (voiceMenu) {
        voiceMenu.querySelectorAll('.claude-menu-item').forEach(it => {
          const isTarget = it.dataset.voiceMode === mode;
          it.classList.toggle('active', isTarget);
          const chk = it.querySelector('.claude-menu-check');
          if (chk) chk.textContent = isTarget ? '✓' : '';
        });
      }
    },

    triggerVoiceAction(mode) {
      if (mode === 'voice') {
        const vBtn = $('btn-voice');
        if (vBtn) {
          vBtn.click();
          toast('Started Voice to Voice mode');
        }
      } else {
        // Transcribe mode
        if (typeof micStart === 'function' && typeof MEDIA !== 'undefined') {
          if (MEDIA.rec) micStop(); else micStart();
        } else {
          toast('Transcribe: dictating speech...');
        }
      }
    },

    syncModel() {
      const sel = $('profile');
      const label = $('model-picker-label');
      const modelTxt = $('claude-model-text');
      if (!modelTxt) return;
      let txt = '';
      if (sel && sel.selectedIndex >= 0 && sel.options[sel.selectedIndex]) {
        const opt = sel.options[sel.selectedIndex];
        if (opt.value) {
          txt = (opt.textContent || '').replace(/\s*\((?:loaded|not loaded)\)\s*$/, '');
          txt = txt.replace(/\s*·\s*(?:[👁🔨⚡◵].*)$/, '').trim();
        }
      }
      if (!txt && label && label.textContent && label.textContent !== 'No model' && label.textContent !== 'Select a model…') {
        txt = label.textContent.trim();
      }
      if (txt && txt !== 'Select a model…' && txt !== 'No model' && txt !== 'No model available') {
        modelTxt.textContent = txt.length > 26 ? txt.slice(0, 25).trimEnd() + '…' : txt;
        modelTxt.title = txt;
      } else {
        modelTxt.textContent = 'Select model';
        modelTxt.title = 'Select a model';
      }
    },

    openModelPicker() {
      const dd = $('model-picker-dropdown');
      const btn = $('btn-claude-model');
      if (!dd || !btn) return;
      if (dd.classList.contains('open')) {
        dd.classList.remove('open');
        return;
      }
      const sel = $('profile');
      if (typeof renderModelPicker === 'function') renderModelPicker();
      if (typeof loadProfiles === 'function' && sel && sel.options.length <= 1) {
        loadProfiles().then(() => {
          if (typeof renderModelPicker === 'function') renderModelPicker();
        }).catch(() => {});
      }
      const r = btn.getBoundingClientRect();
      const targetW = Math.max(340, Math.min(380, window.innerWidth - 32));
      dd.style.width = targetW + 'px';
      dd.style.top = 'auto';
      dd.style.bottom = (window.innerHeight - r.top + 8) + 'px';
      let left = r.right - targetW;
      if (left < 16) left = 16;
      if (left + targetW > window.innerWidth - 16) left = window.innerWidth - targetW - 16;
      dd.style.left = left + 'px';
      dd.classList.add('open');

      const onDocClick = (e) => {
        if (!dd.contains(e.target) && e.target !== btn && !btn.contains(e.target)) {
          dd.classList.remove('open');
          document.removeEventListener('click', onDocClick);
        }
      };
      setTimeout(() => document.addEventListener('click', onDocClick), 10);
    }
  };

  // Expose to window
  RightDock.syncModeVisibility = () => ClaudeChatBar.syncModeVisibility();
  window.RightDock = RightDock;
  window.SlotMachine = SlotMachine;
  window.GitStrip = GitStrip;
  window.ClaudeChatBar = ClaudeChatBar;
  window.logTerminal = (data) => RightDock.logTerminal(data);
  window.logTerminalResult = (data) => RightDock.logTerminalResult(data);

  document.addEventListener('DOMContentLoaded', () => {
    RightDock.init();
    SlotMachine.init();
    GitStrip.init();
    ClaudeChatBar.init();
  });
})();
