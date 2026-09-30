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
