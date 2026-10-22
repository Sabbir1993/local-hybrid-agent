import React, { useEffect, useMemo, useState } from 'react';
import { apiFetch, getClientDeviceId, getDeviceDisplayLabel } from '../../services/api';
import { useAppStore } from '../../stores/useAppStore';

const ICON_PRESETS = ['📧', '📊', '🧪', '📈', '📝', '🔍', '🛡️', '⚙️', '💻', '🧠'];

async function pickFolder(cur: string): Promise<string> {
  try {
    const w = window as any;
    if (w.electronAPI && typeof w.electronAPI.browseFolder === 'function') return (await w.electronAPI.browseFolder(cur || '')) || '';
    if (typeof w.pickFolder === 'function') return (await w.pickFolder(cur || '')) || '';
  } catch (_) {
    // fall through to the prompt
  }
  return window.prompt('Enter the folder path for this agent to work in:', cur || '') || '';
}

async function agentApi(url: string, init: RequestInit = {}): Promise<any> {
  const res = await apiFetch(url, init);
  const data = await res.json().catch(() => ({}));
  if (!res.ok) {
    const d = (data && (data.detail || data.error || data.message)) as any;
    throw new Error(typeof d === 'string' ? d : d ? JSON.stringify(d) : `HTTP ${res.status}`);
  }
  return data;
}

const Overlay: React.FC<{ onClose: () => void; maxWidth?: number; children: React.ReactNode }> = ({ onClose, maxWidth, children }) => {
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => { if (e.key === 'Escape') onClose(); };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [onClose]);
  return (
    <div className="ca-modal-overlay" style={{ display: 'flex' }}>
      <div className="ca-modal-box" style={maxWidth ? { maxWidth } : undefined}>{children}</div>
    </div>
  );
};

interface ToolDef { name: string; label?: string; description?: string; category?: string }

