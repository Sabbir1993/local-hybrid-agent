"""Generate diagram/system_architecture_full.svg: the full system architecture poster.

Run:  python diagram/gen_system_architecture.py
PNG:  msedge --headless --screenshot=diagram/system_architecture_full.png \
        --window-size=2600,<height> file:///.../system_architecture_full.svg
"""
import os
import textwrap
from xml.sax.saxutils import escape

OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "system_architecture_full.svg")

W = 2600
MARGIN = 30
MAIN_W = 1880          # left main column
SIDE_X = MARGIN + MAIN_W + 30
SIDE_W = W - SIDE_X - MARGIN
LH = 17                # line height
FS = 12.5              # body font size
CHAR_W = 6.55          # approx px per char at FS

PAL = {
    "client":  ("#E6F1FB", "#185FA5"),
    "edge":    ("#FCEBEB", "#A32D2D"),
    "router":  ("#F1EFE8", "#5F5E5A"),
    "agent":   ("#EEEDFE", "#534AB7"),
    "tools":   ("#E1F5EE", "#0F6E56"),
    "know":    ("#FAEEDA", "#854F0B"),
    "guard":   ("#FBEAF0", "#993556"),
    "runtime": ("#EAF3DE", "#3B6D11"),
    "infer":   ("#FAECE7", "#993C1D"),
    "hw":      ("#E8E8E8", "#2C2C2A"),
    "store":   ("#E1F5EE", "#085041"),
    "ext":     ("#E6F1FB", "#0C447C"),
    "flow":    ("#F4F0FF", "#3C3489"),
}

svg = []


def t(x, y, s, size=FS, weight="normal", fill="#2C2C2A", anchor="start", family=None, style=""):
    fam = f' font-family="{family}" xml:space="preserve"' if family else ""
    svg.append(f'<text x="{x:.1f}" y="{y:.1f}" font-size="{size}" font-weight="{weight}" '
               f'fill="{fill}" text-anchor="{anchor}"{fam}{style}>{escape(s)}</text>')


def wrap(lines, w):
    """Wrap bullet lines to box width; '•' prefix gets a hanging indent."""
    cols = max(10, int((w - 24) / CHAR_W))
    out = []
    for ln in lines:
        if ln.startswith("§"):          # sub-heading
            out.append(("h", ln[1:]))
            continue
        bullet = not ln.startswith("  ")
        body = ln.strip()
        parts = textwrap.wrap(body, cols - 2) or [""]
        for i, p in enumerate(parts):
            out.append(("b" if (bullet and i == 0) else "c", p))
    return out


def measure(w, lines, title=True):
    return (34 if title else 12) + len(wrap(lines, w)) * LH + 10


def box(x, y, w, title, lines, kind, h=None, sub=None, mono=False):
    fill, stroke = PAL[kind]
    rows = wrap(lines, w) if not mono else [("m", ln) for ln in lines]
    hh = h or ((34 if title else 12) + len(rows) * LH + 10)
    svg.append(f'<rect x="{x}" y="{y}" width="{w}" height="{hh}" rx="8" fill="{fill}" '
               f'stroke="{stroke}" stroke-width="1.2"/>')
    cy = y + 22
    if title:
        svg.append(f'<rect x="{x}" y="{y}" width="{w}" height="30" rx="8" fill="{stroke}"/>')
        svg.append(f'<rect x="{x}" y="{y + 20}" width="{w}" height="10" fill="{stroke}"/>')
        t(x + 12, y + 20, title, size=14, weight="bold", fill="#FFFFFF")
        if sub:
            t(x + w - 10, y + 20, sub, size=11.5, fill="#FFFFFF", anchor="end")
        cy = y + 30 + LH
    for kind_, s in rows:
        if kind_ == "h":
            t(x + 12, cy, s, weight="bold", fill=stroke)
        elif kind_ == "b":
            t(x + 12, cy, "•", fill=stroke, weight="bold")
            t(x + 24, cy, s)
        elif kind_ == "m":
            t(x + 12, cy, s, size=12, family="Consolas, 'Courier New', monospace")
        else:
            t(x + 24, cy, s)
        cy += LH
    return hh


def band(y, h, label, color, x=MARGIN, w=MAIN_W):
    svg.append(f'<rect x="{x}" y="{y}" width="{w}" height="{h}" rx="12" fill="none" '
               f'stroke="{color}" stroke-width="1.5" stroke-dasharray="6 4"/>')
    lw = len(label) * 8.2 + 24
    svg.append(f'<rect x="{x + 16}" y="{y - 12}" width="{lw}" height="24" rx="12" fill="{color}"/>')
    t(x + 28, y + 5, label, size=13, weight="bold", fill="#FFFFFF")


