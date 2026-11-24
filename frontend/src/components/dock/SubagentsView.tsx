import React, { useMemo, useState } from 'react';
import { Check, Circle, X } from 'lucide-react';
import { useAppStore, type SubagentInfo } from '../../stores/useAppStore';
import { latestSubagentsFromMessages } from '../../stores/agentEvents';
import '../../styles/subagents.css';

type Filter = 'all' | 'running' | 'completed' | 'exhausted' | 'failed';

const stateOf = (s: SubagentInfo) => {
  const st = String(s.status || 'pending');
  if (st === 'completed' || st === 'success') return 'completed';
  if (st === 'step_exhausted' || st === 'step_limit') return 'exhausted';
  if (st === 'failed' || st === 'error' || st === 'timeout') return 'failed';
  if (st === 'running') return 'running';
  return 'pending';
};

function getToolInfo(toolName?: string | null, target?: string | null) {
  if (!toolName) return null;
  const name = toolName.toLowerCase();
  if (name.includes('web_search') || name.includes('search_web')) {
    return { icon: '🌐', label: 'Web Search', desc: target ? `Searching: "${target}"` : 'Searching the web...' };
  }
  if (name.includes('web_fetch') || name.includes('fetch_url') || name.includes('browse')) {
    return { icon: '🔗', label: 'Web Fetch', desc: target ? `Fetching: ${target}` : 'Fetching web page...' };
  }
  if (name === 'write_file' || name === 'create_file') {
    return { icon: '✏️', label: 'Write File', desc: target ? `Writing: ${target}` : 'Writing file...' };
  }
  if (name === 'edit_file' || name === 'append_file' || name === 'insert_at_line') {
    return { icon: '📝', label: 'Edit File', desc: target ? `Editing: ${target}` : 'Editing file...' };
  }
  if (name === 'read_file' || name === 'read_symbol') {
    return { icon: '📖', label: 'Read File', desc: target ? `Reading: ${target}` : 'Reading file...' };
  }
  if (name === 'list_files' || name === 'find_by_name') {
    return { icon: '📁', label: 'List Files', desc: target ? `Scanning: ${target}` : 'Scanning files...' };
  }
  if (name === 'grep' || name === 'code_search' || name === 'search_memory') {
    return { icon: '🔍', label: 'Code Search', desc: target ? `Grep: "${target}"` : 'Searching code...' };
  }
  if (name === 'run_python' || name === 'run_shell' || name === 'terminal_run' || name === 'exec') {
    return { icon: '▶️', label: 'Run Command', desc: target ? `Running: ${target}` : 'Executing command...' };
  }
  return { icon: '⚡', label: toolName, desc: target ? `${toolName}: ${target}` : `Running ${toolName}...` };
}

function getRoleMeta(role?: string) {
  const r = (role || 'worker').toLowerCase();
  if (r.includes('plan')) return { icon: '🧭', label: 'Planner' };
  if (r.includes('code')) return { icon: '💻', label: 'Coder' };
  if (r.includes('review') || r.includes('critic')) return { icon: '🔎', label: 'Reviewer' };
  if (r.includes('research') || r.includes('explore')) return { icon: '🔭', label: 'Researcher' };
  return { icon: '🤖', label: role || 'Worker' };
}

