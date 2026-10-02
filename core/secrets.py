"""
core/secrets.py - Built-in credential/secret detection. The deterministic floor under the
semantic guard rules.

Why this exists: `config/app.json` shipped exactly ONE `input_guard` rule and it was of type
"semantic", i.e. a prompt asking a 4B local model for a single YES/NO token. That is the whole
input policy. It is:

  * fail-open by design (semantic.py returns False on timeout, error or an unavailable model),
  * prompt-injectable (the user text is the classifier's input), and
  * role-scoped in that config to `roles: ["user"]`, so admins were exempt.

The regex engine those rules were meant to use is real, cached and tested - it was simply
configured to zero patterns. This module is the same idea as core/pan.py (which is the
built-in PCI floor): a small, high-signal, deterministic rule set that runs regardless of the
admin rule set, so the semantic classifier is an enhancement rather than the only control.

Two tiers, because precision matters more than recall here:

  HIGH  - unambiguous credential formats. Blocked on every lane. A real private key or live
          API key has no legitimate reason to be pasted into a chat agent, local or cloud,
          because local does not mean private: it lands in the llama-server KV cache, in the
          agent's tool logs, and potentially in files the agent then writes.
  LOOSE - generic `secret = "..."` assignments. Cloud egress only. These are the ones with
          false positives ("the secret to a good life", `token = "abc"`), and blocking them
          for a local-only user who is debugging their own config is hostile.

Config: config/app.json "secrets" block, mirroring "pci". Each key accepts "off":
  {"secret_high": "block", "secret_loose": "cloud_only", "secret_output": "mask"}
"""

import re

# --- HIGH tier: unambiguous credential formats -------------------------------------------
_HIGH = (
    # PEM private keys, in any armour type.
    (re.compile(r"-----BEGIN\s+(?:RSA|DSA|EC|OPENSSH|PGP|ENCRYPTED)?\s*PRIVATE KEY(?:\s+BLOCK)?-----"),
     "private key"),
    # AWS access key id / secret access key.
    (re.compile(r"\b(?:AKIA|ASIA|ABIA|ACCA)[0-9A-Z]{16}\b"), "AWS access key id"),
    (re.compile(r"(?i)\baws_secret_access_key\b\s*[:=]\s*[\"']?[A-Za-z0-9/+=]{40}[\"']?"),
     "AWS secret access key"),
    # GitHub tokens (classic, fine-grained, OAuth, app, refresh).
    (re.compile(r"\bgh[pousr]_[A-Za-z0-9]{36,}\b"), "GitHub token"),
    # GitLab / Slack / Stripe / Google / OpenAI-style tokens.
    (re.compile(r"\bglpat-[A-Za-z0-9_-]{20,}\b"), "GitLab token"),
    (re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}\b"), "Slack token"),
    (re.compile(r"\b(?:sk|rk)_(?:live|test)_[A-Za-z0-9]{16,}\b"), "Stripe key"),
    (re.compile(r"\bAIza[0-9A-Za-z_-]{35}\b"), "Google API key"),
    (re.compile(r"\bsk-(?:proj-|ant-|live-|test-)?[A-Za-z0-9_-]{20,}\b"), "API secret key"),
    # JSON Web Tokens: three base64url segments.
    (re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\b"), "JWT"),
    # Credentials embedded in a URL: scheme://user:pass@host
    (re.compile(r"\b[a-z][a-z0-9+.-]*://[^\s/:@]+:[^\s/@]{4,}@[^\s/]+", re.IGNORECASE),
     "credentials in URL"),
    # .env / PEM / key files pasted inline, long base64/hex blob under an obvious filename.
    (re.compile(r"(?i)\b(?:id_rsa|id_ed25519|\.env|\.npmrc|\.pypirc|credentials\.json)\b"),
     None),  # filename alone is only a hint; not high-confidence on its own
)

# --- LOOSE tier: generic assignments. Cloud egress only. ---------------------------------
# The key name is matched as an identifier CONTAINING a sensitive word, not as one exact
# name: real config files spell this `AWS_SECRET_ACCESS_KEY`, `database_password`,
# `CLIENT-SECRET`, `apiKey`, and an anchored alternation misses all of them. Over-broad on
# the name is acceptable here because the value must still be >= 12 chars and survive the
# placeholder filter below, and the whole tier only applies to cloud egress.
_ASSIGN = re.compile(
    r"(?i)(?:^|[\s\"'`,;(\[])"
    r"[\w.-]*(?:pass(?:wor)?d|passphrase|secret|api[_-]?key|apikey|token|credential"
    r"|private[_-]?key)s?[\w.-]*"
    r"\s*(?:[:=]|=>|\bis\b)\s*"
    r"[\"']?([^\s\"',;]{12,})[\"']?")
# Values that look like a variable reference, a placeholder or prose, not a secret.
_NOT_SECRET = re.compile(
    r"(?i)^(?:[<${%]|\$\{?\w|\*{3,}|x{3,}|\.{3,}|your[-_ ]?\w+|changeme|placeholder"
    r"|example|redacted|todo|none|null|n/?a|\w+[-_]?(?:here|value|token|key|secret))\b")

_HIGH_RULES = [rx for rx, _ in _HIGH if _ is not None]

RULE_NAME = "Credential / secret material"


def _cfg() -> dict:
    from .small_model import APP_CONFIG
    c = APP_CONFIG.get("secrets")
    return c if isinstance(c, dict) else {}


def _mode(key: str, default: str) -> str:
    return str(_cfg().get(key, default)).strip().lower()


def high_mode() -> str:
    """block | mask | off - what to do with an unambiguous credential."""
    return _mode("secret_high", "block")


def loose_mode() -> str:
    """cloud_only | block | off - generic assignments are only worth policing on egress."""
    return _mode("secret_loose", "cloud_only")


def _high_hit(text: str):
    for rx, label in _HIGH:
        if rx is None:
            continue
        if rx.search(text):
            return label
    return None


def _loose_hit(text: str):
    for m in _ASSIGN.finditer(text):
        val = m.group(1)
        if len(val) < 12 or _NOT_SECRET.match(val):
            continue
        return val
    return None


def scan(text, any_cloud_lane: bool = False) -> "dict | None":
    """Return a hit dict when `text` carries credential material, else None.

    `any_cloud_lane` selects the LOOSE tier, which only applies to cloud egress. The HIGH tier
    applies everywhere. Callers pass the already-resolved lane state, matching the admin rules'
    `scope: cloud_only` handling, so a rule and this floor cannot disagree about egress.
    """
    if not text or not isinstance(text, str):
        return None
    hi = _high_hit(text)
    if hi and high_mode() != "off":
        return {"tier": "high", "label": hi, "mode": high_mode()}
    if any_cloud_lane and loose_mode() != "off":
        loose = _loose_hit(text)
        if loose:
            return {"tier": "loose", "label": "credential assignment",
                    "mode": loose_mode() if loose_mode() != "off" else "cloud_only"}
    return None


BLOCK_MESSAGE = (
    "Your message appears to contain credential material ({label}). Credentials must not be "
    "pasted into chat or agent tasks - they end up in model context, tool logs and any files "
    "the agent writes. Load them from the environment or a secret store instead."
)