def row(y, items, x0=MARGIN + 16, total_w=MAIN_W - 32, gap=14):
    """items: list of (weight, title, lines, kind[, sub]). Equal-height row. Returns height."""
    tw = sum(i[0] for i in items)
    avail = total_w - gap * (len(items) - 1)
    widths = [avail * i[0] / tw for i in items]
    h = max(measure(wd, it[2]) for wd, it in zip(widths, items))
    x = x0
    for wd, it in zip(widths, items):
        box(round(x), y, round(wd), it[1], it[2], it[3], h=h, sub=it[4] if len(it) > 4 else None)
        x += wd + gap
    return h


def column(x, y, w, items, gap=12):
    cy = y
    for it in items:
        cy += box(x, cy, w, it[0], it[1], it[2], sub=it[3] if len(it) > 3 else None) + gap
    return cy - y - gap


def arrow(x1, y1, x2, y2, label=None, color="#444441", dash=False, lx=None, ly=None):
    d = ' stroke-dasharray="5 4"' if dash else ""
    svg.append(f'<line x1="{x1}" y1="{y1}" x2="{x2}" y2="{y2}" stroke="{color}" stroke-width="2"'
               f'{d} marker-end="url(#ah)"/>')
    if label:
        t(lx if lx is not None else x1 + 8, ly if ly is not None else (y1 + y2) / 2 + 4,
          label, size=11.5, fill=color, weight="bold")


# ───────────────────────────── content ──────────────────────────────
y = 30
t(MARGIN, y + 26, "Local Agent  —  a770-dual-runtime  ·  Full System Architecture", size=32, weight="bold")
t(MARGIN, y + 56, "Self-hosted, multi-user, PCI-DSS-aware LLM platform  ·  FastAPI control plane :8000  ·  "
  "llama.cpp Vulkan b10840 on 2× Intel Arc A770 16 GB  ·  sd.cpp + whisper.cpp media  ·  "
  "Windows 11 (primary) + Linux  ·  branch linux @ 2d3b7b8  ·  2026-09-28", size=15, fill="#5F5E5A")
# legend
lx = MARGIN
ly = y + 78
for k, name in [("client", "Clients"), ("edge", "Edge / security"), ("router", "HTTP routers"),
                ("agent", "Agent & orchestration"), ("tools", "Tools & integrations"),
                ("know", "Knowledge / media / cloud"), ("guard", "Guards"), ("runtime", "Runtime control"),
                ("infer", "Inference plane"), ("hw", "Hardware"), ("store", "Storage"), ("ext", "External")]:
    f, s = PAL[k]
    svg.append(f'<rect x="{lx}" y="{ly}" width="16" height="16" rx="3" fill="{f}" stroke="{s}" stroke-width="1.5"/>')
    t(lx + 22, ly + 13, name, size=12.5)
    lx += 30 + len(name) * 7.2 + 20
y = ly + 50

# ── 1. Clients
band_y = y
y += 24
h = row(y, [
    (1.25, "Web SPA — ui.html", [
        "Vanilla JS, 40 modules in static/js, no bundler; CSS style/lanes/media",
        "Chat mode: web search, attachments, @ mentions, / commands",
        "Agent mode: Plan / Build, workspace tree, diff viewer, preview pane",
        "Image / video studio (media.js); mic dictation → 16 kHz WAV",
        "Answer-check shield, reasoning-effort selector, /compact, export",
        "Git panel, custom-agent picker, monitor, usage report, audit log, DB explorer, GPU pill",
        "CDN (CSP-allowed): mermaid 10.9.3, SheetJS xlsx 0.20.3",
    ], "client", "browser"),
    (1.0, "Settings — settings.html", [
        "iframe, 15 tabs:",
        "Model config · Runtime · GPUs · Sampling",
        "Models & Jobs (lanes) · Cloud · Capabilities / MCP",
        "Custom agents · Knowledge · Input/Output guard",
        "Users & RBAC · Devices (companion) · DB · Theme · Danger zone",
        "login.html: session login + password change",
    ], "client"),
    (1.3, "Companion — Electron 30, v0.2.0", [
        "WS /ws/companion; auth = paired device key (HMAC of machine id) or session cookie; Origin header rejected",
        "fs.* read/write/edit/list/grep/tree/mkdir/browse · shell.run",
        "browser.* (11): Playwright Edge/Chrome, throwaway profile",
        "android.* (14): adb / emulator · ios.* (9): simctl + idb (macOS)",
        "oauth.loopback · companion.capabilities",
        "policy.js: confirmations; refuses password, cc-*, Luhn-valid input",
        "NSIS (Win) / DMG (mac); electron-updater (update URL = placeholder)",
    ], "client", "user device"),
    (0.95, "API clients", [
        "OpenAI-compatible /v1/chat/completions, /slots, /metrics passthrough",
        "Bearer PAT a770_pat_… (sha256-stored, scoped, default 30 d, max 90 d)",
        "Swagger /docs behind login",
        "Agent workspaces live ONLY on the user device (no server workspace)",
    ], "client"),
])
y += h + 20
band(band_y, y - band_y, "1 · CLIENT LAYER", PAL["client"][1])
cl_bottom = y
y += 44

