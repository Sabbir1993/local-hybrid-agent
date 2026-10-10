# Changelog

Entries are kept short on purpose: this is a local single-dev project and long
per-commit prose rots faster than it helps. Versioning starts where the repo
has a tag or release; until then, dates + headline changes.

## 2026-10-08 — preview of server-generated PDFs / HTML / images from agent tool cards

- A tool card's 👁 Preview says "workspace" (the user's device), but files the server generates (PDF, Office, images)
  live in the server's common space. Text previews already fell back to the common space; the PDF, HTML and image
  viewers did not, so a generated PDF showed "File Not Found On Disk" although it existed. They now try the
  workspace once and fall back to `/agent/raw`, and the "New Tab" link follows the URL that worked.

## 2026-10-08 — generated PDFs get real tables; no empty checkboxes

- PDFs written from Markdown (`core/agent_tools/dom_convert.py`) used a line-by-line converter, so a table with
  blank lines between its rows (how models often write them) came out as raw `| a | b |` text. Now `markdown-it-py`
  (already installed and hash-locked; declared in requirements.txt) renders the document: GitHub tables incl.
  alignment, nested lists, quotes, strikethrough; raw HTML in the text is escaped. A pre-pass joins table rows split
  by blank lines (code fences untouched). The no-browser ReportLab fallback now draws real tables too.
