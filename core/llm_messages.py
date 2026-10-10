"""Message-list hygiene before a chat completion request.

Several chat templates (Qwen 3.x, Llama 3, Mistral...) refuse a conversation that has a system message anywhere but
the very start ("System message must be at the beginning" -> HTTP 500 from llama-server). The agent loop builds
messages from many parts - the saved transcript (a compaction marker is stored with role "system"), a digest
folded in by compaction, plan notes - so a stray system message can land mid-conversation, most often after a
conversation was compacted twice (the earlier marker sits in the kept tail of the next one).
"""

CONTEXT_PREFIX = "[context]\n"


def normalize_system_messages(msgs: list) -> list:
    """A new list where only the first message may be a system message.

    * system messages at the very start are merged into one (in order);
    * a system message after any other message becomes a user message prefixed with "[context]", so its text
      still reaches the model, in place, and the template never sees a late system role.
    The input list and its dicts are not modified.
    """
    if not msgs or not any(m.get("role") == "system" for m in msgs[1:]) and not (
            len(msgs) > 1 and msgs[0].get("role") == "system" and msgs[1].get("role") == "system"):
        return msgs
    out: list = []
    lead: list = []
    i = 0
    while i < len(msgs) and msgs[i].get("role") == "system":
        lead.append(str(msgs[i].get("content") or ""))
        i += 1
    if lead:
        first = dict(msgs[0])
        first["content"] = "\n\n".join(t for t in lead if t)
        out.append(first)
    for m in msgs[i:]:
        if m.get("role") == "system":
            m = {**m, "role": "user", "content": CONTEXT_PREFIX + str(m.get("content") or "")}
        out.append(m)
    return out
