"""
core/media_tools.py - agent tools for images, videos and speech (core/media.py).

  generate_image(prompt, aspect?, negative?)   -> markdown with the saved image
  generate_video(prompt, seconds?, aspect?)    -> markdown with the saved video
  transcribe_audio(path)                       -> the words in a WAV file

Each tool is offered to the model only once its job has a model for the
calling user. Cloud generations ask the user first (routes/agent.py, like
run_python) because they can cost money. Sub-agents never get these tools.
"""

import asyncio
import re
import unicodedata

from .registry import registry
from .request_context import get_current_user_id

MEDIA_TOOL_JOBS = {"generate_image": "image_gen", "generate_video": "video_gen",
                   "transcribe_audio": "transcribe"}


def _user():
    from .auth import _to_principal
    uid = get_current_user_id()
    return _to_principal(uid) if uid is not None else None


def job_ready(job: str) -> bool:
    from . import lanes
    try:
        return any(t.available() for t in lanes.targets(job, get_current_user_id()))
    except Exception:
        return False


def first_is_cloud(tool: str) -> tuple:
    """(True, provider) when this tool's job would go to a cloud model first."""
    from . import lanes
    job = MEDIA_TOOL_JOBS.get(tool)
    if not job:
        return False, None
    for t in lanes.targets(job, get_current_user_id()):
        if t.available():
            return t.is_cloud, (t.cm.provider_name if t.is_cloud else None)
    return False, None


def chat_image_tool_schema():
    """generate_image's schema for plain chat, or None. Only when the image job's
    first model is local (stable-diffusion.cpp on this PC): chat has
    no ask-first dialog, so a cloud model that may cost money stays on /image."""
    if not job_ready("image_gen") or first_is_cloud("generate_image")[0]:
        return None
    rt = registry.get("generate_image")
    return rt.schema if rt and rt.schema else None


async def generate_events(kind: str, args: dict):
    """Run a generation, yielding ("progress", {text, pct, elapsed}) while it works
    and finally ("result", tool_result_text, done_payload_or_None)."""
    from . import media
    user = _user()
    if user is None:
        yield ("result", "error: not signed in", None)
        return
    opts = {k: args.get(k) for k in ("aspect", "size", "negative", "seconds", "seed", "steps") if args.get(k) not in (None, "")}
    paths = [str(p) for p in (args.get("images") or []) if str(p or "").strip()] if kind == "image" else []
    if paths:
        # the user's own files only (e.g. generated/... from an earlier reply); this PC only
        from . import media_images
        want = args.get("mode") or ("change" if len(paths) == 1 else "combine")
        mode = "img2img" if want == "change" else "edit"
        route = media.edit_targets(user.id, mode)
        if not route:
            yield ("result", f"error: {media.edit_blocked(user.id, mode)}", None)
            return
        limit = 1 if mode == "img2img" else route[0].inst.edit_caps()["max_refs"]
        try:
            pics = media_images.prepare_inputs([{"path": p} for p in paths], limit)
        except media_images.InputError as e:
            yield ("result", f"error: {e}", None)
            return
        opts.update(inputs=pics, mode=mode, strength=args.get("strength"))
    try:
        job = media.start_job(user, kind, str(args.get("prompt") or ""), opts)
    except media.MediaError as e:
        yield ("result", f"error: {e}", None)
        return
    try:
        async for ev, data in media.job_events(job):
            if ev == "progress":
                yield ("progress", data)
            elif ev == "done":
                yield ("result", result_text(kind, data), data)
                return
            elif ev == "error":
                yield ("result", f"error: {data.get('message') or 'failed'}", None)
                return
    except asyncio.CancelledError:
        if job.task and not job.done:
            job.task.cancel()          # the chat was stopped -> sd-server's own cancel
        raise
    yield ("result", "error: the job ended without a result", None)


def result_text(kind: str, res: dict) -> str:
    return (f"Made the {kind} with {res['model']} ({res['where']}); it is saved and already shown to the "
            f"user. Include this markdown exactly in your reply:\n\n{res['markdown']}\n\n"
            f"Do not write an HTML page or any other file for it - reply with the markdown and a short "
            f"sentence.")


