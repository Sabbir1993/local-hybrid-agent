import re

# A small orchestrator often announces the next step ("Let me read this skill
# first.") and then stops without calling a tool. Taken as a final answer, that
# ends the run after one step. These patterns spot such an announcement so the
# loop can send the model back to work instead.

NARRATION_MAX_CHARS = 240

# small models wrap the announcement: "[Let me ...]", "*Let me ...*", '"Let me ..."', "(Let me ...)"
_WRAP_RX = re.compile(r"^[\[\(\*_`\"'>\s]+|[\]\)\*_`\"'\s]+$")

# lead-ins that can precede the announcement itself: "Okay, let me ...", "Next I will ..."
_LEAD_IN_RX = re.compile(
    r"^(?:(?:okay|ok|sure|alright|all right|great|now|next|then|first(?:ly)?|"
    r"to (?:begin|start))\b[,:]?\s+)+", re.IGNORECASE)
_OPENER_RX = re.compile(
    r"^(?:let me|let's|i will|i'll|i am going to|i'm going to|i'm gonna|i shall)\s+\w+",
    re.IGNORECASE)
# courtesy phrases that begin like an opener but announce no work
_COURTESY_RX = re.compile(r"^let me (?:know|be clear|explain)\b", re.IGNORECASE)
_GERUND_RX = re.compile(
    r"^(?:reading|checking|opening|running|searching|listing|inspecting|looking|"
    r"navigating|analy[sz]ing|starting|loading|fetching|testing|scanning|reviewing)\b",
    re.IGNORECASE)
# "First the server, then the browser." - a sequence with no action verb
_SEQUENCE_RX = re.compile(r"^first\b[^.!?]*\bthen\b", re.IGNORECASE)
_SENTENCE_END_RX = re.compile(r"[.!?]+(?:\s+|$)")
# a reply that is only a tool call written out as text ("Tool Call: read_skill(...)")
_TOOLCALL_TEXT_RX = re.compile(r"^\s*[`*_>-]*(?:tool[ _-]?call|calling(?: tool)?)\b", re.IGNORECASE)


def _is_narration(text) -> bool:
    """True when a reply only announces work it has not done yet: short, one
    paragraph, one sentence, and opening with "let me" / "I will" / a gerund.
    Any real content (a second sentence, a list, a long answer) is not narration."""
    t = _WRAP_RX.sub("", (text or "").strip()).strip()
    if not t or len(t) > NARRATION_MAX_CHARS or "\n\n" in t:
        return False
    if _TOOLCALL_TEXT_RX.match(t):
        return True
    if _SEQUENCE_RX.match(t):
        return True
    sentences = [s for s in _SENTENCE_END_RX.split(t) if s.strip()]
    if len(sentences) != 1:
        return False
    if _COURTESY_RX.match(t):
        return False
    body = _LEAD_IN_RX.sub("", t)
    if _COURTESY_RX.match(body):
        return False
    return bool(_OPENER_RX.match(body) or _GERUND_RX.match(body))
