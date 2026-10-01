DEFAULTS = {"mode": "off", "apply_to": "both", "max_rounds": 1, "min_length": 120}
MODES = ("off", "badge", "gate")
APPLY_TO = ("both", "chat", "agent")
VERIFY_TIMEOUT_S = 90
# A rewrite emits up to MAX_DRAFT_CHARS of answer, so it gets a little more than the check
# does. It was timeout=None -- the only unbounded await left in the gate path, so a stalled
# generator could hang a run that had already paid for its verdict.
REVISE_TIMEOUT_S = 120
MAX_EVIDENCE_CHARS = 6000
MAX_DRAFT_CHARS = 12000

_SYSTEM = (
    "You are a strict answer checker. You are given a user's QUESTION, the EVIDENCE the "
    "assistant had (tool results, documents, search results; may be empty) and the "
    "assistant's DRAFT answer. Check the draft for: (1) not actually answering the question; "
    "(2) claims contradicted by, or missing from, the evidence when the evidence should cover "
    "them (general knowledge is fine); (3) wrong code, math or logic; (4) leaked secrets, "
    "passwords, API keys or card numbers. Ignore style and length.\n"
    "Reply with ONLY one JSON object, no markdown:\n"
    '{"verdict": "PASS" or "FAIL", "issues": [{"severity": "major" or "minor", "text": '
    '"<one plain sentence>"}], "confidence": <0..1>}\n'
    "Use FAIL only for at least one major issue. PASS may list minor issues."
)

_REVISE = (
    "A reviewer found problems in your previous answer. Rewrite the complete answer so it "
    "fixes every issue below, keeps everything that was correct, and follows the same format. "
    "Output only the corrected answer - do not mention the review.\n\nISSUES:\n{issues}"
)