def chat_result_text(kind: str, res: dict) -> str:
    """What plain chat tells the model after a picture/clip is made. One short sentence: the server adds the picture and its
    download link to the reply itself (routes/chat/delivery.py _finalize_media), so the model no longer copies a path
    (it garbled it or said nothing) and the tool result stays small. Agent mode keeps result_text()."""
    return (f"Made the {kind} with {res['model']} ({res['where']}); it is saved and already shown to the user with a "
            f"download link. Reply with one short sentence; do not repeat the picture or the link.")


# How many pictures one chat turn may make: what the user asked for, never more than 4 (each takes minutes on this PC).
# The request is read as a list: the parts joined by "and", commas, "then" are each one picture (or the number in front of
# its noun): "a logo, a poster and a banner" = 3, "2 logos and a poster" = 3. A count that belongs to something else
# ("3 page poster", "3D", "a cat with 2 eyes", "an image of 4 frames") is not a number of pictures.
MAX_COPIES = 4
_NUMBER_WORDS = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10,
                 "a couple of": 2, "a couple": 2, "couple of": 2, "a few": 3, "few": 3,
                 "এক": 1, "দুই": 2, "দুটি": 2, "দুটো": 2, "তিন": 3, "চার": 4, "পাঁচ": 5, "ছয়": 6, "সাত": 7, "আট": 8, "নয়": 9, "দশ": 10}
_NOUNS = (r"(?:images?|pictures?|photos?|logos?|illustrations?|variations?|versions?|designs?|posters?|banners?|icons?|drawings?"
          r"|paintings?|sketch(?:es)?|wallpapers?|avatars?|thumbnails?|stickers?|flyers?|mockups?|renders?"
          r"|ছবি|চিত্র|লোগো|পোস্টার|ব্যানার|আইকন)")
_NUM = (r"(?:\d{1,2}|" + "|".join(re.escape(w) for w in sorted(_NUMBER_WORDS, key=len, reverse=True)) + r")(?:টি|টা|খানা)?")
# a number, up to two words, a picture noun - the number is not "of"/"with"/"in" something else
_COUNTED_RE = re.compile(r"(?<![\w])(?P<n>" + _NUM + r")(?![\w])\s+(?P<mid>(?:[^\W\d_]+[ \t]+){0,2}?)(?P<noun>" + _NOUNS + r")(?![\w])(?P<after>\s+\w+)?",
                         re.I)
_BN_COUNT_AFTER_RE = re.compile(r"(?<![\w])(\d+)(?:টি|টা|খানা)(?![\w])")
_NOUN_RE = re.compile(r"(?<![\w])" + _NOUNS + r"(?![\w])", re.I)
_PLURAL_RE = re.compile(r"(?<![\w])(?:images|pictures|photos|logos|illustrations|variations|versions|designs|posters|banners|icons|drawings"
                        r"|paintings|sketches|wallpapers|avatars|thumbnails|stickers|flyers|mockups|renders)(?![\w])", re.I)
_DRAW_RE = re.compile(r"\b(?:draw|paint|sketch|illustrate|render)\b|আঁক|বানাও|তৈরি", re.I)
_DETERMINER_RE = re.compile(r"\b(?:a|an|another|one|the|this|that|my|our)\b", re.I)
_LEAD_ITEM_RE = re.compile(r"^\s*(?:(?:a|an|another|one)\b|" + _NUM + r"(?![\w]))", re.I)
_SPLIT_RE = re.compile(r"\s*(?:[,;]|\.\s|\n|\s(?:and then|and also|after that|and|then|also|plus)\s|\s(?:এবং|আর|তারপর)\s)\s*", re.I)
# what a number in front of a picture noun may really count (so it is not a number of pictures)
_NOT_PICTURES = {"page", "pages", "slide", "slides", "step", "steps", "d", "px", "pixel", "pixels", "inch", "inches", "colour", "colours",
                 "color", "colors", "column", "columns", "row", "rows", "section", "sections", "word", "words", "line", "lines"}
_OF_BEFORE = {"of", "with", "having", "has", "in", "per", "each", "every", "for", "from", "by"}

CHAT_ALREADY_MADE = ("note: the picture is already made and shown to the user, with a download link. "
                     "Do not make it again: reply with one short sentence.")
CHAT_SAME_AGAIN = ("note: that is the same picture as one just made. The user asked for more than one, so make a different one "
                   "(another subject or style), or reply with one short sentence if they are all made.")


