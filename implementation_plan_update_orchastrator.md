# Production Implementation Plan: Florence-2 + Cactus Needle + Sub-Agent + Dual-A770 Escalation Pipeline

This document details the complete architectural overhaul and implementation roadmap for the `vulkan-arc` runtime platform. It combines **Florence-2** for perceptual visual grounding, **Cactus Needle** for sub-15ms tool routing, **Spark-X2.5-4B / MiniCPM 5 2B** for fast local ReAct loops, and **Dual Intel Arc A770 Qwen3.8-27B** with optional **Frontier Cloud Escalation (DeepSeek-V3 / Claude 3.7 Sonnet)**.

---

## 1. System Topology & Hardware Resource Budget

### Hardware Baseline (Verified on Host)
- **CPU:** Intel Core i5-13500 (14 Cores / 20 Threads, AVX2, VNNI) + 64 GB DDR4/5 RAM.
- **GPU 1 (Display):** Intel Arc A770 16GB GDDR6 (PCIe 4.0 x16, Vulkan Index 1).
- **GPU 2 (Headless):** Intel Arc A770 16GB GDDR6 (PCIe 3.0 x16, Vulkan Index 2).
- **llama.cpp Binary:** Vulkan build `b10840` at `E:\AI\llama-vulkan`.

### Topology & VRAM Allocation Map
```
┌─────────────────────────────────────────────────────────────────────────────┐
│                          User Request (Text + Image)                        │
└──────────────────────────────────────┬──────────────────────────────────────┘
                                       │
                                       ▼
┌─────────────────────────────────────────────────────────────────────────────┐
│                  Perceptual Layer: Florence-2-Large                         │
│  • Device: Intel i5-13500 CPU (via OpenVINO) or Arc GPU 1                   │
│  • Tasks: <OCR_WITH_REGION>, <OD> (UI Bounding Boxes), <DETAILED_CAPTION>   │
│  • Latency: ~180ms – 400ms | Memory: ~1.4 GB                                │
│  • Invariant: Strips raw image; passes structured text & coordinates only   │
└──────────────────────────────────────┬──────────────────────────────────────┘
                                       │ (Lean text + [ymin, xmin, ymax, xmax])
                                       ▼
┌─────────────────────────────────────────────────────────────────────────────┐
│                 Routing Layer: Cactus Needle (needle 2.0.13)                │
│  • Device: Intel i5-13500 CPU (0 MB VRAM, ~45 MB System RAM)                │
│  • Architecture: Simple Attention Network (SAN, no FFNs)                    │
│  • Latency: <15ms | Speed: ~940 prefill t/s                                 │
│  • Role: Instant semantic classification & JSON function call assembly      │
└──────────────────┬──────────────────────────────────────┬───────────────────┘
                   │ (Single Tool Intent, Conf >= 0.75)   │ (Multi-step / Code)
                   ▼                                      ▼
┌──────────────────────────────────┐ ┌────────────────────────────────────────┐
│     Direct Tool Execution        │ │  Local Sub-Agent Lane (Port 8091)      │
│  • list_files, read_file,        │ │  • Model: Spark-X2.5-4B (or MiniCPM 5) │
│    UI click coordinate action    │ │  • Device: Vulkan1 (~2.6 GB VRAM)      │
│  • Returns immediate answer      │ │  • Speed: ~42 t/s | Context: 16k–32k   │
└──────────────────────────────────┘ │  • Local sandboxed tool & code loop    │
                                     └────────────────────┬───────────────────┘
                                                          │
                                               [Escalation Triggered]
                                               (Multi-file refactor, deep
                                                logic bug, loop detection)
                                                          │
                                                          ▼
                                     ┌────────────────────────────────────────┐
                                     │  Main Lane: Dual-A770 Qwen-27B (:8090) │
                                     │  or Frontier Cloud LM (DeepSeek/Claude)│
                                     │  • VRAM: ~24 GB (Layer split 9,11)     │
                                     │  • Context: 32k–128k                   │
                                     │  • Zero-vision-token input payload     │
                                     └────────────────────────────────────────┘
```

---

## 2. Model Selection Matrix for the Local Sub-Agent (Port 8091)

