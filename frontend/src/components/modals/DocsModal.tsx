import React, { useState, useEffect } from 'react';
import { useAppStore } from '../../stores/useAppStore';
import { apiFetch } from '../../services/api';

export const DocsModal: React.FC = () => {
  const { activeModal, setActiveModal, selectedModel, serverStatus } = useAppStore();
  const isOpen = activeModal === 'docs';

  const [copiedField, setCopiedField] = useState<string | null>(null);
  const [modelId, setModelId] = useState<string>('');
  const [isLoaded, setIsLoaded] = useState<boolean>(false);

  useEffect(() => {
    if (!isOpen) return;

    // Fetch live status from control API
    apiFetch('/control/status')
      .then((r) => r.json())
      .then((status) => {
        if (status && status.model) {
          setModelId(status.model);
          setIsLoaded(Boolean(status.pid || status.healthy));
        } else if (selectedModel) {
          setModelId(selectedModel);
          setIsLoaded(Boolean(serverStatus?.pid || serverStatus?.healthy));
        } else {
          setModelId('E:\\AI\\Models\\nemotron-3.5-lightning.gguf');
          setIsLoaded(false);
        }
      })
      .catch(() => {
        if (selectedModel) {
          setModelId(selectedModel);
          setIsLoaded(Boolean(serverStatus?.pid || serverStatus?.healthy));
        } else {
          setModelId('E:\\AI\\Models\\nemotron-3.5-lightning.gguf');
          setIsLoaded(false);
        }
      });
  }, [isOpen, selectedModel, serverStatus]);

  if (!isOpen) return null;

  const copyToClipboard = (text: string, fieldId: string) => {
    navigator.clipboard?.writeText(text);
    setCopiedField(fieldId);
    setTimeout(() => setCopiedField(null), 2000);
  };

  const pythonExample = `from openai import OpenAI

client = OpenAI(
    base_url="http://localhost:8000/v1",
    api_key="none",
)
resp = client.chat.completions.create(
    model="${modelId || '<paste the Model ID from step 3>'}",
    messages=[{"role": "user", "content": "Hello!"}],
)
print(resp.choices[0].message.content)`;

  const mcpExample = `"capabilities": {
  "mcp_servers": {
    "filesystem": {
      "transport": "stdio",
      "command": "npx",
      "args": ["-y", "@modelcontextprotocol/server-filesystem", "E:\\\\AI\\\\workspace"]
    },
    "remote": {
      "transport": "http",
      "url": "http://127.0.0.1:9000/mcp"
    }
  }
}`;

  const skillExample = `skills/my-skill/SKILL.md
---
name: my-skill
description: One line summary shown to the agent.
triggers: keyword1, keyword2
---
Full instructions the agent reads via read_skill.`;

  const pluginExample = `def register(api):
    api.add_system_prompt("Always do X.")
    api.add_tool("my_tool", my_fn, "Description shown to the agent.",
                 {"type": "object", "properties": {}})
    api.on_event("after_tool", lambda name, args, result: None)`;

  return (
    <div
      id="docs-modal-overlay"
      className="fixed inset-0 z-50 flex items-center justify-center bg-black/70 backdrop-blur-sm select-none p-4 animate-in fade-in"
      onClick={() => setActiveModal('none')}
    >
      <div
        id="docs-modal-container"
        className="relative flex flex-col rounded-2xl border border-border bg-panel shadow-2xl overflow-hidden w-full max-w-3xl max-h-[90vh] animate-in zoom-in-95"
        style={{ padding: 0 }}
        onClick={(e) => e.stopPropagation()}
      >
        {/* Modal Header */}
        <div className="flex items-center justify-between px-6 py-3.5 border-b border-border bg-panel2 flex-shrink-0">
          <div className="flex items-center gap-3">
            <div className="w-8 h-8 rounded-lg bg-accent/15 border border-accent/30 flex items-center justify-center text-accent text-base flex-shrink-0">
              📖
            </div>
            <div>
              <div className="text-sm font-bold text-text leading-tight">Connect an External Agent</div>
              <div className="text-[11px] text-text-muted mt-0.5">
                Step-by-step developer guide: connect Continue, Cline, LibreChat, or custom SDK agents
              </div>
            </div>
          </div>
          <button
            className="w-7 h-7 rounded-lg border border-border flex items-center justify-center text-text-muted hover:text-text hover:bg-panel transition-colors flex-shrink-0"
            id="docs-close"
            onClick={() => setActiveModal('none')}
            title="Close (Esc)"
          >
            ✕
          </button>
        </div>

        {/* Modal Body: All 6 Steps */}
        <div className="p-6 overflow-y-auto flex flex-col gap-6 text-xs text-text">
          {/* Step 1: Load a model */}
          <div className="flex gap-4">
            <div className="w-6 h-6 rounded-full bg-accent text-white flex items-center justify-center font-bold text-xs flex-shrink-0 mt-0.5">
              1
            </div>
            <div className="flex-1">
              <div className="flex items-center gap-2">
                <b className="text-sm text-text font-bold">Load a model</b>
                {isLoaded ? (
                  <span className="px-2 py-0.5 rounded-full text-[10px] font-bold bg-emerald-500/15 text-emerald-400 border border-emerald-500/30">
                    ● Model Loaded
                  </span>
                ) : (
                  <span className="px-2 py-0.5 rounded-full text-[10px] font-bold bg-amber-500/15 text-amber-400 border border-amber-500/30">
                    ○ Server Offline / Idle
                  </span>
                )}
              </div>
              <p className="text-text-muted mt-1 leading-relaxed">
                Pick a model from the picker at the bottom of the composer and press <b>▶ Load</b>. The engine must show loaded before external agents can query endpoints.
              </p>
            </div>
          </div>

          {/* Step 2: Base URL */}
          <div className="flex gap-4">
            <div className="w-6 h-6 rounded-full bg-accent text-white flex items-center justify-center font-bold text-xs flex-shrink-0 mt-0.5">
              2
            </div>
            <div className="flex-1">
              <b className="text-sm text-text font-bold">Point your agent at the base URL</b>
              <div className="flex items-center gap-2 my-2">
                <code
                  id="doc-base-url"
                  className="bg-bg font-mono px-3 py-1.5 rounded-lg border border-border text-accent text-xs flex-1 truncate"
                >
                  http://localhost:8000/v1
                </code>
                <button
                  type="button"
                  className="px-3 py-1.5 rounded-lg bg-panel2 border border-border hover:bg-panel text-text font-medium text-xs flex items-center gap-1 transition-colors flex-shrink-0"
                  onClick={() => copyToClipboard('http://localhost:8000/v1', 'base-url')}
                >
                  {copiedField === 'base-url' ? '✓ Copied' : '📋 Copy'}
                </button>
              </div>
              <p className="text-text-muted leading-relaxed">
                OpenAI-compatible. Works directly with any client that accepts an OpenAI base URL — Continue.dev, Cline, LibreChat, Open WebUI, LangChain, LlamaIndex, or official OpenAI SDKs.
              </p>
            </div>
          </div>

          {/* Step 3: Model ID */}
          <div className="flex gap-4">
            <div className="w-6 h-6 rounded-full bg-accent text-white flex items-center justify-center font-bold text-xs flex-shrink-0 mt-0.5">
              3
            </div>
            <div className="flex-1">
              <b className="text-sm text-text font-bold">Use this exact Model ID</b>
              <div className="flex items-center gap-2 my-2">
                <code
                  id="doc-model-id"
                  className="bg-bg font-mono px-3 py-1.5 rounded-lg border border-border text-emerald-400 text-xs flex-1 truncate"
                  title={modelId}
                >
                  {modelId || '(load a model first — its path becomes the Model ID)'}
                </code>
                <button
                  type="button"
                  className="px-3 py-1.5 rounded-lg bg-panel2 border border-border hover:bg-panel text-text font-medium text-xs flex items-center gap-1 transition-colors flex-shrink-0"
                  onClick={() => copyToClipboard(modelId || 'nemotron-3.5-lightning', 'model-id')}
                >
                  {copiedField === 'model-id' ? '✓ Copied' : '📋 Copy'}
                </button>
              </div>
              <p className="text-text-muted leading-relaxed">
                This is the <b>currently loaded model's file path</b>. Most agents require it in the <code>model</code> parameter. No authorization token required on localhost — use <code>api_key="none"</code> if the SDK demands a non-empty key.
              </p>
            </div>
          </div>

          {/* Step 4: Python SDK Example */}
          <div className="flex gap-4">
            <div className="w-6 h-6 rounded-full bg-accent text-white flex items-center justify-center font-bold text-xs flex-shrink-0 mt-0.5">
              4
            </div>
            <div className="flex-1">
              <div className="flex items-center justify-between">
                <b className="text-sm text-text font-bold">Example: OpenAI Python SDK</b>
                <button
                  type="button"
                  className="text-[11px] px-2.5 py-1 rounded bg-panel2 border border-border hover:bg-panel text-text flex items-center gap-1"
                  onClick={() => copyToClipboard(pythonExample, 'python-code')}
                >
                  {copiedField === 'python-code' ? '✓ Copied' : '📋 Copy Code'}
                </button>
              </div>
              <pre className="bg-bg font-mono text-[11.5px] p-3.5 rounded-xl border border-border mt-2 overflow-x-auto text-zinc-300 leading-relaxed">
                {pythonExample}
              </pre>
            </div>
          </div>

          {/* Step 5: Verification & Monitor */}
          <div className="flex gap-4">
            <div className="w-6 h-6 rounded-full bg-accent text-white flex items-center justify-center font-bold text-xs flex-shrink-0 mt-0.5">
              5
            </div>
            <div className="flex-1">
              <b className="text-sm text-text font-bold">Verify it's working</b>
              <p className="text-text-muted mt-1 leading-relaxed">
                Open <b>📡 Realtime Monitor</b> in the top navigation cluster — your agent's calls will stream in real time showing TTFT, generation speed (tokens/sec), KV cache hits, and VRAM bandwidth. All activity is logged in <b>📊 Report</b>.
              </p>
            </div>
          </div>

          {/* Step 6: Extend with Capabilities */}
          <div className="flex gap-4">
            <div className="w-6 h-6 rounded-full bg-accent text-white flex items-center justify-center font-bold text-xs flex-shrink-0 mt-0.5">
              6
            </div>
            <div className="flex-1 flex flex-col gap-3">
              <div>
                <b className="text-sm text-text font-bold">Extend the agent with capabilities</b>
                <p className="text-text-muted mt-0.5">
                  Managed in <b>Settings → 🧩 Capabilities</b> (persists to <code>config/app.json</code>).
                </p>
              </div>

              {/* Web Browsing */}
              <div className="bg-panel2/60 border border-border/80 rounded-xl p-3">
                <div className="font-bold text-xs text-text flex items-center gap-1.5">
                  <span>🌐</span>
                  <span>Web browsing</span>
                </div>
                <p className="text-text-muted text-[11.5px] mt-1 leading-relaxed">
                  Built-in zero-config tools: <code>web_search</code> (DuckDuckGo, keyless) and <code>web_fetch</code> for retrieving page text.
                </p>
              </div>

              {/* Skills */}
              <div className="bg-panel2/60 border border-border/80 rounded-xl p-3">
                <div className="flex items-center justify-between">
                  <div className="font-bold text-xs text-text flex items-center gap-1.5">
                    <span>🎯</span>
                    <span>Custom Skills</span>
                  </div>
                  <button
                    type="button"
                    className="text-[10.5px] px-2 py-0.5 rounded bg-panel border border-border text-text hover:bg-panel2"
                    onClick={() => copyToClipboard(skillExample, 'skill-example')}
                  >
                    {copiedField === 'skill-example' ? '✓ Copied' : '📋 Copy'}
                  </button>
                </div>
                <pre className="bg-bg font-mono text-[11px] p-2.5 rounded-lg border border-border mt-2 text-zinc-300">
                  {skillExample}
                </pre>
              </div>

              {/* MCP Servers */}
              <div className="bg-panel2/60 border border-border/80 rounded-xl p-3">
                <div className="flex items-center justify-between">
                  <div className="font-bold text-xs text-text flex items-center gap-1.5">
                    <span>🔌</span>
                    <span>Model Context Protocol (MCP)</span>
                  </div>
                  <button
                    type="button"
                    className="text-[10.5px] px-2 py-0.5 rounded bg-panel border border-border text-text hover:bg-panel2"
                    onClick={() => copyToClipboard(mcpExample, 'mcp-example')}
                  >
                    {copiedField === 'mcp-example' ? '✓ Copied' : '📋 Copy'}
                  </button>
                </div>
                <pre className="bg-bg font-mono text-[11px] p-2.5 rounded-lg border border-border mt-2 text-zinc-300">
                  {mcpExample}
                </pre>
              </div>

              {/* Plugins */}
              <div className="bg-panel2/60 border border-border/80 rounded-xl p-3">
                <div className="flex items-center justify-between">
                  <div className="font-bold text-xs text-text flex items-center gap-1.5">
                    <span>🧩</span>
                    <span>Python Plugins</span>
                  </div>
                  <button
                    type="button"
                    className="text-[10.5px] px-2 py-0.5 rounded bg-panel border border-border text-text hover:bg-panel2"
                    onClick={() => copyToClipboard(pluginExample, 'plugin-example')}
                  >
                    {copiedField === 'plugin-example' ? '✓ Copied' : '📋 Copy'}
                  </button>
                </div>
                <pre className="bg-bg font-mono text-[11px] p-2.5 rounded-lg border border-border mt-2 text-zinc-300">
                  {pluginExample}
                </pre>
              </div>
            </div>
          </div>
        </div>

        {/* Modal Footer */}
        <div className="px-6 py-3 border-t border-border bg-panel2 flex justify-end flex-shrink-0">
          <button
            type="button"
            className="px-4 py-1.5 rounded-lg border border-border bg-panel hover:bg-panel2 text-text text-xs font-semibold transition-colors"
            onClick={() => setActiveModal('none')}
          >
            Close Guide
          </button>
        </div>
      </div>
    </div>
  );
};
