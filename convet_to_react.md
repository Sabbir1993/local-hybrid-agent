# React Frontend Migration Plan (`convet_to_react.md`)

This document outlines the detailed architectural roadmap and execution plan for migrating the local AI server's frontend from vanilla HTML/CSS/JavaScript (`ui.html`, `settings.html`, `login.html`, and `static/js/*.js`) to a modern **React + TypeScript + Vite + Tailwind CSS** Single Page Application (SPA).

---

## 1. Objectives & Architectural Guardrails

### Primary Goals
1. **Component-Driven Architecture**: Replace raw string template literals and ad-hoc DOM queries (`document.querySelector`, innerHTML concatenation) with reusable, typed React components.
2. **Deterministic State Management**: Replace 15+ disconnected global JS state files with typed, reactive Zustand stores.
3. **High-Throughput Streaming**: Handle rapid LLM token generation (30–60+ tokens/sec over SSE) without React re-render thrashing.
4. **Native Companion Compatibility**: Maintain seamless integration with the Electron Companion app and its `window.electronAPI` bridge.
5. **Zero-Friction Deployment**: Ensure production users can still run the server with a single command (`.\scripts\start.ps1` / `./scripts/start.sh`) **without needing Node.js installed in production**.

### Non-Negotiable Requirements
* **No Regression in Electron Desktop App**: `companion/preload.js` and all native bridge functions (`browseFolder`, `browseDir`, `mkdir`, `notify`, `flashFrame`) must function identically.
* **Cookie-Based Authentication**: Support session cookies (`a770_session`, `a770_csrf`) with automatic CSRF token inclusion.
* **FastAPI Compatibility**: FastAPI will serve the compiled static build (`dist/`) in production with client-side SPA routing fallback.

---

## 2. Recommended Technology Stack

| Layer | Selection | Justification |
| :--- | :--- | :--- |
| **Framework & Build** | **Vite + React 18 / 19 + TypeScript** | Instant HMR in dev; compiles down to optimized, static HTML/JS/CSS bundles for production. |
| **Styling & Theme** | **Tailwind CSS + CSS Variables** | Preserves the existing dark/light theme design system (`theme.css`) with utility classes. |
| **State Management** | **Zustand** | Lightweight (<2KB), unopinionated, zero-boilerplate, and supports subscription outside React render cycles (crucial for streaming). |
| **Server State & Caching** | **TanStack Query (React Query)** | Manages polling, cache invalidation, and data fetching for models, settings, and MCP configurations. |
| **Icons & UI Primitives** | **Lucide React + Radix UI Primitives** | Accessible dialogs, dropdowns, tooltips, and sliders matching the current Claude/Antigravity design language. |
| **Markdown & Math** | **react-markdown + remark-gfm + rehype-katex** | Zero-latency streaming Markdown and LaTeX rendering with GitHub Flavored Markdown support. |
| **Virtualization** | **@tanstack/react-virtual** | Smooth scrolling for massive chat histories and large agent tool step lists. |

---

## 3. Target Project Directory Structure

A new `frontend/` directory is introduced at the project root. The FastAPI backend remains in Python, serving the pre-compiled `frontend/dist/` in production:

```text
a770-dual-runtime/
├── frontend/                   # Modern React Frontend Root
│   ├── package.json
│   ├── tsconfig.json
│   ├── vite.config.ts
│   ├── tailwind.config.js
│   ├── index.html              # Main SPA HTML shell
│   └── src/
│       ├── main.tsx            # App bootstrap & providers
│       ├── App.tsx             # Root router & layout
│       ├── types/              # TypeScript API & Domain contracts
│       │   ├── api.d.ts        # Models, routes, request/response types
│       │   ├── companion.d.ts  # Electron window.electronAPI declarations
│       │   ├── chat.d.ts       # SSE event types & chat messages
│       │   └── agent.d.ts      # Agent steps, tool calls, checkpoints
│       ├── stores/             # Zustand State Stores
│       │   ├── useAuthStore.ts
│       │   ├── useSessionStore.ts
│       │   ├── useModelStore.ts
│       │   ├── useGpuStatusStore.ts
│       │   ├── useAgentStore.ts
│       │   └── useWorkspaceStore.ts
│       ├── hooks/              # Custom React Hooks
│       │   ├── useCompanion.ts # Native Electron bridge wrapper
│       │   ├── useChatStream.ts# High-performance SSE reader
│       │   ├── useStatusWs.ts  # Real-time /ws/status WebSocket manager
│       │   └── useTheme.ts     # System / Dark / Light theme manager
│       ├── services/           # HTTP & API Communication Layer
│       │   ├── client.ts       # Fetch wrapper with CSRF & credentials: "include"
│       │   ├── chatApi.ts
│       │   ├── controlApi.ts
│       │   └── projectsApi.ts
│       └── components/         # Reusable Component Hierarchy
│           ├── common/         # Buttons, Modals, Inputs, Tooltips
│           ├── layout/         # Header, Navigation, Sidebar, SplitView
│           ├── chat/           # ChatArea, MessageBubble, StreamingText, ReasoningBlock
│           ├── agent/          # AgentStepsList, ToolCallCard, ApprovalModal, DiffViewer
│           ├── dock/           # ClaudeDock (Preview, Terminal, Artifact, Workspace)
│           ├── status/         # GpuGauge, VramBar, ModelResidencyBadge
│           ├── models/         # ModelSelector, ModelConfigDrawer, EffortSlider
│           └── settings/       # MCPManager, CodeRunnerPairing, ProviderKeys
├── static/                     # Legacy static files (kept until cutover complete)
├── routes/pages.py             # Updated to serve frontend/dist/ in production
└── scripts/
    ├── build_frontend.ps1     # One-time build helper
    └── build_frontend.sh
```

---

## 4. Phase-by-Phase Migration Strategy

### Phase 1: Tooling & Development Pipeline Setup
1. **Initialize Vite Application**:
   * Create `frontend/` with Vite, React, TypeScript, and Tailwind CSS.
   * Configure `vite.config.ts` with a development proxy:
     ```typescript
     server: {
       port: 5173,
       proxy: {
         '/api': 'http://127.0.0.1:8000',
         '/control': 'http://127.0.0.1:8000',
         '/chat': 'http://127.0.0.1:8000',
         '/agent': 'http://127.0.0.1:8000',
         '/ws': { target: 'ws://127.0.0.1:8000', ws: true }
       }
     }
     ```
2. **FastAPI Dual-Mode Serving**:
   * Modify `routes/pages.py` to check for `frontend/dist/index.html`.
   * If `frontend/dist/` exists, serve the React app; otherwise fallback to legacy `ui.html`. This ensures zero downtime during development.

---

### Phase 2: Core State Layer & API Foundations
Replace fragmented global JavaScript variables with isolated, reactive Zustand stores:

1. **`useAuthStore`**: Handles logged-in principal, session validation, MFA state, and login redirects.
2. **`useGpuStatusStore`**: Manages the `/ws/status` connection:
   * Real-time VRAM allocation (GPU 1, GPU 2, System RAM).
   * Active llama-server process status, residency state, and health checks.
3. **`useModelStore`**:
   * Current model, provider selection (local vs cloud).
   * Launch configuration (context size, tensor split, split mode, KV cache type, `--n-cpu-moe`).
   * Reasoning effort and thinking budget matrix.
4. **`useSessionStore`**:
   * Active conversation thread, message history, session list, search filter.
5. **`useWorkspaceStore`**:
   * Project directory tree, file selection, git status, diff tracking.

---

### Phase 3: High-Performance SSE Streaming Layer

> [!IMPORTANT]
> **Preventing the "React Re-Render Trap"**:
> Naive React code updates state on every streaming chunk (`setText(prev => prev + chunk)`), resulting in 60+ re-renders per second that freeze the UI.

#### The High-Performance Streaming Pattern:
1. **Bypass React state during active generation**:
   * Chunks are accumulated in a plain JavaScript buffer or `useRef`.
   * Directly write tokens to an unmanaged DOM container ref, or throttle React state updates to **16ms (60 FPS)** using `requestAnimationFrame`.
2. **Commit on completion**:
   * When the SSE stream emits `[DONE]` or `turn_complete`, commit the full accumulated markdown string to the Zustand session store once.
3. **Isolated Component Scoping**:
   * Keep the active streaming token bubble in an isolated component (`<StreamingMessage />`) so typing updates do not cause the entire chat history or sidebar to re-render.

---

### Phase 4: Component Implementation & Feature Porting

Port UI modules from legacy `static/js/` into modular React components:

1. **Chat Area (`static/js/chat.js` → `components/chat/`)**:
   * Markdown rendering with syntax highlighting and code copy buttons.
   * Collapsible `<ReasoningBlock />` for thinking models (DeepSeek-R1, Gemma-Reasoning).
   * Integrated action hover buttons and context actions.
2. **Model Control & Hardware Drawer (`controls.js`, `gpu-status.js` → `components/models/`)**:
   * Dual-GPU VRAM gauges with live memory breakdown (Weights vs KV Cache vs Compute).
   * Context budget calculator (warning user if 96k context requires MoE CPU RAM offload).
   * Model launch parameter drawer (tensor split, unified KV pool, mmap).
3. **Agent Workspace & Steps List (`agent-run.js`, `claude-dock.js` → `components/agent/`)**:
   * Fixed-height scrollable step tree with theme scrollbars.
   * Real-time file diff viewer with syntax-highlighted side-by-side comparison.
   * Confirmation modals for native tool approvals.
4. **Projects & Files Rail (`projects.js` → `components/dock/`)**:
   * Collapsible directory tree with file icons and modified badges.
   * Right-click context menus for creating, renaming, and deleting files.

---

### Phase 5: Electron Companion Integration

Create a typed hook to interface with the native desktop Companion:

```typescript
// frontend/src/hooks/useCompanion.ts
export function useCompanion() {
  const isNative = typeof window !== 'undefined' && Boolean(window.electronAPI?.isNativeApp);
  const deviceId = window.electronAPI?.deviceId || null;

  const browseFolder = async (initialDir = ''): Promise<string | null> => {
    if (window.electronAPI?.browseFolder) {
      return await window.electronAPI.browseFolder(initialDir);
    }
    return null;
  };

  const showNotification = (title: string, body: string) => {
    window.electronAPI?.notify?.({ title, body });
    window.electronAPI?.flashFrame?.(true);
  };

  return {
    isNative,
    deviceId,
    browseFolder,
    showNotification,
  };
}
```

---

### Phase 6: Settings & Administrative Panels
Migrate `settings.html`:
* **Code Runner Management**: View pairing status, trigger self-tests, enter pairing codes for remote runners or WSL2.
* **Provider Keys**: Keychain-backed credential inputs for OpenAI, Anthropic, Gemini, Groq, and OpenRouter.
* **MCP Server Management**: Add, toggle, and configure stdio / SSE Model Context Protocol servers.
* **Supervision Limits**: Sliders for restart caps, watchdog timeouts, and memory limits.

---

## 5. Deployment & Production Build Workflow

To preserve the zero-dependency Python deployment for end users:

1. **Build Artifacts Committed or Bundled**:
   * Developers run `npm run build` inside `frontend/`, which compiles the app into static files in `frontend/dist/`.
   * The `frontend/dist/` directory can be checked into git or packaged with releases.
2. **Production Startup (`start.ps1` / `start.sh`)**:
   * FastAPI mounts `frontend/dist` at the web root:
     ```python
     if Path("frontend/dist/index.html").exists():
         app.mount("/", StaticFiles(directory="frontend/dist", html=True), name="spa")
     ```
   * End users run `.\scripts\start.ps1` and get the modern React UI with **no Node.js requirement**.

---

## 6. Migration Risk Analysis & Mitigation

| Potential Risk | Root Cause | Mitigation Strategy |
| :--- | :--- | :--- |
| **Token Streaming Latency** | Virtual DOM diffing during 60+ token/s bursts. | Use `requestAnimationFrame` chunk throttling + isolated leaf streaming components. |
| **Companion Bridge Breakdown** | Renamed or lost `window.electronAPI` calls. | Define strict TypeScript interfaces in `companion.d.ts`; mock bridge in web dev. |
| **Session Cookie Loss** | Fetch calls omitting cookies across origins. | Enforce `credentials: "include"` on all HTTP/WebSocket requests. |
| **Increased Repository Weight** | Checking in `node_modules`. | Keep `frontend/node_modules` in `.gitignore`; only commit source and optional production `dist/`. |

---

## 7. Next Steps for Implementation (When Ready to Build)

1. Create `frontend/` directory with `vite` template.
2. Copy CSS variables and design tokens from `static/css/theme.css` into Tailwind theme config.
3. Implement `types/` and `stores/` for Auth and WebSocket status.
4. Build the core layout and Chat stream container.
5. Wire FastAPI to serve `frontend/dist/index.html`.
6. Run side-by-side verification with both the web browser and native Electron Companion.