def _number(tok: str) -> int:
    t = re.sub(r"(?:টি|টা|খানা)$", "", tok.strip().lower())
    if t in _NUMBER_WORDS:
        return _NUMBER_WORDS[t]
    try:
        return int("".join(str(unicodedata.digit(c)) for c in t))     # digits of any script (Bangla, Arabic-Indic, ...)
    except (TypeError, ValueError):
        return 1


def _segment_count(seg: str) -> tuple:
    """(pictures this part of the request asks for, whether it names a picture or a drawing action)."""
    for m in _COUNTED_RE.finditer(seg):
        before = re.findall(r"[\w]+", seg[:m.start()].lower())[-1:]
        mid = set(re.findall(r"\w+", m.group("mid").lower()))
        after = (m.group("after") or "").strip().lower()
        if (before and before[0] in _OF_BEFORE) or (mid & _NOT_PICTURES) or after in _NOT_PICTURES:
            continue
        n = _number(m.group("n"))
        if 1 <= n <= 20:
            return n, True
    m = _BN_COUNT_AFTER_RE.search(seg)
    if m and (_NOUN_RE.search(seg) or _DRAW_RE.search(seg)):
        return max(1, min(20, _number(m.group(0)))), True
    if _NOUN_RE.search(seg):
        return 1, True
    if _DRAW_RE.search(seg):
        n = re.search(r"\b(?:draw|paint|sketch|illustrate|render)\s+(?:me\s+)?(" + _NUM + r")(?![\w])", seg, re.I)
        return (_number(n.group(1)) if n else 1), True
    return 0, False


def copies_wanted(query: str, kind: str = "image") -> int:
    """Pictures the user's own message asks for, at most MAX_COPIES: the parts of the request joined by "and" / commas / "then"
    (one each, or the number in front of the noun), numbers 1-20 as digits of any script or as words, "a couple" = 2, "a few" = 3.
    A video is always one."""
    if kind == "video":
        return 1
    total, listed_by_verb, first = 0, False, True
    for seg in _SPLIT_RE.split(str(query or "")):
        if not seg or not seg.strip():
            continue
        n, hit = _segment_count(seg)
        if hit:
            bare_plural = bool(_PLURAL_RE.search(seg)) and not _DETERMINER_RE.search(seg) and not _COUNTED_RE.search(seg)
            if bare_plural and total:
                n = 0                       # "2 logos and posters": the plural noun joins the list, it is not one more picture
            listed_by_verb = listed_by_verb or (first and not _NOUN_RE.search(seg))
            first = False
            total += n
        elif (listed_by_verb and _LEAD_ITEM_RE.match(seg)) or (total and re.match(r"\s*(?:\d{1,2})(?![\w])\s+of\b", seg)):
            lead = re.match(r"\s*(" + _NUM + r")(?![\w])", seg, re.I)
            total += _number(lead.group(1)) if lead and lead.group(1)[0].isdigit() else 1      # "draw a cat and a dog", "... then 1 of dogs"
    return max(1, min(MAX_COPIES, total or 1))


# Words that mean the message asks for something besides the picture ("write a report and include an image", "search the web and
# make a banner", "draw a cat and tell me the weather"). Then the chat keeps its tools after the picture is made.
_OTHER_INTENT_RE = re.compile(
    r"\b(write|report|summar\w*|search|look\s*up|research|find|weather|news|forecast|price|tell\s+me|explain\w*|describ\w*|document"
    r"|blog|article|essay|email|letter|poem|story|analy\w*|compare|translate|calculat\w*|code|script|spreadsheet|table|presentation"
    r"|slides?|deck|pdf|plan|list|what|how|why|who|when|where)\b", re.I)


def has_other_intent(query: str) -> bool:
    """True when the message asks for more than pictures: the tools stay on after the picture is made."""
    return bool(_OTHER_INTENT_RE.search(str(query or "")))


def _prompt_words(prompt: str) -> set:
    return set(re.findall(r"\w+", str(prompt or "").lower()))


