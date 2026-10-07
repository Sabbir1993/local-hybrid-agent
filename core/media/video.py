"""Reading a video: sample frames (and the audio) with ffmpeg so the Image reader and speech-to-text
can describe it. The video bytes only live in a temp folder for the length of the call; nothing is
stored or logged."""

import asyncio
import base64
import json
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Optional

from .constants import MediaError, media_cfg

_SCENE_RE = re.compile(r"pts_time:([0-9.]+)")
_STEP_TIMEOUT_S = 90
_NOWIN = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def video_limits() -> dict:
    vu = media_cfg().get("video_understanding")
    vu = vu if isinstance(vu, dict) else {}
    def _n(v, d, lo, hi):
        try:
            return max(lo, min(hi, int(v)))
        except (TypeError, ValueError):
            return d
    return {"max_mb": _n(media_cfg().get("max_video_mb"), 100, 1, 2000),
            "max_frames": _n(vu.get("max_frames"), 16, 1, 64),
            "max_minutes": _n(vu.get("max_minutes"), 10, 1, 180)}


def _ffmpeg() -> Optional[Path]:
    mod = sys.modules.get("core.small_model")
    from ..small_model import find_ffmpeg
    return (getattr(mod, "find_ffmpeg", find_ffmpeg) if mod else find_ffmpeg)()


def ffmpeg_ready() -> bool:
    return _ffmpeg() is not None


def _run(cmd: list, **kw) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, timeout=_STEP_TIMEOUT_S, creationflags=_NOWIN, **kw)


def _probe(ffmpeg: Path, src: Path) -> dict:
    """{duration, has_audio, has_video} from ffprobe (next to ffmpeg), else from ffmpeg's banner."""
    probe = ffmpeg.with_name("ffprobe" + ffmpeg.suffix)
    if probe.is_file():
        r = _run([str(probe), "-v", "error", "-show_entries", "format=duration:stream=codec_type",
                  "-of", "json", str(src)])
        try:
            j = json.loads(r.stdout or b"{}")
            kinds = [s.get("codec_type") for s in j.get("streams") or []]
            return {"duration": float((j.get("format") or {}).get("duration") or 0),
                    "has_audio": "audio" in kinds, "has_video": "video" in kinds}
        except (ValueError, TypeError):
            pass
    r = _run([str(ffmpeg), "-hide_banner", "-i", str(src)])
    err = (r.stderr or b"").decode("utf-8", "replace")
    m = re.search(r"Duration:\s*(\d+):(\d+):([\d.]+)", err)
    dur = int(m.group(1)) * 3600 + int(m.group(2)) * 60 + float(m.group(3)) if m else 0.0
    return {"duration": dur, "has_audio": "Audio:" in err, "has_video": "Video:" in err}


def pick_times(duration: float, scenes: list, max_frames: int) -> list:
    """Frame times: scene changes plus an even grid (so quiet stretches still get covered),
    thinned to `max_frames` evenly across the video. Always includes the very start."""
    grid_n = max(2, min(max_frames, int(duration // 3) + 1))
    grid = [duration * (i + 0.5) / grid_n for i in range(grid_n)]
    cand = sorted({round(t, 2) for t in [0.1, *scenes, *grid] if 0 <= t < max(duration, 0.2)})
    merged = []
    for t in cand:                       # a scene cut and a grid point a second apart are one moment
        if not merged or t - merged[-1] >= 1.0:
            merged.append(t)
    if len(merged) <= max_frames:
        return merged
    step = (len(merged) - 1) / (max_frames - 1) if max_frames > 1 else 0
    return [merged[round(i * step)] for i in range(max_frames)]


def _scene_times(ffmpeg: Path, src: Path) -> list:
    r = _run([str(ffmpeg), "-hide_banner", "-i", str(src), "-an", "-vf", "select='gt(scene,0.3)',showinfo",
              "-f", "null", "-"])
    return [float(x) for x in _SCENE_RE.findall((r.stderr or b"").decode("utf-8", "replace"))][:200]


def _frame(ffmpeg: Path, src: Path, t: float) -> Optional[bytes]:
    r = _run([str(ffmpeg), "-hide_banner", "-loglevel", "error", "-ss", f"{t:.2f}", "-i", str(src),
              "-frames:v", "1", "-vf", "scale=w=1024:h=1024:force_original_aspect_ratio=decrease",
              "-q:v", "4", "-f", "image2pipe", "-vcodec", "mjpeg", "-"])
    return r.stdout if r.returncode == 0 and r.stdout[:2] == b"\xff\xd8" else None


def _audio(ffmpeg: Path, src: Path, seconds: float) -> Optional[bytes]:
    r = _run([str(ffmpeg), "-hide_banner", "-loglevel", "error", "-i", str(src), "-vn", "-t", f"{seconds:.0f}",
              "-ac", "1", "-ar", "16000", "-f", "wav", "-"])
    return r.stdout if r.returncode == 0 and r.stdout[:4] == b"RIFF" else None


def extract(data: bytes, max_frames: int, max_minutes: int) -> dict:
    """-> {duration, frames: [(t_seconds, jpeg_bytes)], wav: bytes | None}. Blocking."""
    ffmpeg = _ffmpeg()
    if ffmpeg is None:
        raise MediaError("Reading videos isn't set up - ffmpeg wasn't found on this PC.")
    tmp = tempfile.mkdtemp(prefix="vid-")
    try:
        src = Path(tmp) / "in.bin"
        src.write_bytes(data)
        try:
            info = _probe(ffmpeg, src)
            if not info["has_video"] or info["duration"] <= 0:
                raise MediaError("That doesn't look like a video file I can read.")
            if info["duration"] > max_minutes * 60:
                raise MediaError(f"That video is {info['duration'] / 60:.0f} minutes long "
                                 f"({max_minutes} minutes max).")
            times = pick_times(info["duration"], _scene_times(ffmpeg, src), max_frames)
            frames = [(t, f) for t in times for f in [_frame(ffmpeg, src, t)] if f]
            if not frames:
                raise MediaError("Couldn't read any pictures from that video.")
            wav = _audio(ffmpeg, src, info["duration"]) if info["has_audio"] else None
        except subprocess.TimeoutExpired:
            raise MediaError("Reading that video took too long.")
        return {"duration": info["duration"], "frames": frames, "wav": wav}
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


async def read_video(user, data: bytes, transcribe: bool = True) -> dict:
    """-> {duration, frames: [{t, b64, mime}], transcript, transcript_note}. Frames are JPEGs the
    client then describes with /agent/vision; the audio track (if any) is transcribed here so the
    WAV never travels back to the browser."""
    lim = video_limits()
    res = await asyncio.to_thread(extract, data, lim["max_frames"], lim["max_minutes"])
    out = {"duration": round(res["duration"], 1),
           "frames": [{"t": round(t, 1), "mime": "image/jpeg", "b64": base64.b64encode(f).decode()}
                      for t, f in res["frames"]],
           "transcript": None, "transcript_note": None}
    if transcribe and res["wav"]:
        mod = sys.modules.get("core.media")
        fn = getattr(mod, "transcribe", None)
        if fn is None:
            from .service import transcribe as fn
        try:
            out["transcript"] = (await fn(user, res["wav"]))["text"] or None
        except MediaError as e:                # no speech model, or cloud speech not allowed
            out["transcript_note"] = str(e)
    return out
