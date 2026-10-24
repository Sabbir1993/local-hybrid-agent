import { samplingBody, syncSamplingFromServer } from '../services/sampling';
import { notifyTaskFinished, requestNotificationPermission } from '../services/notify';
import { clipHistoryContent } from '../services/attachments';
import { holdSession, startSessionWatch, stopSessionWatch } from '../services/session';
import { GAP_NOTE, answerCheckEnd, applyAgentEvent, interruptRun, newAgentRunState } from './agentEvents';
import { create } from 'zustand';
import { api, type ServerStatusPayload, type AvailableModelItem, type UserProfile, apiFetch } from '../services/api';
import { logger } from '../utils/logger';

export type AppMode = 'chat' | 'agent' | 'code';
export type AgentMode = 'auto' | 'manual' | 'accept_edits' | 'plan' | 'bypass';
export type ReasoningEffort = 'none' | 'low' | 'medium' | 'high' | 'extra';
export type DockTab = 'plan' | 'tasks' | 'files' | 'preview' | 'terminal';
export type ActiveModal = 'none' | 'audit' | 'settings' | 'report' | 'db' | 'monitor' | 'customize' | 'docs' | 'preview';

export const isCompanionClient = (): boolean => {
  if (typeof window === 'undefined') return false;
  return Boolean(
    (window as any).electronAPI?.isNativeApp ||
    (window as any).isCompanionApp ||
    navigator.userAgent.includes('Electron') ||
    navigator.userAgent.includes('A770NativeApp') ||
    window.location.search.includes('companion=true')
  );
};

export interface ToastState {
  id: number;
  message: string;
  type: 'info' | 'error' | 'success' | 'warn';
}

export interface DialogState {
  title: string;
  message: string;
  confirmLabel?: string;
  cancelLabel?: string;
  onConfirm?: () => void;
  onCancel?: () => void;
}

export interface PreviewModalState {
  isOpen: boolean;
  filePath: string;
  title: string;
  directContent?: string | null;
  source?: 'common' | 'ws';
}

export interface ChatMessage {
  id: string;
  role: 'user' | 'assistant' | 'system';
  content: string;
  timestamp: string;
  createdAt?: number;
  runId?: string;
  check?: any;
  model?: string;
  modelProvider?: string;
  thinking?: string;
  statusText?: string;
  acts?: any[];
  secs?: number;
  ntok?: number;
  promptTokens?: number;   // the server's real prompt size at the end of this reply: anchors the context ring
  tps?: number;
  errorAlert?: string;
  canContinue?: boolean;   // the run stopped because it used its steps: the answer offers a Continue button
  images?: string[];
  files?: string;
  meta?: any;
}

export interface TermEntry {
  id: string;
  cmd: string;
  source: 'user' | 'agent' | 'system' | 'companion';
  status: 'running' | 'done';
  stdout?: string;
  stderr?: string;
  exitCode?: number;
  time: number;
}

export interface ProjectItem {
  id: string;
  name: string;
  path: string;
  workspace_dir?: string;
  device_name?: string;
  active?: boolean;
}

export interface SessionItem {
  id: string | number;
  title: string;
  updatedAt?: string;
  active?: boolean;
  model?: string | null;   // the model this chat was last used with
}

interface AppState {
  // Auth State
  isAuthenticated: boolean | null;
  currentUser: UserProfile | null;

  // Connection & Server Status
  isConnected: boolean;
  serverStatus: ServerStatusPayload | null;
  isModelLoading: boolean;

  // Theme & Layout
  theme: string;
  sidebarCollapsed: boolean;
  dockCollapsed: boolean;
  dockExpanded: boolean;
  dockTab: DockTab;

  // Active Modes
  appMode: AppMode;
  agentMode: AgentMode;
  reasoningEffort: ReasoningEffort;
  deepResearch: boolean;
  webSearchEnabled: boolean;
  kbMode: 'auto' | 'always' | 'off';

  // Selection & Data
  selectedModel: string;
  availableModels: AvailableModelItem[];
  projects: ProjectItem[];
  activeProjectId: string | null;
  activeCustomAgent: any | null;
  sessions: SessionItem[];
  hasMoreSessions: boolean;
  isLoadingMoreSessions: boolean;
  activeSessionId: string | number | null;
  messages: ChatMessage[];
  sessionFilter: string;

  // Streaming State
  isStreaming: boolean;
  activeStreamingMessageId: string | null;

  // Toast & Themed Dialog
  toast: ToastState | null;
  dialog: DialogState | null;
  showToast: (message: string, type?: 'info' | 'error' | 'success' | 'warn', duration?: number) => void;
  hideToast: () => void;
  sessionLoading: boolean;
  bgMessages: Record<string, ChatMessage[]>;
  runningSessions: Record<string, boolean>;
  answerCheck: 'off' | 'badge' | 'gate';
  setAnswerCheck: (m: 'off' | 'badge' | 'gate') => void;
  loadAnswerCheckDefault: () => Promise<void>;
  patchMessage: (id: string, patch: Partial<ChatMessage>) => void;
  regenerateLast: () => Promise<void>;
  reattachDetachedRuns: (sessionId: string | number) => Promise<void>;
  projectModal: { mode: 'create' | 'folder'; project?: ProjectItem } | null;
  openProjectModal: (mode: 'create' | 'folder', project?: ProjectItem) => void;
  closeProjectModal: () => void;
  termEntries: TermEntry[];
  addTermEntry: (e: TermEntry) => void;
  updateTermEntry: (id: string, patch: Partial<TermEntry>) => void;
  clearTerm: () => void;
  compactConversation: (instructions?: string) => Promise<void>;
  idleWarning: boolean;
  sessionExpired: () => void;
  startSession: () => Promise<void>;
  agentModal: { mode: 'create' | 'edit' | 'fork'; agent: any | null } | null;
  agentsVersion: number;
  openAgentModal: (mode: 'create' | 'edit' | 'fork', agent?: any | null) => void;
  closeAgentModal: () => void;
  bumpAgents: () => void;
  permRequest: { reqId: string; cmd: string; kind: string; saveable: boolean; tainted: boolean; sid?: string } | null;
  answerPermission: (decision: 'allow' | 'project' | 'user' | 'always' | 'deny') => Promise<void>;
  showDialog: (dialog: DialogState) => void;
  closeDialog: () => void;

  // Modals & Panels
  activeModal: ActiveModal;
  settingsTab: string;
  customizeTab: 'skills' | 'connectors' | 'plugins' | 'agents';
  previewModal: PreviewModalState;
  openPreviewModal: (filePath: string, title?: string, directContent?: string | null, source?: 'common' | 'ws' | 'modal', forceModal?: boolean) => void;
  closePreviewModal: () => void;

  // Actions
  checkAuth: () => Promise<void>;
  setAuthenticatedUser: (user: UserProfile | null) => void;
  logout: () => Promise<void>;

  setIsConnected: (connected: boolean) => void;
  setServerStatus: (status: ServerStatusPayload) => void;
  setTheme: (theme: string) => void;
  toggleSidebar: () => void;
  toggleDock: (tab?: DockTab) => void;
  setDockCollapsed: (collapsed: boolean) => void;
  setDockExpanded: (expanded: boolean) => void;
  setDockTab: (tab: DockTab) => void;
  setAppMode: (mode: AppMode) => void;
  setAgentMode: (mode: AgentMode) => void;
  setReasoningEffort: (effort: ReasoningEffort) => void;
  toggleDeepResearch: () => void;
  toggleWebSearch: () => void;
  setKbMode: (mode: 'auto' | 'always' | 'off') => void;
  setSelectedModel: (model: string) => Promise<void> | void;
  setActiveProjectId: (id: string | null) => void;
  setActiveCustomAgent: (agent: any | null) => void;
  setSessionFilter: (filter: string) => void;

  // Session Actions
  fetchSessions: () => Promise<void>;
  fetchMoreSessions: () => Promise<void>;
  openSession: (id: string | number) => Promise<void>;
  createSession: (title?: string) => Promise<void>;
  deleteSession: (id: string | number) => Promise<void>;

  // Real Backend API Actions
  fetchStatus: () => Promise<void>;
  fetchModels: () => Promise<void>;
  fetchProjects: () => Promise<void>;
  loadSelectedModel: () => Promise<void>;
  sendPrompt: (text: string, options?: { images?: string[]; files?: string; displayContent?: string; voice?: boolean; truncateTo?: number; reattachRunId?: string; onAccepted?: () => void }) => Promise<void>;
  abortGeneration: () => void;
  setActiveModal: (modal: ActiveModal) => void;
  openSettingsModal: (tab?: string) => void;
  openCustomizeModal: (tab?: 'skills' | 'connectors' | 'plugins' | 'agents') => void;
  hasPerm: (permission: string) => boolean;
}

