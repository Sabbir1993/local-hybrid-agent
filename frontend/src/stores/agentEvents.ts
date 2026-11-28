// Event handling for the /agent/run SSE stream. Mirrors static/js/agent-run.js + sse-stream.js:
// every event is folded into the assistant message (`acts` is what the chat renders as the action timeline).
import type { ChatMessage } from './useAppStore';
import { parseSubagentsFromText } from '../utils/agentTrace';

export interface AgentRunCtx {
  toast: (message: string, type?: 'info' | 'error' | 'success' | 'warn') => void;
  /** true while a finished/old run is being replayed from the server: no toasts for what already happened */
  replay?: boolean;
  onPermission: (req: { reqId: string; cmd: string; kind: string; saveable: boolean; tainted: boolean; suspicious?: boolean }) => void;
  /** shell commands the agent runs are mirrored into the dock terminal */
  onShell?: {
    start: (id: string, cmd: string) => void;
    finish: (id: string, patch: { stdout?: string; stderr?: string; exitCode?: number }) => void;
  };
  /** Right-Dock sub-agent monitor. Injected by the store: importing it here would be a cycle
   *  (useAppStore imports applyAgentEvent), which is why the old `window.__useAppStore`
   *  shortcut existed - and was never assigned, so every live update was silently dropped. */
  onSubagents?: {
    setBatch: (list: Array<{ id: string; index: number; role: string; task: string; status: 'running'; step: number; max_steps: number; lane: string }>) => void;
    update: (key: string, patch: Record<string, unknown>) => void;
    activate: () => void;
  };
  onScreenshot?: (shotPath: string) => void;
}

/** Per-run scratch state that is not part of the saved message. */
export interface AgentRunState {
  curThought: any | null;
  resetPrev: string | null;
  ctxToastShown: boolean;
  reasoning: string;
  runId?: string;
  gap?: boolean;
  runState?: string;
  lane?: string;
  modelSource?: string;
}

export function newAgentRunState(): AgentRunState {
  return { curThought: null, resetPrev: null, ctxToastShown: false, reasoning: '' };
}

export const GAP_NOTE = '(earlier output of this run was dropped)';
const SHELL_TOOLS = ['run_shell', 'shell', 'bash', 'exec'];
const FILE_TOOLS = ['write_file', 'edit_file', 'append_file', 'insert_at_line'];

function closeThought(S: AgentRunState) {
  if (!S.curThought) return;
  S.curThought.duration_s = Math.max(1, Math.round((performance.now() - S.curThought.t0) / 1000));
  S.curThought = null;
}

function formatToolStatus(name: string, args: any): string {
  const p = args && (args.path || args.file || args.filename);
  const base = p ? String(p).split(/[\\/]/).pop() : '';
  if (name === 'write_file') return `Writing ${base}...`;
  if (name === 'edit_file' || name === 'insert_at_line') return `Editing ${base}...`;
  if (name === 'append_file') return `Appending to ${base}...`;
  if (name === 'read_file') return `Reading ${base}...`;
  if (name === 'run_shell' || name === 'shell' || name === 'bash' || name === 'exec') return 'Running command...';
  return `Running ${name}...`;
}

/** A stream that ended without `done`, or failed mid-run: close dangling tool calls and record the final state. */
export function interruptRun(L: ChatMessage, S: AgentRunState, reason: string, state: 'interrupted' | 'cancelled' | 'failed') {
  const acts = (L.acts = L.acts || []);
  const answered = new Set(acts.filter((a) => a.type === 'tool_result').map((a) => a.id));
  acts
    .filter((a) => a.type === 'tool_call' && !answered.has(a.id))
    .forEach((c) => acts.push({ type: 'tool_result', id: c.id, name: c.name, ok: false, interrupted: true, result: 'interrupted: ' + reason }));
  S.runState = state;
  if (!acts.some((a) => a.type === 'stopped')) {
    acts.push({
      type: 'stopped', reason: state, note: reason, detail: '',
      steps: acts.filter((a) => a.type === 'step').length,
      pending: 0, plan_total: 0, plan_done: 0, plan_failed: 0, elapsed_s: 0,
    });
  }
}

