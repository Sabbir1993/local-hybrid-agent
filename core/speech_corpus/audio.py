"""Strict checks for one uploaded clip. Only plain 16 kHz mono 16-bit PCM WAV is accepted; nothing is converted or run.

The browser page records at 16 kHz mono and encodes that exact format, so anything else is a wrong client or junk.
"""
import struct

import numpy as np

RATE = 16000
MIN_SECONDS = 1.0
SILENT_RMS = 0.004          # about -48 dBFS: room noise, no speech
SILENT_PEAK = 0.03
CLIP_LEVEL = 32000          # |sample| at or above this counts as clipped
MAX_CLIPPED_RATIO = 0.01    # more than 1% clipped samples: too loud / distorted


class ClipError(Exception):
    """The clip is not usable; the message tells the speaker what to do."""


def check_clip(data: bytes, max_seconds: float) -> dict:
    """-> {seconds, rms, peak, clipped_ratio}. Raises ClipError."""
    if len(data) < 44 or data[:4] != b"RIFF" or data[8:12] != b"WAVE":
        raise ClipError("The recording is not a WAV file. Please record again.")
    fmt = None
    pcm = None
    pos = 12
    while pos + 8 <= len(data):
        cid = data[pos:pos + 4]
        size = struct.unpack("<I", data[pos + 4:pos + 8])[0]
        body = data[pos + 8:pos + 8 + size]
        if cid == b"fmt " and len(body) >= 16:
            fmt = struct.unpack("<HHIIHH", body[:16])
        elif cid == b"data":
            pcm = body
            break
        pos += 8 + size + (size & 1)        # chunks are word-aligned
    if fmt is None or pcm is None:
        raise ClipError("The recording is damaged. Please record again.")
    audio_format, channels, rate, _byte_rate, _align, bits = fmt
    if audio_format != 1 or channels != 1 or rate != RATE or bits != 16:
        raise ClipError("The recording must be 16 kHz mono 16-bit WAV. Please reload the page and record again.")
    if len(pcm) % 2:
        pcm = pcm[:-1]
    samples = np.frombuffer(pcm, dtype="<i2")
    seconds = len(samples) / RATE
    if seconds < MIN_SECONDS:
        raise ClipError("The recording is too short. Press Record, read the whole line, then Stop.")
    if seconds > max_seconds:
        raise ClipError(f"The recording is longer than {int(max_seconds)} seconds. Please record the line again.")
    x = samples.astype(np.float32) / 32768.0
    rms = float(np.sqrt(np.mean(x * x)))
    peak = float(np.max(np.abs(x)))
    clipped = float(np.mean(np.abs(samples.astype(np.int32)) >= CLIP_LEVEL))
    if rms < SILENT_RMS or peak < SILENT_PEAK:
        raise ClipError("We could not hear you. Move closer to the microphone and record again.")
    if clipped > MAX_CLIPPED_RATIO:
        raise ClipError("The recording is too loud and distorted. Move a little away from the microphone and record again.")
    return {"seconds": round(seconds, 3), "rms": round(rms, 5), "peak": round(peak, 5),
            "clipped_ratio": round(clipped, 5)}