# ── 2. Edge
band_y = y
y += 24
h = row(y, [
    (1.15, "Uvicorn / FastAPI — server_manager.py", [
        "0.0.0.0:8000 (--host / A770_HOST), proxy_headers=True",
        "§Lifespan startup",
        "FK repair (db_repair) → super-admin bootstrap → legacy migrations",
        "Register tools: builtin, web, skills, shell, file, media, browser, device, plugins",
        "Kill orphan llama-servers",
        "§Background tasks",
        "MCP connect · watchdog 5 s · keepalive 25 s · small-model reaper · memory task · router tuner",
    ], "edge", "entrypoint"),
    (1.0, "SecurityHeadersMiddleware (pure ASGI)", [
        "CSP default-src 'self'; frame-ancestors 'self'; object-src 'none'",
        "X-Frame-Options SAMEORIGIN, nosniff, Referrer-Policy, Permissions-Policy",
        "HSTS on https; sandbox CSP for /agent/raw previews",
        "SSE passes through unbuffered",
        "Plain-HTTP warning when not on localhost (PCI 4.2.1)",
    ], "edge"),
    (1.0, "Authentication — core/auth.py", [
        "Argon2 password hashes",
        "Cookie a770_session (sha256 stored); idle 15 min (PCI 8.2.8), absolute 7 d",
        "Lockout 10 fails → 30 min; per-IP 30 fails / 15 min (10k IPs tracked)",
        "CSRF double-submit: a770_csrf + X-CSRF-Token",
        "API tokens (PAT); swappable auth_provider (SSO stub)",
    ], "edge"),
    (0.9, "Authorization — RBAC", [
        "19 permission keys: chat.use, model.local.load, knowledge.manage, database.manage, git.push, custom_agents.publish …",
        "Built-in roles admin / user + custom roles",
        "Privileged actions → audit_log",
    ], "edge"),
    (0.9, "Admission / fair share", [
        "routes/common.py _Admission",
        "Global concurrency = n_slots (queue_enabled)",
        "max_inflight_per_user = 1",
        "Slot affinity id_slot = uid % n_slots",
        "Cloud media daily caps (image/video per day)",
    ], "edge"),
])
y += h + 20
band(band_y, y - band_y, "2 · EDGE, AUTH & ADMISSION", PAL["edge"][1])
edge_top, edge_bottom = band_y, y
y += 44

