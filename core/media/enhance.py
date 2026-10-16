"""Prompt hygiene for the local image model: a short, bare description gets a quality tail. Deterministic on purpose - no extra
model call (the chat server has one slot) and nothing that changes what the user asked for."""
import re

from .constants import media_cfg

SHORT_WORDS = 24
TAIL = "highly detailed, sharp focus, balanced composition, natural lighting"
# the prompt already says how it should look, or it carries text to draw: leave it alone
_STYLED = re.compile(
    r"(?i)\b(photo(?:graph|realistic)?|render(?:ed)?|illustration|painting|watercolou?r|sketch|cartoon|anime|vector|logo|poster|"
    r"cinematic|8k|4k|hdr|bokeh|macro|isometric|pixel art|oil|pencil|3d|flat design|style|lighting|detailed)\b")


def enhance(prompt: str, opts: dict) -> str:
    """The prompt to send to the local model (the user's own wording stays the file name and the policy-checked text)."""
    p = str(prompt or "").strip()
    if not p or opts.get("inputs") or media_cfg().get("enhance_prompt", True) is False:
        return p
    if len(p.split()) > SHORT_WORDS or _STYLED.search(p) or re.search(r"[\"'“”‘’«»]", p):
        return p
    return p.rstrip(" .,;") + ", " + TAIL
