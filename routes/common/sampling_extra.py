"""Extra llama.cpp sampler fields (repeat_last_n, frequency_penalty, seed, DRY, dynamic
temperature) taken from a chat/agent request. The result goes in the `extra` argument of
_llm_chat_stream, which only the local main llama-server receives."""
from typing import Optional

from pydantic import BaseModel


class SamplerFields(BaseModel):
    """Mixed into ChatRunRequest and AgentRequest; None = not sent by the client."""
    repeat_last_n: Optional[int] = None
    frequency_penalty: Optional[float] = None
    seed: Optional[int] = None
    dry_multiplier: Optional[float] = None
    dry_base: Optional[float] = None
    dry_allowed_length: Optional[int] = None
    dry_penalty_last_n: Optional[int] = None
    dynatemp_range: Optional[float] = None
    dynatemp_exponent: Optional[float] = None


def _clamp(v, lo, hi):
    return max(lo, min(hi, v))


def sampler_extra(req) -> Optional[dict]:
    """Payload fields for the local server. Off values are left out: seed < 0 is random,
    dry_multiplier 0 disables DRY, dynatemp_range 0 disables dynamic temperature."""
    out: dict = {}
    if req.repeat_last_n is not None:
        out["repeat_last_n"] = _clamp(int(req.repeat_last_n), -1, 8192)
    if req.frequency_penalty is not None:
        out["frequency_penalty"] = _clamp(float(req.frequency_penalty), -2.0, 2.0)
    if req.seed is not None and int(req.seed) >= 0:
        out["seed"] = min(int(req.seed), 2147483647)
    if req.dry_multiplier is not None and float(req.dry_multiplier) > 0:
        out["dry_multiplier"] = _clamp(float(req.dry_multiplier), 0.0, 5.0)
        if req.dry_base is not None:
            out["dry_base"] = _clamp(float(req.dry_base), 1.0, 4.0)
        if req.dry_allowed_length is not None:
            out["dry_allowed_length"] = _clamp(int(req.dry_allowed_length), 0, 64)
        if req.dry_penalty_last_n is not None:
            out["dry_penalty_last_n"] = _clamp(int(req.dry_penalty_last_n), -1, 32768)
    if req.dynatemp_range is not None and float(req.dynatemp_range) > 0:
        out["dynatemp_range"] = _clamp(float(req.dynatemp_range), 0.0, 2.0)
        if req.dynatemp_exponent is not None:
            out["dynatemp_exponent"] = _clamp(float(req.dynatemp_exponent), 0.1, 5.0)
    return out or None


# Samplers that make a model spell text it has already seen differently: DRY punishes repeating a sequence that is in the
# context (the penalty grows exponentially with the length of the repeat), the repeat / presence / frequency penalties push
# away from tokens already used, dynamic temperature adds noise. Fine for prose, wrong for a tool call, which copies file
# names, paths and old_string text out of its context: one changed character and the call misses its file.
_COPY_UNSAFE = ("dry_multiplier", "dry_base", "dry_allowed_length", "dry_penalty_last_n", "dynatemp_range",
                "dynatemp_exponent", "frequency_penalty", "repeat_last_n")


def tool_step_sampling(req, tools, repeat_penalty):
    """(repeat_penalty, presence_penalty, extra) for one model call.

    A step that offers tools runs with DRY, every repeat penalty and dynamic temperature OFF, whatever the saved sampler
    panel says (the web UI even defaults DRY on). The zeros are sent explicitly, so a server started with its own DRY
    default stays off too. A step with no tools (a plain answer) keeps the user's sampling. Temperature, top-p, top-k and
    min-p are left to the panel."""
    extra = sampler_extra(req)
    if not tools:
        return repeat_penalty, req.presence_penalty, extra
    safe = {k: v for k, v in (extra or {}).items() if k not in _COPY_UNSAFE}
    safe.update({"dry_multiplier": 0.0, "dynatemp_range": 0.0, "frequency_penalty": 0.0})
    return 1.0, 0.0, safe