Both models are already resident on disk at `E:\AI\Models\`:

| Parameter | **Option A: Spark-X2.5-4B** (Recommended) | **Option B: MiniCPM 5 2B** | **Option C: Qwen2.5-Coder-3B** |
| :--- | :--- | :--- | :--- |
| **Path on Disk** | `E:\AI\Models\Spark-X2.5-4B-Q4_K_M.gguf` | `E:\AI\Models\orchestrator\MiniCPM5-2B-Q4_K_M.gguf` | Download on demand |
| **VRAM on GPU 1** | ~2.60 GB | ~1.56 GB | ~2.05 GB |
| **Architecture** | Hybrid 1:3 Sliding-Window Attention | Standard Dense | Standard Dense |
| **Agentic Tool Bench**| **Top Tier (BFCL-V4 & $\tau^3$-bench)** | High | High |
| **Code Specialization**| Strong general coding (~74% HumanEval)| Good | **Class Leading (81.7% HumanEval)**|
| **KV Cache Growth** | **Minimal (Sliding-window compression)** | Linear growth | Linear growth |
| **Best Suited For** | Autonomous tool chaining & long sessions | Fast terminal ops & lightweight chat | Strict line-by-line file patching |

---

## 3. User Review Required

> [!IMPORTANT]
> **Sub-Agent Model Choice for Port 8091:**
> We recommend defaulting to **`Spark-X2.5-4B-Q4_K_M.gguf`** because of its native hybrid attention and superior function-calling leaderboard scores. If you prefer the lowest possible VRAM overhead, **`MiniCPM5-2B-Q4_K_M.gguf`** uses 1.0 GB less VRAM.
>
> **Florence-2 Runtime Deployment:**
> For Florence-2, we provide a Python OpenVINO preprocessor worker that runs on CPU (or DirectML). When Florence-2 weights are not yet present, it gracefully falls back to the existing `SmolVLM` / `Qwen2.5-VL` models to extract OCR/summaries without breaking the pipeline.

---

## 4. Proposed File Changes

### Configuration & State
#### [MODIFY] [config.json](file:///e:/AI/vulkan-arc/a770-dual-runtime/config.json)
- Set `small_models.executor.model` to `Spark-X2.5-4B-Q4_K_M.gguf` (or `MiniCPM5-2B-Q4_K_M.gguf`).
- Set `small_models.executor.ctx` to `16384`.
- Update `router` configuration to declare `cactus_needle` with a confidence threshold of `0.75`.
- Add `vision_preprocessor` config for Florence-2 backend (OpenVINO / CPU / DirectML).

---

### Core Routing & Tool Execution
#### [MODIFY] [core/small_model.py](file:///e:/AI/vulkan-arc/a770-dual-runtime/core/small_model.py)
- **Dynamic Cactus Needle Router:**
  - Cache the `needle.Needle` instance by hashing active tool definitions so it updates whenever skills, plugins, or MCP tools are registered.
  - Return formatted schema calls with confidence metrics and inference timing.
- **Vision Preprocessor Fallback:**
  - Route image inputs through Florence-2 (or fallback vision models) to produce structured OCR + coordinate bounding boxes, stripping raw image tokens.

#### [MODIFY] [core/agent_loop.py](file:///e:/AI/vulkan-arc/a770-dual-runtime/core/agent_loop.py)
- **Zero-Vision-Token Ingestion:**
  - When a user prompt contains an image attachment or path, run the perceptual extraction first.
  - Replace the raw multimodal attachment in the message history with clean markdown text:
    ```markdown
    [VISUAL EXTRACTION - ZERO IMAGE TOKENS]
    Scene: Web dashboard showing server metrics and terminal.
    UI Coordinates:
      - "Run Build" button: [120, 840, 150, 960]
      - "Terminal output tab": [80, 200, 110, 320]
    OCR Text: "Listening on http://127.0.0.1:8000 ... status: 200 OK"
    ```
- **Automated Escalation Protocol:**
  - If the sub-agent on port 8091 triggers `is_degeneration_or_loop()` or fails a test twice, seamlessly transfer the clean, stripped context to the Dual-A770 Qwen-27B main lane on port 8090 (or external frontier API).

#### [MODIFY] [core/file_tools.py](file:///e:/AI/vulkan-arc/a770-dual-runtime/core/file_tools.py)
- Register image extensions (`.png`, `.jpg`, `.jpeg`, `.webp`) so that uploads to `/agent/upload` automatically trigger structured OCR and UI bounding box extraction alongside documents.

#### [MODIFY] [routes/agent.py](file:///e:/AI/vulkan-arc/a770-dual-runtime/routes/agent.py)
- Connect the Needle routing pass to execute confident single-turn tool calls before the main generation loop starts.
- Ensure the SSE streaming channel reports tool calls and execution telemetry accurately.

---

## 5. Verification & Testing Plan

### Automated Unit & Benchmark Tests
1. **Cactus Needle Validation:**
   ```powershell
   python -c "import sys; sys.path.insert(0, 'a770-dual-runtime'); from core.small_model import needle_route; from core.agent_tools import AGENT_TOOLS; print(needle_route('list all files in directory', AGENT_TOOLS))"
   ```
2. **Sub-Agent Health Check:**
   Verify `llama-server.exe` spawns cleanly on port 8091 with `Spark-X2.5-4B-Q4_K_M.gguf` or `MiniCPM5-2B-Q4_K_M.gguf` on Vulkan1.
3. **Core Module Import Test:**
   ```powershell
   python -c "import sys; sys.path.insert(0, 'a770-dual-runtime'); from core import config, db, gpu, profiles, monitor, process, small_model, state, agent_tools, agent_loop; print('All core modules OK')"
   ```

### Manual Verification Scenarios
- **Scenario 1 (Direct Tool Routing):** Send `"read README.md"` $\rightarrow$ verify Needle handles it in <20ms without waking up GPU models.
- **Scenario 2 (Vision & UI Coordinate Click):** Upload a UI screenshot asking to click a button $\rightarrow$ verify Florence-2/Vision extracts exact coordinates `[ymin, xmin, ymax, xmax]` and the agent issues the coordinate click tool without passing raw image tokens to the 27B model.
- **Scenario 3 (Escalation):** Submit a multi-file refactor prompt $\rightarrow$ verify sub-agent attempts initial pass and escalates cleanly to the Dual-A770 Qwen-27B model when needed.
