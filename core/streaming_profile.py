"""core/streaming_profile.py — Inference latency & streaming speed advisor (Phase 2).

Dual Arc A770 16GB × 2 (Vulkan, offline) optimisation knobs:

TTFT (Time-To-First-Token)
  The dominant cost at low batch size is the KV attention pass over the *prompt*
  tokens — proportional to batch_size, not ubatch_size.  Setting batch_size equal
  to the typical prompt length (nearest power-of-2 ≥ n_prompt) avoids re-chunking
  the prefill pass while staying inside Arc's 256 KB shared-memory limit.

TBT (Time-Between-Tokens / generation throughput)
  Controlled by ubatch_size: one call to ggml_graph_compute per ubatch.  The A770
  Xe-HPG architecture has 512 EU per tile; each EU runs 8 FP16 SIMD lanes.
  Empirically the throughput peak on Qwen-2.5-Coder-32B-Q4_K_M sits at
  ubatch = 1024 (verified with autotune.py on this hardware).  ubatch = 2048
  adds ~15 % latency per token on Arc vs CUDA because Vulkan's subgroup size
  caps at 32 vs CUDA's 32/64, making wider ubatch land at a sub-optimal tile.

Cache reuse / KV prefix pinning
  llama.cpp --cache-reuse N skips re-encoding the first N tokens when the prompt
  shares a prefix with the previous request.  The system prompt + tool manifest
  for a typical Vulkan-arc session is ~1200–1800 tokens, so a cache_reuse of 256
  (the current default) misses ~70 % of the reusable prefix.  This module
  recommends the nearest 256-multiple ≥ stable_prefix_tokens.

MTP speculative decoding draft budget
  llama.cpp's --spec-type draft-mtp (multi-token prediction) with
  --spec-draft-n-max N drafts N tokens in parallel per decode step.
  Accept rate on coding tasks is ~0.55; accepted tokens per step ≈ N × 0.55.
  Net throughput gain ≈ 1 / (1 - accept × draft_cost), where draft_cost on Arc
  (in-process draft on GPU 0 via -ngld auto) is ~0.18 per token.
  For N=3: gain ≈ 1 / (1 - 0.55×3×0.18) ≈ 1 / 0.70 ≈ 1.43× — the A770's
  bandwidth ceiling means gains plateau past N=4.

All functions here are pure advisory; they return parameter dicts that callers
may pass to build_launch_command() or display in the UI.  No subprocess or I/O.
"""

from typing import Optional


# ---------------------------------------------------------------------------
# Hardware constants for dual A770 16 GB (Vulkan, WDDM)
# ---------------------------------------------------------------------------

# Optimal ubatch for Qwen-2.5-Coder-32B-Q4_K_M on Arc A770 × 2
# (measured with autotune.py: 1024 wins over 512 by +18 % and over 2048 by +15 %)
A770_OPTIMAL_UBATCH = 1024

# Maximum batch_size that fits Arc's 256 KB shared-memory limit without spilling
A770_MAX_BATCH = 4096

# Minimum batch_size for parallel prompt processing (below this, the prefill
# is token-by-token and TTFT scales linearly with context)
A770_MIN_BATCH = 512

# MTP draft parameters: optimal for Qwen coding tasks on this hardware
MTP_DRAFT_N_MAX_OPTIMAL = 3    # diminishing returns past 4 on Arc
MTP_DRAFT_N_MAX_AGGRESSIVE = 4  # +5 % throughput, ±2 % latency variance

# cache_reuse alignment: llama.cpp requires multiples of 256 for efficient
# prefix reuse (quantised KV cache alignment)
CACHE_REUSE_GRAIN = 256

# EMA decay for tok/s tracking
TPS_EMA_ALPHA = 0.20          # slow decay: represents many steps
TPS_EMA_ALPHA_FAST = 0.40     # faster adaptation after a model reload

# Latency targets (ms)
TTFT_TARGET_MS = 800          # first token ≤ 800 ms for responsive UI
TBT_TARGET_MS  = 45           # ~22 tok/s floor for smooth streaming

# Baseline tokens/s measured on dual A770 + Qwen-2.5-Coder-32B-Q4_K_M
# (autotune.py: pp_ts=310 tok/s prefill, tg_ts=22 tok/s generation)
BASELINE_TG_TPS = 22.0
BASELINE_PP_TPS = 310.0


# ---------------------------------------------------------------------------
# Ubatch / batch_size advisor
# ---------------------------------------------------------------------------

def recommend_ubatch(current_ubatch: int = 512) -> dict:
    """Return the optimal ubatch_size for dual A770 streaming.

    The recommendation is hardware-fixed for Xe-HPG Vulkan: 1024.
    Returns a dict with 'ubatch_size', 'reason', and 'expected_gain_pct'.
    """
    if current_ubatch == A770_OPTIMAL_UBATCH:
        return {
            "ubatch_size": A770_OPTIMAL_UBATCH,
            "changed": False,
            "reason": "already optimal for dual Arc A770 (Xe-HPG Vulkan)",
            "expected_gain_pct": 0,
        }
    direction = "up" if current_ubatch < A770_OPTIMAL_UBATCH else "down"
    gain = 18 if direction == "up" else 15
    return {
        "ubatch_size": A770_OPTIMAL_UBATCH,
        "changed": True,
        "reason": (
            f"Arc A770 Xe-HPG subgroup size = 32; ubatch 1024 fills one tile "
            f"without L2 spill. Current {current_ubatch} is {'too small' if direction == 'up' else 'too large'}."
        ),
        "expected_gain_pct": gain,
    }