export const SubagentsView: React.FC = () => {
  const activeMap = useAppStore((s) => s.activeSubagents);
  const messages = useAppStore((s) => s.messages);
  const [filter, setFilter] = useState<Filter>('all');
  const [expanded, setExpanded] = useState<Record<string, boolean>>({});

  const subagents = useMemo(() => {
    const fromStore = Object.values(activeMap);
    return fromStore.length > 0 ? fromStore : latestSubagentsFromMessages(messages);
  }, [activeMap, messages]);

  const count = (st: string) => subagents.filter((s) => stateOf(s) === st).length;
  const running = count('running'), completed = count('completed');
  const exhausted = count('exhausted'), failed = count('failed');
  const filtered = filter === 'all'
    ? subagents
    : subagents.filter((s) => stateOf(s) === filter);

  const isOpen = (s: SubagentInfo, idx: number) => {
    const key = s.id || String(idx);
    if (expanded[key] !== undefined) return expanded[key];
    const st = stateOf(s);
    return st === 'running' || st === 'failed' || st === 'exhausted' || subagents.length <= 2;
  };

  if (!subagents.length) {
    return (
      <div className="sa-empty">
        <div className="sa-empty-rail" aria-hidden="true"><span /><span /><span /></div>
        <div className="sa-empty-copy">
          <span className="sa-empty-kicker">Standby</span>
          <h2>No sub-agents yet</h2>
          <p>When the agent delegates work, each worker reports its steps and findings here.</p>
        </div>
      </div>
    );
  }

  return (
    <div className="sa-shell">
      <header className="sa-head">
        <div className="sa-head-row">
          <span className="sa-head-label">Sub-agents</span>
          <span className={`sa-head-count${running ? ' is-live' : ''}`}>{completed}<span>/{subagents.length} done</span></span>
        </div>
        <div className="sa-head-status">
          {running ? `${running} running` : failed ? `${failed} failed` : exhausted ? `${exhausted} exhausted` : 'All workers finished'}
        </div>
        <div className="sa-filters">
          {([
            ['all', `All ${subagents.length}`],
            ['running', `Running ${running}`],
            ['completed', `Done ${completed}`],
            ...(exhausted ? [['exhausted', `Exhausted ${exhausted}`] as [Filter, string]] : []),
            ...(failed ? [['failed', `Failed ${failed}`] as [Filter, string]] : []),
          ] as [Filter, string][]).map(([key, label]) => (
            <button key={key} type="button" className={`sa-filter ${filter === key ? 'active' : ''}`} onClick={() => setFilter(key)}>
              {label}
            </button>
          ))}
        </div>
      </header>

      <ul className="sa-list">
        {filtered.map((s, idx) => {
          const st = stateOf(s);
          const key = s.id || String(idx);
          const open = isOpen(s, idx);
          const cur = s.step || 0, max = s.max_steps || 10;
          const pct = Math.min(100, Math.round((cur / max) * 100));
          const roleMeta = getRoleMeta(s.role);
          const activeTool = getToolInfo(s.current_tool, s.current_target);
          const lastTool = s.last_tool ? getToolInfo(s.last_tool.tool, s.last_tool.target) : null;
          const filesList = Array.from(new Set([...(s.files_modified || []), ...(s.files || [])]));

          return (
            <li key={key} className={`sa-item is-${st}`}>
              <button type="button" className="sa-sum" onClick={() => setExpanded((e) => ({ ...e, [key]: !open }))}>
                <span className="sa-node" aria-hidden="true">
                  {st === 'completed' ? <Check size={12} strokeWidth={2.6} />
                    : st === 'failed' || st === 'exhausted' ? <X size={12} strokeWidth={2.6} />
                      : st === 'running' ? <span className="sa-live-spinner" /> : <Circle size={7} strokeWidth={2} />}
                </span>
                <span className="sa-name" title={s.role || 'subagent'}>
                  {roleMeta.icon} {s.role || 'subagent'}
                </span>
                {st === 'running' ? (
                  <span className="sa-item-head-preview" title={activeTool?.desc || (lastTool ? `Last: ${lastTool.label}` : 'reasoning…')}>
                    {activeTool ? `${activeTool.icon} ${activeTool.label}` : (lastTool ? `⚡ ${lastTool.label}` : 'reasoning…')}
                  </span>
                ) : null}
                <span className="sa-meta">{cur}/{max}</span>
                <span className="sa-chev" aria-hidden="true">{open ? '⌄' : '›'}</span>
              </button>
              <div className="sa-progress"><div className="sa-progress-fill" style={{ width: `${pct}%` }} /></div>
              {open ? (
                <div className="sa-detail">
                  {/* Active Tool Execution or Reasoning */}
                  {st === 'running' && (
                    <div className="sa-detail-section sa-current-section">
                      <div className="sa-section-header">
                        <span className="sa-pulse-dot" />
                        <span className="sa-section-title">Current Activity</span>
                      </div>
                      {s.current_tool && activeTool ? (
                        <div className="sa-current-tool-card">
                          <div className="sa-tool-badge-row">
                            <span className="sa-tool-icon">{activeTool.icon}</span>
                            <span className="sa-tool-name-badge">{activeTool.label}</span>
                            <code className="sa-tool-code">{s.current_tool}</code>
                          </div>
                          {s.current_target && (
                            <div className="sa-tool-target-box" title={s.current_target}>
                              {s.current_target}
                            </div>
                          )}
                        </div>
                      ) : (
                        <div className="sa-reasoning-card">
                          <div className="sa-reasoning-text">🧠 Model reasoning &amp; planning next step...</div>
                          {s.last_tool && (
                            <div className="sa-last-tool-hint">
                              <span>Last action:</span> <code>{s.last_tool.tool}</code>
                              {s.last_tool.target && <span className="sa-last-target">({s.last_tool.target})</span>}
                              <span className={`sa-tool-verdict ${s.last_tool.ok ? 'ok' : 'fail'}`}>
                                {s.last_tool.ok ? '✓ ok' : '✕ err'}
                              </span>
                            </div>
                          )}
                        </div>
                      )}
                    </div>
                  )}

                  {/* Task Assigned */}
                  {s.task ? (
                    <div className="sa-detail-section">
                      <div className="sa-section-header">
                        <span className="sa-section-title">Task</span>
                      </div>
                      <div className="sa-task-box">{s.task}</div>
                    </div>
                  ) : null}

                  {/* Tool Calls History */}
                  {s.tools && s.tools.length > 0 ? (
                    <div className="sa-detail-section">
                      <div className="sa-section-header">
                        <span className="sa-section-title">Tool Calls &amp; Actions ({s.tools.length})</span>
                      </div>
                      <div className="sa-tools-timeline">
                        {s.tools.slice(-10).map((t: any, tIdx: number) => {
                          const tm = getToolInfo(t.tool, t.target);
                          return (
                            <div key={tIdx} className={`sa-timeline-item ${t.ok === false ? 'is-error' : 'is-ok'}`}>
                              <span className="sa-timeline-icon">{tm?.icon || '⚡'}</span>
                              <div className="sa-timeline-body">
                                <div className="sa-timeline-row">
                                  <code className="sa-timeline-tool">{t.tool}</code>
                                  {t.ok !== undefined && (
                                    <span className={`sa-timeline-badge ${t.ok ? 'ok' : 'error'}`}>
                                      {t.ok ? '✓ ok' : '✕ err'}
                                    </span>
                                  )}
                                </div>
                                {t.target && (
                                  <div className="sa-timeline-target" title={t.target}>
                                    {t.target}
                                  </div>
                                )}
                              </div>
                            </div>
                          );
                        })}
                      </div>
                    </div>
                  ) : null}

                  {/* Files Modified / Touched */}
                  {filesList.length > 0 ? (
                    <div className="sa-detail-section">
                      <div className="sa-section-header">
                        <span className="sa-section-title">Files Modified ({filesList.length})</span>
                      </div>
                      <div className="sa-files-chips">
                        {filesList.map((f, fIdx) => (
                          <span key={fIdx} className="sa-file-chip" title={f}>
                            📄 {f}
                          </span>
                        ))}
                      </div>
                    </div>
                  ) : null}

                  {/* Failure / Issue Reason */}
                  {(st === 'failed' || st === 'exhausted') && (s.error || s.failure_reason) ? (
                    <div className="sa-detail-section is-failed">
                      <div className="sa-section-header">
                        <span className="sa-section-title">Issue</span>
                      </div>
                      <div className="sa-issue-box">{s.error || s.failure_reason}</div>
                    </div>
                  ) : null}

                  {/* Output Preview */}
                  {s.output && st !== 'running' ? (
                    <div className="sa-detail-section">
                      <div className="sa-section-header">
                        <span className="sa-section-title">Output</span>
                      </div>
                      <pre className="sa-output">{s.output}</pre>
                    </div>
                  ) : null}
                </div>
              ) : null}
            </li>
          );
        })}
      </ul>
    </div>
  );
};