# ── 3. Routers (chips)
band_y = y
y += 26
chips = [
    ("auth", "/auth · 4"), ("api_tokens", "/admin/api-tokens · 3"), ("api_docs", "/docs"),
    ("control", "/control · 17"), ("projects", "/projects · 17"), ("capabilities", "· 11"),
    ("cloud", "/control/cloud · 8"), ("lanes", "/control/lanes · 10"), ("media", "/media · 5"),
    ("chat", "/chat/run, /compact"), ("agent", "/agent · 19 (SSE)"), ("git", "/git · 10"),
    ("mcp_manager", "/mcp · 16 + OAuth"), ("plugins", "/plugins · 5"), ("customize", "/customize · 8"),
    ("admin_rbac", "/admin · 14"), ("knowledge", "/knowledge · 10"), ("input_guard", "· 4"),
    ("db_explorer", "/db · 4"), ("custom_agents", "/custom-agents · 7"),
    ("companion_bridge", "WS /ws/companion, /pair"), ("proxy", "/v1/* → :8090"),
]
per_row = 11
cw = (MAIN_W - 32 - 10 * (per_row - 1)) / per_row
fill, stroke = PAL["router"]
for i, (name, sub) in enumerate(chips):
    cx = MARGIN + 16 + (i % per_row) * (cw + 10)
    cy = y + (i // per_row) * 58
    svg.append(f'<rect x="{cx:.1f}" y="{cy}" width="{cw:.1f}" height="48" rx="6" fill="{fill}" stroke="{stroke}"/>')
    t(cx + cw / 2, cy + 20, name, size=13, weight="bold", anchor="middle")
    t(cx + cw / 2, cy + 37, sub, size=11.5, fill="#5F5E5A", anchor="middle")
y += 2 * 58 + 14
t(MARGIN + 16, y, "22 routers in routes/ · SSE events (/agent/run): step, lane, thought(_delta), delta, delta_reset, "
  "tool_call, verify, permission_request, tool_result, plan, validated, done, queued · "
  "media jobs: queued → progress → done/error · core/sse.py PAN-masks tool events", size=12.5, fill="#444441")
y += 18
band(band_y, y - band_y, "3 · HTTP API SURFACE — routes/", PAL["router"][1])
y += 44

# ── 4. Core services — 4 columns
band_y = y
y += 24
cols_w = (MAIN_W - 32 - 3 * 14) / 4
xA = MARGIN + 16
xB, xC, xD = [xA + i * (cols_w + 14) for i in (1, 2, 3)]
cw4 = round(cols_w)
hA = column(xA, y, cw4, [
    ("Agent loop", [
        "routes/agent.py (2078 L) + core/agent_loop.py",
        "Plan / Build modes; plan state in plan_items",
        "Recovers text-written tool calls; 3-tier retry",
        "max_steps 60; chat 12 tool rounds / 8 web calls (deep 20 / 12)",
        "Permission prompts streamed as SSE",
    ], "agent", "core"),
    ("Lane registry & jobs — core/lanes.py", [
        "Lanes: main · executor · vision · embedder + admin local lanes + per-user cloud lanes",
        "§13 jobs → default lane",
        "agent.reason → main",
        "agent.tool_step, summarize, commit_msg, subagent, verify → executor",
        "search_rewrite, input_guard, embed → local_only",
        "vision → vision · image_gen, video_gen, transcribe → media",
        "Fallback: mapped (cloud→local) → lane fallback → job default → main",
        "Presets: Private · Balanced · Best quality · Main only",
    ], "agent"),
    ("Router — policy / tuner / log", [
        "CPU classifier laya 421M ModernBERT (alt cactus_needle 45M), confidence 0.85",
        "Keyword categories, repeat_streak_limit 2",
        "router_tuner proposes rules after ≥30 samples; admin applies",
        "Telemetry → usage.db (no prompt text, 90-day retention)",
        "Executor uses GBNF grammar; weak output escalates to main (delta_reset)",
    ], "agent"),
    ("Answer check — verifier.py", [
        "Modes: off · badge · check-before-showing",
        "Strict-JSON verdict pass / fail / unverified; fail-open",
    ], "agent"),
    ("Reasoning, sub-agents & roles", [
        "Effort budgets: low 1024 · medium 4096 · high 12288 · extra ∞",
        "spawn_agent depth ≤ 1",
        "roles.json: planner, coder, reviewer, multi-analyzer / architect / reviewer",
        "/init writes AGENTS.md (project_context)",
    ], "agent"),
    ("Custom agents & Agent Library", [
        "user_custom_agents: tool allowlist enforced via contextvar, preferred_lane, input template, public sharing",
        ".agents: 43 profiles · ~75 commands · 14 skills; default deny, allowlist 14 agents / 21 commands",
        "multi_lanes: backend → main, frontend → executor",
    ], "agent"),
])
hB = column(xB, y, cw4, [
    ("Tool registry — core/registry.py", [
        "Single registry; sources: builtin · web · skill · shell · mcp__<srv>__<tool> · plugin__<name>__<tool> · per-user tools",
    ], "tools", "core"),
    ("Builtin & document tools", [
        "agent_tools.py (1795 L): file tools, run_python, list_diff / revert, analyze_image, search_memory, plan tools",
        "read_file_chunk: xlsx, xls, csv, pdf, pptx, docx",
        "doc_ops/: pptx_ops (979 L), xlsx_ops (701), docx_ops (417), csv_ops, md_ops → doc_inspect / doc_edit / doc_create; versions in doc_files",
    ], "tools"),
    ("Shell gate — shell_tools.py", [
        "allow · project · always · deny; 180 s approval wait",
        "Allow-patterns per user, forbidden list; shell operators need explicit approval",
    ], "tools"),
    ("Device & browser tools", [
        "browser_tools · device_tools → companion_bridge (60 s timeout)",
        "Workspace is device-only (WorkspaceAccessDenied → 403)",
    ], "tools"),
    ("MCP client — core/mcp.py", [
        "Hand-rolled; stdio + streamable HTTP; protocol 2025-03-26",
        "Global: echo (stdio), isms (npx mcp-remote)",
        "Personal servers in auth.db; non-admins: mcp-atlassian / mcp-remote only",
        "mcp_oauth: OAuth 2.1 + PKCE S256, RFC 9728 / 8414 discovery, tokens in keychain, refresh < 60 s",
        "GitHub connector: RFC 8628 device flow or PAT",
    ], "tools"),
    ("Web search — web_search.py", [
        "Keyless: SearXNG (optional) + DuckDuckGo HTML + Bing HTML",
        "Reciprocal-rank fusion + nomic embedder rerank",
        "Query PII redaction; region bd-en",
    ], "tools"),
    ("Skills, plugins & git", [
        "Skills: SKILL.md names only in prompt; read_skill ≤ 12k chars; 30 in skill_catalog, installed via /customize",
        "Plugins: bd_finance, devtools, json_tools, pci_masker, text_codec",
        "git_tools (local) · git_ai commit messages via executor · GitHub PR via MCP",
    ], "tools"),
])
hC = column(xC, y, cw4, [
    ("Knowledge ingest — knowledge_ingest.py", [
        "Text, URL (SSRF-guarded), file + chunked upload (/upload/chunk, /complete)",
        "pdfplumber, DOCX, XLSX extractors",
        "Chunks 900 chars / 150 overlap; embed batch 16 → nomic :8093",
        "knowledge_sources + per-role visibility (knowledge_source_role_access)",
        "PAN scan on upload disabled by policy; scripts/scrub_pans.py for cleanup",
    ], "know", "RAG"),
    ("Retrieval — core/memory.py", [
        "Hybrid cosine + lexical (search_memory_hybrid)",
        "ACL filter BEFORE scoring (knowledge_access)",
        "min_cos 0.45 · auto-inject 0.55",
        "knowledge_router detects organizational questions",
        "cloud_policy local_only — KB never sent to cloud",
    ], "know"),
    ("Media — media.py (881 L)", [
        "sd.cpp native async /sdcpp/v1/img_gen | vid_gen",
        "img2img / edit: ≤10 refs, 10 MB each / 40 MB total, resized ≤1536 px, EXIF stripped, local only",
        "Cloud image/video: OpenAI-style APIs, Google Imagen / Veo (video asks before spend)",
        "STT local whisper; cloud audio off unless media.allow_cloud_audio",
        "Output → common/user_<id>/generated/",
    ], "know"),
    ("Cloud client — core/cloud.py", [
        "Per-user OpenAI-compatible providers (OpenRouter-style gateways, Google Gemini)",
        "Keys in OS keychain; strips llama-only fields",
        "PAN mask on egress (CloudClient._prepare); fallback_local configurable",
    ], "know"),
    ("Chat, projects & memory", [
        "projects / sessions / messages (meta JSON)",
        "/chat/compact → compacts/; memory_background_task",
        "Usage accounting → usage.db requests (tokens, t/s, status)",
    ], "know"),
])
hD = column(xD, y, cw4, [
    ("PAN guard — core/pan.py", [
        "13–19 digits, any separators incl. Bengali numerals + Luhn",
        "pan_input: block · pan_output: mask · pan_cloud_egress: mask",
        "SSE tool events masked (core/sse.py)",
    ], "guard", "PCI"),
    ("Input / output guard", [
        "Scopes: cloud_only · block_all · role / user bound",
        "Active input rule: block sensitive attachment to cloud",
        "Output: rolling redaction window; rule “never share personal info” (role user)",
    ], "guard"),
    ("SSRF guard — core/net_guard.py", [
        "Applied to every server-side fetch + each redirect (web, KB URL, marketplace)",
    ], "guard"),
    ("Runtime manager", [
        "process.py build_launch_command: -dev VulkanN, --tensor-split, -ctk/-ctv, -np, -ncmoe, --jinja, --spec-type draft-mtp",
        "backend.py: presets vulkan [1,2] / cuda [0]; LLAMA_RUNTIME override",
        "state.py: watchdog 5 s, health timeout 120 s, restart backoff ≤ 60 s",
        "Keepalive 1-token ping / 25 s (WDDM idle demotion: 7.7 → 14.4 t/s)",
        "vram.py: preflight mode block, 1.0 GB headroom, compute_tensor_split",
        "gpu.py perf counters (4 s cache); small_model.py spawn + reaper",
    ], "runtime", "control"),
    ("autotune.py", [
        "llama-bench sweep of tensor-split / --n-cpu-moe → profile[\"tuned\"]",
    ], "runtime"),
    ("OpenAI proxy — routes/proxy.py", [
        "Catch-all /v1/* → 127.0.0.1:8090; auto-starts the selected model",
    ], "runtime"),
])
y += max(hA, hB, hC, hD) + 20
band(band_y, y - band_y, "4 · CORE SERVICES — core/ (~60 modules)", PAL["agent"][1])
core_bottom = y
y += 44

# ── 5. Inference plane
band_y = y
y += 24
h = row(y, [
    (1.35, "main — llama-server", [
        "127.0.0.1:8090 · Vulkan1 + Vulkan2, split_mode layer",
        "Default: Ornith-1.5-9B Q4_K_M (split 15,5); big models via tensor split + CPU offload",
        "-np = n_slots (currently 1 → serial generation)",
        "batch 2048 / ubatch 512, KV q8_0, MTP draft where supported",
    ], "infer", ":8090"),
    (1.0, "executor", [
        "“Fast helper” — Spark-X2.5-4B Q4_K_M",
        "GPU1 · ctx 32576 · np 1 · KV q8_0",
        "fallback → main",
    ], "infer", ":8091"),
    (1.0, "vision", [
        "Qwen2.5-VL-3B-Instruct Q4_K_M + mmproj f16",
        "GPU1 · ctx 16384 · np 1",
    ], "infer", ":8092"),
    (1.0, "embedder", [
        "nomic-embed-text-v1.5 Q8_0",
        "GPU1 · ctx 16384 · batch 16",
    ], "infer", ":8093"),
    (1.2, "image_gen — sd.cpp sd-server", [
        "z_image_turbo Q4_K + Qwen3-4B-Instruct-2507 Q4_K_M (text enc) + ae.safetensors",
        "GPU2 (Vulkan) · 12 steps, cfg 2.0, euler · manual load, never auto-unloaded",
    ], "infer", ":8095"),
    (1.0, "stt — whisper-server", [
        "ggml-large-v3-turbo q5_0",
        "CPU · language auto (Bangla + English)",
    ], "infer", ":8096"),
    (0.9, "CPU router", [
        "laya 421M / cactus_needle 45M",
        "in-process · 0 VRAM",
    ], "infer", "in-proc"),
])
y += h + 20
band(band_y, y - band_y, "5 · INFERENCE PLANE — local model servers", PAL["infer"][1])
inf_top, inf_bottom = band_y, y
y += 44

# ── 6. Hardware
band_y = y
y += 24
h = row(y, [
    (0.8, "GPU0 — Intel UHD 770 iGPU", [
        "Vulkan0 · excluded from all configs (gpu_devices [1,2])",
    ], "hw"),
    (1.1, "GPU1 — Intel Arc A770 16 GB", [
        "Vulkan1 · main-model layers + helper lanes (executor, vision, embedder)",
        "small_model_gpu = 1",
    ], "hw"),
    (1.1, "GPU2 — Intel Arc A770 16 GB", [
        "Vulkan2 · PCIe 3.0 slot · main-model layers + sd.cpp image generation",
    ], "hw"),
    (1.0, "CPU / RAM", [
        "Intel i5-13500 · 64 GB RAM",
        "whisper STT, router classifier, MoE / big-model offload (-ncmoe, ngl < max)",
    ], "hw"),
    (1.3, "OS, drivers & measured speed", [
        "Windows 11 WDDM, Arc driver 32.0.101.8991 · Linux: Mesa ANV Vulkan (setup_linux.sh / start.sh)",
        "llama.cpp Vulkan b10840 (SYCL avoided: ~85% MoE loss, llama.cpp#19918)",
        "Single A770 9.7 t/s → dual (split 9,11) 14.44 t/s gen · 252.6 t/s prompt",
    ], "hw"),
])
y += h + 20
band(band_y, y - band_y, "6 · HARDWARE & OS", PAL["hw"][1])
hw_top = band_y
y += 44

# ── 7. Request lifecycle strip
band_y = y
y += 26
steps = [
    "Client request (cookie / PAT)", "Security headers, CSRF, session", "RBAC permission + admission queue",
    "Input guard + PAN block", "Router → job → lane (local / cloud)", "RAG: ACL filter, hybrid retrieve, inject",
    "Agent loop: LLM ⇄ tools / MCP / companion", "Cloud egress PAN-mask (if cloud lane)",
    "Answer check (verifier)", "Output guard + PAN mask", "SSE stream to client", "usage / route_log / audit_log",
]
sw = (MAIN_W - 32 - 22 * (len(steps) - 1)) / len(steps)
fill, stroke = PAL["flow"]
for i, s in enumerate(steps):
    sx = MARGIN + 16 + i * (sw + 22)
    svg.append(f'<rect x="{sx:.1f}" y="{y}" width="{sw:.1f}" height="76" rx="8" fill="{fill}" stroke="{stroke}"/>')
    svg.append(f'<circle cx="{sx + 14:.1f}" cy="{y + 14}" r="10" fill="{stroke}"/>')
    t(sx + 14, y + 18, str(i + 1), size=11.5, weight="bold", fill="#FFFFFF", anchor="middle")
    for j, part in enumerate(textwrap.wrap(s, int(sw / 6.6))):
        t(sx + sw / 2, y + 40 + j * 15, part, size=12, anchor="middle")
    if i < len(steps) - 1:
        svg.append(f'<line x1="{sx + sw + 2:.1f}" y1="{y + 38}" x2="{sx + sw + 18:.1f}" y2="{y + 38}" '
                   f'stroke="{stroke}" stroke-width="2" marker-end="url(#ah2)"/>')
y += 76 + 20
band(band_y, y - band_y, "7 · REQUEST LIFECYCLE (chat / agent run)", PAL["flow"][1])
main_bottom = y

# ── vertical arrows between main bands
ax = MARGIN + MAIN_W - 60
arrow(ax, cl_bottom + 2, ax, edge_top - 14, "HTTPS / WS", lx=ax - 90)
arrow(ax, core_bottom + 2, ax, inf_top - 14, "OpenAI-compat HTTP", lx=ax - 150)
arrow(ax, inf_bottom + 2, ax, hw_top - 14, "Vulkan", lx=ax - 62)

# ───────────────────────────── sidebar ──────────────────────────────
sy = 158
band_y = sy
sy += 24
sx = SIDE_X + 16
sw_ = SIDE_W - 32
sy += box(sx, sy, sw_, "SQLite stores (WAL, ThreadLocalDB)", [
    "§auth.db",
    "users (lockout), roles, permissions, role_permissions, user_roles, auth_sessions, audit_log, "
    "knowledge_sources, knowledge_source_role_access, user_allow_patterns, user_mcp_servers, api_tokens, "
    "companion_devices, user_custom_agents",
    "§projects.db",
    "projects, sessions, messages (meta JSON), plan_items, doc_files (version chain)",
    "§usage.db",
    "requests (tokens, t/s, status), route_runs, route_events, router_suggestions",
    "§memory.db",
    "chunks(source, path, chunk_idx, text, vec BLOB, dim, version), meta (chunks_gen trigger)",
    "§Operations",
    "busy_timeout, FKs on after repair (db_repair.py), db_backups/ (pre-FK snapshots)",
    "scripts: repair_db_schema.py, scrub_pans.py, migrate_common_per_user.py",
], "store", sub="persistence") + 12
sy += box(sx, sy, sw_, "Secrets, config & files", [
    "OS keychain via keyring (Windows Credential Manager / Linux Secret Service; never keyrings.alt): cloud keys, MCP OAuth tokens",
    "config/app.json (main), model_configs.json (per-GGUF launch), roles.json, providers/user_<id>.json, commands/*.md (11)",
    "atomic_write_json + CONFIG_LOCK",
    "knowledge_uploads/ (+ .chunks) · E:/AI/common/user_<id>/ · plugin_registry.json",
    "In-memory: request monitor (last 60), GPU cache, web-search cache, workspace-change snapshots",
    "Logs: stdout + audit_log table (for Bangladesh Bank / PCI auditors)",
], "store") + 12
sy += 8
band(band_y, sy - band_y, "8 · STORAGE", PAL["store"][1], x=SIDE_X, w=SIDE_W)
sy += 44

band_y = sy
sy += 24
sy += box(sx, sy, sw_, "Cloud AI (per user, opt-in)", [
    "OpenRouter-style OpenAI-compatible gateways",
    "Google Gemini API · Imagen · Veo (generativelanguage.googleapis.com)",
    "Egress: PAN-masked; KB context never sent (local_only); audio blocked by default",
], "ext", sub="HTTPS") + 12
sy += box(sx, sy, sw_, "MCP servers & connectors", [
    "ISMS — isms-mcp.sslwireless.com via mcp-remote",
    "Gmail MCP — gmailmcp.googleapis.com (gmail.readonly + gmail.compose, Google Desktop client, Internal consent)",
    "GitHub (device flow / PAT) · Atlassian (mcp-atlassian) · public MCP Registry (browse only)",
], "ext") + 12
sy += box(sx, sy, sw_, "Web & assets", [
    "DuckDuckGo HTML · Bing HTML · SearXNG (optional)",
    "Hugging Face: convaiinnovations/laya router checkpoint",
    "cdn.jsdelivr.net (mermaid) · cdn.sheetjs.com (xlsx) · youtube-nocookie frames",
], "ext") + 12
sy += 8
band(band_y, sy - band_y, "9 · EXTERNAL SERVICES", PAL["ext"][1], x=SIDE_X, w=SIDE_W)
ext_mid = band_y + (sy - band_y) / 2
sy += 44

band_y = sy
sy += 24
models = [
    "GGUF (all gpu [1,2], layer)   ctx     ngl  split  KV   MTP",
    "─────────────────────────────────────────────────────────",
    "ornith-1.5-9b q4_k_m ★      131920   999  15,5  q8_0  -",
    "qwen3.8-27b q4_k_m          131072    48  10,10 q8_0  3",
    "qwen3.8-flash-next q2_k_xl   65536     -   -    f16   -",
    "qwen3.5-9b-uncens q4_k_m    131072   999  9,11  q8_0  y",
    "kat-coder-v2.5-dev q4_k_s   131072   999  9,11  q8_0  -",
    "spark-x2.5-4b q4_k_m        231072   999  1,0   q8_0  y",
    "bonsai-2-27b ptq1_0         132768    45  10,10 q8_0  y",
    "qwen2.5-coder-14b q4_k_m    128000   999  9,11  q8_0  -",
    "qwen3.8-9b-distill q4_k_m   132768   999  9,11  q8_0  - (vision)",
    "neohorse-1-9b q4_k_m        132768   999  9,11  q8_0  - (kv_unified)",
    "",
    "★ default main (5-user plan) · n_slots 1 · batch 2048/512",
]
sy += box(sx, sy, sw_, "Main-model catalog — config/model_configs.json", models, "infer", sub="10 entries", mono=True) + 12
sy += 8
band(band_y, sy - band_y, "10 · MODEL CATALOG", PAL["infer"][1], x=SIDE_X, w=SIDE_W)
sy += 44

band_y = sy
sy += 24
sy += box(sx, sy, sw_, "Compliance controls (PCI DSS / Bangladesh Bank)", [
    "Session idle 15 min (PCI 8.2.8) · lockout + per-IP throttle · Argon2",
    "Plain-HTTP warning off-localhost (PCI 4.2.1) — terminate TLS in front",
    "PAN block on input, mask on output / SSE / cloud egress",
    "RBAC-scoped knowledge; KB local-only; audit_log for auditors",
    "Hash-locked requirements.lock, CVE-pinned floors, pip-audit (audit_deps.py), defusedxml",
    "No Docker dependencies; no server-side agent workspace",
], "guard", sub="security") + 12
sy += box(sx, sy, sw_, "Open items / watch list", [
    "n_slots = 1 on every model → generations are serial; 5-user plan needs n_slots ≥ 5 + kv_unified",
    "KB upload PAN scan off by policy choice — keep scrub_pans.py in the ops runbook",
    "Rotate the Gmail OAuth client secret previously stored in plaintext",
    "Companion auto-update URL is still a placeholder",
    "PROJECT_KNOWLEDGE.md partly stale (app.js / config.json / profiles/)",
], "edge") + 12
sy += 8
band(band_y, sy - band_y, "11 · COMPLIANCE & OPEN ITEMS", PAL["guard"][1], x=SIDE_X, w=SIDE_W)

H = max(main_bottom, sy) + 40

head = f'''<svg xmlns="http://www.w3.org/2000/svg" width="{W}" height="{H}" viewBox="0 0 {W} {H}" font-family="'Segoe UI', 'Noto Sans', Arial, sans-serif">
<title>Local Agent (a770-dual-runtime) — full system architecture</title>
<defs>
<marker id="ah" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7" orient="auto-start-reverse"><path d="M0,0 L10,5 L0,10 z" fill="#444441"/></marker>
<marker id="ah2" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="6" markerHeight="6" orient="auto"><path d="M0,0 L10,5 L0,10 z" fill="#3C3489"/></marker>
</defs>
<rect width="{W}" height="{H}" fill="#FFFFFF"/>
'''
with open(OUT, "w", encoding="utf-8") as f:
    f.write(head + "\n".join(svg) + "\n</svg>\n")
print(OUT, W, H)
