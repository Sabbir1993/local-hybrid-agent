import React, { useState, useEffect, useRef } from 'react';
import { useAppStore } from '../../stores/useAppStore';
import { apiFetch } from '../../services/api';
import { ConnectorOAuthPanel, PluginMarketplace, useConnectorOAuth } from './ConnectorFlows';

type Kind = 'skills' | 'connectors' | 'plugins' | 'agents';
type View = 'yours' | 'discover';

interface CatalogItem {
  id: string;
  title: string;
  description?: string;
  category?: string;
  author?: string;
  version?: string;
  in_catalog?: boolean;
  installed?: boolean;
  installed_scope?: string;
  can_uninstall?: boolean;
  color?: string;
  egress?: boolean;
  sensitivity?: string;
  allowed?: boolean;
  oauth?: boolean;
  signed_in?: boolean;
  client?: string;
  needs_client?: boolean;
  has_client?: boolean;
  status?: string;
  modified?: boolean;
  error?: string;
  enabled?: boolean;
  triggers?: string[];
  tools?: string[];
  command?: string;
  homepage?: string;
  note?: string;
  transport?: string;
  scope_default?: string;
  secret_env?: { label?: string; key: string }[];
  fields?: { label?: string; key: string; placeholder?: string }[];
}

interface CustomizeData {
  kind: string;
  enabled: boolean;
  items: CatalogItem[];
  categories: Record<string, number>;
}

const KIND_LABEL: Record<Kind, string> = {
  skills: 'skills',
  connectors: 'connectors',
  plugins: 'plugins',
  agents: 'agents',
};

const CAT_ICON: Record<string, string> = {
  security: '🛡️',
  compliance: '⚖️',
  engineering: '⌨️',
  operations: '🧰',
  finance: '💰',
  productivity: '✅',
  web: '🌐',
  tickets: '🎫',
  developer: '🧪',
  data: '🗃️',
  general: '🧩',
  custom: '🔧',
  design: '🎨',
  communication: '💬',
  crm: '🤝',
  payments: '💳',
  cloud: '☁️',
  commerce: '🛍️',
};

const SENS: Record<string, string> = {
  pii: 'Personal data',
  payments: 'Payments',
  data: 'Project data',
};

const SENS_HINT: Record<string, string> = {
  pii: 'Can reach personal or customer records. Each person signs in with their own account.',
  payments: 'Can reach payment data. Test mode first; a PCI-DSS review is needed before real data.',
  data: 'Can reach business or project data. Each person signs in with their own account.',
};

const DEFAULT_BRAND_COLORS: Record<string, string> = {
  atlassian: '#3b82f6',
  canva: '#06b6d4',
  discord: '#6366f1',
  elasticsearch: '#ef4444',
  figma: '#ec4899',
  github: '#10b981',
  gitlab: '#059669',
  'google-calendar': '#f97316',
  'google-drive': '#2563eb',
  'google-maps': '#d97706',
  gmail: '#dc2626',
  hubspot: '#059669',
  jira: '#8b5cf6',
  linear: '#0284c7',
  notion: '#18181b',
  postgres: '#7c3aed',
  postgresql: '#7c3aed',
  salesforce: '#0ea5e9',
  slack: '#10b981',
  stripe: '#14b8a6',
  zendesk: '#f59e0b',
};

const catIcon = (cat?: string) => (cat && CAT_ICON[cat]) || CAT_ICON.general;
const catLabel = (cat?: string) =>
  (cat || 'general').replace(/[-_]/g, ' ').replace(/\b\w/g, (m) => m.toUpperCase());