def repeat_blocked(made: int, allowed: int, prompt: str, made_prompts: list) -> bool:
    """True when another picture must not be made this turn: as many as were asked for exist already, or this prompt is
    (nearly) one already used. While more are still wanted only an almost identical prompt counts (95% of the words the
    same): two pictures of one subject in two styles are what "make 2 logos" asks for."""
    if made >= allowed:
        return True
    words = _prompt_words(prompt)
    if not words:
        return False
    return any(len(words & old) / max(len(words), len(old)) >= 0.95 for old in map(_prompt_words, made_prompts) if old)


async def _generate(kind: str, args: dict) -> str:
    out = "error: failed"
    async for ev, *rest in generate_events(kind, args):
        if ev == "result":
            out = rest[0]
    return out


async def tool_generate_image(args: dict) -> str:
    return await _generate("image", args or {})


async def tool_generate_video(args: dict) -> str:
    return await _generate("video", args or {})


async def tool_transcribe_audio(args: dict) -> str:
    from . import media
    from .agent_tools import _common_resolve
    user = _user()
    if user is None:
        return "error: not signed in"
    path = str((args or {}).get("path") or "").strip()
    if not path:
        return "error: give the path of a .wav file in your files"
    try:
        p = _common_resolve(path)
    except Exception as e:
        return f"error: {e}"
    if not p.is_file():
        return f"error: file not found: {path}"
    if p.suffix.lower() != ".wav":
        return ("error: only .wav files can be transcribed here - ask the user to attach the audio with the "
                "attach button (it is converted and transcribed automatically) or use the mic button")
    try:
        res = await media.transcribe(user, p.read_bytes(), (args or {}).get("language"))
    except media.MediaError as e:
        return f"error: {e}"
    return res["text"] or "(no speech found in the audio)"


def register_media_tools() -> None:
    specs = [
        ("generate_image", tool_generate_image, "image_gen", {
            "description": "Create a picture from a text description and save it to the user's files. "
                           "Use only when the user asks for an image/picture/illustration to be made. "
                           "Write a detailed visual description (subject, style, lighting, composition).",
            "parameters": {"type": "object", "properties": {
                "prompt": {"type": "string", "description": "detailed description of the image, or of the "
                           "change to make; refer to input pictures as <image1>, <image2>"},
                "aspect": {"type": "string", "enum": ["square", "wide", "tall"]},
                "size": {"type": "string", "enum": ["small", "medium", "large", "xlarge"],
                         "description": "optional: only when the user asks for a size (smaller is faster)"},
                "negative": {"type": "string", "description": "optional: things to avoid in the image"},
                "steps": {"type": "integer", "minimum": 1, "maximum": 60,
                          "description": "optional: leave unset (the model's tuned value is used); more steps is slower"},
                "images": {"type": "array", "maxItems": 10, "items": {"type": "string"},
                           "description": "optional: paths of pictures in the user's files to start from "
                                          "(e.g. generated/... from an earlier reply)"},
                "mode": {"type": "string", "enum": ["change", "combine"],
                         "description": "with images: change = restyle/alter the one picture, "
                                        "combine = new picture using the pictures as references"},
                "strength": {"type": "number", "minimum": 0.05, "maximum": 1,
                             "description": "for change: how much to change it (0.3 small, 0.8 a lot)"}},
                "required": ["prompt"]}}),
        ("generate_video", tool_generate_video, "video_gen", {
            "description": "Create a short video clip from a text description and save it to the user's files. "
                           "Slow (minutes). Use only when the user explicitly asks for a video.",
            "parameters": {"type": "object", "properties": {
                "prompt": {"type": "string", "description": "detailed description of the scene and motion"},
                "seconds": {"type": "integer", "minimum": 1, "maximum": 20},
                "aspect": {"type": "string", "enum": ["wide", "tall", "square"]}},
                "required": ["prompt"]}}),
        ("transcribe_audio", tool_transcribe_audio, "transcribe", {
            "description": "Turn speech in a .wav file from the user's files into text.",
            "parameters": {"type": "object", "properties": {
                "path": {"type": "string", "description": "path of the .wav file"},
                "language": {"type": "string", "description": "optional language code, e.g. en or bn"}},
                "required": ["path"]}}),
    ]
    for name, fn, job, fnspec in specs:
        registry.register(
            name, fn, {"type": "function", "function": {"name": name, **fnspec}},
            source="media", meta={"label": fnspec["description"][:60], "visible": (lambda j=job: job_ready(j))},
            replace=True)
