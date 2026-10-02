"""core/media/streaming.py - live (chunked) speech to text.

The batch endpoint (POST /media/transcribe) waits for the whole recording.
The stream endpoints slice the mic feed into ~4s windows while the user is
still talking and transcribe each through the normal transcribe lane, so
interim text appears live:

  POST /media/transcribe_stream/start   {language?} -> {stream_id}
  POST /media/transcribe_stream/chunk   multipart WAV + stream_id + seq -> {seq, text, interim}
  POST /media/transcribe_stream/finish  {stream_id} -> {text, chunks, ms, audio_s, rtf}
  POST /media/transcribe_stream/cancel  {stream_id} -> {ok: true}

Windows overlap (the client re-sends ~0.75s of the previous window), so the
join strips a repeated word-run at each boundary (join_interims). Sessions
live in memory, belong to one user, and expire after MAX_AGE_S.
"""

import asyncio
import re
import secrets
import time
from typing import Optional

from .constants import MediaError, media_cfg
from .service import transcribe as _transcribe, wav_seconds
from .storage import check_wav

MAX_CHUNKS = 200          # ~13 min of 4s windows; the mic caps at 10 min anyway
MAX_CHUNK_B = 2 * 1024 * 1024   # one window is ~150 KB; this only trips on abuse
MAX_AGE_S = 11 * 60
SILENCE_RMS = 0.01        # windows quieter than this are room noise: answered
                          # empty without waking whisper (which invents
                          # "Thank you"/"Hello" on silence)

_STREAMS: dict = {}


def _purge() -> None:
    now = time.time()
    for sid in [s for s, st in _STREAMS.items() if now - st["created"] > MAX_AGE_S]:
        _STREAMS.pop(sid, None)


def _overlap_tail(prev_words: list, new_words: list, max_k: int = 12) -> list:
    """new_words minus a leading run that repeats prev_words' tail."""
    if not prev_words or not new_words:
        return new_words
    n = min(max_k, len(prev_words), len(new_words))
    for k in range(n, 0, -1):
        if prev_words[-k:] == new_words[:k]:
            return new_words[k:]
        # same words, different case ("Hello" vs "hello" at a cut)
        if [w.lower() for w in prev_words[-k:]] == [w.lower() for w in new_words[:k]]:
            return new_words[k:]
    return new_words


def join_interims(texts: list) -> str:
    """Join per-window transcripts, stripping overlap repeats at boundaries."""
    words: list = []
    for t in texts:
        words += _overlap_tail(words, str(t or "").split())
    return " ".join(words).strip()


def wav_rms(wav: bytes) -> Optional[float]:
    """Loudness of a WAV (0..1), from its PCM data (None when unreadable)."""
    try:
        import array
        import math
        import struct
        if len(wav) < 44 or wav[:4] != b"RIFF" or wav[8:12] != b"WAVE":
            return None
        pos = wav.find(b"data", 12)
        if pos < 0:
            return None
        size = struct.unpack("<I", wav[pos + 4:pos + 8])[0]
        raw = wav[pos + 8:pos + 8 + size]
        n = len(raw) // 2
        if n < 16:
            return None
        samples = array.array("h")
        samples.frombytes(raw[:n * 2])
        stride = max(1, n // 32000)
        sel = samples[::stride]
        return math.sqrt(sum(v * v for v in sel) / len(sel)) / 32768.0
    except Exception:
        return None


def _get(uid, stream_id: str) -> dict:
    st = _STREAMS.get(stream_id)
    if st is None or st["uid"] != uid or time.time() - st["created"] > MAX_AGE_S:
        if st is not None and time.time() - st["created"] > MAX_AGE_S:
            _STREAMS.pop(stream_id, None)
        raise MediaError("That live transcription isn't here any more - start again.")
    return st


async def stream_start(user, language: Optional[str] = None) -> dict:
    """Open a live session. -> {stream_id}."""
    from .. import lanes
    uid = getattr(user, "id", None)
    lang = (language or "").strip().lower() or None
    if lang and lang != "auto" and not re.fullmatch(r"[a-z]{2,3}", lang):
        lang = None
    route = [t for t in lanes.targets("transcribe", uid) if t.available()]
    if not route:
        raise MediaError("Speech to text isn't set up yet - add a model in Settings -> Models & Jobs.")
    _purge()
    sid = secrets.token_urlsafe(24)
    _STREAMS[sid] = {"uid": uid, "lang": lang, "texts": [], "bytes": 0,
                     "ms": 0, "audio_s": 0.0, "created": time.time(),
                     "lock": asyncio.Lock()}
    return {"stream_id": sid}


async def stream_chunk(user, stream_id: str, seq: int, wav: bytes) -> dict:
    """Transcribe one window. -> {seq, text, interim}. Chunks must arrive in order."""
    st = _get(getattr(user, "id", None), stream_id)
    if not isinstance(seq, int) or seq != len(st["texts"]):
        raise MediaError(f"Window {seq} arrived out of order - start again.")
    if len(st["texts"]) >= MAX_CHUNKS:
        raise MediaError("That recording is too long - stop and start again.")
    if len(wav) > MAX_CHUNK_B:
        raise MediaError("That window is too big - start again.")
    check_wav(wav)
    cap = int(media_cfg().get("max_audio_mb") or 25) * 1024 * 1024
    if st["bytes"] + len(wav) > cap:
        raise MediaError(f"That recording is too long ({cap // (1024 * 1024)} MB max).")
    async with st["lock"]:
        if seq != len(st["texts"]):   # a retry raced the first attempt
            raise MediaError(f"Window {seq} arrived out of order - start again.")
        rms = wav_rms(wav)
        if rms is not None and rms < SILENCE_RMS:
            text, ms, secs = "", 0, wav_seconds(wav) or 0.0
        else:
            res = await _transcribe(user, wav, st["lang"])
            text, ms, secs = res["text"], res.get("ms") or 0, res.get("audio_s") or 0
        st["texts"].append(text)
        st["bytes"] += len(wav)
        st["ms"] += ms
        st["audio_s"] += secs
        interim = join_interims(st["texts"])
    return {"seq": seq, "text": text, "interim": interim}


async def stream_finish(user, stream_id: str) -> dict:
    """Close the session, return the joined transcript."""
    st = _get(getattr(user, "id", None), stream_id)
    async with st["lock"]:
        _STREAMS.pop(stream_id, None)
    text = join_interims(st["texts"])
    out = {"text": text, "chunks": len(st["texts"]), "ms": st["ms"]}
    if st["audio_s"]:
        out["audio_s"] = round(st["audio_s"], 1)
        out["rtf"] = round(st["ms"] / 1000 / st["audio_s"], 2) if st["ms"] else 0.0
    return out


async def stream_cancel(user, stream_id: str) -> dict:
    """Discard a session without transcribing anything further."""
    uid = getattr(user, "id", None)
    st = _STREAMS.get(stream_id)
    if st is not None and st["uid"] == uid:
        _STREAMS.pop(stream_id, None)
    return {"ok": True}