export const CustomizeModal: React.FC = () => {
  const { activeModal, setActiveModal, currentUser, customizeTab, openAgentModal, agentsVersion } = useAppStore();
  const isOpen = activeModal === 'customize';

  const [kind, setKind] = useState<Kind>(customizeTab || 'skills');

  useEffect(() => {
    if (isOpen && customizeTab) {
      setKind(customizeTab);
    }
  }, [isOpen, customizeTab]);
  const [view, setView] = useState<View>('discover');
  const [search, setSearch] = useState<string>('');
  const [category, setCategory] = useState<string | null>(null);

  const [data, setData] = useState<CustomizeData | null>(null);
  const [loading, setLoading] = useState<boolean>(true);
  const [selectedItem, setSelectedItem] = useState<CatalogItem | null>(null);
  const [previewContent, setPreviewContent] = useState<{ format: string; text: string } | null>(null);
  const [previewLoading, setPreviewLoading] = useState<boolean>(false);

  // Custom Agents tab specific state
  const [agentsData, setAgentsData] = useState<{ templates: any[]; shared: any[]; pending: any[] }>({
    templates: [],
    shared: [],
    pending: [],
  });
  const [forkingAgent, setForkingAgent] = useState<any | null>(null);
  const [forkDir, setForkDir] = useState<string>('');

  // MCP Public Registry state for Connectors
  const [registryQuery, setRegistryQuery] = useState<string>('');
  const [registryServers, setRegistryServers] = useState<any[]>([]);
  const [registryLoading, setRegistryLoading] = useState<boolean>(false);

  const [actionLoading, setActionLoading] = useState<string | null>(null);
  const [installMsg, setInstallMsg] = useState<{ id: string; text: string; error: boolean } | null>(null);
  // values typed into the detail card's install form for connectors that need secrets / URL fields
  const [formSecrets, setFormSecrets] = useState<Record<string, string>>({});
  const [formFields, setFormFields] = useState<Record<string, string>>({});
  const kindRef = useRef<Kind>(kind);
  kindRef.current = kind;

  useEffect(() => {
    setInstallMsg(null);
  }, [kind, selectedItem?.id]);
  useEffect(() => {
    setFormSecrets({});
    setFormFields({});
  }, [kind, selectedItem?.id, isOpen]);

  const canInstall = () =>
    !!(currentUser && (currentUser.is_super_admin || (currentUser.permissions && currentUser.permissions.includes('capabilities.install'))));

  const canReviewAgents = () =>
    !!(currentUser && (currentUser.is_super_admin || (currentUser.permissions && currentUser.permissions.includes('custom_agents.publish'))));

  // Re-fetch the current catalog without the loading flash and keep the open detail card in sync.
  // `target` is the kind the caller means; results are dropped if the user switched tab meanwhile.
  const refreshCatalog = async (target?: Kind) => {
    const k = target || kindRef.current;
    if (k === 'agents' || k !== kindRef.current) return;
    const r = await apiFetch(`/customize/${k}`);
    if (!r.ok) return;
    const json: CustomizeData = await r.json();
    if (kindRef.current !== k) return;
    setData(json);
    setSelectedItem((cur) => (cur ? json.items.find((i) => i.id === cur.id) || null : cur));
  };

  const oauth = useConnectorOAuth(() => refreshCatalog('connectors'));

  const loadAgents = async () => {
    setLoading(true);
    try {
      const r = await apiFetch('/custom-agents');
      const json = await r.json();
      const all = json.agents || [];
      let pending: any[] = [];
      try {
        const pr = await apiFetch('/custom-agents/pending');
        if (pr.ok) {
          const pjson = await pr.json();
          pending = pjson.agents || [];
        }
      } catch (_) {}
      setAgentsData({
        templates: all.filter((a: any) => a.scope === 'template'),
        shared: all.filter((a: any) => a.scope === 'shared'),
        pending,
      });
    } catch {
      setAgentsData({ templates: [], shared: [], pending: [] });
    } finally {
      setLoading(false);
    }
  };

  const loadData = async (targetKind: Kind) => {
    if (targetKind === 'agents') {
      await loadAgents();
      return;
    }

    setLoading(true);
    try {
      const r = await apiFetch(`/customize/${targetKind}`);
      if (!r.ok) throw new Error('HTTP ' + r.status);
      const json = await r.json();
      if (kindRef.current !== targetKind) return;
      setData(json);
    } catch {
      if (kindRef.current === targetKind) setData(null);
    } finally {
      if (kindRef.current === targetKind) setLoading(false);
    }
  };

  const needsForm = (it: CatalogItem) => (it.secret_env || []).length > 0 || (it.fields || []).length > 0;

  useEffect(() => {
    if (!isOpen) {
      setSelectedItem(null);
      setPreviewContent(null);
      setForkingAgent(null);
      setForkDir('');
      return;
    }
    loadData(kind);
  }, [isOpen, kind, agentsVersion]);

  // Load preview when an item is selected
  useEffect(() => {
    if (!selectedItem || !isOpen || kind === 'agents') {
      setPreviewContent(null);
      return;
    }

    setPreviewLoading(true);
    apiFetch(`/customize/${kind}/${encodeURIComponent(selectedItem.id)}/preview`)
      .then((r) => r.json())
      .then((res) => {
        setPreviewContent(res);
      })
      .catch((err) => {
        setPreviewContent({ format: 'text', text: 'Failed to load preview: ' + err.message });
      })
      .finally(() => {
        setPreviewLoading(false);
      });
  }, [selectedItem, kind, isOpen]);

  const handleInstall = async (e: React.MouseEvent, it: CatalogItem) => {
    e.stopPropagation();
    const k = kind;
    let secrets: Record<string, string> = {};
    let fields: Record<string, string> = {};
    if (k === 'connectors' && needsForm(it)) {
      if (selectedItem?.id !== it.id) {
        // the values are entered on the detail card
        setSelectedItem(it);
        return;
      }
      const miss = [...(it.secret_env || []).filter((s) => !(formSecrets[s.key] || '').trim()),
        ...(it.fields || []).filter((f) => !(formFields[f.key] || '').trim())];
      if (miss.length) {
        setInstallMsg({ id: it.id, text: 'Fill in: ' + miss.map((m) => m.label || m.key).join(', '), error: true });
        return;
      }
      secrets = Object.fromEntries((it.secret_env || []).map((s) => [s.key, formSecrets[s.key].trim()]));
      fields = Object.fromEntries((it.fields || []).map((f) => [f.key, formFields[f.key].trim()]));
    }
    setActionLoading(it.id);
    try {
      const resp = await apiFetch(`/customize/${k}/${encodeURIComponent(it.id)}/install`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ scope: 'user', secrets, fields }),
      });
      if (!resp.ok) {
        const err = await resp.json().catch(() => ({}));
        setInstallMsg({ id: it.id, text: 'Install failed: ' + (err.detail || err.error || 'HTTP ' + resp.status), error: true });
      } else {
        const body = await resp.json().catch(() => ({}));
        const inst = body && body.item ? body.item : null;
        const startSignIn = !!(inst && inst.oauth && inst.installed_scope && (inst.installed_scope === 'user' || canInstall()));
        setFormSecrets({});
        if (k === 'connectors') {
          setInstallMsg({
            id: it.id,
            text: `Installed '${it.id}' ✓` + (inst && inst.oauth ? ' - sign in next' : ''),
            error: false,
          });
        }
        // refreshCatalog also updates the open detail card (installed flag, OAuth panel)
        await refreshCatalog(k);
        // a connector that needs a sign-in goes straight to it (same as the legacy UI)
        if (k === 'connectors' && startSignIn && kindRef.current === 'connectors') oauth.connect(it.id, inst.installed_scope);
      }
    } catch (err: any) {
      setInstallMsg({ id: it.id, text: 'Install failed: ' + err.message, error: true });
    } finally {
      setActionLoading(null);
    }
  };

  const handleRemove = async (e: React.MouseEvent, it: CatalogItem) => {
    e.stopPropagation();
    if (!confirm(`Are you sure you want to remove ${it.title}?`)) return;
    setActionLoading(it.id);
    try {
      const resp = await apiFetch(`/customize/${kind}/${encodeURIComponent(it.id)}`, {
        method: 'DELETE',
      });
      if (!resp.ok) {
        const err = await resp.json().catch(() => ({}));
        alert(err.detail || err.error || 'Remove failed');
      } else {
        await refreshCatalog(kind);
        if (selectedItem?.id === it.id) setSelectedItem(null);
      }
    } catch (err: any) {
      alert('Remove error: ' + err.message);
    } finally {
      setActionLoading(null);
    }
  };

  const handleSearchRegistry = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!registryQuery.trim()) return;
    setRegistryLoading(true);
    try {
      const r = await apiFetch(`/customize/connectors/registry?q=${encodeURIComponent(registryQuery.trim())}`);
      const json = await r.json();
      setRegistryServers(json.servers || []);
    } catch {
      setRegistryServers([]);
    } finally {
      setRegistryLoading(false);
    }
  };

  const handleReviewAgent = async (id: number | string, approve: boolean) => {
    try {
      const resp = await apiFetch(`/custom-agents/${encodeURIComponent(id)}/${approve ? 'approve' : 'reject'}`, {
        method: 'POST',
      });
      if (!resp.ok) {
        const err = await resp.json().catch(() => ({}));
        alert(err.detail || err.error || 'Failed to review agent');
      } else {
        await loadAgents();
      }
    } catch (err: any) {
      alert('Review error: ' + err.message);
    }
  };

  const handleBrowseFolder = async () => {
    try {
      if (typeof (window as any).pickFolder === 'function') {
        const p = await (window as any).pickFolder(forkDir);
        if (p) setForkDir(p);
      } else if ((window as any).electronAPI?.browseFolder) {
        const p = await (window as any).electronAPI.browseFolder(forkDir);
        if (p) setForkDir(p);
      } else {
        const p = prompt('Enter folder path for this agent to work in:', forkDir || 'D:/reports/weekly');
        if (p) setForkDir(p);
      }
    } catch {
      const p = prompt('Enter folder path for this agent to work in:', forkDir || 'D:/reports/weekly');
      if (p) setForkDir(p);
    }
  };

  const handleForkAgent = async () => {
    if (!forkingAgent || !forkDir.trim()) return;
    setActionLoading('fork');
    try {
      const resp = await apiFetch(`/custom-agents/${encodeURIComponent(forkingAgent.id)}/fork`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ work_dir: forkDir.trim() }),
      });
      if (!resp.ok) {
        const err = await resp.json().catch(() => ({}));
        alert(err.detail || err.error || 'Failed to fork agent');
      } else {
        const made = await resp.json();
        alert(`Added '${made.name}' to your Personal Agents`);
        setForkingAgent(null);
        setForkDir('');
        await loadAgents();
      }
    } catch (err: any) {
      alert('Error: ' + err.message);
    } finally {
      setActionLoading(null);
    }
  };

  if (!isOpen) return null;

  // Filter items for non-agents tabs
  const allItems = data?.items || [];
  const filtered = allItems.filter((it) => {
    if (category && it.category !== category) return false;
    if (view === 'yours' && !it.installed) return false;
    if (view === 'discover' && !it.in_catalog) return false;
    if (!search.trim()) return true;
    const q = search.trim().toLowerCase();
    return [it.id, it.title, it.description, it.author, it.category].some((v) =>
      String(v || '').toLowerCase().includes(q)
    );
  });

  const categoriesEntries = Object.entries(data?.categories || {}).sort((a, b) => b[1] - a[1]);

  const renderTile = (it: CatalogItem, large = false) => {
    if (kind === 'connectors') {
      const col = it.color || DEFAULT_BRAND_COLORS[it.id] || DEFAULT_BRAND_COLORS[it.title.toLowerCase()] || '#3b82f6';
      const ini =
        String(it.title || it.id)
          .replace(/[^A-Za-z0-9 ]/g, ' ')
          .trim()
          .split(/\s+/)
          .slice(0, 2)
          .map((w) => w[0])
          .join('')
          .toUpperCase() || 'C';

      return (
        <div
          className={`cz-icon cz-mono ${large ? 'lg' : ''}`}
          style={{ background: col, color: '#ffffff', fontWeight: 700, fontSize: large ? '20px' : '14px', borderRadius: '10px' }}
          aria-hidden="true"
        >
          {ini}
        </div>
      );
    }

    return (
      <div className={`cz-icon ${large ? 'lg' : ''}`}>
        {catIcon(it.category)}
      </div>
    );
  };

  const renderStatusChips = (it: CatalogItem) => {
    const chips: React.ReactNode[] = [];

    if (kind === 'connectors' && it.installed) {
      const st = it.status || 'configured';
      const cls = st === 'ready' ? 'ok' : st === 'error' ? 'bad' : 'mid';
      chips.push(
        <span key="st" className={`cz-chip ${cls}`}>
          {st}
        </span>
      );
    }

    if (kind === 'plugins' && it.installed) {
      if (it.error) chips.push(<span key="err" className="cz-chip bad">error</span>);
      else if (it.enabled === false) chips.push(<span key="dis" className="cz-chip mid">disabled</span>);
    }

    if (it.modified) {
      chips.push(
        <span key="mod" className="cz-chip mid">
          modified
        </span>
      );
    }

    if (it.installed && (!kind || kind !== 'connectors')) {
      chips.push(
        <span key="inst" className="cz-chip ok">
          installed
        </span>
      );
    }

    if (kind === 'connectors' && it.in_catalog) {
      if (it.sensitivity) {
        chips.push(
          <span key="sens" className="cz-chip bad" title={SENS_HINT[it.sensitivity] || ''}>
            {SENS[it.sensitivity] || it.sensitivity}
          </span>
        );
      }
      if (it.installed && it.installed_scope) {
        chips.push(
          <span key="scope" className="cz-chip mid" title={it.installed_scope === 'user' ? 'Only you can use its tools' : 'Shared'}>
            {it.installed_scope === 'user' ? 'Just you' : 'Shared'}
          </span>
        );
      }
      if (it.installed && it.oauth && !it.signed_in) {
        chips.push(
          <span key="signin" className="cz-chip mid">
            Sign-in needed
          </span>
        );
      }
      if (!it.installed && !it.allowed) {
        chips.push(
          <span key="adm" className="cz-chip mid" title="An administrator has to allow this connector first">
            Needs admin
          </span>
        );
      }
    }

    return chips;
  };

  // Agents filtering
  const agentMatches = (a: any) => {
    if (!search.trim()) return true;
    const q = search.trim().toLowerCase();
    return [a.name, a.slug, a.description].some((v) => String(v || '').toLowerCase().includes(q));
  };

  const pendingFiltered = agentsData.pending.filter(agentMatches);
  const sharedFiltered = agentsData.shared.filter(agentMatches);
  const templatesFiltered = agentsData.templates.filter(agentMatches);

  return (
    <div
      id="cz-modal"
      className="fixed inset-0 z-50 flex items-center justify-center bg-black/70 backdrop-blur-sm"
      onClick={() => setActiveModal('none')}
    >
      <div
        id="cz-box"
        onClick={(e) => e.stopPropagation()}
      >
        {/* Header */}
        <div className="cz-head">
          <h2 id="cz-title">Customize</h2>
          <button
            className="rep-close"
            id="cz-close"
            onClick={() => setActiveModal('none')}
            title="Close (Esc)"
          >
            ✕
          </button>
        </div>

        {/* Toolbar Bar */}
        <div className="cz-bar">
          <div className="cz-tabs" role="tablist">
            {(['skills', 'connectors', 'plugins', 'agents'] as const).map((k) => (
              <button
                key={k}
                className={`cz-tab ${kind === k ? 'on' : ''}`}
                onClick={() => {
                  setKind(k);
                  setCategory(null);
                  setSelectedItem(null);
                  setForkingAgent(null);
                }}
              >
                {k.charAt(0).toUpperCase() + k.slice(1)}
              </button>
            ))}
          </div>

          <span className="cz-sep" />

          {kind !== 'agents' && (
            <div className="cz-seg">
              <button
                className={`cz-view ${view === 'yours' ? 'on' : ''}`}
                onClick={() => {
                  setView('yours');
                  setSelectedItem(null);
                }}
              >
                Yours
              </button>
              <button
                className={`cz-view ${view === 'discover' ? 'on' : ''}`}
                onClick={() => {
                  setView('discover');
                  setSelectedItem(null);
                }}
              >
                Discover
              </button>
            </div>
          )}

          <input
            type="search"
            id="cz-q"
            className="cz-search"
            placeholder={kind === 'agents' ? 'Search agents...' : `Search ${KIND_LABEL[kind]}`}
            value={search}
            onChange={(e) => setSearch(e.target.value)}
          />
        </div>

        {/* Modal Body */}
        <div id="cz-body">
          {kind === 'agents' ? (
            /* AGENTS TAB BODY (Matching media_1792600731820_aa54a93a.png) */
            loading ? (
              <div className="cz-empty">Loading agents…</div>
            ) : forkingAgent ? (
              /* Fork Form View */
              <div className="cz-detail">
                <button className="cz-link cz-back" onClick={() => setForkingAgent(null)}>
                  ← Back
                </button>
                <div className="cz-dhead">
                  <div className="cz-icon lg" style={{ fontSize: '32px' }}>{forkingAgent.icon || '🤖'}</div>
                  <div>
                    <div className="cz-dtitle font-bold text-base">{forkingAgent.name}</div>
                    <div className="cz-desc full text-xs text-text-muted">{forkingAgent.description}</div>
                  </div>
                </div>
                <div className="cz-sec mt-4">
                  <span>Where should it work?</span>
                </div>
                <p className="cz-note text-xs text-text-muted my-2">
                  A copy of this agent is added to your Personal Agents. Pick the folder on your computer it works in. It can read, write and analyse files there, and nothing outside it. You can change the folder later.
                </p>
                <div className="cz-fork-row flex items-center gap-2 my-3">
                  <input
                    type="text"
                    id="cz-fork-dir"
                    className="cz-search flex-1"
                    placeholder="D:/reports/weekly"
                    value={forkDir}
                    onChange={(e) => setForkDir(e.target.value)}
                    autoComplete="off"
                    spellCheck="false"
                  />
                  <button
                    type="button"
                    className="ru-btn browse"
                    id="cz-fork-browse"
                    onClick={handleBrowseFolder}
                  >
                    📁 Browse…
                  </button>
                </div>
                <div className="cz-actions mt-4">
                  <button
                    className="btn accent cz-act"
                    id="cz-fork-go"
                    disabled={!forkDir.trim() || actionLoading === 'fork'}
                    onClick={handleForkAgent}
                  >
                    {actionLoading === 'fork' ? 'Forking…' : 'Add to my agents'}
                  </button>
                </div>
              </div>
            ) : (
              /* Agents List with 3 Sections */
              <div className="flex flex-col gap-4">
                <div className="cz-note">
                  Fork an agent to use it. You choose the folder it works in. Create your own from the Personal Agents card in Chat.
                </div>

                {/* Section 1: Waiting for approval */}
                {canReviewAgents() && (
                  <div>
                    <div className="cz-sec">
                      <span>Waiting for approval</span>
                      <span className="cz-count">{pendingFiltered.length}</span>
                    </div>
                    {pendingFiltered.length === 0 ? (
                      <div className="cz-empty">Nothing is waiting for approval.</div>
                    ) : (
                      <div className="cz-grid">
                        {pendingFiltered.map((a: any) => {
                          const tools =
                            a.tool_allowlist && a.tool_allowlist.length
                              ? `${a.tool_allowlist.length} TOOLS`
                              : 'ALL TOOLS';
                          return (
                            <div key={a.id} className="cz-card static cz-agent">
                              <div className="cz-icon">{a.icon || '🤖'}</div>
                              <div className="cz-main">
                                <div className="cz-name">
                                  {a.name} <span className="cz-chip mid">{tools}</span>
                                </div>
                                <div className="cz-desc">{a.description}</div>
                                <div className="cz-by">
                                  shared by {a.owner_name || 'a user'} · <code>/{a.slug}</code>
                                </div>
                              </div>
                              <div className="cz-agent-btns">
                                <button
                                  className="ru-btn primary"
                                  onClick={() => handleReviewAgent(a.id, true)}
                                >
                                  Approve
                                </button>
                                <button
                                  className="ru-btn"
                                  onClick={() => handleReviewAgent(a.id, false)}
                                >
                                  Reject
                                </button>
                              </div>
                            </div>
                          );
                        })}
                      </div>
                    )}
                  </div>
                )}

                {/* Section 2: Shared by users */}
                <div>
                  <div className="cz-sec">
                    <span>Shared by users</span>
                    <span className="cz-count">{sharedFiltered.length}</span>
                  </div>
                  {sharedFiltered.length === 0 ? (
                    <div className="cz-empty">No one has shared an agent yet.</div>
                  ) : (
                    <div className="cz-grid">
                      {sharedFiltered.map((a: any) => {
                        const tools =
                          a.tool_allowlist && a.tool_allowlist.length
                            ? `${a.tool_allowlist.length} TOOLS`
                            : 'ALL TOOLS';
                        return (
                          <div key={a.id} className="cz-card static cz-agent">
                            <div className="cz-icon">{a.icon || '🤖'}</div>
                            <div className="cz-main">
                              <div className="cz-name">
                                {a.name} <span className="cz-chip mid">{tools}</span>
                              </div>
                              <div className="cz-desc">{a.description}</div>
                              <div className="cz-by">
                                shared by another user · <code>/{a.slug}</code>
                              </div>
                            </div>
                            <div className="cz-agent-btns">
                              <button
                                className="ru-btn primary"
                                onClick={() => {
                                  openAgentModal('fork', a);
                                }}
                              >
                                Fork
                              </button>
                            </div>
                          </div>
                        );
                      })}
                    </div>
                  )}
                </div>

                {/* Section 3: Starter templates */}
                <div>
                  <div className="cz-sec">
                    <span>Starter templates</span>
                    <span className="cz-count">{templatesFiltered.length}</span>
                  </div>
                  {templatesFiltered.length === 0 ? (
                    <div className="cz-empty">No templates match your search.</div>
                  ) : (
                    <div className="cz-grid">
                      {templatesFiltered.map((a: any) => {
                        const tools =
                          a.tool_allowlist && a.tool_allowlist.length
                            ? `${a.tool_allowlist.length} TOOLS`
                            : 'ALL TOOLS';
                        return (
                          <div key={a.id} className="cz-card static cz-agent">
                            <div className="cz-icon">{a.icon || '🤖'}</div>
                            <div className="cz-main">
                              <div className="cz-name">
                                {a.name} <span className="cz-chip mid">{tools}</span>
                              </div>
                              <div className="cz-desc">{a.description}</div>
                              <div className="cz-by">
                                starter template · <code>/{a.slug}</code>
                              </div>
                            </div>
                            <div className="cz-agent-btns">
                              <button
                                className="ru-btn primary"
                                onClick={() => {
                                  openAgentModal('fork', a);
                                }}
                              >
                                Fork
                              </button>
                            </div>
                          </div>
                        );
                      })}
                    </div>
                  )}
                </div>
              </div>
            )
          ) : selectedItem ? (
            /* Detail View (Skills / Connectors / Plugins) */
            <div className="cz-detail">
              <button className="cz-link cz-back" id="cz-back" onClick={() => setSelectedItem(null)}>
                ← Back
              </button>
              <div className="cz-dhead">
                {renderTile(selectedItem, true)}
                <div>
                  <div className="cz-dtitle">
                    {selectedItem.title}
                    {selectedItem.in_catalog && <span className="cz-verified" title="Reviewed in-repo entry">✓</span>}
                    {renderStatusChips(selectedItem)}
                  </div>
                  <div className="cz-desc full">{selectedItem.description}</div>
                </div>
              </div>

              {selectedItem.egress && (
                <div className="cz-banner warn">
                  ⚠ Tool calls go to a third-party service outside this server. Card numbers (PAN) are masked in tool arguments.
                </div>
              )}

              {selectedItem.sensitivity && (
                <div className="cz-banner warn">
                  <b>{SENS[selectedItem.sensitivity] || ''}.</b> {SENS_HINT[selectedItem.sensitivity] || ''}
                </div>
              )}

              {selectedItem.note && <div className="cz-banner">{selectedItem.note}</div>}

              <table className="cz-meta">
                <tbody>
                  {selectedItem.author && (
                    <tr>
                      <th>Author</th>
                      <td>{selectedItem.author}</td>
                    </tr>
                  )}
                  {selectedItem.version && (
                    <tr>
                      <th>Version</th>
                      <td>{selectedItem.version}</td>
                    </tr>
                  )}
                  <tr>
                    <th>Category</th>
                    <td>{catLabel(selectedItem.category)}</td>
                  </tr>
                  <tr>
                    <th>Source</th>
                    <td>{selectedItem.in_catalog ? 'Reviewed catalog' : 'Local (hand-written)'}</td>
                  </tr>
                  {selectedItem.triggers && selectedItem.triggers.length > 0 && (
                    <tr>
                      <th>Triggers</th>
                      <td>
                        {selectedItem.triggers.map((t) => (
                          <code key={t} style={{ marginRight: '4px' }}>
                            {t}
                          </code>
                        ))}
                      </td>
                    </tr>
                  )}
                  {selectedItem.tools && selectedItem.tools.length > 0 && (
                    <tr>
                      <th>Tools</th>
                      <td>
                        {selectedItem.tools.map((t) => (
                          <code key={t} style={{ marginRight: '4px' }}>
                            {t}
                          </code>
                        ))}
                      </td>
                    </tr>
                  )}
                  {selectedItem.command && (
                    <tr>
                      <th>Runs</th>
                      <td>
                        <code>{selectedItem.command}</code>
                      </td>
                    </tr>
                  )}
                  {selectedItem.homepage && (
                    <tr>
                      <th>Docs</th>
                      <td>
                        <a href={selectedItem.homepage} target="_blank" rel="noopener noreferrer">
                          {selectedItem.homepage} ↗
                        </a>
                      </td>
                    </tr>
                  )}
                </tbody>
              </table>

              {/* Preview Content */}
              {previewLoading ? (
                <div className="cz-empty">Loading preview…</div>
              ) : (
                previewContent && (
                  <div className="cz-preview-container" style={{ marginTop: '16px' }}>
                    <div className="cz-sec">
                      <span>Inspection & Code</span>
                    </div>
                    <pre
                      style={{
                        background: 'var(--panel2)',
                        padding: '12px',
                        borderRadius: '8px',
                        fontSize: '11.5px',
                        overflowX: 'auto',
                        border: '1px solid var(--border)',
                      }}
                    >
                      <code>{previewContent.text}</code>
                    </pre>
                  </div>
                )
              )}

              {kind === 'connectors' && (
                <ConnectorOAuthPanel item={selectedItem} isAdmin={canInstall()} oauth={oauth} onChanged={() => refreshCatalog('connectors')} />
              )}

              {kind === 'connectors' && !selectedItem.installed && needsForm(selectedItem) && (
                <div className="cz-client" style={{ marginTop: '16px' }}>
                  <div className="cz-sec"><span>Setup</span></div>
                  {(selectedItem.fields || []).map((f) => (
                    <label key={f.key} className="cz-secret">
                      {f.label || f.key}
                      <input
                        type="text"
                        className="cz-search"
                        value={formFields[f.key] || ''}
                        onChange={(e) => setFormFields((p) => ({ ...p, [f.key]: e.target.value }))}
                        placeholder={f.placeholder || ''}
                        autoComplete="off"
                        spellCheck={false}
                      />
                    </label>
                  ))}
                  {(selectedItem.secret_env || []).map((s) => (
                    <label key={s.key} className="cz-secret">
                      {s.label || s.key} <code>{s.key}</code>
                      <input
                        type="password"
                        className="cz-search"
                        value={formSecrets[s.key] || ''}
                        onChange={(e) => setFormSecrets((p) => ({ ...p, [s.key]: e.target.value }))}
                        placeholder="stored in the OS keychain"
                        autoComplete="new-password"
                      />
                    </label>
                  ))}
                </div>
              )}

              {installMsg && installMsg.id === selectedItem.id && (
                <div className={installMsg.error ? 'cz-dim' : 'cz-note'} style={installMsg.error ? { color: 'var(--red, #e5484d)' } : undefined}>
                  {installMsg.text}
                </div>
              )}
              {/* Install / Remove Action Bar */}
              <div className="cz-actions" style={{ marginTop: '20px' }}>
                {selectedItem.installed ? (
                  <button
                    className="btn ghost cz-act"
                    style={{ color: 'var(--red)' }}
                    disabled={actionLoading === selectedItem.id}
                    onClick={(e) => handleRemove(e, selectedItem)}
                  >
                    Uninstall
                  </button>
                ) : (
                  <button
                    className="btn accent cz-act"
                    disabled={actionLoading === selectedItem.id || (!canInstall() && !selectedItem.allowed && kind !== 'connectors')}
                    onClick={(e) => handleInstall(e, selectedItem)}
                  >
                    Install {selectedItem.title}
                  </button>
                )}
              </div>
            </div>
          ) : (
            /* Catalog Grid View (Skills, Connectors, Plugins) */
            <>
              {/* Category Filter Pills (if active) */}
              {category && (
                <div style={{ display: 'flex', alignItems: 'center', gap: '8px', marginBottom: '12px' }}>
                  <button
                    className="cz-chip mid"
                    style={{ cursor: 'pointer', padding: '4px 10px', fontSize: '11px' }}
                    onClick={() => setCategory(null)}
                  >
                    Category: <b>{catLabel(category)}</b> ✕
                  </button>
                </div>
              )}

              {installMsg && (
                <div className={installMsg.error ? 'cz-dim' : 'cz-note'} style={{ marginBottom: '10px', ...(installMsg.error ? { color: 'var(--red, #e5484d)' } : {}) }}>
                  {installMsg.text}
                  {oauth.notes[installMsg.id] ? ' ' + oauth.notes[installMsg.id] : ''}
                  {oauth.errors[installMsg.id] ? ' ' + oauth.errors[installMsg.id] : ''}
                  {oauth.links[installMsg.id] && (
                    <>
                      {' '}
                      <a href={oauth.links[installMsg.id]} target="_blank" rel="noopener noreferrer">Open sign-in page ↗</a>
                    </>
                  )}
                </div>
              )}

              {/* Items Cards Grid */}
              {loading ? (
                <div className="cz-empty">Loading {kind}…</div>
              ) : filtered.length === 0 ? (
                <div className="cz-empty">
                  {view === 'yours'
                    ? search
                      ? 'Nothing installed matches your search.'
                      : 'Nothing installed yet. Switch to Discover to browse the catalog.'
                    : 'No catalog entries match.'}
                </div>
              ) : (
                <div className="cz-grid">
                  {filtered.map((it) => (
                    <div
                      key={it.id}
                      className="cz-card"
                      onClick={() => setSelectedItem(it)}
                      tabIndex={0}
                    >
                      {renderTile(it)}
                      <div className="cz-main">
                        <div className="cz-name">
                          {it.title}
                          {it.in_catalog && (
                            <span className="cz-verified" title="Reviewed in-repo catalog entry">
                              ✓
                            </span>
                          )}
                          {renderStatusChips(it)}
                        </div>
                        <div className="cz-desc">{it.description}</div>
                        <div className="cz-by">
                          {it.author ? `by ${it.author}` : ''}
                          {it.egress && (
                            <span className="cz-egress" title="Tool calls send data to a third party">
                              {' '}· external
                            </span>
                          )}
                        </div>
                      </div>

                      <span className="cz-acts" style={{ display: 'flex', alignItems: 'center', gap: '6px' }}>
                        {kind === 'connectors' && it.installed && it.oauth && !it.signed_in && (
                          <button
                            className="ru-btn primary"
                            disabled={!!oauth.connecting[it.id]}
                            style={{
                              padding: '4px 10px',
                              fontSize: '11px',
                              borderRadius: '6px',
                              background: 'var(--accent)',
                              color: '#ffffff',
                              border: 'none',
                              cursor: 'pointer',
                            }}
                            title={`Sign in to ${it.title}`}
                            onClick={(e) => {
                              e.stopPropagation();
                              oauth.connect(it.id, it.installed_scope || 'user');
                            }}
                          >
                            {oauth.connecting[it.id] ? 'Connecting…' : 'Connect account'}
                          </button>
                        )}

                        {it.installed ? (
                          <span
                            className="cz-plus done"
                            title="Installed"
                            onClick={(e) => handleRemove(e, it)}
                          >
                            ✓
                          </span>
                        ) : (
                          <button
                            className="cz-plus"
                            title={canInstall() ? 'Install' : 'Needs admin'}
                            disabled={actionLoading === it.id || (!canInstall() && !it.allowed && kind !== 'connectors')}
                            onClick={(e) => handleInstall(e, it)}
                          >
                            +
                          </button>
                        )}
                      </span>
                    </div>
                  ))}
                </div>
              )}

              {/* Categories Section (only on Discover view without category filter) */}
              {view === 'discover' && !category && categoriesEntries.length > 1 && (
                <>
                  <div className="cz-sec">
                    <span>Categories</span>
                  </div>
                  <div className="cz-cats">
                    {categoriesEntries.map(([c, count]) => (
                      <button
                        key={c}
                        type="button"
                        className="cz-cat"
                        onClick={() => setCategory(c)}
                      >
                        <span className="cz-cat-ic">{catIcon(c)}</span>
                        <span>{catLabel(c)}</span>
                        <span className="cz-count">{count}</span>
                      </button>
                    ))}
                  </div>
                </>
              )}

              {/* Public MCP Registry (for Connectors tab) */}
              {kind === 'connectors' && view === 'discover' && (
                <div style={{ marginTop: '24px' }}>
                  <div className="cz-sec">
                    <span>Public MCP Registry</span>
                    <span className="cz-dim">browse only · not installable</span>
                  </div>
                  <form className="cz-reg-form" onSubmit={handleSearchRegistry}>
                    <input
                      type="search"
                      id="cz-reg-q"
                      placeholder="Search registry.modelcontextprotocol.io"
                      value={registryQuery}
                      onChange={(e) => setRegistryQuery(e.target.value)}
                      maxLength={100}
                    />
                    <button className="btn ghost" type="submit" disabled={registryLoading}>
                      {registryLoading ? 'Searching…' : 'Search'}
                    </button>
                  </form>

                  {registryServers.length > 0 && (
                    <div className="cz-grid" style={{ marginTop: '10px' }}>
                      {registryServers.map((s, idx) => (
                        <div key={idx} className="cz-card static">
                          <div className="cz-icon">🌐</div>
                          <div className="cz-main">
                            <div className="cz-name">
                              {s.title} <span className="cz-dim">{s.version}</span>
                            </div>
                            <div className="cz-desc">{s.description}</div>
                            <div className="cz-by">
                              {s.name}
                              {s.url && (
                                <>
                                  {' · '}
                                  <a href={s.url} target="_blank" rel="noopener noreferrer">
                                    source ↗
                                  </a>
                                </>
                              )}
                            </div>
                          </div>
                        </div>
                      ))}
                    </div>
                  )}
                </div>
              )}

              {/* Plugin marketplace + remote install (Plugins tab) */}
              {kind === 'plugins' && view === 'discover' && (
                <PluginMarketplace canInstall={canInstall()} onInstalled={refreshCatalog} />
              )}

              {/* Footer Note */}
              <div className="cz-note">
                Only reviewed entries from the in-repo catalog can be installed. New {KIND_LABEL[kind]} are added to the catalog through code review.
              </div>
            </>
          )}
        </div>
      </div>
    </div>
  );
};