// One entry per chat whose answer is still streaming; a chat keeps running when you switch to another one.
const runs = new Map<string, { ctrl: AbortController; agentRunId: string | null }>();
// runs whose result this browser already saved: a lost acknowledgement must never make a reload show a result twice
const SAVED_RUNS_KEY = 'a770_saved_runs';
const runWasSaved = (id: string): boolean => {
  try { return (JSON.parse(localStorage.getItem(SAVED_RUNS_KEY) || '[]') as string[]).includes(id); } catch (_) { return false; }
};
const markRunSaved = (id: string) => {
  try {
    const l = (JSON.parse(localStorage.getItem(SAVED_RUNS_KEY) || '[]') as string[]).filter((x) => x !== id);
    l.push(id);
    localStorage.setItem(SAVED_RUNS_KEY, JSON.stringify(l.slice(-50)));
  } catch (_) {
    // storage unavailable: the server's own acknowledgement still applies
  }
};

// the media module imports this store: load it once, after this module has finished evaluating
let mediaModule: Promise<typeof import('../services/media')> | null = null;
const loadMedia = () => (mediaModule = mediaModule || import('../services/media'));
setTimeout(() => { loadMedia().catch(() => { mediaModule = null; }); }, 0);

// a prompt is being set up (model switch, session create): a second Enter must not start a second run
let sendGate = false;
// chats whose history is being compacted right now
const compacting = new Set<string>();

/** Stop a run (and tell the server to cancel an agent run); the chat's own bookkeeping is cleared by the caller. */
function stopRun(key: string) {
  const run = runs.get(key);
  if (!run) return;
  if (run.agentRunId) {
    apiFetch(`/agent/run/${encodeURIComponent(run.agentRunId)}/cancel`, { method: 'POST', keepalive: true }).catch(() => {});
    run.agentRunId = null;
  }
  run.ctrl.abort();
  runs.delete(key);
}
export const sessionKey = (id: string | number | null | undefined) => String(id ?? '0');
/** Chats with work in flight that is not a chat stream (image / video jobs): their messages are parked on a switch too. */
export const externalBusy = new Set<string>();
const isBusy = (key: string) => runs.has(key) || externalBusy.has(key);
let oldestSessionIdTracker: string | number | null = null;

const STARTER_AGENT_SUBSTRINGS = [
  'System & Metric Report',
  'Email Analyzer',
  'Code QA & Test',
  'Architecture & System Design',
  'Security & Vulnerability Audit',
  'Doc & Knowledge Synth',
];

/** What the model is sent: from the latest compact marker forward (plus the verbatim tail it kept), else everything. */
export function contextMessages(msgs: ChatMessage[]): ChatMessage[] {
  let idx = -1;
  for (let i = msgs.length - 1; i >= 0; i--) {
    if (msgs[i].meta?.compact) {
      idx = i;
      break;
    }
  }
  if (idx === -1) return msgs.slice();
  const kept = msgs[idx].meta?.kept_messages || 0;
  return [msgs[idx], ...msgs.slice(Math.max(0, idx - kept), idx), ...msgs.slice(idx + 1)];
}

export function getActiveSessionKey(mode: AppMode = 'chat'): string {
  if (mode === 'agent') return 'active_agent_session_id';
  if (mode === 'code') return 'active_code_session_id';
  return 'active_chat_session_id';
}

function isCustomAgentSession(s: any): boolean {
  if (!s) return false;
  if (s.agent_id != null && s.agent_id !== 0 && s.agent_id !== '') return true;
  try {
    const map = JSON.parse(localStorage.getItem('custom_agent_sessions') || '{}') || {};
    if (map[String(s.id)]) return true;
  } catch (_) {}
  const title = (s.title || s.name || '').trim();
  if (!title) return false;
  for (const starter of STARTER_AGENT_SUBSTRINGS) {
    if (title.includes(starter)) return true;
  }
  return false;
}

function filterRawSessions(rawSessions: any[], appMode: AppMode, activeCustomAgent: any): any[] {
  if (!Array.isArray(rawSessions)) return [];
  if (appMode === 'chat') {
    return rawSessions.filter((s: any) => !isCustomAgentSession(s));
  } else if (appMode === 'agent') {
    if (activeCustomAgent) {
      const targetId = String(activeCustomAgent.id);
      const targetName = (activeCustomAgent.name || '').trim();
      const targetIcon = (activeCustomAgent.icon || '').trim();
      return rawSessions.filter((s: any) => {
        if (!isCustomAgentSession(s)) return false;
        if (String(s.agent_id) === targetId) return true;
        const title = (s.title || '').trim();
        if (targetName && (title.includes(targetName) || (targetIcon && title.startsWith(targetIcon + ' ' + targetName)))) return true;
        return false;
      });
    } else {
      return rawSessions.filter((s: any) => isCustomAgentSession(s));
    }
  }
  return rawSessions;
}

// the dock's Preview belongs to the chat it was opened from: a new or different chat starts with it empty
const EMPTY_PREVIEW: PreviewModalState = { isOpen: false, filePath: '', title: '', directContent: null, source: 'common' };

function formatSessionItem(s: any, appMode: AppMode, activeSessionId: string | number | null): SessionItem {
  const ts = s.created_at || s.updated_at;
  let dateStr = 'Recent';
  if (ts) {
    const ms = typeof ts === 'number' ? (ts < 1e11 ? ts * 1000 : ts) : Date.parse(ts);
    if (!isNaN(ms)) {
      dateStr = new Date(ms).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
    }
  }
  return {
    id: s.id,
    title: s.title || s.name || (appMode === 'code' ? 'Task' : 'Chat'),
    updatedAt: dateStr,
    active: activeSessionId === s.id,
    model: s.model || null,
  };
}

const getInitialAppMode = (): AppMode => {
  if (typeof window === 'undefined') return 'chat';
  const isNative = isCompanionClient();
  const saved = localStorage.getItem('app_mode') as AppMode;
  if (isNative) {
    if (saved === 'agent' || saved === 'code') return saved;
    return 'code';
  }
  return saved === 'code' || saved === 'agent' ? saved : 'chat';
};