/** Answer-check events (shield): verify_start / verify_result. Returns true when the event was one of them. */
export function applyAnswerCheck(L: ChatMessage, ev: string, d: any): boolean {
  if (ev === 'verify_start') {
    // keep what the gate already knows (start time, "Show now" clicked) while the check runs
    L.check = { ...(L.check || {}), state: 'checking', mode: d.mode, checker: d.checker, source: d.source, t1: Date.now() };
    L.statusText = 'Double-checking with ' + (d.checker || 'the checker') + '…';
    return true;
  }
  if (ev === 'verify_result') {
    L.check = { ...(L.check || {}), ...d, state: 'done' };
    L.statusText = '';
    return true;
  }
  return false;
}

/** The run ended: a gate that never started must not keep the answer hidden. */
export function answerCheckEnd(L: ChatMessage) {
  if (L.check && L.check.state !== 'done' && L.check.state !== 'checking') L.check = undefined;
  if (L.check && L.check.state === 'checking') L.check = { ...L.check, state: 'done', verdict: 'unverified', issues: [] };
}

/** Fold one SSE event into the message. Throws on an `error` event. */
export function applyAgentEvent(L: ChatMessage, S: AgentRunState, ev: string, d: any, ctx: AgentRunCtx) {
  if (applyAnswerCheck(L, ev, d)) return;
  if (!L.acts) L.acts = [];
  const acts = L.acts;
  const toast: AgentRunCtx['toast'] = (m, t) => { if (!ctx.replay) ctx.toast(m, t); };

  switch (ev) {
    case 'gap':
      // the server dropped the oldest frames of a very large run: the store appends GAP_NOTE when the run ends
      S.gap = true;
      break;
    case 'queued':
      // all local model slots are busy (admission gate), or the shared memory pool is taken by long conversations
      // (pool guard, core/serving_slots.py): it starts as soon as enough of them finish
      L.statusText = d.reason === 'memory'
        ? '⏳ Waiting for model memory: other long conversations are running' + (d.position ? ` (#${d.position})` : '') + '...'
        : '⏳ Waiting for a free model slot' + (d.position ? ` (#${d.position} in queue)` : '') + '...';
      break;
    case 'run':
      S.runId = d.run_id;
      L.runId = d.run_id;   // used for thumbs up/down
      break;
    case 'step':
      closeThought(S);
      acts.push({ type: 'step', ...d });
      L.statusText = d.step ? `Planning step ${d.step}...` : 'Planning next step...';
      break;
    case 'lane':
      acts.push({ type: 'lane', ...d });
      L.model = d.display || d.model;
      L.modelProvider = d.provider;
      S.modelSource = d.source;
      if (d.lane) S.lane = d.lane;
      break;
    case 'thought':
      closeThought(S);
      acts.push({ type: 'thought', duration_s: d.duration_s || 2, ...d });
      L.statusText = 'Synthesizing strategy...';
      break;
    case 'thought_delta': {
      if (!S.curThought) {
        S.curThought = { type: 'thought', text: '', t0: performance.now(), step: d.step, model: d.model };
        acts.push(S.curThought);
      }
      S.curThought.text += d.delta || '';
      S.curThought.duration_s = Math.max(1, Math.floor((performance.now() - S.curThought.t0) / 1000));
      S.reasoning += d.delta || '';
      L.thinking = S.reasoning;
      L.statusText = 'Thinking...';
      break;
    }
    case 'delta_to_thought': {
      const moved = (L.content || '').trim();
      L.content = '';
      if (moved) {
        S.reasoning = S.reasoning ? S.reasoning + '\n\n' + moved : moved;
        L.thinking = S.reasoning;
        if (!S.curThought) {
          S.curThought = { type: 'thought', text: '', t0: performance.now(), step: d.step, model: d.model };
          acts.push(S.curThought);
        }
        S.curThought.text += moved;
      }
      break;
    }
    case 'tool_preparing': {
      closeThought(S);
      const p = d.path ? String(d.path).split(/[\\/]/).pop() : '';
      const verb = d.name === 'write_file' ? 'Preparing to write' : d.name === 'append_file' ? 'Preparing to append to'
        : d.name === 'edit_file' || d.name === 'insert_at_line' ? 'Preparing to edit' : `Preparing ${d.name}`;
      L.statusText = p ? `${verb} ${p}...` : `${verb}...`;
      break;
    }
    case 'tool_call':
      closeThought(S);
      acts.push({ type: 'tool_call', ...d });
      L.statusText = formatToolStatus(d.name, d.args);
      if (['spawn_parallel_agents', 'spawn_agent', 'spawn_reviewed_coder'].includes(d.name)) {
        const specs = Array.isArray(d.args?.agents) ? d.args.agents : d.args?.task ? [d.args] : [];
        if (specs.length > 0 && ctx.onSubagents) {
          const list = specs.map((spec: any, idx: number) => ({
            id: String(idx),
            index: idx + 1,
            role: spec.role || 'subagent',
            task: spec.task || '',
            status: 'running' as const,
            step: 0,
            max_steps: spec.max_steps || 10,
            lane: spec.lane || 'default',
          }));
          ctx.onSubagents.setBatch(list);
          ctx.onSubagents.activate();
        }
      }
      if (SHELL_TOOLS.includes(d.name)) {
        const c = d.args && (d.args.command || d.args.cmd);
        if (c) ctx.onShell?.start(String(d.id), String(c));
      }
      // text streamed before a tool call was a preamble ("Reading the config next."), not the answer: it stays in the
      // trace as narration, between the groups of calls, where the agent said it
      if (L.content && L.content.trim()) {
        acts.splice(acts.length - 1, 0, { type: 'narration', text: L.content.trim() });
        L.content = '';
      }
      break;
    case 'tool_progress': {
      const call = acts.slice().reverse().find((a) => a.type === 'tool_call' && (a.id === d.id || (!d.id && a.name === d.name)));
      if (call) {
        call.progress = { text: d.text || '', pct: d.pct, elapsed: d.elapsed };
        L.statusText = d.text || L.statusText;
      }
      break;
    }
    case 'subagent_event': {
      const subIdx = d.index !== undefined
        ? Number(d.index)
        : d.sub_index !== undefined
        ? Number(d.sub_index)
        : (typeof d.subagent_id === 'string' && d.subagent_id.match(/sub_(\d+)/))
        ? parseInt(d.subagent_id.match(/sub_(\d+)/)![1], 10)
        : undefined;
      const key = subIdx !== undefined ? String(subIdx - 1) : String(d.subagent_id ?? '0');
      const call = acts.slice().reverse().find((a) => a.type === 'tool_call' && (a.id === d.id || (!d.id && a.name === d.name)));
      const ev = d.event;
      const subStatus = ev === 'done'
        ? (d.status === 'step_exhausted' || d.status === 'step_limit' ? 'step_exhausted' : d.status === 'error' || d.status === 'failed' ? 'failed' : d.status === 'timeout' ? 'timeout' : 'completed')
        : ev === 'error'
        ? 'failed'
        : 'running';

      const target = d.target || d.args?.path || d.args?.file_path || d.args?.file || d.args?.query || d.args?.command || d.args?.pattern || d.args?.url || '';

      if (call) {
        if (!call.subagents) call.subagents = {};
        const prev = call.subagents[key] || {};
        const prevTools = Array.isArray(prev.tools) ? [...prev.tools] : [];
        let lastTool = prev.last_tool;
        let filesModified = Array.isArray(prev.files_modified) ? [...prev.files_modified] : [];

        if (ev === 'tool_start') {
          prevTools.push({ tool: d.tool, target, args: d.args, step: d.step, time: Date.now() });
          if (['write_file', 'edit_file', 'append_file'].includes(d.tool) && target && !filesModified.includes(target)) {
            filesModified.push(target);
          }
        } else if (ev === 'tool_done') {
          if (prevTools.length > 0) {
            prevTools[prevTools.length - 1] = { ...prevTools[prevTools.length - 1], ok: d.ok };
          }
          lastTool = { tool: d.tool, target: target || prev.current_target || '', ok: d.ok, step: d.step };
        } else if (ev === 'done' && Array.isArray(d.files)) {
          d.files.forEach((f: string) => {
            if (!filesModified.includes(f)) filesModified.push(f);
          });
        }

        call.subagents[key] = {
          ...prev,
          sub_index: subIdx !== undefined ? subIdx : prev.sub_index,
          index: subIdx !== undefined ? subIdx : prev.index,
          subagent_id: `sub_${subIdx !== undefined ? subIdx : Number(key) + 1}`,
          role: d.role || prev.role || 'subagent',
          task: d.task || prev.task || '',
          status: subStatus,
          step: d.step !== undefined ? d.step : prev.step,
          max_steps: d.max_steps !== undefined ? d.max_steps : prev.max_steps,
          current_tool: ev === 'tool_start' ? d.tool : (ev === 'tool_done' ? null : prev.current_tool),
          current_target: ev === 'tool_start' ? target : (ev === 'tool_done' ? null : prev.current_target),
          current_tool_args: ev === 'tool_start' ? d.args : (ev === 'tool_done' ? null : prev.current_tool_args),
          last_tool: lastTool,
          tools: prevTools,
          files_modified: filesModified,
          files: filesModified,
          elapsed_s: d.elapsed_s !== undefined ? d.elapsed_s : prev.elapsed_s,
          duration_s: d.duration_s !== undefined ? d.duration_s : prev.duration_s,
          error: d.error || prev.error,
          last_event: ev,
        };
        const roleLabel = d.role || prev.role || `#${Number(key) + 1}`;
        if (ev === 'step') {
          L.statusText = `[Sub-agent ${roleLabel}] Step ${d.step}/${d.max_steps || '?'}`;
        } else if (ev === 'tool_start') {
          const targetShort = target ? ` (${String(target).split(/[\\/]/).pop()})` : '';
          L.statusText = `[Sub-agent ${roleLabel}] ${d.tool}${targetShort}...`;
        } else if (ev === 'done') {
          L.statusText = `[Sub-agent ${roleLabel}] ${subStatus === 'step_exhausted' ? 'Exhausted steps' : subStatus === 'failed' ? 'Failed' : 'Done'} in ${d.duration_s || 0}s`;
        } else if (ev === 'error') {
          L.statusText = `[Sub-agent ${roleLabel}] Error: ${d.error || 'Execution failed'}`;
        }
      }

      // Live Right Dock update - injected by the store, no global shortcut.
      const currentCallSub = call?.subagents?.[key];
      ctx.onSubagents?.update(key, {
        id: `sub_${subIdx !== undefined ? subIdx : Number(key) + 1}`,
        index: (subIdx !== undefined ? subIdx : Number(key) + 1),
        role: d.role,
        task: d.task,
        status: subStatus,
        step: d.step,
        max_steps: d.max_steps,
        current_tool: ev === 'tool_start' ? d.tool : (ev === 'tool_done' ? null : undefined),
        current_target: ev === 'tool_start' ? target : (ev === 'tool_done' ? null : undefined),
        current_tool_args: ev === 'tool_start' ? d.args : (ev === 'tool_done' ? null : undefined),
        last_tool: currentCallSub?.last_tool,
        tools: currentCallSub?.tools,
        files_modified: currentCallSub?.files_modified,
        files: currentCallSub?.files_modified,
        elapsed_s: d.elapsed_s,
        duration_s: d.duration_s,
        output: d.output,
        error: d.error,
      });
      break;
    }
    case 'tool_result': {
      acts.push({ type: 'tool_result', ...d });
      L.statusText = 'Crunching tool results...';
      const subagentTools = ['spawn_parallel_agents', 'spawn_agent', 'spawn_reviewed_coder'];
      if (subagentTools.includes(d.name) && d.result) {
        try {
          const parsed = parseSubagentsFromText(d.result);
          if (parsed && parsed.length > 0) {
            const call = acts.slice().reverse().find((a) => a.type === 'tool_call' && (a.id === d.id || (!d.id && a.name === d.name)));
            parsed.forEach((item: any, pIdx: number) => {
              const idx = item.index || pIdx + 1;
              const subKey = String(idx - 1);
              const canonStatus = (item.status === 'success' || item.status === 'ok')
                ? 'completed'
                : (item.status === 'step_limit' || item.status === 'step_exhausted')
                ? 'step_exhausted'
                : item.status === 'timeout'
                ? 'timeout'
                : 'failed';
              const patch = {
                id: `sub_${idx}`,
                index: idx,
                role: item.role || 'subagent',
                status: canonStatus as any,
                failure_reason: item.failure_reason,
                error: item.failure_reason,
                output: item.output,
              };
              if (call) {
                if (!call.subagents) call.subagents = {};
                call.subagents[subKey] = {
                  ...(call.subagents[subKey] || {}),
                  ...patch,
                };
              }
              ctx.onSubagents?.update(subKey, patch);
            });
          }
        } catch (_) {}
      }
      if (SHELL_TOOLS.includes(d.name)) {
        const raw = String(d.result ?? '');
        let stdout = raw;
        let stderr = '';
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
        ctx.onShell?.finish(String(d.id), { stdout, stderr, exitCode: m ? parseInt(m[1], 10) : d.ok === false ? 1 : 0 });
      }
      if (FILE_TOOLS.includes(d.name) && d.ok === false) {
        const p = d.args && (d.args.path || d.args.file || d.args.filename);
        toast(`⚠️ Failed to write ${p ? String(p).split(/[\\/]/).pop() : 'file'}`, 'error');
      }
      if (d.name === 'browser_screenshot' || d.name === 'mobile_screenshot') {
        const shotImg = d.image || (typeof d.result === 'string' ? d.result.match(/saved:\s*([^\s()]+\.png)/i)?.[1] : null);
        if (shotImg) {
          ctx.onScreenshot?.(shotImg);
        }
      }
      break;
    }
    case 'verify':
      acts.push({ type: 'verify', ...d });
      L.statusText = 'Verifying tool changes...';
      break;
    case 'permission_request':
      ctx.onPermission({ reqId: d.req_id, cmd: d.cmd, kind: d.kind, saveable: d.saveable !== false, tainted: !!d.tainted, suspicious: !!d.suspicious });
      break;
    case 'delta':
      closeThought(S);
      if (S.resetPrev != null) {
        // after a delta_reset: keep the earlier text as reasoning only when the replacement is genuinely different
        const prev = S.resetPrev;
        S.resetPrev = null;
        if (!(d.text || '').includes(prev.slice(0, 160))) {
          S.reasoning = S.reasoning ? S.reasoning + '\n\n' + prev : prev;
          L.thinking = S.reasoning;
        }
      }
      L.content += d.text || '';
      L.statusText = '';
      break;
    case 'delta_replace':
      L.content = d.text || '';
      L.statusText = '';
      break;
    case 'budget_stop':
      L.canContinue = true;
      break;
    case 'delta_reset':
      if (d && d.purge) {
        // a policy withheld the answer: neither it nor the thinking that quoted it stays on screen
        L.content = '';
        S.reasoning = '';
        L.thinking = '';
        S.resetPrev = null;
      } else {
        if (L.content && L.content.trim()) S.resetPrev = L.content.trim();
        L.content = '';
      }
      break;
    case 'validated':
      acts.push({ type: 'validated', ...d });
      L.statusText = 'Validating solution...';
      break;
    case 'ctx':
      if (!S.ctxToastShown) {
        S.ctxToastShown = true;
        const kb = (n: number) => (n >= 1000 ? (n / 1000).toFixed(1) + 'k' : String(n));
        const who = d.lane === 'executor' ? 'Helper model' : 'Main model';
        toast(`🧹 ${who}: older steps summarised to fit its window (${kb(d.before_tokens)} → ${kb(d.after_tokens)} tokens)`);
      }
      break;
    case 'plan': {
      // only the latest checklist snapshot is kept
      // cleared: this message is a new task, so the last task's plan no longer applies (the Plan tab empties)
      const planAct = { type: 'plan', items: d.items || [], ...(d.cleared ? { cleared: true } : {}) };
      const pi = acts.findIndex((a) => a.type === 'plan');
      if (pi >= 0) acts[pi] = planAct;
      else acts.push(planAct);
      break;
    }
    case 'kb_blocked':
      acts.push({ type: 'tool_call', id: 'kb_blocked', name: 'search_knowledge_base', args: {} });
      acts.push({ type: 'tool_result', id: 'kb_blocked', name: 'search_knowledge_base', ok: false, result: d.message || '' });
      break;
    case 'lane_warning':
      // "Routine steps run on the main model" is a choice in Settings, not a failure: a plain info toast. The toast draws its own icon.
      toast(d.message || 'The executor model is not being used for this run.',
            d.reason === 'steps_on_main' ? 'info' : d.retrying ? 'warn' : 'error');
      // /parallel that could not fan out: a toast is easy to miss on a long run, so the reason also stays in the trace
      if (d.reason === 'parallel_off' && d.message) acts.push({ type: 'narration', text: `ℹ️ ${d.message}` });
      break;
    case 'guard':
      acts.push({ type: 'guard', rule: d.rule, message: d.message });
      toast('🧼 ' + (d.message || 'Response filtered by policy: ' + (d.rule || '')), 'warn');
      break;
    case 'done':
      closeThought(S);
      S.runState = d.state || (d.reason ? 'stopped' : 'completed');
      if (d.reason) {
        acts.push({
          type: 'stopped', reason: d.reason, note: d.note || '', detail: d.detail || '', steps: d.steps,
          pending: d.pending || 0, plan_total: d.plan_total || 0, plan_done: d.plan_done || 0,
          plan_failed: d.plan_failed || 0, elapsed_s: d.elapsed_s || 0,
        });
      }
      L.statusText = '';
      break;
    case 'error':
      throw new Error(d.error || 'Agent run failed');
    default:
      break;
  }
}

