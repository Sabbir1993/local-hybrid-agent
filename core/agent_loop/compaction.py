import json
from typing import Optional


def estimate_prompt_tokens(msgs: list, tools: Optional[list] = None) -> int:
    """Prompt-size estimate factoring in tokenizer ratio (~3 chars/token),
    tool schemas, and message envelope overhead."""
    total = 0
    for m in msgs:
        content_str = str(m.get("content") or "")
        total += max(1, len(content_str) // 3)
        for tc in (m.get("tool_calls") or []):
            try:
                total += len(json.dumps(tc)) // 3 + 12
            except Exception:
                total += 64
        total += 8  # role/framing overhead
    if tools:
        try:
            total += len(json.dumps(tools)) // 3 + 50
        except Exception:
            total += len(tools) * 75
    return total


def _digest_message(m: dict) -> str:
    role = str(m.get("role") or "?")
    content = str(m.get("content") or "").strip()
    if role == "user":
        return "user: " + content[:240] + ("..." if len(content) > 240 else "")
    if role == "assistant":
        names = [str((tc.get("function") or {}).get("name") or "?")
                 for tc in (m.get("tool_calls") or [])]
        head = content[:160] + ("..." if len(content) > 160 else "")
        return "assistant: " + head + (f" [tools: {', '.join(names)}]" if names else "")
    if role == "tool":
        return f"tool[{m.get('tool_call_id', '')}]: " + content[:160] + ("..." if len(content) > 160 else "")
    return f"{role}: {content[:200]}"


def compact_messages(msgs: list, budget_tokens: int, tools: Optional[list] = None) -> list:
    """Mechanical context compaction for agent and chat lanes.

    Keeps the system prompt and the most recent ~60% of the token budget
    verbatim (preserving assistant/tool_call/tool pairing), and rolls older
    turns into a compact deterministic digest. If the tail alone
    exceeds the budget, aggressively prunes oversized tool and message contents
    to guarantee the prompt stays within the model's context window.
    """
    if budget_tokens <= 0 or len(msgs) < 2:
        return msgs
    if estimate_prompt_tokens(msgs, tools) <= budget_tokens:
        return msgs

    # group into units: (assistant + its tool replies) or a single message
    units = []
    i = len(msgs) - 1
    while i >= 1:
        if msgs[i].get("role") == "tool":
            j = i
            while j >= 1 and msgs[j].get("role") == "tool":
                j -= 1
            if j >= 1 and msgs[j].get("role") == "assistant":
                units.append((j, i))
                i = j - 1
            else:
                units.append((i, i))
                i -= 1
        else:
            units.append((i, i))
            i -= 1
    units.reverse()

    tool_overhead = (len(json.dumps(tools)) // 3 + 50) if tools else 0
    effective_budget = max(2000, budget_tokens - tool_overhead)
    tail_budget = int(effective_budget * 0.6)
    tail_start = len(msgs)
    acc = 0
    for start, end in reversed(units):
        u_tok = estimate_prompt_tokens(msgs[start:end + 1])
        if acc + u_tok > tail_budget and tail_start != len(msgs):
            break
        acc += u_tok
        tail_start = start

    # Aging bound: at most `keep_recent_results` tool results stay verbatim, however
    # much budget is left. Whole units move into the digest, so assistant/tool_call
    # pairing is preserved. Without this, three max-size results can occupy the whole
    # "recent" tail of a long run.
    keep_results = _keep_recent_results()
    if keep_results:
        # The newest unit that carries tool results is protected: it is the evidence
        # the model is answering right now, and a turn with several parallel tool
        # calls is the normal way to exceed the bound. (A trailing assistant message
        # with no results is not such a unit.)
        protected = None
        for u_start, u_end in reversed(units):
            if any(m.get("role") == "tool" for m in msgs[u_start:u_end + 1]):
                protected = (u_start, u_end)
                break
        seen = 0
        for start, end in reversed(units):
            if start < tail_start:
                break
            n_results = sum(1 for m in msgs[start:end + 1] if m.get("role") == "tool")
            if seen + n_results > keep_results:
                if protected is not None and (start, end) == protected:
                    # the protected unit alone exhausts the allowance: keep it (it is
                    # the current evidence) and digest everything older
                    tail_start = start
                else:
                    # tail_start is the first KEPT index, so cutting this unit means
                    # skipping it entirely (its lines go into the digest)
                    tail_start = end + 1
                break
            seen += n_results

    # If the tail alone still exceeds the budget, prune oversized contents from the recent turns
    if tail_start <= 1:
        pruned_msgs = [msgs[0]] if msgs and msgs[0].get("role") == "system" else []
        recent_units = units[-2:] if len(units) >= 2 else units
        msg_count = max(1, sum(end - start + 1 for start, end in recent_units))
        max_content_chars = max(500, (effective_budget * 2) // msg_count)
        for start, end in recent_units:
            for m in msgs[start:end + 1]:
                m_copy = dict(m)
                c = str(m_copy.get("content") or "")
                if len(c) > max_content_chars:
                    half = max_content_chars // 2
                    m_copy["content"] = c[:half] + "\n...[truncated to fit context budget]...\n" + c[-half:]
                pruned_msgs.append(m_copy)
        return pruned_msgs

    digest_lines = []
    for start, end in units:
        if start >= tail_start:
            break
        for m in msgs[start:end + 1]:
            digest_lines.append(_digest_message(m))
    digest = ("[CONVERSATION DIGEST - earlier steps were compacted to fit the "
              "context window. Tool results are summarized; call read_file / "
              "list_files again if you need exact content.]\n" + "\n".join(digest_lines))
    if len(digest) > 6000:
        digest = digest[:6000] + "\n..."
    # Fold the digest into the first digested user turn instead of inserting a new
    # system message at index 1. The system prompt + tool schemas prefix then stays
    # byte-identical, so llama.cpp can reuse that part of the KV cache (--cache-reuse)
    # instead of reprocessing everything from message 1 on every compacted step.
    # msgs[0] is never touched, and the digest is still visible ahead of the tail.
    head = list(msgs[1:tail_start])
    body = list(msgs[tail_start:])
    for m in head:
        if m.get("role") == "user":
            merged = dict(m)
            merged["content"] = digest + "\n\n" + str(merged.get("content") or "")
            return [msgs[0], merged] + body
    # No user turn carried the compacted-away context (unusual): keep the historic
    # shape so the information is never silently dropped.
    return [msgs[0], {"role": "system", "content": digest}] + body


def _keep_recent_results(default: int = 2) -> int:
    """How many tool results may stay verbatim in a compacted tail.

    Without a bound, "keep the most recent 60% of the budget" can still mean three
    max-size tool results (~6.7k tokens each at MAX_TOOL_OUTPUT = 20,000 chars), so
    one heavy step can dominate every later request. Anything pushed past this many
    falls into the digest as one line per message instead. 0 disables the bound.
    Config: context.aging.keep_recent_results."""
    try:
        from ..small_model import APP_CONFIG
        cfg = ((APP_CONFIG.get("context") or {}).get("aging") or {})
        return max(0, int(cfg.get("keep_recent_results", default)))
    except Exception:
        return default
