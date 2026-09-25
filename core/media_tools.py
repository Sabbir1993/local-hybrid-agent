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
    opts = {k: args.get(k) for k in ("aspect", "size", "negative", "seconds", "seed") if args.get(k) not in (None, "")}
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