/** Create / edit a custom agent (port of the legacy "Custom Agent Builder"). */
const AgentBuilder: React.FC<{ agent: any | null }> = ({ agent }) => {
  const { closeAgentModal, bumpAgents, showToast, setActiveCustomAgent } = useAppStore();
  const [name, setName] = useState(agent?.name || '');
  const [slug, setSlug] = useState(agent?.slug || '');
  const [icon, setIcon] = useState(agent?.icon || '🤖');
  const [desc, setDesc] = useState(agent?.description || '');
  const [prompt, setPrompt] = useState(agent?.system_prompt || '');
  const [template, setTemplate] = useState(agent?.input_template || '');
  const [lane, setLane] = useState(agent?.preferred_lane || 'auto');
  const [effort, setEffort] = useState(agent?.reasoning_effort || 'medium');
  const [temp, setTemp] = useState<number>(agent?.temperature != null ? Number(agent.temperature) : 0.4);
  const [workDir, setWorkDir] = useState(agent?.work_dir || '');
  const [isPublic, setIsPublic] = useState(Boolean(agent?.is_public) || agent?.share_status === 'pending');
  const [tools, setTools] = useState<ToolDef[]>([]);
  const initialSel: string[] = agent?.tool_allowlist || [];
  const [restricted, setRestricted] = useState(initialSel.length > 0);
  const [selected, setSelected] = useState<Set<string>>(new Set(initialSel));
  const [toolFilter, setToolFilter] = useState('');
  const [saving, setSaving] = useState(false);

  useEffect(() => {
    agentApi('/custom-agents/tools/available').then((d) => setTools(d.tools || [])).catch(() => {});
  }, []);

  const groups = useMemo(() => {
    const q = toolFilter.trim().toLowerCase();
    const g: Record<string, ToolDef[]> = {};
    for (const t of tools) {
      const cat = t.category || 'General';
      if (q && !`${t.label || ''} ${t.name} ${t.description || ''} ${cat}`.toLowerCase().includes(q)) continue;
      (g[cat] = g[cat] || []).push(t);
    }
    return g;
  }, [tools, toolFilter]);

  const devLabel = getDeviceDisplayLabel();
  const isAlwaysOn = (n: string) => n === 'write_file' || n === 'edit_file';   // never withheld from an agent

  const save = async () => {
    if (!name.trim()) { showToast('Agent name is required', 'error'); return; }
    if (!prompt.trim()) { showToast('System prompt instructions are required', 'error'); return; }
    let tool_allowlist: string[] = [];
    if (restricted) {
      tool_allowlist = tools.filter((t) => isAlwaysOn(t.name) || selected.has(t.name)).map((t) => t.name);
      if (!tool_allowlist.length) { showToast('Pick at least one tool, or choose "All Available Tools"', 'error'); return; }
    }
    const payload = {
      name: name.trim(),
      slug: slug.trim() || undefined,
      icon: icon.trim() || '🤖',
      description: desc.trim(),
      system_prompt: prompt.trim(),
      tool_allowlist,
      input_template: template.trim(),
      preferred_lane: lane,
      reasoning_effort: effort,
      temperature: Number.isFinite(temp) ? temp : 0.4,
      is_public: isPublic,
      work_dir: workDir.trim(),
      device_id: agent?.device_id || getClientDeviceId(),
      device_name: agent?.device_name || devLabel,
    };
    setSaving(true);
    try {
      if (agent) {
        await agentApi(`/custom-agents/${agent.id}`, { method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(payload) });
        showToast(payload.is_public ? `Updated "${payload.name}". Sharing needs approval before others see it.` : `Updated agent "${payload.name}"`, 'success');
      } else {
        const created = await agentApi('/custom-agents', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(payload) });
        showToast(`Created agent "${payload.name}"`, 'success');
        setActiveCustomAgent(created);
      }
      closeAgentModal();
      bumpAgents();
    } catch (e: any) {
      showToast(`Error saving agent: ${e.message}`, 'error');
    }
    setSaving(false);
  };

  const remove = async () => {
    if (!agent || !window.confirm('Are you sure you want to delete this custom agent?')) return;
    try {
      await agentApi(`/custom-agents/${agent.id}`, { method: 'DELETE' });
      if (useAppStore.getState().activeCustomAgent?.id === agent.id) setActiveCustomAgent(null);
      closeAgentModal();
      bumpAgents();
      showToast('Agent deleted', 'success');
    } catch (e: any) {
      showToast(`Delete failed: ${e.message}`, 'error');
    }
  };

  return (
    <Overlay onClose={closeAgentModal}>
      <div className="ca-modal-head">
        <div className="ca-modal-title-wrap">
          <div className="ca-modal-device-badge">
            <span>💻</span>
            <span>{agent ? `Bound Device: ${agent.device_name || devLabel}` : `Binding to Device: ${devLabel}`}</span>
          </div>
          <span>{agent ? `Edit Agent: ${agent.name}` : 'Create Custom Agent'}</span>
        </div>
        <button className="rep-close" title="Close (Esc)" onClick={closeAgentModal}>✕</button>
      </div>
      <div className="ca-modal-body">
        <div className="ca-form-row">
          <div className="ca-field" style={{ flex: 1 }}>
            <label htmlFor="ca-name">Agent Name *</label>
            <input type="text" id="ca-name" autoFocus placeholder="e.g. Email Analyzer & Drafter" value={name} onChange={(e) => setName(e.target.value)} />
          </div>
          <div className="ca-field" style={{ width: 130 }}>
            <label htmlFor="ca-slug">Slug / Tag</label>
            <input type="text" id="ca-slug" placeholder="e.g. email-analyzer" value={slug} onChange={(e) => setSlug(e.target.value)} />
          </div>
          <div className="ca-field" style={{ width: 80 }}>
            <label htmlFor="ca-icon">Icon</label>
            <input type="text" id="ca-icon" value={icon} onChange={(e) => setIcon(e.target.value)} style={{ textAlign: 'center', fontSize: 16 }} />
          </div>
        </div>

        <div className="ca-icon-presets-row">
          <span className="dim" style={{ fontSize: 11 }}>Quick Icon:</span>
          {ICON_PRESETS.map((i) => (
            <button key={i} type="button" className="ca-icon-preset" onClick={() => setIcon(i)}>{i}</button>
          ))}
        </div>

        <div className="ca-field">
          <label htmlFor="ca-desc">Short Description *</label>
          <input type="text" id="ca-desc" placeholder="e.g. Summarizes email threads and prepares draft replies" value={desc} onChange={(e) => setDesc(e.target.value)} />
        </div>

        <div className="ca-field">
          <label htmlFor="ca-prompt">System Persona &amp; Directives (Instructions) *</label>
          <textarea id="ca-prompt" rows={7} placeholder="Define this agent's domain expertise, procedure, instructions, and output format..." value={prompt} onChange={(e) => setPrompt(e.target.value)} />
        </div>

        <div className="ca-field">
          <label htmlFor="ca-template">Prompt Input Wrapper Template (Optional)</label>
          <textarea id="ca-template" rows={3} placeholder={'Wraps your first message; {input} = what you type. e.g.\nAnalyze the following request:\n{input}'} value={template} onChange={(e) => setTemplate(e.target.value)} />
          <span className="ca-field-hint">If provided, user's input will replace {'{input}'} before execution.</span>
        </div>

        <div className="ca-form-row">
          <div className="ca-field" style={{ flex: 1 }}>
            <label htmlFor="ca-lane">Preferred Model Lane</label>
            <select id="ca-lane" value={lane} onChange={(e) => setLane(e.target.value)}>
              <option value="auto">Auto (Runtime Default)</option>
              <option value="main">Main LLM (Arc A770)</option>
              <option value="executor">Fast Executor (Local 3B)</option>
              <option value="cloud">Cloud Provider</option>
            </select>
          </div>
          <div className="ca-field" style={{ flex: 1 }}>
            <label htmlFor="ca-effort">Reasoning Effort</label>
            <select id="ca-effort" value={effort} onChange={(e) => setEffort(e.target.value)}>
              <option value="none">None</option>
              <option value="low">Low</option>
              <option value="medium">Medium</option>
              <option value="high">High</option>
              <option value="extra">Extra (Deep)</option>
            </select>
          </div>
          <div className="ca-field" style={{ flex: 1 }}>
            <label htmlFor="ca-temp">Temperature: <span>{temp}</span></label>
            <input type="range" id="ca-temp" min={0} max={1} step={0.05} value={temp} onChange={(e) => setTemp(parseFloat(e.target.value))} />
          </div>
        </div>

        <div className="ca-field">
          <label htmlFor="ca-workdir">Work folder on your computer (Optional)</label>
          <div style={{ display: 'flex', gap: 6 }}>
            <input type="text" id="ca-workdir" placeholder="D:/reports/weekly" autoComplete="off" spellCheck={false} style={{ flex: 1 }} value={workDir} onChange={(e) => setWorkDir(e.target.value)} />
            <button type="button" className="ca-browse" onClick={async () => { const p = await pickFolder(workDir); if (p) setWorkDir(p); }}>
              <span aria-hidden="true">📁</span> Browse…
            </button>
          </div>
          <span className="ca-field-hint">With a folder set, this agent can read, write and analyse files there when you chat with it in the desktop app, and repeat the same task from the same place every time. It cannot leave the folder. Without one it only chats. The folder is private to you and is never shared or copied to forks.</span>
        </div>

        <div className="ca-field">
          <label>Tool Permissions &amp; Capabilities</label>
          <div id="ca-tools-container">
            <div className="ca-tool-preset-bar">
              <label><input type="radio" name="ca-tool-scope" checked={!restricted} onChange={() => setRestricted(false)} /> All Available Tools</label>
              <label><input type="radio" name="ca-tool-scope" checked={restricted} onChange={() => setRestricted(true)} /> Restricted Toolset</label>
            </div>
            {restricted && (
              <>
                <input type="search" id="ca-tool-filter" className="ca-filter" placeholder="Filter tools, e.g. file, web, browser" autoComplete="off" style={{ display: 'block' }} value={toolFilter} onChange={(e) => setToolFilter(e.target.value)} />
                <div id="ca-tools-list-wrap" style={{ display: 'grid' }}>
                  {Object.entries(groups).map(([cat, list]) => (
                    <div key={cat} className="ca-tool-group">
                      <div className="ca-tool-group-title">{cat}</div>
                      <div className="ca-tool-group-items">
                        {list.map((t) => {
                          const always = isAlwaysOn(t.name);
                          return (
                            <label key={t.name} className="ca-tool-item" title={always ? 'Every agent can write files, so this stays on' : t.description}>
                              <input
                                type="checkbox"
                                className="ca-tool-cb"
                                checked={always || selected.has(t.name)}
                                disabled={always}
                                onChange={(e) => setSelected((prev) => { const n = new Set(prev); if (e.target.checked) n.add(t.name); else n.delete(t.name); return n; })}
                              />
                              <span className="ca-tool-label">{t.label || t.name}</span>
                              {always && <span className="ca-tool-on">always on</span>}
                            </label>
                          );
                        })}
                      </div>
                    </div>
                  ))}
                </div>
              </>
            )}
          </div>
        </div>

        <div className="ca-field-check">
          <label><input type="checkbox" checked={isPublic} onChange={(e) => setIsPublic(e.target.checked)} /> Ask to share with all users on this server</label>
          <span className="ca-field-hint">Someone with approval rights reviews it first. Once approved it appears in Customize → Agents, without your folder, and others fork it and choose their own. Editing it later sends it for review again.</span>
        </div>
      </div>
      <div className="ca-modal-foot">
        {agent && agent.can_edit !== false && agent.scope !== 'template' && (
          <button type="button" className="btn red" style={{ marginRight: 'auto' }} onClick={remove}>Delete Agent</button>
        )}
        <button type="button" className="btn ghost" onClick={closeAgentModal}>Cancel</button>
        <button type="button" className="btn blue" disabled={saving} onClick={save}>{saving ? 'Saving…' : 'Save Agent'}</button>
      </div>
    </Overlay>
  );
};