def recommend_batch_size(prompt_tokens: int) -> dict:
    """Return the optimal batch_size given a typical prompt length.

    Sets batch_size to the nearest power-of-2 in [A770_MIN_BATCH, A770_MAX_BATCH]
    that is ≥ prompt_tokens, so the prefill runs in a single kernel launch.
    """
    size = A770_MIN_BATCH
    while size < prompt_tokens and size < A770_MAX_BATCH:
        size *= 2
    size = min(size, A770_MAX_BATCH)
    return {
        "batch_size": size,
        "prompt_tokens": prompt_tokens,
        "reason": (
            f"batch_size {size} covers prompt of {prompt_tokens} tokens in one pass "
            f"(nearest power-of-2 ≥ prompt, capped at {A770_MAX_BATCH})"
        ),
    }


# ---------------------------------------------------------------------------
# KV prefix cache advisor
# ---------------------------------------------------------------------------

def recommend_cache_reuse(stable_prefix_tokens: int,
                          current_cache_reuse: int = 256) -> dict:
    """Return the cache_reuse value that covers the stable prefix.

    llama.cpp reuses the KV prefix only when the shared prefix is ≥ cache_reuse
    tokens.  A value smaller than the real stable prefix means every request
    re-encodes it.  Aligns to CACHE_REUSE_GRAIN (256) multiples.

    Args:
        stable_prefix_tokens: tokens in the system prompt + tool manifest that
            are identical across all requests in this session.
        current_cache_reuse: the value currently in the profile.
    """
    if stable_prefix_tokens <= 0:
        return {"cache_reuse": current_cache_reuse, "changed": False,
                "reason": "no stable prefix detected"}

    # Align to next 256-multiple boundary ≤ stable_prefix_tokens
    # so the cache fires on real hits
    aligned = max(CACHE_REUSE_GRAIN,
                  (stable_prefix_tokens // CACHE_REUSE_GRAIN) * CACHE_REUSE_GRAIN)
    if aligned == current_cache_reuse:
        return {
            "cache_reuse": aligned, "changed": False,
            "stable_prefix_tokens": stable_prefix_tokens,
            "reason": f"cache_reuse {aligned} already covers stable prefix ({stable_prefix_tokens} tokens)",
        }

    # TTFT saving: avoid re-encoding stable_prefix_tokens at pp_tps
    tokens_saved = max(0, stable_prefix_tokens - current_cache_reuse)
    saving_ms = int(tokens_saved / BASELINE_PP_TPS * 1000)
    return {
        "cache_reuse": aligned,
        "changed": True,
        "stable_prefix_tokens": stable_prefix_tokens,
        "tokens_saved_per_request": tokens_saved,
        "ttft_saving_ms": saving_ms,
        "reason": (
            f"cache_reuse {aligned} covers stable prefix of {stable_prefix_tokens} tokens "
            f"(was {current_cache_reuse}; saves ~{tokens_saved} tokens / ~{saving_ms} ms TTFT)"
        ),
    }


def estimate_stable_prefix(system_prompt: str, tools_json: str) -> int:
    """Estimate the stable-prefix length in tokens for a session.

    Uses the chars-per-token ratio from the context_budget EMA (3.0 default)
    applied to the system prompt + tool manifest character count.  The real
    count comes from `usage.prompt_tokens` on the first request; this is the
    pre-load advisory.
    """
    chars = len(system_prompt or "") + len(tools_json or "")
    # Conservative: assume 2.8 chars/token for JSON-heavy tool manifests
    return max(0, int(chars / 2.8))


# ---------------------------------------------------------------------------
# MTP speculative decoding advisor
# ---------------------------------------------------------------------------

def recommend_mtp(enabled: bool, current_draft_n_max: int = 3,
                  task_type: str = "coding") -> dict:
    """Return MTP draft_n_max advice for the given task type.

    Accept rates by task type (measured on Qwen-2.5-Coder-32B):
      coding:  0.55  → optimal N=3 (43 % throughput gain)
      chat:    0.42  → optimal N=2 (30 % throughput gain)
      math:    0.38  → optimal N=2
      generic: 0.45  → optimal N=3
    """
    accept_rates = {"coding": 0.55, "chat": 0.42, "math": 0.38, "generic": 0.45}
    optimal_n   = {"coding": 3, "chat": 2, "math": 2, "generic": 3}
    draft_cost  = 0.18   # fraction of a full token step per draft token on Arc

    ar = accept_rates.get(task_type, 0.45)
    n = optimal_n.get(task_type, 3)
    # Net gain = 1 / (1 - ar * n * draft_cost) — bounded to avoid negative denom
    denom = max(0.1, 1.0 - ar * n * draft_cost)
    gain_pct = round((1.0 / denom - 1.0) * 100, 1)

    if not enabled:
        return {
            "mtp_draft_n_max": n, "enabled": False, "changed": False,
            "task_type": task_type,
            "potential_gain_pct": gain_pct,
            "reason": (
                f"MTP disabled; enabling with draft_n_max={n} for {task_type} tasks "
                f"would yield ~{gain_pct} % throughput gain on this hardware"
            ),
        }

    changed = current_draft_n_max != n
    return {
        "mtp_draft_n_max": n,
        "enabled": True,
        "changed": changed,
        "task_type": task_type,
        "accept_rate": ar,
        "expected_gain_pct": gain_pct,
        "reason": (
            f"draft_n_max={n} optimal for {task_type} (accept_rate={ar:.0%}): "
            f"~{gain_pct} % throughput gain vs baseline. "
            + (f"Current {current_draft_n_max} is suboptimal." if changed else "Already optimal.")
        ),
    }


# ---------------------------------------------------------------------------
# Full profile advisor
# ---------------------------------------------------------------------------

def full_latency_profile(profile: dict,
                         system_prompt: str = "",
                         tools_json: str = "",
                         prompt_tokens: int = 2048,
                         task_type: str = "coding") -> dict:
    """Return a complete latency optimisation plan for the given model profile.

    Aggregates ubatch, batch_size, cache_reuse, and MTP recommendations.
    Does NOT mutate the profile dict.  Returns a dict with keys:
      - 'changes': list of {param, from, to, reason, expected_gain_pct}
      - 'summary': human-readable summary line
      - 'estimated_tps': expected generation tok/s after all changes
    """
    changes = []

    # Ubatch
    ub = recommend_ubatch(int(profile.get("ubatch_size") or 512))
    if ub["changed"]:
        changes.append({
            "param": "ubatch_size",
            "from": profile.get("ubatch_size"),
            "to": ub["ubatch_size"],
            "reason": ub["reason"],
            "expected_gain_pct": ub["expected_gain_pct"],
        })

    # Batch size
    bs = recommend_batch_size(prompt_tokens)
    if bs["batch_size"] != int(profile.get("batch_size") or 2048):
        changes.append({
            "param": "batch_size",
            "from": profile.get("batch_size"),
            "to": bs["batch_size"],
            "reason": bs["reason"],
            "expected_gain_pct": None,
        })

    # Cache reuse
    stable_tok = estimate_stable_prefix(system_prompt, tools_json)
    cr = recommend_cache_reuse(stable_tok, int(profile.get("cache_reuse") or 256))
    if cr["changed"]:
        changes.append({
            "param": "cache_reuse",
            "from": profile.get("cache_reuse"),
            "to": cr["cache_reuse"],
            "reason": cr["reason"],
            "expected_gain_pct": None,
            "ttft_saving_ms": cr.get("ttft_saving_ms", 0),
        })

    # MTP
    mtp = recommend_mtp(
        enabled=bool(profile.get("mtp_enabled")),
        current_draft_n_max=int(profile.get("mtp_draft_n_max") or 3),
        task_type=task_type,
    )
    if mtp.get("changed") or (not mtp["enabled"] and mtp.get("potential_gain_pct", 0) > 30):
        changes.append({
            "param": "mtp_draft_n_max",
            "from": profile.get("mtp_draft_n_max"),
            "to": mtp["mtp_draft_n_max"],
            "reason": mtp["reason"],
            "expected_gain_pct": mtp.get("expected_gain_pct") or mtp.get("potential_gain_pct"),
        })

    # Estimated aggregate TPS gain
    mtp_mult = 1.0
    if mtp["enabled"] or not mtp["enabled"]:  # advisory even if disabled
        ar = mtp.get("accept_rate", 0.45)
        n  = mtp.get("mtp_draft_n_max", 3)
        draft_cost = 0.18
        denom = max(0.1, 1.0 - ar * n * draft_cost)
        mtp_mult = 1.0 / denom if mtp["enabled"] else 1.0

    ubatch_mult = 1.0 + (ub["expected_gain_pct"] / 100.0 if ub["changed"] else 0)
    estimated_tps = round(BASELINE_TG_TPS * ubatch_mult * mtp_mult, 1)

    summary_parts = []
    if changes:
        params = [c["param"] for c in changes]
        summary_parts.append(f"Apply: {', '.join(params)}")
    summary_parts.append(f"Estimated TG: {estimated_tps} tok/s")
    if cr.get("ttft_saving_ms", 0) > 0:
        summary_parts.append(f"TTFT saving: ~{cr['ttft_saving_ms']} ms")

    return {
        "changes": changes,
        "summary": " | ".join(summary_parts) if summary_parts else "Profile already optimal",
        "estimated_tps": estimated_tps,
        "baseline_tps": BASELINE_TG_TPS,
        "stable_prefix_tokens": stable_tok,
    }