/** Latest plan checklist found in a conversation (drives the dock's Plan tab). */
export function latestPlanItems(messages: ChatMessage[]): any[] {
  for (let i = messages.length - 1; i >= 0; i--) {
    const acts = messages[i].acts;
    if (!acts) continue;
    for (let j = acts.length - 1; j >= 0; j--) {
      if (acts[j].type !== 'plan') continue;
      if (Array.isArray(acts[j].items) && acts[j].items.length) return acts[j].items;
      if (acts[j].cleared) return [];       // a new task started here without the old plan: older plans are not shown
    }
  }
  return [];
}

/** Latest subagents list found in a conversation (drives the dock's Sub-Agents tab). */
export function latestSubagentsFromMessages(messages: ChatMessage[]): any[] {
  for (let i = messages.length - 1; i >= 0; i--) {
    const acts = messages[i].acts;
    if (!acts) continue;
    for (let j = acts.length - 1; j >= 0; j--) {
      const a = acts[j];
      if (a.type === 'tool_call' && ['spawn_parallel_agents', 'spawn_agent', 'spawn_reviewed_coder'].includes(a.name)) {
        const resAct = acts.find((r) => r.type === 'tool_result' && (r.id === a.id || (!r.id && r.name === a.name)));
        const parsed = resAct?.result ? parseSubagentsFromText(resAct.result) : [];

        if (a.subagents && Object.keys(a.subagents).length > 0) {
          const list = Object.values(a.subagents);
          if (parsed && parsed.length > 0) {
            return list.map((item: any) => {
              const p = parsed.find((pItem: any) => pItem.index === (item.index || item.sub_index));
              if (p) {
                const canonStatus = (p.status === 'success' || p.status === 'ok')
                  ? 'completed'
                  : (p.status === 'step_limit' || p.status === 'step_exhausted')
                  ? 'step_exhausted'
                  : p.status === 'timeout'
                  ? 'timeout'
                  : 'failed';
                return {
                  ...item,
                  status: canonStatus || item.status,
                  failure_reason: p.failure_reason || item.failure_reason,
                  error: p.failure_reason || item.error,
                  output: item.output || p.output,
                  files_modified: item.files_modified || p.files || [],
                  files: item.files || item.files_modified || p.files || [],
                  tools: item.tools || [],
                  last_tool: item.last_tool,
                };
              }
              return item;
            });
          }
          return list;
        }
        if (parsed && parsed.length > 0) {
          return parsed.map((p: any) => {
            const canonStatus = (p.status === 'success' || p.status === 'ok')
              ? 'completed'
              : (p.status === 'step_limit' || p.status === 'step_exhausted')
              ? 'step_exhausted'
              : p.status === 'timeout'
              ? 'timeout'
              : 'failed';
            return {
              id: `sub_${p.index}`,
              index: p.index,
              role: p.role,
              task: p.task || '',
              status: canonStatus,
              step: p.stepsCount || 0,
              max_steps: p.stepsCount || 10,
              output: p.final_content || '',
              failure_reason: p.failure_reason,
              error: p.failure_reason,
              files_modified: p.files || [],
              files: p.files || [],
              tools: p.steps?.map((st: any) => st.tools?.[0]).filter(Boolean) || [],
            };
          });
        }
        if (Array.isArray(a.args?.agents)) {
          return a.args.agents.map((ag: any, idx: number) => ({
            id: String(idx),
            index: idx + 1,
            role: ag.role || 'subagent',
            task: ag.task || '',
            status: a.running ? 'running' : 'completed',
            step: 0,
            max_steps: ag.max_steps || 10,
          }));
        }
      }
    }
  }
  return [];
}