- Root cause of "PDFs still look plain": the headless-browser step never succeeded on current Edge, so every
  PDF silently came from the ReportLab fallback (no theme, no tables, raw backticks). Edge exits at once and writes
  the PDF ~1 s later (we checked too early), and `--blink-settings=scriptEnabled=false` makes `--print-to-pdf` hang
  without writing anything. `_render_html_to_pdf` now waits for the file to appear and stop growing, and the flag is
  gone (scripts stay blocked by the page's CSP `script-src 'none'`). Needs a server restart.
- `- [ ]` / `- [x]` lines: the empty box is dropped in chat and PDFs (it meant nothing); a done item keeps a muted tick.

## 2026-10-08 — question cards only answerable on the newest message

- The "Decision Options" cards (static/js/chat.js) stayed clickable on old messages after a reload or when another
  chat was opened: the "already answered" flag lived in memory, keyed by message position, and was shared across chats.
  Now a card is live only while its message is the newest one; anything after it makes it read-only
  ("Earlier question (closed)"), and the card state is reset when you switch chats.
- A list with more than 12 choices (e.g. a 74-line checklist) is no longer turned into a decision form.

## 2026-10-08 — chat renders full Markdown

- Replies now render GitHub-style Markdown: tables (scrollable, aligned columns), `>` quotes, `---` rules, real
  headings, nested/ordered/task lists, *italic*, ~~strike~~ and bare links. Before, only bold, inline code and
  simple bullets worked and every newline became `<br>`.
- Parser: `marked` 18.1.0 + sanitiser `DOMPurify` 3.4.16, vendored in `static/vendor/` with SRI hashes (same way as
  mermaid / xlsx; both verified against their npm registry integrity hashes, licences alongside). Glue in
  `static/js/markdown.js`: raw HTML from the model is shown as text, links / images / `[DOWNLOAD:]` / `[VIDEO:]` /
  code blocks are built by our code and added after sanitising, finished blocks are cached while streaming and an
  unfinished `**` / `` ` `` is closed for display only. If the libraries fail to load, the old renderer is used.
- Checks: `tests/js/test_markdown.js` (node), `tests/js/markdown_browser_check.html` (real DOMPurify in a browser).

## 2026-10-08 — web only for what the knowledge base cannot answer

- Chat now looks at how well the knowledge base covers the question before offering the web
  (`core/kb_coverage.py`): full -> no web tools (saves ~1.3k tokens/turn), partial -> a ~150-token "fill the
  gap" hint and a small call budget, none -> web only when the question asks for it. The grey zone is settled by
  one tiny call to the small local model (falls back to "partial"). Replaces the "force web when the KB is blocked" patch.
- The web-call budget grows with reasoning effort and Deep mode (`chat.web_budget`: low/medium/high/deep x
  full/partial/open; deep + nothing in the KB gets 20), the tool-round limit follows, repeated searches are
  skipped and late results trimmed. Settings -> Knowledge Base -> "Web next to the knowledge base" edits it.
- Agent mode: `tool_surface.web_on_demand` (off by default) offers the web tools only when needed and caps calls per run.

## 2026-10-08 — knowledge base: what cloud models may read

- The all-or-nothing "KB never goes to cloud" block is now per source and per chunk, still local-only by
  default. Layer 1: admin-defined categories (Public / Internal / HR / ...) each marked "cloud may read",
  every source filed under one (sources with no category keep their own switch). Layer 2: sensitive-content
  rules (salary, IDs, bank accounts, phone/email, card numbers, credentials + admin keyword/regex rules)
  hold individual chunks back at retrieval time, so edits apply at once with no reindex. Rules only restrict.
- A question whose hits include any non-cleared source or held-back chunk is answered by the local model as
  before; the search tool on a cloud lane returns only cleared, rule-clean chunks.
- UI: Settings -> Knowledge Base ("What cloud models may read" card + per-source category picker);
  `/knowledge/policy`, `/knowledge/categories`, `/knowledge/rules`, `/knowledge/{id}/category`.
  Code: `core/knowledge_rules.py`, `core/knowledge_access.py`, `static/js/knowledge-policy.js`.

## 2026-10-08 — video attachments

- Attaching a video samples frames (scene changes + an even grid, <= 16, <= 10 min) with ffmpeg on
  the server (`POST /media/video`, `core/media/video.py`), describes each with the Image reader
  (`/agent/vision`) and transcribes the audio track; the result is prompt text with timestamps.
  No ffmpeg: the browser samples frames instead (`static/js/video-frames.js`). Temp files are
  deleted after each call; nothing is stored or logged. Limits: `media.max_video_mb`,
  `media.video_understanding`.
- Unsupported binaries (zip, exe...) now say so instead of "max size is 512 KB".
- Fixed: `describe_image_bytes` raised UnboundLocalError (import order) after the vision token cap change;
  image descriptions use `vision_max_tokens`.

## 2026-10-08 — vision capability is a flag, "Reading images" is automatic

- Lanes carry a `vision` capability: image-reader lanes, the executor with an mmproj, local main
  started with an mmproj, and cloud models marked 👁 (per model in Cloud Models, or per cloud lane).
- "Reading images" defaults to Automatic: main if it can see, else the helper, else the dedicated
  Image reader (own model + port). The Image reader shows "Not needed" and never starts when another
  model already reads images. An explicit pick in Models & Jobs still wins.
- Editable from Settings: executor "can also read images" toggle (one model, one port), cloud
  "can read images" marker, and an "Automatic" option on the Reading images job.

## 2026-10-07 — executor serves vision from one port

- `registry()` now lets a builtin lane's kind be overridden by config, so the
  executor lane (`kind: "vision"` + `mmproj`) serves both routine tool calls
  and image reading from a single llama-server process. Shared `role_map` maps
  the vision job to the executor lane for all users. Repo config runs Ornith
  1.5-9B (Q4 + its mmproj) as that lane; the separate Qwen-VL vision lane is
  disabled. Switch back = clear executor kind + re-enable the vision lane.

## 2026-10-07 — lane fallback switch, per-lane VRAM, OpenCode-style cards

- Lanes: `fallback_enabled` (0/1, all lanes) — 0 means "no fallback"; a failing
  lane ends the call with a clear error instead of silently escalating to main.
  Exposed as a checkbox in Models & Jobs (local + cloud). A vision-kind lane
  already serves chat jobs, so one vision-capable model (gguf + mmproj) can be
  the helper AND vision from a single port; README documents the recipe.
- `GET /control/vram/lanes`: per-lane estimated weights/KV/total via the same
  plan_launch preflight (the numbers that decide fit). The Settings GPU panel
  now draws a per-GPU lane stack (weights vs KV).
- Agent streaming UI (OpenCode/Antigravity style): render-diff — a running
  agent updates only the acts block per SSE chunk instead of re-rendering the
  whole bubble. Tool arguments collapse by default with a click-to-expand
  toggle; long results capped with a scroll; the stopped banner gets a status
  dot + step/time chips; a tool call auto-collapses the still-open thinking
  card. Card chrome classed, not inline-styled.

## 2026-10-07 — frontend D-phase status

- D4 (checkpoint rail UI) shipped with Phase A2: the git panel lists checkpoints
  per run with confirm-gated Rewind, wired to /git/checkpoints + /git/rewind.
- D1 (inline style -> classes) and D3 (icon-button labels) deferred. Survey:
  131 inline style attrs but 93 unique with no heavy repeat (top = 19
  `display:none;`), and the app already uses `data-tip` tooltips on most icon
  buttons. Both are cosmetic churn with real regression risk on a single-file
  template - not worth the diff until the UI is test-covered the way the
  checkpoint rail now is. The JS lint gate ships these later safely.

## 2026-10-07 — git checkpoints + rewind (Phase A2)

- `core/agent_loop/checkpoint.py`: per-run git checkpoints via `git stash
  create` (records a commit without touching the worktree), stored in a
  `checkpoints` table (survives restart - the old in-memory undo died with the
  process). Rewind = `git reset --hard` onto a ref this module recorded; any
  other ref is refused. Pruned to checkpoint_max per (user, session).
- Agent loop snapshots before the first write; `/git/checkpoints` +
  `/git/rewind` routes + a Checkpoints rail in the git panel with Rewind
  buttons (confirm-gated). Checkpoint no-ops silently when the workspace is
  not a git repo or the companion can't run git.

## 2026-10-07 — git panel companion-routed (was disabled)

- `core/git_tools` now routes every git call through the companion
  (`git.run` RPC + `companion/gitops.js`), so the panel works on the user's
  machine instead of the server's disk (it was disabled by design before).
  Output parsers unchanged; `require_device_workspace` supplies the cwd.
- Defense in depth, both sides: destructive git args (`reset --hard`,
  `clean -f`, `push --force`, `branch -D`, `filter-branch`, `update-ref`,
  ...) refused by the server before an RPC and re-refused by the companion.
  Guard unit-tested on both sides (`tests/test_git_routes.py`,
  `tests/js/test_companion_gitops.js`).

## 2026-10-07 — code intel: TypeScript/TSX parsed

- `tree-sitter-typescript` added to the hash-locked deps. `.ts`/`.tsx` are no
  longer "counted, never parsed": the TS grammar is a node-name superset of JS,
  so the existing JS walker covers it (class/method names via
  `type_identifier`/`property_identifier`). Eval code slice extended with TS +
  TSX questions (def/callers/outline) against new fixtures
  (`tests/fixtures/code_repo/types.ts`, `widget.tsx`); a regression that
  re-skips TS makes the gate fail. Scope honesty unchanged: structure-aware
  grep, not type resolution (that is the optional LSP tier).

## 2026-10-06 — docs hygiene + SWE-bench harness

- Plan/spec docs consolidated to one live roadmap; stale halves archived to
  `docs/history/`; doc-reference integrity enforced by a test (every
  backticked code path in tracked docs must exist, with implied-parent
  resolution for `core/`, `config/`, `static/js/`).
- `scripts/run_swebench.py`: SWE-bench Lite grading harness reusing the live
  eval's login/SSE plumbing. Grade = gold test patch: resolved iff every
  FAIL_TO_PASS green and no PASS_TO_PASS regressed, Wilson ci95 rollup. The
  grading/loader/parser core is unit-tested (`tests/test_swebench_harness.py`)
  and runs in CI; the number itself needs a GPU + model + Companion and is
  documented as an operator runbook in README.

## 2026-10-06 — frontend: offline deps, JS lint gate, two found bugs

- Mermaid 10.9.3 + SheetJS 0.20.3 vendored to `static/vendor/` (same bytes,
  same SRI as the CDN) so diagrams/spreadsheets work fully offline - this is a
  local-first product and the CDN broke that promise.
- ESLint `no-undef` gate on `static/js` (globals computed from source so the
  list cannot rot), wired into CI + pre-commit. `npm ci` + `npm run lint` on
  node 24.
- The gate found two real dead paths: `input-guard.js` called a never-defined
  `showToast` (the "rules saved" toast never appeared); `gpu-status.js` read a
  never-assigned `APP_MODELS` (executor ctx ceiling always fell through).
  Fixed + pinned by `tests/js/test_lint_fixes.js`.
- Skipped: moving 131 inline `style=` attrs to CSS classes (cosmetic, no
  behavior change) and `tsc --checkJs` (wrong tool for a global-scope app;
  `no-undef` is the right check).

## 2026-10-06 — parallel reads + subagent envelope hardening + RAG config

- Agent loop: the parallel fast path now covers all-read steps
  (`PARALLEL_READ_TOOLS` allow-list), not just the `spawn_agent` fan-out.
  `read_file`/`grep`/`web_fetch`/`search_memory` batches run concurrently;
  writes, code, permission tools stay strictly sequential.
- Sub-agent envelope: `subagent_result_verdict` no longer lets a successful
  child's quoted `[sub-agent ...]` body line flip it to failure (only the
  first line votes for a single child; parallel scans all lines).
- Risk-register R10 closed: a child's file writes are dropped from the
  parent's `_ws_changes` diff/undo set when the child finishes
  (`core/subagent/scope.py::drop_child_write_tracking`). Sibling files survive.
- Retrieval: `_SYNONYM_MAP` and `COMPANY_KEYWORDS` moved under
  `config/app.json -> knowledge.{synonyms,company_keywords}` (defaults stay in
  code; deployments add their own vocabulary without a code change).
  `core/memory/indexing.py` coverage 16% -> 86%.
- Deferred: container sandbox. The spawn seam is
  `companion/shellops.js` (Electron, runs on the user's machine); no server-side
  sandbox can work there (TIER1_SANDBOX_SPIKE). Docker is not used by this
  project (policy: no Docker); containment is process-tree kill plus, later,
  native Windows Job Objects in the companion.

## 2026-10-06 — eval hygiene + CI green

- CI gates now pass at HEAD: removed 3 dead-code lint violations, added
  `GET /favicon.ico` to the reviewed `PUBLIC_HTTP` set, and made 3 test
  modules runnable standalone (`_source` import path).
- Coverage floor raised 55 -> 65 (measured 67, branch).
- Pre-commit hooks: ruff (CI's exact ruleset/version) + the auth-invariant
  route walk, so a new public endpoint cannot land unnoticed.
- Live eval harness now bootstraps the project it needs: after login it
  creates/activates the `__eval__` project (the recorded 0/3 run was 100%
  `agent_workspace_unavailable` because no project was selected), preflights
  the Companion connection, requires an absolute `--live-workspace`, and can
  write/compare a `tests/eval_live_baseline.json`.
- Docs: the 4 competing plan docs and the stale A1–A17 half of
  `PROJECT_KNOWLEDGE.md` archived to `docs/history/`; README/kitchen-sink
  path references corrected (packages were referenced as files);
  `tests/eval_results*.json` git-ignored (closes risk-register R5);
  `.claude/skills` untracked (byte-identical copy of `.agents/skills`);
  doc-reference integrity test added.
- New files: `CHANGELOG.md`, `CONTRIBUTING.md`, `SECURITY.md`,
  `.pre-commit-config.yaml`, `tests/test_doc_refs.py`.

## 2026-09-29/10-02 — package split + eval gates

- `routes/{agent,chat,control,capabilities,projects,proxy}` and
  `core/{agent_loop,agent_tools,vram,memory,mcp,lanes,subagent,verifier}`
  split from monoliths into packages.
- Mock eval baseline + offline gate + JS tests wired into CI; pip-audit gate.
- Tree-sitter AST index, unified-diff `edit_file`, lane-health circuit
  breaker, router auto-tuner with rollback + audit.

## 2026-09-13 — agent mode

- Autonomous multi-step tool-using loop (plan mode, shell permission flow,
  web search, skills, MCP), persistable sessions.

## 2026-09-12 — baseline

- Dual-A770 Vulkan runtime manager: llama-server process control, model
  hot-swap, VRAM preflight, web UI + OpenAI-compatible proxy.