export const useAppStore = create<AppState>((set, get) => ({
  isAuthenticated: null,
  currentUser: null,

  isConnected: false,
  serverStatus: null,
  isModelLoading: false,

  theme: 'slate',
  sidebarCollapsed: false,
  dockCollapsed: true,
  dockExpanded: false,
  dockTab: 'plan',

  appMode: getInitialAppMode(),
  agentMode: 'auto',
  reasoningEffort: 'medium',
  deepResearch: false,
  webSearchEnabled: true,
  kbMode: 'auto',

  selectedModel: '',
  availableModels: [],
  projects: [],
  activeProjectId: null,
  activeCustomAgent: null,
  sessions: [],
  hasMoreSessions: false,
  isLoadingMoreSessions: false,
  activeSessionId: null,
  messages: [],
  sessionFilter: '',

  isStreaming: false,
  activeStreamingMessageId: null,
  toast: null,
  dialog: null,
  showToast: (message: string, type: 'info' | 'error' | 'success' | 'warn' = 'info', duration: number = 4000) => {
    const id = Date.now();
    set({ toast: { id, message, type } });
    setTimeout(() => {
      if (get().toast?.id === id) {
        set({ toast: null });
      }
    }, duration);
  },
  hideToast: () => set({ toast: null }),
  sessionLoading: false,
  bgMessages: {},
  runningSessions: {},
  answerCheck: ((): 'off' | 'badge' | 'gate' => {
    try {
      const v = localStorage.getItem('answer_check');
      if (v === 'off' || v === 'badge' || v === 'gate') return v;
    } catch (_) {
      // storage unavailable
    }
    return 'off';
  })(),
  setAnswerCheck: (answerCheck) => {
    try { localStorage.setItem('answer_check', answerCheck); } catch (_) { /* storage unavailable */ }
    set({ answerCheck });
  },
  // the saved default from Settings > Models applies until the button is clicked
  loadAnswerCheckDefault: async () => {
    try {
      if (localStorage.getItem('answer_check')) return;
      const r = await apiFetch('/control/lanes', { background: true });
      const d = r.ok ? await r.json() : null;
      const m = d?.verification?.mode;
      if (m === 'badge' || m === 'gate') set({ answerCheck: m });
    } catch (_) {
      // keep off
    }
  },
  patchMessage: (id, patch) => set((st) => ({ messages: st.messages.map((m) => (m.id === id ? { ...m, ...patch } : m)) })),
  // answer the last question again: drop the last exchange from the screen and send the question once more
  // On opening a chat: an agent run that kept going while the page was closed is replayed into the chat, so its
  // result shows up instead of being lost. Runs this browser already saved are skipped.
  reattachDetachedRuns: async (sessionId) => {
    const key = sessionKey(sessionId);
    if (runs.has(key) || get().isStreaming) return;
    let rows: Array<{ id: string; running?: boolean }> = [];
    try {
      const r = await apiFetch(`/agent/runs?session_id=${encodeURIComponent(String(sessionId))}`, { background: true });
      if (!r.ok) return;
      rows = (await r.json()).runs || [];
    } catch (_) {
      return;
    }
    // oldest first, one at a time; a run that stays unsaved (still running / interrupted) is tried only once per open
    const tried = new Set<string>();
    for (;;) {
      const row = rows.find((x) => !runWasSaved(x.id) && !tried.has(x.id));
      // the chat on screen must still be this one, and nothing may have started meanwhile
      if (!row || sessionKey(get().activeSessionId) !== key || runs.has(key) || get().isStreaming) return;
      tried.add(row.id);
      const accepted = () => get().showToast(row.running ? 'Reconnecting to a run that kept going…' : 'Showing a run that finished while you were away', 'info');
      let ok = false;
      const opts = { reattachRunId: row.id, onAccepted: () => { ok = true; accepted(); } };
      await get().sendPrompt('', opts);
      if (!ok) {
        // the send gate was busy: retry once after it clears
        await new Promise((res) => setTimeout(res, 600));
        if (sessionKey(get().activeSessionId) !== key || runs.has(key) || get().isStreaming) return;
        await get().sendPrompt('', opts);
        if (!ok) return;
      }
    }
  },

  // answer the last question again: the old exchange is dropped from the screen once the new send goes ahead
  regenerateLast: async () => {
    const { messages, isStreaming, appMode, activeProjectId } = get();
    if (isStreaming) return;
    let u = -1;
    for (let i = messages.length - 1; i >= 0; i--) {
      if (messages[i].role === 'user') { u = i; break; }
    }
    if (u < 0) return;
    if (appMode === 'code' && !activeProjectId) return get().showToast('Please select a project first', 'error');
    const q = messages[u];
    await get().sendPrompt(q.content, { images: q.images, files: q.files, displayContent: q.meta?.displayContent, truncateTo: u });
  },
  projectModal: null,
  openProjectModal: (mode, project) => set({ projectModal: { mode, project } }),
  closeProjectModal: () => set({ projectModal: null }),
  termEntries: [],
  addTermEntry: (e) => set((st) => (st.termEntries.some((x) => x.id === e.id) ? {} : { termEntries: [...st.termEntries.slice(-299), e] })),
  updateTermEntry: (id, patch) => set((st) => ({ termEntries: st.termEntries.map((x) => (x.id === id ? { ...x, ...patch } : x)) })),
  clearTerm: () => set({ termEntries: [] }),
  compactConversation: async (instructions) => {
    const { messages, activeSessionId, appMode, activeProjectId, isStreaming } = get();
    const key = sessionKey(activeSessionId);
    if (isStreaming || runs.has(key)) return get().showToast('Wait for the current generation to finish first', 'warn');
    if (compacting.has(key)) return;
    if (appMode === 'code' && !activeProjectId) return get().showToast('Please select a project first', 'error');
    const hist = contextMessages(messages).filter(
      (m) => (m.role === 'user' || m.role === 'assistant' || m.meta?.compact) && (m.content || '').trim(),
    );
    if (hist.length < 2) return get().showToast('Nothing to compact yet. Send a few messages first.', 'warn');
    get().showToast('Compacting conversation…', 'info', 180000);   // stays up while the model writes the summary
    compacting.add(key);
    try {
      const res = await apiFetch('/chat/compact', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          messages: hist.map((m) => ({ role: m.role, content: m.content })),
          session_id: activeSessionId || null,
          instructions: instructions || undefined,
          agent_mode: appMode === 'code',
          project_id: appMode === 'code' && !isNaN(Number(activeProjectId)) ? Number(activeProjectId) : null,
        }),
      });
      get().hideToast();
      const j = await res.json().catch(() => ({}));
      if (!res.ok) throw new Error(j.error || `HTTP ${res.status}`);
      const cm = j.compact_message || {};
      const marker: ChatMessage = {
        id: `compact-${Date.now()}`,
        role: 'system',
        content: cm.content || '',
        timestamp: '',
        createdAt: Date.now(),
        meta: cm.meta || { compact: true },
      };
      // the summary belongs to the chat it was made from; it is saved on the server, so another chat never shows it
      if (sessionKey(get().activeSessionId) === key) set((st) => ({ messages: [...st.messages, marker] }));
      const kb = (n: number) => (n >= 1000 ? (n / 1000).toFixed(1) + 'k' : String(n));
      const pct = j.before_tokens > 0 ? Math.max(0, Math.round((1 - j.after_tokens / j.before_tokens) * 100)) : 0;
      get().showToast(`Compacted: ${kb(j.before_tokens)} to ${kb(j.after_tokens)} tokens (${pct}% smaller)`, 'success');
    } catch (e: any) {
      get().showToast('Compact failed: ' + e.message, 'error');
    } finally {
      compacting.delete(key);
    }
  },
  idleWarning: false,
  // the server said 401 (cookie expired / revoked / idle): drop back to the sign-in screen
  sessionExpired: () => {
    if (!get().isAuthenticated) return;
    stopSessionWatch();
    Array.from(runs.keys()).forEach(stopRun);
    set({ isAuthenticated: false, currentUser: null, sessions: [], messages: [], activeSessionId: null, activeModal: 'none', idleWarning: false, isStreaming: false, bgMessages: {}, runningSessions: {}, permRequest: null });
  },
  startSession: async () => {
    const policy = await api.auth.getSessionPolicy();
    if (!get().isAuthenticated) return;
    startSessionWatch({
      idleSeconds: policy.idleSeconds,
      idleEnabled: policy.idleEnabled,
      onLogout: () => {
        api.auth.logout().finally(() => get().sessionExpired());
      },
      onWarn: (show) => set({ idleWarning: show }),
    });
  },
  agentModal: null,
  agentsVersion: 0,
  openAgentModal: (mode, agent = null) => set({ agentModal: { mode, agent } }),
  closeAgentModal: () => set({ agentModal: null }),
  bumpAgents: () => set((st) => ({ agentsVersion: st.agentsVersion + 1 })),
  permRequest: null,
  answerPermission: async (decision) => {
    const req = get().permRequest;
    set({ permRequest: null });
    if (!req) return;
    const remember = decision === 'always' || decision === 'project' || decision === 'user';
    const patternless = ['browser_eval', 'browser_open', 'device', 'edit', 'rule', 'python', 'media', 'mcp'].includes(req.kind) || req.saveable === false;
    const pattern = remember && !patternless ? `${(req.cmd.trim().split(/\s+/)[0] || '*')} *` : null;
    try {
      const r = await apiFetch('/agent/permission', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ req_id: req.reqId, decision, pattern, project_id: get().activeProjectId || null }),
      });
      if (r.status === 404) {
        // the request is gone (answered already, expired, or the run ended): never ask again
        if (get().permRequest?.reqId === req.reqId) set({ permRequest: null });
      } else if (!r.ok) {
        const j = await r.json().catch(() => ({}));
        get().showToast('Could not save your answer: ' + (j.error || j.detail || 'HTTP ' + r.status) + '. Choose again.', 'error');
        if (!get().permRequest) set({ permRequest: req });   // the run is still waiting: ask again
      } else if (remember) {
        const j = await r.json().catch(() => ({}));
        if (j.saved === false) get().showToast('Allowed this time, but it was not remembered', 'warn');
      }
    } catch (_) {
      // the stream may have ended
    }
  },
  showDialog: (dialog: DialogState) => set({ dialog }),
  closeDialog: () => set({ dialog: null }),

  activeModal: 'none',
  settingsTab: 'sec-theme',
  customizeTab: 'skills',
  previewModal: {
    isOpen: false,
    filePath: '',
    title: '',
    directContent: null,
    source: 'common',
  },
  openPreviewModal: (filePath, title = '', directContent = null, source = 'common', forceModal = false) => {
    const cleanPath = (filePath || '').split('?')[0].toLowerCase();
    const ext = cleanPath.includes('.') ? cleanPath.split('.').pop() || '' : '';
    const isAttachment =
      forceModal ||
      source === 'modal' ||
      title === 'Attachment' ||
      (typeof title === 'string' && title.toLowerCase().includes('attachment'));

    const isDockPreview = !isAttachment && (
      ['html', 'htm', 'ppt', 'pptx', 'pdf', 'md', 'markdown', 'svg', 'mermaid'].includes(ext) ||
      cleanPath.includes('diagram')
    );

    if (isDockPreview) {
      set({
        dockCollapsed: false,
        dockTab: 'preview',
        activeModal: 'none',
        previewModal: {
          isOpen: false,
          filePath,
          title: title || filePath,
          directContent,
          source,
        },
      });
    } else {
      // Modal preview for attachments, images, and documents
      set({
        activeModal: 'preview',
        previewModal: {
          isOpen: true,
          filePath,
          title: title || filePath,
          directContent,
          source: source === 'modal' ? 'common' : source,
        },
      });
    }
  },
  closePreviewModal: () => {
    set((state) => ({
      previewModal: {
        ...state.previewModal,
        isOpen: false,
      },
    }));
  },

  hasPerm: (permission: string) => {
    const user = get().currentUser;
    if (!user) return false;
    if (user.is_super_admin) return true;
    return Boolean(user.permissions && user.permissions.includes(permission));
  },

  checkAuth: async () => {
    try {
      const me = await api.auth.getMe();
      if (me) {
        set({ isAuthenticated: true, currentUser: me });
        get().startSession();
      } else {
        set({ isAuthenticated: false, currentUser: null });
      }
    } catch (_) {
      set({ isAuthenticated: false, currentUser: null });
    }
  },

  setAuthenticatedUser: (user) => {
    set({
      isAuthenticated: Boolean(user),
      currentUser: user,
    });
    if (user) {
      get().startSession();
      get().loadAnswerCheckDefault();
      get().fetchModels();
      get().fetchProjects();
      get().fetchSessions();
      void syncSamplingFromServer();
    }
  },

  logout: async () => {
    try {
      await api.auth.logout();
    } catch (_) {}
    stopSessionWatch();
    Array.from(runs.keys()).forEach(stopRun);
    set({
      isAuthenticated: false,
      currentUser: null,
      sessions: [],
      messages: [],
      activeSessionId: null,
      activeModal: 'none',
      isStreaming: false,
      bgMessages: {},
      runningSessions: {},
      permRequest: null,
      termEntries: [],
    });
  },

  setIsConnected: (isConnected) => set({ isConnected }),

  setServerStatus: (serverStatus) => {
    const prev = get().serverStatus;
    set({ serverStatus });
    // Follow the server only when what it runs changed; otherwise every status tick would undo a pick the user just made.
    const changed = !prev || prev.model !== serverStatus.model || prev.cloud_main?.key !== serverStatus.cloud_main?.key;
    // The picker lists the model that is running: refresh it when a model starts, finishes loading or stops, not only on a page reload.
    const listStale = !prev || prev.model !== serverStatus.model || Boolean(prev.pid) !== Boolean(serverStatus.pid)
      || Boolean(prev.healthy) !== Boolean(serverStatus.healthy) || Boolean(prev.loading) !== Boolean(serverStatus.loading)
      || prev.cloud_main?.key !== serverStatus.cloud_main?.key;
    if (listStale && get().isAuthenticated) {
      void get().fetchModels().then(() => {
        // The model that was selected is gone (unloaded, stopped): the picker no longer lists it, so select what it does list.
        // Not while one is still starting: it only appears in the list once it is up.
        const st = get();
        if (st.serverStatus?.pid || st.serverStatus?.loading || st.serverStatus?.cloud_main) return;
        const known = st.availableModels.some((m) => m.display === st.selectedModel || m.id === st.selectedModel);
        if (!known) set({ selectedModel: st.availableModels[0]?.display || '' });
      });
    }
    if (!changed) return;
    const ownModel = get().sessions.find((x) => sessionKey(x.id) === sessionKey(get().activeSessionId))?.model;
    if (ownModel) return;   // this chat keeps the model it was started with
    // A bound cloud main lane wins over the (possibly stale) local model name.
    if (serverStatus.cloud_main?.display) {
      set({ selectedModel: serverStatus.cloud_main.display });
    } else if (serverStatus.model) {
      // the model list names a loaded model by its display name; the status gives its file path
      const listed = get().availableModels.find((m) => m.id === serverStatus.model);
      const base = serverStatus.model.split('\\').pop()?.split('/').pop() || serverStatus.model;
      set({ selectedModel: listed ? listed.display : base.replace(/\.gguf$/i, '') });
    }
  },

  setTheme: (theme) => {
    document.documentElement.setAttribute('data-theme', theme);
    set({ theme });
  },

  toggleSidebar: () => set((state) => ({ sidebarCollapsed: !state.sidebarCollapsed })),
  toggleDock: (tab?: DockTab) => {
    const { dockCollapsed, dockTab } = get();
    if (tab) {
      if (!dockCollapsed && dockTab === tab) {
        set({ dockCollapsed: true });
      } else {
        set({ dockTab: tab, dockCollapsed: false });
      }
    } else {
      set({ dockCollapsed: !dockCollapsed });
    }
  },
  setDockCollapsed: (collapsed) => set({ dockCollapsed: collapsed }),
  setDockExpanded: (expanded) => set({ dockExpanded: expanded }),
  setDockTab: (dockTab) => set({ dockTab, dockCollapsed: false }),

  setAppMode: (appMode) => {
    try {
      localStorage.setItem('app_mode', appMode);
    } catch (_) {}
    set({ appMode });
    // In chat mode, hide code & terminal tabs if active
    if (appMode === 'chat' && (get().dockTab === 'files' || get().dockTab === 'terminal')) {
      set({ dockTab: 'plan' });
    }
    get().fetchSessions();
  },
  setAgentMode: (agentMode) => set({ agentMode }),
  setReasoningEffort: (reasoningEffort) => set({ reasoningEffort }),
  toggleDeepResearch: () => set((state) => ({ deepResearch: !state.deepResearch })),
  toggleWebSearch: () => set((state) => ({ webSearchEnabled: !state.webSearchEnabled })),
  setKbMode: (kbMode) => set({ kbMode }),
  setSelectedModel: async (modelIdentifier: string) => {
    const { availableModels } = get();
    const match = availableModels.find((m) => m.id === modelIdentifier || m.display === modelIdentifier);
    const targetDisplay = match ? match.display : modelIdentifier;

    set({ selectedModel: targetDisplay });
    // the choice belongs to this chat (stored with it, so it survives a reload); a chat that does not exist yet gets it when it is created.
    // Each request carries its own model, so nothing is switched on the server here and other chats keep theirs.
    const sid = get().activeSessionId;
    if (sid) {
      set((st) => ({ sessions: st.sessions.map((x) => (sessionKey(x.id) === sessionKey(sid) ? { ...x, model: targetDisplay } : x)) }));
      apiFetch(`/control/sessions/${sid}`, {
        method: 'PATCH',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ model: targetDisplay }),
      }).catch(() => {});
    }
  },
  setActiveProjectId: (activeProjectId) => {
    set({ activeProjectId });
    if (activeProjectId) {
      try {
        localStorage.setItem('active_project_id', String(activeProjectId));
      } catch (_) {}
      api.projects.activate(activeProjectId).catch(() => {});
      if (get().appMode === 'code') {
        get().fetchSessions();
      }
    }
  },
  setActiveCustomAgent: (activeCustomAgent) => {
    set({ activeCustomAgent });
    if (activeCustomAgent) {
      // the composer starts at the agent's own effort (the legacy UI did the same); a change made afterwards still wins
      const eff = activeCustomAgent.reasoning_effort;
      if (['none', 'low', 'medium', 'high', 'extra'].includes(eff)) set({ reasoningEffort: eff });
      try {
        localStorage.setItem('active_custom_agent_id', String(activeCustomAgent.id));
      } catch (_) {}
    } else {
      try {
        localStorage.removeItem('active_custom_agent_id');
      } catch (_) {}
    }
    get().fetchSessions();
  },
  setSessionFilter: (sessionFilter) => set({ sessionFilter }),

  fetchSessions: async () => {
    const { appMode, activeProjectId, activeCustomAgent } = get();
    const pid = appMode === 'code' && activeProjectId ? activeProjectId : 0;
    const { sessions: rawSessions, hasMore } = await api.sessions.list(pid, 50);

    oldestSessionIdTracker = rawSessions.length > 0 ? rawSessions[rawSessions.length - 1].id : null;

    const filteredRaw = filterRawSessions(rawSessions, appMode, activeCustomAgent);
    const items: SessionItem[] = filteredRaw.map((s: any) => formatSessionItem(s, appMode, get().activeSessionId));

    set({ sessions: items, hasMoreSessions: hasMore, isLoadingMoreSessions: false });

    // Restore active session from localStorage if present
    const key = getActiveSessionKey(appMode);
    const savedSid = localStorage.getItem(key);
    if (savedSid) {
      const target = items.find((x) => String(x.id) === String(savedSid));
      if (target) {
        if (get().activeSessionId !== target.id) {
          get().openSession(target.id);
        }
        return;
      }
    }

    // If activeSessionId is set but not in items, clear it
    if (get().activeSessionId && !items.some((x) => x.id === get().activeSessionId)) {
      const cur = get();
      // a run in that chat keeps going in the background; its messages are parked, not dropped
      set((st) => ({
        activeSessionId: null,
        previewModal: EMPTY_PREVIEW,
        messages: [],
        isStreaming: false,
        bgMessages: cur.isStreaming || externalBusy.has(sessionKey(cur.activeSessionId)) ? { ...st.bgMessages, [sessionKey(cur.activeSessionId)]: cur.messages } : st.bgMessages,
      }));
    }
  },

  fetchMoreSessions: async () => {
    if (get().isLoadingMoreSessions || !get().hasMoreSessions || !oldestSessionIdTracker) return;
    set({ isLoadingMoreSessions: true });
    try {
      const { appMode, activeProjectId, activeCustomAgent, sessions } = get();
      const pid = appMode === 'code' && activeProjectId ? activeProjectId : 0;
      const { sessions: rawSessions, hasMore } = await api.sessions.list(pid, 50, oldestSessionIdTracker);

      if (rawSessions.length > 0) {
        oldestSessionIdTracker = rawSessions[rawSessions.length - 1].id;
      }

      const filteredRaw = filterRawSessions(rawSessions, appMode, activeCustomAgent);
      const existingIds = new Set(sessions.map((s) => String(s.id)));
      const freshItems = filteredRaw
        .filter((s: any) => !existingIds.has(String(s.id)))
        .map((s: any) => formatSessionItem(s, appMode, get().activeSessionId));

      set({
        sessions: [...sessions, ...freshItems],
        hasMoreSessions: hasMore,
      });
    } catch (err) {
      logger.error('Session', 'Failed to fetch more sessions', err);
    } finally {
      set({ isLoadingMoreSessions: false });
    }
  },

  openSession: async (id: string | number) => {
    try {
      localStorage.setItem(getActiveSessionKey(get().appMode), String(id));
    } catch (_) {}

    // a chat that is still answering keeps running: park its messages, and show a running chat's live ones
    const prev = get();
    const key = sessionKey(id);
    const prevKey = sessionKey(prev.activeSessionId);
    const parked = prev.isStreaming || externalBusy.has(prevKey) ? { [prevKey]: prev.messages } : {};
    const liveMsgs = prev.bgMessages[key] || parked[key];
    const bgNext = { ...prev.bgMessages, ...parked };
    if (liveMsgs && !parked[key]) delete bgNext[key];
    if (key === sessionKey(prev.activeSessionId)) delete bgNext[key];
    set((state) => ({
      activeSessionId: id,
      ...(key !== prevKey ? { previewModal: EMPTY_PREVIEW } : {}),
      sessions: state.sessions.map((s) => ({ ...s, active: s.id === id })),
      messages: isBusy(key) ? liveMsgs || [] : [],
      sessionLoading: !isBusy(key),
      isStreaming: runs.has(key),
      bgMessages: bgNext,
    }));
    const own = get().sessions.find((x) => sessionKey(x.id) === key)?.model;
    if (own) set({ selectedModel: own });
    if (isBusy(key)) return;

    try {
      const rawMsgs = await api.sessions.getMessages(id);
      const formatted: ChatMessage[] = rawMsgs.map((m: any, idx: number) => {
        const meta = m.meta || {};
        let timeStr = meta.timeStr || '';
        let createdMs = 0;
        if (m.created_at) {
          const ms = typeof m.created_at === 'number' ? (m.created_at < 1e11 ? m.created_at * 1000 : m.created_at) : Date.parse(m.created_at);
          if (!isNaN(ms)) {
            createdMs = ms;
            if (!timeStr) timeStr = new Date(ms).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
          }
        }
        return {
          id: String(m.id || `msg-${idx}`),
          role: m.role || 'user',
          content: m.content || '',
          thinking: meta.reasoning || m.reasoning || '',
          timestamp: timeStr,
          createdAt: createdMs || undefined,
          runId: meta.runId || undefined,
          check: meta.check || undefined,
          model: meta.modelDisplay || meta.model || '',
          acts: meta.acts || m.acts || [],
          secs: meta.secs || m.secs || 0,
          ntok: meta.ntok || m.ntok || 0,
          promptTokens: meta.promptTokens || 0,
          tps: meta.tps || m.tps || 0,
          errorAlert: meta.errorAlert || m.errorAlert || '',
          images: meta.images || m.images || [],
          files: meta.files || m.files || '',
          meta,
        };
      });

      // a failed or cancelled media card followed by a finished one for the same prompt is a retry: show only the result
      const finished = new Set(formatted.filter((m) => m.meta?.media?.state === 'done').map((m) => m.meta.media.prompt));
      for (let i = formatted.length - 1; i >= 0; i--) {
        const md = formatted[i].meta?.media;
        if (md && (md.state === 'error' || md.state === 'cancelled') && finished.has(md.prompt)) formatted.splice(i, 1);
      }
      // a slow load must not overwrite the chat that is on screen now (another click, or a run that started meanwhile)
      if (sessionKey(get().activeSessionId) !== key || isBusy(key)) return;
      set({ messages: formatted, sessionLoading: false });
      void get().reattachDetachedRuns(id);
    } catch (err) {
      logger.error('Session', `Failed to load messages for session ${id}`, err);
      if (sessionKey(get().activeSessionId) === key) set({ sessionLoading: false });
    }
  },

  createSession: async () => {
    const { appMode } = get();
    try {
      localStorage.removeItem(getActiveSessionKey(appMode));
    } catch (_) {}

    const prev = get();
    set((state) => ({
      activeSessionId: null,
      previewModal: EMPTY_PREVIEW,
      messages: [],
      isStreaming: false,
      bgMessages: prev.isStreaming || externalBusy.has(sessionKey(prev.activeSessionId)) ? { ...state.bgMessages, [sessionKey(prev.activeSessionId)]: prev.messages } : state.bgMessages,
      sessions: state.sessions.map((s) => ({ ...s, active: false })),
    }));
    // a new chat opens with the cursor in the message box (the composer listens for this; any draft stays)
    window.setTimeout(() => window.dispatchEvent(new CustomEvent('edit-user-prompt', { detail: { focusOnly: true } })), 0);
  },

  deleteSession: async (id: string | number) => {
    // a chat that is being deleted must not keep answering into nowhere
    const delKey = sessionKey(id);
    // image / video jobs of that chat stop too (the module is preloaded; awaited so they are cancelled before the chat vanishes)
    try {
      (await loadMedia()).cancelMediaForSession(id);
    } catch (e) {
      mediaModule = null;
      logger.warn('Session', 'Could not cancel the media jobs of the deleted chat', e);
    }
    stopRun(delKey);
    // Optimistically remove from state immediately
    set((state) => {
      const running = { ...state.runningSessions };
      delete running[delKey];
      const bg = { ...state.bgMessages };
      delete bg[delKey];
      return {
        sessions: state.sessions.filter((s) => String(s.id) !== String(id)),
        runningSessions: running,
        bgMessages: bg,
        ...(String(state.activeSessionId) === String(id) ? { activeSessionId: null, messages: [], isStreaming: false } : {}),
      };
    });

    try {
      const ok = await api.sessions.delete(id);
      if (!ok) {
        get().showToast('Failed to delete session', 'error');
        await get().fetchSessions();
        return;
      }
      get().showToast('Session deleted', 'success');
      await get().fetchSessions();
    } catch (err: any) {
      logger.error('Session', `Failed to delete session ${id}`, err);
      get().showToast('Failed to delete session', 'error');
      await get().fetchSessions();
    }
  },

  fetchStatus: async () => {
    const status = await api.control.getStatus();
    if (status) {
      get().setServerStatus(status);
    }
  },

  fetchModels: async () => {
    const models = await api.control.getAvailableModels();
    set({ availableModels: models });
    if (models.length > 0 && !get().selectedModel) {
      const loaded = models.find((m) => m.currently_loaded);
      const chosen = loaded ? loaded.display : models[0].display;
      set({ selectedModel: chosen });
    }
  },

  fetchProjects: async () => {
    const prjs = await api.projects.list();
    if (prjs.length > 0) {
      const savedProjId = localStorage.getItem('active_project_id');
      const activeProjId = get().activeProjectId || savedProjId;
      const validActive = prjs.find((p: any) => String(p.id) === String(activeProjId));
      const targetId = validActive ? String(validActive.id) : String(prjs[0].id);

      set({
        projects: prjs.map((p: any) => ({
          id: String(p.id || p.path),
          name: p.name || p.path?.split('\\').pop() || 'Project',
          path: p.workspace_dir || p.path || '',
          workspace_dir: p.workspace_dir || p.path || '',
          device_name: p.device_name || 'Windows PC',
          active: String(p.id) === targetId,
        })),
        activeProjectId: targetId,
      });

      api.projects.activate(targetId).catch(() => {});
      if (get().appMode === 'code') {
        get().fetchSessions();
      }
    } else {
      set({ projects: [] });
    }
  },

  loadSelectedModel: async () => {
    const { selectedModel, availableModels, serverStatus } = get();
    const match = availableModels.find((m) => m.display === selectedModel || m.id === selectedModel);
    const target = match ? match.id : selectedModel;

    if (serverStatus?.pid) {
      set({ isModelLoading: true });
      logger.info('Control', 'Unloading current model from GPU VRAM...');
      await api.control.stopModel();
      set({ isModelLoading: false });
      return;
    }

    if (!target) return;
    if (runs.size > 0 && serverStatus?.model && serverStatus.model !== target) {
      get().showToast('Another chat is still answering with the loaded model. Load a different local model after it finishes.', 'warn');
      return;
    }
    set({ isModelLoading: true });
    logger.info('Control', `Switching/Loading model: ${target}`);
    try {
      await api.control.switchModel(target);
      await get().fetchStatus();
      await get().fetchModels();
    } catch (err) {
      logger.error('Control', `Failed to switch/load model ${target}:`, err);
    } finally {
      set({ isModelLoading: false });
    }
  },

  sendPrompt: async (text: string, options?: { images?: string[]; files?: string; displayContent?: string; voice?: boolean; truncateTo?: number; reattachRunId?: string; onAccepted?: () => void }) => {
    if (sendGate) return;
    sendGate = true;
    let released = false;
    const release = () => {
      if (!released) { released = true; sendGate = false; }
    };
    try {
      await (async () => {
    const { webSearchEnabled, kbMode, deepResearch, reasoningEffort, appMode, activeProjectId, selectedModel, availableModels, serverStatus } = get();
    const reattachId = options?.reattachRunId || '';
    if ((!reattachId && !text.trim() && (!options?.images || !options.images.length)) || get().isStreaming) return;

    const matchModel = availableModels.find((m) => m.display === selectedModel || m.id === selectedModel);
    const targetModelId = matchModel ? matchModel.id : selectedModel;
    const isCloud = !reattachId && (String(targetModelId).startsWith('cloud:') || matchModel?.kind === 'cloud');

    if ((!reattachId && !text.trim() && (!options?.images || !options.images.length)) || get().isStreaming) return;

    // Coding Agent mode (and a personal agent that has a work folder) runs through the agent loop, not plain chat
    const ca = appMode === 'agent' ? get().activeCustomAgent : null;
    const useAgent = Boolean(reattachId) || appMode === 'code' || (appMode === 'agent' && Boolean(ca && ca.scope === 'mine' && ca.work_dir));
    if (appMode === 'code' && !reattachId) {
      if (!activeProjectId) {
        get().showToast('Please select a project first', 'error');
        return;
      }
      // the page can show a project the server forgot (restart, another tab): re-select it before the run
      await api.projects.activate(activeProjectId).catch(() => {});
    }

    let currentSessionId = get().activeSessionId;
    const startKey = sessionKey(currentSessionId);
    let createdHere = false;
    if (currentSessionId && targetModelId) {
      const stored = get().sessions.find((x) => sessionKey(x.id) === startKey)?.model;
      if (!stored) {
        set((st) => ({ sessions: st.sessions.map((x) => (sessionKey(x.id) === startKey ? { ...x, model: matchModel?.display || targetModelId } : x)) }));
        apiFetch(`/control/sessions/${currentSessionId}`, {
          method: 'PATCH', headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ model: matchModel?.display || targetModelId }),
        }).catch(() => {});
      }
    }

    // If no active session, ensure/create one now with first prompt title
    if (!currentSessionId) {
      let title = text.trim().slice(0, 48) || (options?.files ? options.files.slice(0, 48) : 'New Chat');
      const pid = appMode === 'code' && activeProjectId ? activeProjectId : 0;
      let agentId: string | number | undefined = undefined;
      if (appMode === 'agent' && get().activeCustomAgent) {
        title = `${get().activeCustomAgent.icon || '🤖'} ${get().activeCustomAgent.name}`;
        agentId = get().activeCustomAgent.id;
      }
      try {
        const res = await apiFetch(`/control/projects/${pid}/sessions`, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ title, agent_id: agentId, model: targetModelId || undefined }),
        });
        const data = await res.json();
        if (!res.ok || !data || !data.session) throw new Error((data && data.error) || `HTTP ${res.status}`);
        currentSessionId = data.session.id;
        createdHere = true;
        try {
          localStorage.setItem(getActiveSessionKey(appMode), String(currentSessionId));
        } catch (_) {}
        set({ activeSessionId: currentSessionId });
        get().fetchSessions();
      } catch (err: any) {
        logger.error('Session', 'Failed to create session on first prompt', err);
        get().showToast('Could not start the chat: ' + (err?.message || 'server error'), 'error');
        return;
      }
    }
    // the user opened another chat while the model was being switched: do not write this prompt into it
    if (!createdHere && sessionKey(get().activeSessionId) !== startKey) return;

    const timeNow = new Date().toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
    const userImages = options?.images || [];
    const userFiles = options?.files || '';
    const userDisplay = options?.displayContent || text;

    const userMsg: ChatMessage = {
      id: `user-${Date.now()}`,
      role: 'user',
      content: text,
      timestamp: timeNow,
      createdAt: Date.now(),
      images: userImages,
      files: userFiles,
      meta: {
        timeStr: timeNow,
        displayContent: userDisplay,
        images: userImages,
        files: userFiles,
      },
    };

    const assistantMsgId = `assistant-${Date.now()}`;
    const assistantMsg: ChatMessage = {
      id: assistantMsgId,
      role: 'assistant',
      content: '',
      thinking: '',
      timestamp: timeNow,
      statusText: reattachId ? 'Reconnecting to the run...' : 'Connecting to model...',
      check: !reattachId && get().answerCheck === 'gate' ? { state: 'pending', mode: 'gate', t0: Date.now() } : undefined,
    };

    const liveMessages = get().messages;
    const baseMessages = options?.truncateTo != null ? liveMessages.slice(0, options.truncateTo) : liveMessages;
    // a reattach continues a question that is already in the chat: only the answer is new
    const updatedMessages = reattachId ? [...baseMessages, assistantMsg] : [...baseMessages, userMsg, assistantMsg];
    const sidKey = sessionKey(currentSessionId);
    const runCtrl = new AbortController();
    runs.set(sidKey, { ctrl: runCtrl, agentRunId: null });
    set((st) => ({
      messages: updatedMessages,
      isStreaming: true,
      activeStreamingMessageId: assistantMsgId,
      runningSessions: { ...st.runningSessions, [sidKey]: true },
    }));

    release();   // the run is registered: another chat may be started now
    options?.onAccepted?.();

    // Updates go to the screen while this chat is open, otherwise to its parked copy (so a run keeps going in the background).
    const setRun = (fn: (state: AppState) => any) =>
      set((state) => {
        if (sessionKey(state.activeSessionId) === sidKey) return fn(state);
        const view = { ...state, messages: state.bgMessages[sidKey] || [] } as AppState;
        const res = fn(view);
        if (!res || res === view) return state;
        const { messages: nextMsgs, ...rest } = res;
        return { ...rest, bgMessages: { ...state.bgMessages, [sidKey]: nextMsgs || view.messages } };
      });
    const getRunMsgs = (): ChatMessage[] => (sessionKey(get().activeSessionId) === sidKey ? get().messages : get().bgMessages[sidKey] || []);

    // Persist user message to backend SQLite
    if (currentSessionId && !reattachId) {
      api.sessions.persistMessage(currentSessionId, 'user', text, userMsg.meta);
    }

    const ctxMessages = contextMessages(updatedMessages);
    const lastUserIdx = ctxMessages.length - 2;
    const msgsPayload = ctxMessages.slice(0, -1).map((m, i) => {
      let promptContent = m.content;
      // older turns are re-sent shortened: a long attached file keeps only its start
      if (m.role === 'user' && i < lastUserIdx) promptContent = clipHistoryContent(promptContent);
      const hasBlocks = promptContent.includes('--- FILE:') || promptContent.includes('--- IMAGE:');
      if (m.role === 'user' && m.files && !hasBlocks && !promptContent.includes('[Attached Files:')) {
        promptContent = promptContent ? `${promptContent}\n\n[Attached Files: ${m.files}]` : `[Attached Files: ${m.files}]`;
      }
      return {
        role: m.role,
        content: promptContent,
      };
    });

    const releaseSession = holdSession();   // an open stream is not idle
    if (!reattachId) requestNotificationPermission();
    const agentRunId = reattachId || 'r' + Date.now().toString(36) + Math.random().toString(36).slice(2, 10);
    runs.get(sidKey)!.agentRunId = agentRunId;     // chat answers run detached on the server too: Stop must cancel them there
    const agentState = newAgentRunState();
    let agentSawTerminal = false;
    let agentFailed = false;

    let tpsVal = 0;
    let ntokVal = 0;
    let promptTokVal = 0;
    let secsVal = 0;
    let modelDisplay = '';
    const runStart = performance.now();
    let reattachStatus = 0;

    // The tail of an agent run: record how it ended, save the message (with its action timeline), release the server run.
    const finishAgentRun = (endState: 'interrupted' | 'cancelled' | 'failed' | null, reason: string) => {
      const holder: { msg?: ChatMessage } = {};
      setRun((state) => {
        const msgs = [...state.messages];
        const L = msgs.find((m) => m.id === assistantMsgId);
        if (!L) return state;
        if (endState) interruptRun(L, agentState, reason, endState);
        answerCheckEnd(L);
        if (!L.content && agentState.runState === 'completed' && L.acts && L.acts.length) {
          L.content = 'Task completed. See tool operations above for details.';
        }
        L.statusText = '';
        if (agentState.gap && !(L.content || '').startsWith(GAP_NOTE)) L.content = GAP_NOTE + '\n\n' + (L.content || '');
        // a reattach replays the run: the time it took here is not the run's own
        if (!reattachId) L.secs = Math.max(0.001, (performance.now() - runStart) / 1000);
        holder.msg = { ...L };
        return { messages: msgs };
      });
      set((st) => (st.permRequest && st.permRequest.sid === sidKey ? { permRequest: null } : {}));
      const L = holder.msg;
      const interrupted = agentState.runState === 'interrupted';
      // a run that is cut but still reattachable, or lost frames, is shown but not saved or acknowledged
      const final = !interrupted && !agentState.gap;
      // already saved (another tab attached to the same run): do not save it twice
      let skipSave = !final && (interrupted || agentState.gap) && (interrupted ? !L?.content : true);
      if (final) {
        if (runWasSaved(agentRunId)) skipSave = true;
        else markRunSaved(agentRunId);
      }
      if (L && currentSessionId && !skipSave) {
        const cut = agentState.runState === 'interrupted' || agentState.runState === 'cancelled' || agentState.runState === 'failed';
        api.sessions.persistMessage(currentSessionId, 'assistant', L.content || (cut ? '(run interrupted)' : ''), {
          reasoning: agentState.reasoning || undefined,
          secs: L.secs,
          acts: (L.acts || []).map((a) => (a && a.image ? { ...a, image: undefined } : a)),
          modelDisplay: L.model || undefined,
          modelSource: agentState.modelSource || undefined,
          modelProvider: L.modelProvider || undefined,
          runId: agentState.runId || undefined,
          runState: agentState.runState || undefined,
          check: L.check?.state === 'done' ? L.check : undefined,
          timeStr: new Date().toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' }),
        });
      }
      // a dropped connection is not an end: that run keeps going on the server and can still be reattached
      if (final) {
        apiFetch(`/agent/run/${encodeURIComponent(agentRunId)}/ack`, { method: 'POST' }).catch(() => {});
      }
    };

    // The tail of a plain chat answer: close the placeholder state and save what is on screen.
    const finishChatRun = (errorText: string) => {
      const holder: { msg?: ChatMessage } = {};
      setRun((state) => {
        const msgs = [...state.messages];
        const L = msgs.find((m) => m.id === assistantMsgId);
        if (!L) return state;
        answerCheckEnd(L);
        if (errorText) L.content = L.content || `⚠️ Error: ${errorText}`;
        L.statusText = '';
        // speed chip: tokens come from the server when it says, else an estimate from the text (as the old UI did)
        if (!errorText && (L.content || '').trim()) {
          const secs = Math.max(0.001, (performance.now() - runStart) / 1000);
          const ntok = ntokVal || Math.max(1, Math.round(((L.content || '').length + agentState.reasoning.length) / 3.5));
          L.secs = secs;
          L.ntok = ntok;
          L.tps = ntok / secs;
        }
        if (promptTokVal) L.promptTokens = promptTokVal;
        holder.msg = { ...L };
        return { messages: msgs };
      });
      const L = holder.msg;
      if (L && currentSessionId && (L.content || '').trim()) {
        api.sessions.persistMessage(currentSessionId, 'assistant', L.content, {
          check: L.check?.state === 'done' ? L.check : undefined,
          reasoning: agentState.reasoning || undefined,
          acts: (L.acts || []).length ? L.acts : undefined,
          tps: L.tps || tpsVal || undefined,
          ntok: L.ntok || ntokVal || undefined,
          promptTokens: L.promptTokens || undefined,
          secs: L.secs || secsVal || undefined,
          modelDisplay: modelDisplay || L.model || undefined,
          errorAlert: errorText ? errorText : undefined,
          timeStr: new Date().toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' }),
        });
      }
      // the answer is saved here: release the server's copy (and never save it again if this browser re-attaches)
      if (detachedRun && !errorText) {
        markRunSaved(agentRunId);
        apiFetch(`/agent/run/${encodeURIComponent(agentRunId)}/ack`, { method: 'POST' }).catch(() => {});
      }
    };

    // a reattach that found nothing: drop the empty bubble instead of leaving "Reconnecting..." on screen
    const failReattach = (gone: boolean) => {
      setRun((state) => ({ messages: state.messages.filter((m) => m.id !== assistantMsgId) }));
      set((st) => (st.permRequest && st.permRequest.sid === sidKey ? { permRequest: null } : {}));
      if (gone) markRunSaved(agentRunId);   // the server no longer has it: do not retry on every open
    };

    let reader: ReadableStreamDefaultReader<Uint8Array> | undefined;
    let detachedRun = false;   // the server accepted the run and keeps it going without this page
    try {
      const kbParam = kbMode === 'always' ? 'on' : kbMode;   // the server's values are auto | on | off
      const customAgentId = ca && ca.id != null && !isNaN(Number(ca.id)) ? Number(ca.id) : undefined;
      let res: Response;
      if (reattachId) {
        logger.info('Agent', 'Reattaching to run ' + reattachId);
        res = await apiFetch(`/agent/run/${encodeURIComponent(reattachId)}/events?after=-1`, { signal: runCtrl.signal, background: true });
      } else if (useAgent) {
        logger.info('Agent', 'Dispatching /agent/run SSE stream');
        const cloudBound = isCloud || Boolean(serverStatus?.cloud_main);
        res = await apiFetch('/agent/run', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({
            client_run_id: agentRunId,
            kb: kbParam,
            messages: msgsPayload,
            mode: cloudBound ? 'main-cloud-rest-local' : 'all-local',
            plan: get().agentMode === 'plan',
            permission_mode: get().agentMode,
            session_id: currentSessionId || null,
            reasoning_effort: reasoningEffort,
            custom_agent_id: customAgentId,
            verify: get().answerCheck,
            ...samplingBody({ customAgent: Boolean(ca) }),
            personal: appMode !== 'code' || undefined,
            voice: options?.voice || undefined,   // spoken turn: the server answers in short plain sentences
          }),
          signal: runCtrl.signal,
        });
      } else {
        logger.info('Chat', `Dispatching /chat/run SSE stream`);
        res = await apiFetch('/chat/run', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({
            client_run_id: agentRunId,
            messages: msgsPayload,
            session_id: currentSessionId || undefined,
            model: targetModelId || undefined,
            web_search: webSearchEnabled || deepResearch,
            kb: kbParam,
            deep_mode: deepResearch,
            voice: options?.voice || undefined,
            reasoning_effort: reasoningEffort,
            custom_agent_id: customAgentId,
            verify: get().answerCheck,
            ...samplingBody({ customAgent: Boolean(ca) }),
          }),
          signal: runCtrl.signal,
        });
      }

      reattachStatus = reattachId ? res.status : 0;
      detachedRun = res.ok && Boolean(res.headers.get('X-Run-Id'));
      if (!res.ok) {
        const errJson = await res.json().catch(() => ({}));
        throw new Error(errJson.error || errJson.detail || `HTTP ${res.status}`);
      }

      if (!res.body) {
        throw new Error('No response stream returned from /chat/run');
      }

      reader = res.body.getReader();
      const dec = new TextDecoder();
      let buf = '';

      while (true) {
        const { done, value } = await reader.read();
        if (done) break;

        buf += dec.decode(value, { stream: true });
        buf = buf.replace(/\r\n?/g, '\n');

        let i: number;
        while ((i = buf.indexOf('\n\n')) >= 0) {
          const raw = buf.slice(0, i);
          buf = buf.slice(i + 2);

          let ev = '';
          const dataLines: string[] = [];
          for (const line of raw.split('\n')) {
            if (line.startsWith('event:')) ev = line.slice(6).trim();
            else if (line.startsWith('data:')) dataLines.push(line.slice(5).replace(/^ /, ''));
          }

          if (!ev || dataLines.length === 0) continue;

          let data: any = {};
          try {
            data = JSON.parse(dataLines.join('\n'));
          } catch (_) {
            continue;
          }

          if (data.prompt_tokens) promptTokVal = data.prompt_tokens;
          if (data.tps) tpsVal = data.tps;
          if (data.ntok || data.completion_tokens) ntokVal = data.ntok || data.completion_tokens;
          if (data.secs) secsVal = data.secs;
          if (data.modelDisplay || data.model) modelDisplay = data.modelDisplay || data.model;

          {
            if (ev === 'done' || ev === 'error') agentSawTerminal = true;
            if (ev === 'error') agentFailed = true;
            let agentErr: Error | null = null;
            setRun((state) => {
              const msgs = [...state.messages];
              const target = msgs.find((m) => m.id === assistantMsgId);
              if (!target) return state;
              try {
                applyAgentEvent(target, agentState, ev, data, {
                  replay: Boolean(reattachId),
                  toast: (msg, type) => get().showToast(msg, type),
                  onPermission: (req) => set({ permRequest: { ...req, sid: sidKey } }),
                  onShell: {
                    start: (id, cmd) => get().addTermEntry({ id, cmd, source: 'agent', status: 'running', time: Date.now() }),
                    finish: (id, patch) => get().updateTermEntry(id, { status: 'done', ...patch }),
                  },
                });
              } catch (e: any) {
                agentErr = e;
              }
              return { messages: msgs };
            });
            if (agentErr) throw agentErr;
            // a replay re-sends old permission_request frames: one that has a later frame was answered long ago
            if (ev !== 'permission_request' && ['step', 'tool_call', 'tool_result', 'delta', 'delta_replace', 'thought_delta', 'done', 'error'].includes(ev)) {
              set((st) => (st.permRequest && st.permRequest.sid === sidKey ? { permRequest: null } : {}));
            }
            if (useAgent && !reattachId && ev === 'done' && agentState.runState !== 'completed') {
              get().showToast(`Run ${agentState.runState || 'stopped'}`, 'warn');
            }
            continue;
          }

        }
      }

      if (reattachId && !agentState.runId && !agentSawTerminal) {
        failReattach(false);
      } else if (useAgent) {
        // a stream that ends without `done` did not finish: that is an interruption, never a success
        finishAgentRun(agentSawTerminal ? null : 'interrupted', 'the connection to the server ended before the run finished');
      } else {
        finishChatRun('');
      }
    } catch (err: any) {
      const aborted = err.name === 'AbortError';
      try { reader?.cancel().catch(() => {}); } catch (_) { /* nothing to cancel */ }
      if (reattachId && !agentState.runId && !(getRunMsgs().find((m) => m.id === assistantMsgId)?.acts || []).length) {
        if (!aborted) logger.warn('Agent', 'Reattach failed: ' + (err.message || err));
        failReattach(reattachStatus === 404);
      } else if (useAgent) {
        const started = Boolean(agentState.runId || (getRunMsgs().find((m) => m.id === assistantMsgId)?.acts || []).length);
        if (!aborted) {
          setRun((state) => {
            const msgs = [...state.messages];
            const t = msgs.find((m) => m.id === assistantMsgId);
            if (t) t.content += (t.content ? ' ' : '') + '⚠️ ' + (err.message || 'Agent run failed');
            return { messages: msgs };
          });
        }
        // a server `error` event is a failed run, not a dropped connection
        if (started) finishAgentRun(aborted ? 'cancelled' : agentFailed ? 'failed' : 'interrupted', aborted ? 'you stopped the run' : err.message);
        else set((st) => (st.permRequest && st.permRequest.sid === sidKey ? { permRequest: null } : {}));
      } else if (!aborted && detachedRun) {
        // the connection dropped (a refresh, a network blip) but the server is still writing the answer: show nothing
        // wrong, and pick it up again. Nothing is saved from here; the re-attached run saves it when it ends.
        logger.warn('Chat', 'Connection lost mid-answer; re-attaching to the run on the server: ' + (err.message || err));
        setRun((state) => ({ messages: state.messages.filter((m) => m.id !== assistantMsgId) }));
        if (currentSessionId) window.setTimeout(() => { void get().reattachDetachedRuns(currentSessionId); }, 1500);
      } else {
        if (!aborted) logger.error('Chat', 'Error during /chat/run generation', err);
        // Stop keeps (and saves) the partial answer; an error shows its message
        finishChatRun(aborted ? '' : err.message || 'Failed to generate response');
      }
    } finally {
      releaseSession();
      const done = getRunMsgs().find((m) => m.id === assistantMsgId);
      // a newer run in the same chat (or Stop, which already cleared it) owns the shared state now
      const mine = runs.get(sidKey)?.ctrl === runCtrl;
      if (mine) {
        runs.delete(sidKey);
        set((st) => {
          const running = { ...st.runningSessions };
          delete running[sidKey];
          const active = sessionKey(st.activeSessionId) === sidKey;
          const bg = { ...st.bgMessages };
          if (!active) delete bg[sidKey];   // the finished answer is saved on the server; opening the chat loads it
          return { runningSessions: running, bgMessages: bg, ...(active ? { isStreaming: false, activeStreamingMessageId: null } : {}) };
        });
      }
      if (done && !reattachId && !(done.acts || []).some((a) => a.type === 'stopped' && a.reason === 'cancelled')) {
        // no answer text in the alert: it can show on a lock screen and stays in the OS notification history
        notifyTaskFinished({ title: useAgent ? 'Agent task finished' : 'Answer ready', body: 'Open the app to read it.' });
      }
    }
      })();
    } finally {
      release();
    }
  },

  abortGeneration: () => {
    const key = sessionKey(get().activeSessionId);
    if (!runs.has(key)) return;
    stopRun(key);
    set((st) => {
      const running = { ...st.runningSessions };
      delete running[key];
      return { runningSessions: running, isStreaming: false, activeStreamingMessageId: null, permRequest: st.permRequest?.sid === key ? null : st.permRequest };
    });
    logger.info('Chat', 'User aborted generation');
  },

  setActiveModal: (activeModal) => {
    if (activeModal === 'settings') {
      try {
        if (window.location.pathname !== '/settings' && !window.location.hash.includes('settings')) {
          window.history.pushState({ modal: 'settings' }, '', '/settings');
        }
      } catch (_) {}
    } else if (activeModal === 'report') {
      try {
        if (window.location.pathname !== '/report' && !window.location.hash.includes('report')) {
          window.history.pushState({ modal: 'report' }, '', '/report');
        }
      } catch (_) {}
    } else if (activeModal === 'audit') {
      try {
        if (window.location.pathname !== '/audit' && !window.location.hash.includes('audit')) {
          window.history.pushState({ modal: 'audit' }, '', '/audit');
        }
      } catch (_) {}
    } else if (activeModal === 'db') {
      try {
        if (window.location.pathname !== '/db' && !window.location.hash.includes('db')) {
          window.history.pushState({ modal: 'db' }, '', '/db');
        }
      } catch (_) {}
    } else if (activeModal === 'none') {
      try {
        if (['/settings', '/report', '/audit', '/db'].includes(window.location.pathname)) {
          window.history.pushState({}, '', '/');
        }
      } catch (_) {}
    }
    set({ activeModal });
  },
  openSettingsModal: (settingsTab = 'sec-theme') => {
    try {
      window.history.pushState({ modal: 'settings' }, '', `/settings#${settingsTab}`);
    } catch (_) {}
    set({ activeModal: 'settings', settingsTab });
  },
  openCustomizeModal: (customizeTab = 'skills') => {
    set({ activeModal: 'customize', customizeTab });
  },
}));
