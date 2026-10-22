// Slash commands and @file mentions (port of the menu logic in static/js/agent-cmd.js).
import { apiFetch } from './api';
import { cachedMediaStatus, mediaReady } from './media';

export interface CmdItem {
  icon: string;
  name: string;
  desc?: string;
  category?: 'mode' | 'utility' | 'create' | 'skills' | 'library' | 'custom_agents';
  template?: boolean;
  isSkill?: boolean;
  isLibrary?: boolean;
  agent?: any;
  value?: string; // @file path
}

export const CMD_GROUP_LABELS: Record<string, string> = {
  mode: 'Mode',
  utility: 'Utility',
  create: 'Create',
  skills: 'Skills',
  library: 'Agent Library',
  custom_agents: 'Custom Agents',
};

export const TEMPLATES: Record<string, string> = {
  subagent: 'Please delegate the following to a sub-agent: ',
  parallel: 'Please dispatch parallel sub-agents to investigate simultaneously:\n1. Audit: \n2. Audit: ',
  multiagent: 'Please delegate this task to multiple sub-agents (use parallel dispatch if independent):\n1. planner: \n2. coder: \n3. reviewer: ',
};

/** The @ or / token ending at the caret: "/com" or "@src/ma". */
export function currentToken(value: string, caret: number): { ch: '@' | '/'; text: string; start: number } | null {
  const upto = value.slice(0, caret);
  const m = upto.match(/(?:^|[\s[({<,;:"'])([@/])([^\s@/\],>)}:;]*)$/);
  return m ? { ch: m[1] as '@' | '/', text: m[2], start: caret - m[2].length - 1 } : null;
}

let capsCache: { at: number; data: any } | null = null;
let libraryNames = new Set<string>();

async function capabilities(): Promise<any> {
  if (capsCache && Date.now() - capsCache.at < 30000) return capsCache.data;
  try {
    const r = await apiFetch('/control/capabilities');
    const data = r.ok ? await r.json() : {};
    capsCache = { at: Date.now(), data };
    return data;
  } catch (_) {
    return {};
  }
}

/** Agent Library command names, so /name works even before the slash menu was opened. */
export async function loadLibraryNames(): Promise<void> {
  try {
    const r = await apiFetch('/agent/commands');
    if (!r.ok) return;
    const d = await r.json();
    libraryNames = new Set((d.items || []).map((c: any) => String(c.name).toLowerCase()));
  } catch (_) {
    // the menu fills it in later
  }
}

export function isLibraryCommand(name: string): boolean {
  return libraryNames.has(String(name || '').toLowerCase());
}

export async function slashItems(query: string, codeMode: boolean, customAgents: any[]): Promise<CmdItem[]> {
  const base: CmdItem[] = codeMode
    ? [
        { icon: '📋', name: 'plan', desc: 'switch to Plan mode (read-only, propose)', category: 'mode' },
        { icon: '🔨', name: 'build', desc: 'switch to Build mode (execute)', category: 'mode' },
        { icon: '🎯', name: 'goal', desc: 'autonomous goal execution, drive to production readiness', category: 'mode' },
        { icon: '🧭', name: 'init', desc: 'scan the project and write AGENTS.md (loaded into agent tasks)', category: 'utility' },
        { icon: '🧹', name: 'compact', desc: 'compress conversation history', category: 'utility' },
        { icon: '🤖', name: 'subagent', desc: 'delegate a sub-task to a focused sub-agent', category: 'utility', template: true },
        { icon: '👥', name: 'parallel', desc: 'dispatch several independent sub-agents at once', category: 'utility', template: true },
        { icon: '🧩', name: 'multiagent', desc: 'delegate planner, coder and reviewer roles in one prompt', category: 'utility', template: true },
      ]
    : [
        { icon: '📋', name: 'plan', desc: 'switch to Plan mode (read-only, propose)', category: 'mode' },
        { icon: '🔨', name: 'build', desc: 'switch out of Plan mode', category: 'mode' },
        { icon: '🎯', name: 'goal', desc: 'autonomous goal execution, drive to completion', category: 'utility' },
        { icon: '🧹', name: 'compact', desc: 'compress conversation history', category: 'utility' },
      ];
  // /image and /video are listed in both modes; the hint says when they still need setting up
  const ms = await cachedMediaStatus();
  base.push(
    { icon: '🎨', name: 'image', desc: mediaReady('image', ms) ? 'make a picture from a description' : 'make a picture (set up a model in Settings first)', category: 'create' },
    { icon: '🎬', name: 'video', desc: mediaReady('video', ms) ? 'make a short video clip' : 'make a video (set up a model in Settings first)', category: 'create' },
  );
  const has = (n: string) => base.some((x) => x.name.toLowerCase() === n.toLowerCase());
  const d = await capabilities();
  ((d.skills && d.skills.items) || []).forEach((s: any) => {
    if (!has(s.name)) base.push({ icon: '🎯', name: s.name, desc: s.description || '', isSkill: true, category: 'skills' });
  });
  if (codeMode) {
    const lib = (d.agent_library && d.agent_library.commands) || [];
    libraryNames = new Set(lib.map((c: any) => String(c.name).toLowerCase()));
    lib.forEach((c: any) => {
      if (!has(c.name)) base.push({ icon: '📚', name: c.name, desc: c.description || '', isLibrary: true, category: 'library' });
    });
  }
  customAgents
    .filter((a) => a.scope === 'mine' && a.slug)
    .forEach((a) => {
      if (!has(a.slug)) base.push({ icon: a.icon || '🤖', name: a.slug, desc: `${a.name}: ${a.description || 'Custom Agent'}`, agent: a, category: 'custom_agents' });
    });
  const q = query.toLowerCase();
  return base.filter((i) => !q || i.name.toLowerCase().includes(q)).slice(0, 30);
}

export async function fileItems(query: string): Promise<CmdItem[]> {
  try {
    const r = await apiFetch('/agent/workspace');
    const d = await r.json();
    const q = query.toLowerCase();
    return (d.files || [])
      .filter((f: any) => !q || String(f.path).toLowerCase().includes(q))
      .slice(0, 12)
      .map((f: any) => ({ icon: '📄', name: f.path, desc: typeof f.size === 'number' ? `${(f.size / 1024).toFixed(1)} KB` : '', value: f.path }));
  } catch (_) {
    return [];
  }
}

/** @path tokens are replaced by the file's text (code mode), up to 5 files. */
export async function expandAtTags(text: string): Promise<string> {
  if (!text.includes('@')) return text;
  // whole tokens: "@src/app.ts" but not "user@example.com" or the start of "@a.json" when "@a.js" is meant
  const re = /(^|[\s([{,;:"'])@([^\s@,;:)\]}"']+\.[\p{L}\p{N}]+)(?=$|[\s,;:!?)\]}"'])/gu;
  const tags = Array.from(new Set([...text.matchAll(re)].map((m) => m[2]))).slice(0, 5);
  if (!tags.length) return text;
  const files = new Map<string, string>();
  for (const t of tags) {
    try {
      const r = await apiFetch('/agent/ws/file?path=' + encodeURIComponent(t));
      const d = await r.json();
      if (!r.ok || d.error) continue;
      const body = String(d.content || '');
      files.set(t, body.length > 12000 ? body.slice(0, 12000) + `\n... (truncated, ${body.length} chars)` : body);
    } catch (_) {
      // leave the tag as it is
    }
  }
  return text.replace(re, (whole, pre, path) => (files.has(path) ? `${pre}see file "${path}":\n${files.get(path)}` : whole));
}

export async function expandLibraryCommand(name: string, args: string): Promise<string> {
  const r = await apiFetch('/agent/command/expand', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ name, args }),
  });
  const d = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(d.error || `HTTP ${r.status}`);
  return String(d.prompt || '');
}

export async function initPrompt(extra: string): Promise<string> {
  const r = await apiFetch('/agent/project_instructions');
  const d = r.ok ? await r.json() : null;
  if (!d || !d.init_prompt) throw new Error('Could not load the /init prompt from the server');
  return extra ? `${d.init_prompt}\n\nExtra focus from the user: ${extra}` : d.init_prompt;
}