/** Fork an agent onto this device (port of the legacy "Fork Agent for This Device" dialog). */
const AgentFork: React.FC<{ agent: any }> = ({ agent }) => {
  const { closeAgentModal, bumpAgents, showToast, setActiveCustomAgent, createSession } = useAppStore();
  const [name, setName] = useState(`${agent.name} (Fork)`);
  const [workDir, setWorkDir] = useState('');
  const [busy, setBusy] = useState(false);
  const devLabel = getDeviceDisplayLabel();
  const myId = getClientDeviceId().toLowerCase();
  const onThisDevice = !agent.device_id || String(agent.device_id).toLowerCase() === myId;

  const submit = async () => {
    const finalName = name.trim() || `${agent.name} (Fork)`;
    if (!workDir.trim()) { showToast('Please specify a workspace path on this device', 'error'); return; }
    setBusy(true);
    try {
      const forked = await agentApi(`/custom-agents/${agent.id}/fork`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ name: finalName, work_dir: workDir.trim(), device_id: getClientDeviceId(), device_name: devLabel }),
      });
      closeAgentModal();
      bumpAgents();
      showToast(`Forked & bound to this device as "${forked.name}" ⚡`, 'success');
      setActiveCustomAgent(forked);
      createSession();
    } catch (e: any) {
      showToast(`Fork failed: ${e.message}`, 'error');
    }
    setBusy(false);
  };

  return (
    <Overlay onClose={closeAgentModal} maxWidth={520}>
      <div className="ca-modal-head">
        <div className="ca-modal-title-wrap">
          <div className="ca-modal-device-badge">
            <span>💻</span>
            <span>Target Device: {devLabel}</span>
          </div>
          <span>Fork Agent: {agent.name}</span>
        </div>
        <button className="rep-close" title="Close (Esc)" onClick={closeAgentModal}>✕</button>
      </div>
      <div className="ca-modal-body" style={{ padding: '16px 20px' }}>
        <div style={{ background: 'var(--panel2, rgba(255,255,255,0.04))', border: '1px solid var(--border-subtle, rgba(255,255,255,0.08))', borderRadius: 6, padding: '10px 12px', marginBottom: 14, fontSize: 12, lineHeight: 1.5 }}>
          <div style={{ color: 'var(--text)', fontWeight: 600 }}>{agent.icon || '🤖'} {agent.name}</div>
          <div style={{ color: 'var(--dim)', fontSize: 11, marginTop: 2 }}>
            {onThisDevice
              ? 'Forking creates a distinct clone bound to this machine, allowing you to configure a different workspace directory or prompt.'
              : `🔒 Barrier Active: This agent is bound to "${agent.device_name || 'another device'}". To use it on this machine ("${devLabel}"), fork it and choose a local workspace directory below.`}
          </div>
        </div>
        <div className="ca-field" style={{ marginBottom: 12 }}>
          <label htmlFor="ca-fork-name" style={{ display: 'block', fontSize: 11, fontWeight: 600, color: 'var(--dim)', marginBottom: 5 }}>Agent Name *</label>
          <input type="text" id="ca-fork-name" placeholder="Agent Name" style={{ width: '100%', boxSizing: 'border-box' }} value={name} onChange={(e) => setName(e.target.value)} />
        </div>
        <div className="ca-field" style={{ marginBottom: 8 }}>
          <label htmlFor="ca-fork-workdir" style={{ display: 'block', fontSize: 11, fontWeight: 600, color: 'var(--dim)', marginBottom: 5 }}>Workspace Path on This Device *</label>
          <div style={{ display: 'flex', gap: 6 }}>
            <input type="text" id="ca-fork-workdir" autoFocus placeholder="e.g. D:\Projects\MyWorkspace or C:\Users\..." style={{ flex: 1, boxSizing: 'border-box' }} value={workDir} onChange={(e) => setWorkDir(e.target.value)} />
            <button type="button" className="btn secondary" style={{ width: 'auto', padding: '6px 12px', whiteSpace: 'nowrap' }} onClick={async () => { const p = await pickFolder(workDir); if (p) setWorkDir(p); }}>Browse…</button>
          </div>
          <div style={{ fontSize: '10.5px', color: 'var(--dim)', marginTop: 4 }}>Select the directory where this agent will read, write, and execute tasks on this machine.</div>
        </div>
      </div>
      <div className="ca-modal-foot" style={{ display: 'flex', justifyContent: 'flex-end', gap: 8, padding: '12px 20px', borderTop: '1px solid var(--border-subtle, rgba(255,255,255,0.08))' }}>
        <button type="button" className="btn ghost" onClick={closeAgentModal}>Cancel</button>
        <button type="button" className="btn blue" disabled={busy} onClick={submit}>{busy ? 'Forking…' : 'Fork & Bind to Device'}</button>
      </div>
    </Overlay>
  );
};

export const AgentModals: React.FC = () => {
  const agentModal = useAppStore((s) => s.agentModal);
  if (!agentModal) return null;
  if (agentModal.mode === 'fork' && agentModal.agent) return <AgentFork key={agentModal.agent.id} agent={agentModal.agent} />;
  return <AgentBuilder key={agentModal.agent?.id ?? 'new'} agent={agentModal.mode === 'edit' ? agentModal.agent : null} />;
};
