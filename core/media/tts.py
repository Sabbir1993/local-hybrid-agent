"""Local text-to-speech for the voice conversation (CPU, sherpa-onnx; audio never leaves this PC).

  clean_for_speech(text)      markdown / links / code removed, card numbers masked, ready to be read aloud
  voice_for(text)             "bn" when the text is mostly Bengali script, else "en"
  split_for_tts(text)         pieces of at most `limit` characters, cut at sentence ends
  synthesize_stream(text)     generator of (pcm16 bytes, sample_rate), one item per piece  <- the unit a streaming
                              transport (WebSocket / WebRTC) will forward as it is produced
  synthesize_wav(text)        the same, joined into one WAV (what POST /media/speak returns)
  status()                    what is installed / ready, for Settings and the voice button

Models live in <media_dirs.voice_models>/tts/<dir>/ (see media.tts.voices in config/app.json):
  type "kokoro": model.onnx, voices.bin, tokens.txt, espeak-ng-data/        (slow on CPU: ~2x slower than real time)
  type "vits"  : *.onnx, tokens.txt, optional lexicon.txt, espeak-ng-data/   (Piper English, Coqui Bangla)
sherpa-onnx is imported lazily: without it, or without a model, status() says what is missing and speaking
raises MediaError with a plain message, so the rest of the app is unaffected.
"""
import re
import struct
import threading
import time
from pathlib import Path
from typing import Iterator, Optional

from .constants import MediaError, media_cfg

MAX_TEXT_CHARS = 1200
PIECE_CHARS = 300

_DEFAULTS = {
    "enabled": True,
    "speed": 1.0,
    "threads": 6,
    "voices": {
        "en": {"type": "vits", "dir": "vits-piper-en_US-lessac-medium", "speaker": 0},
        "bn": {"type": "vits", "dir": "vits-coqui-bn-custom_female", "speaker": 0},
    },
}

_engines: dict = {}            # voice -> sherpa OfflineTts
_locks: dict = {}              # voice -> Lock (OfflineTts.generate is not re-entrant)
_load_lock = threading.Lock()


def tts_cfg() -> dict:
    raw = media_cfg().get("tts")
    cfg = dict(_DEFAULTS)
    if isinstance(raw, dict):
        cfg.update({k: v for k, v in raw.items() if k != "voices"})
        voices = dict(_DEFAULTS["voices"])
        for name, spec in (raw.get("voices") or {}).items():
            if isinstance(spec, dict):
                voices[name] = {**voices.get(name, {}), **spec}
        cfg["voices"] = voices
    try:
        cfg["speed"] = min(2.0, max(0.5, float(cfg.get("speed") or 1.0)))
        cfg["threads"] = min(16, max(1, int(cfg.get("threads") or 2)))
    except (TypeError, ValueError):
        cfg["speed"], cfg["threads"] = 1.0, 2
    return cfg


# ---------------------------------------------------------------- text preparation

_FENCE_RE = re.compile(r"```[\s\S]*?```|~~~[\s\S]*?~~~")
_INLINE_CODE_RE = re.compile(r"`([^`\n]*)`")
_IMAGE_RE = re.compile(r"!\[[^\]]*\]\([^)]*\)")
_LINK_RE = re.compile(r"\[([^\]]+)\]\([^)]*\)")
_URL_RE = re.compile(r"\bhttps?://\S+|\bwww\.\S+", re.I)
_HEADING_RE = re.compile(r"^\s{0,3}#{1,6}\s*", re.M)
_BULLET_RE = re.compile(r"^\s*(?:[-*+•]|\d{1,3}[.)])\s+", re.M)
_QUOTE_RE = re.compile(r"^\s*>\s?", re.M)
_TABLE_RULE_RE = re.compile(r"^\s*\|?[\s:|-]{3,}\|?\s*$", re.M)
_EMOJI_RE = re.compile("[\U0001F000-\U0001FFFF\U00002600-\U000027BF\U0000FE0F\U0000200D]")
_THINK_RE = re.compile(r"<think>[\s\S]*?(?:</think>|$)", re.I)
_CARD_TAG_RE = re.compile(r"\[card \*{4}\d{4}\]")


def clean_for_speech(text: str) -> str:
    """Plain spoken text. A card number is never read aloud: spoken output cannot be reviewed afterwards."""
    t = str(text or "")
    t = _THINK_RE.sub(" ", t)
    t = _FENCE_RE.sub(" code omitted. ", t)
    t = _IMAGE_RE.sub(" ", t)
    t = _LINK_RE.sub(r"\1", t)
    t = _URL_RE.sub(" link ", t)
    t = _INLINE_CODE_RE.sub(r"\1", t)
    t = _HEADING_RE.sub("", t)
    t = _TABLE_RULE_RE.sub(" ", t)
    t = _QUOTE_RE.sub("", t)
    t = _BULLET_RE.sub("", t)
    t = t.replace("|", ", ")
    t = re.sub(r"[*_~]{1,3}", "", t)
    t = _EMOJI_RE.sub("", t)
    try:
        from .. import pan
        t = pan.mask_pans(t)[0]
    except Exception:
        pass
    t = _CARD_TAG_RE.sub("a card number was hidden", t)
    return re.sub(r"\s+", " ", t).strip()


def voice_for(text: str) -> str:
    """"bn" when Bengali script is at least as common as Latin letters in `text` (English loanwords inside a
    Bangla sentence stay Bangla), else "en"."""
    bn = sum(1 for ch in text if "ঀ" <= ch <= "৿")
    en = sum(1 for ch in text if ch.isascii() and ch.isalpha())
    return "bn" if bn and bn >= en else "en"


_SENTENCE_END_RE = re.compile(r"(?<=[.!?।॥])\s+|\n+")      # . ! ? and the Bengali danda


def split_for_tts(text: str, limit: int = PIECE_CHARS) -> list:
    """Sentence-sized pieces no longer than `limit`; an over-long sentence is cut at a comma or a space."""
    pieces = []
    for sentence in _SENTENCE_END_RE.split(text or ""):
        sentence = sentence.strip()
        while len(sentence) > limit:
            cut = max(sentence.rfind(",", 0, limit), sentence.rfind(" ", 0, limit))
            cut = cut if cut > limit // 3 else limit
            pieces.append(sentence[:cut + 1].strip(" ,"))
            sentence = sentence[cut + 1:].strip()
        if sentence:
            pieces.append(sentence)
    return [p for p in pieces if re.search(r"\w", p)]


# ---------------------------------------------------------------- engines

def _models_root() -> Path:
    from ..small_model import APP_CONFIG
    base = ((APP_CONFIG.get("media_dirs") or {}).get("voice_models") or "").strip()
    return Path(base) / "tts" if base else Path()


def _voice_files(spec: dict) -> Optional[dict]:
    """The files of one voice, or None when the folder is missing / incomplete."""
    root = _models_root() / str(spec.get("dir") or "")
    if not spec.get("dir") or not root.is_dir():
        return None
    model = root / "model.onnx"
    if not model.is_file():
        found = sorted(root.glob("*.onnx"))
        if not found:
            return None
        model = found[0]
    tokens = root / "tokens.txt"
    if not tokens.is_file():
        return None
    if spec.get("type") == "kokoro" and not (root / "voices.bin").is_file():
        return None
    return {"model": str(model), "tokens": str(tokens),
            "voices": str(root / "voices.bin") if (root / "voices.bin").is_file() else "",
            "lexicon": str(root / "lexicon.txt") if (root / "lexicon.txt").is_file() else "",
            "data_dir": str(root / "espeak-ng-data") if (root / "espeak-ng-data").is_dir() else ""}


def _sherpa():
    try:
        import sherpa_onnx
        return sherpa_onnx
    except ImportError:
        return None


def status() -> dict:
    cfg = tts_cfg()
    installed = _sherpa() is not None
    voices = {}
    for name, spec in cfg["voices"].items():
        files = _voice_files(spec)
        voices[name] = {"ready": bool(files and installed), "model_found": bool(files), "dir": spec.get("dir"),
                        "loaded": name in _engines}
    return {"enabled": bool(cfg["enabled"]), "engine": "sherpa-onnx", "installed": installed,
            "models_dir": str(_models_root()), "voices": voices,
            "ready": bool(cfg["enabled"] and any(v["ready"] for v in voices.values()))}


def _engine(voice: str):
    cfg = tts_cfg()
    if not cfg["enabled"]:
        raise MediaError("Spoken replies are turned off in Settings.")
    sherpa = _sherpa()
    if sherpa is None:
        raise MediaError("Spoken replies need the sherpa-onnx package (pip install sherpa-onnx) - it isn't installed yet.")
    spec = cfg["voices"].get(voice)
    files = _voice_files(spec or {})
    if files is None:
        raise MediaError(f"The {'Bangla' if voice == 'bn' else 'English'} voice isn't set up: put its model "
                         f"in {_models_root() / str((spec or {}).get('dir') or voice)}.")
    with _load_lock:
        if voice not in _engines:
            if spec.get("type") == "kokoro":
                model_cfg = sherpa.OfflineTtsModelConfig(
                    kokoro=sherpa.OfflineTtsKokoroModelConfig(
                        model=files["model"], voices=files["voices"], tokens=files["tokens"],
                        data_dir=files["data_dir"]),
                    num_threads=cfg["threads"], provider="cpu")
            else:
                model_cfg = sherpa.OfflineTtsModelConfig(
                    vits=sherpa.OfflineTtsVitsModelConfig(
                        model=files["model"], lexicon=files["lexicon"], tokens=files["tokens"],
                        data_dir=files["data_dir"]),
                    num_threads=cfg["threads"], provider="cpu")
            _engines[voice] = sherpa.OfflineTts(sherpa.OfflineTtsConfig(model=model_cfg, max_num_sentences=1))
            _locks[voice] = threading.Lock()
    return _engines[voice], _locks[voice], int(spec.get("speaker") or 0)


def warmup(voice: str = "en") -> dict:
    """Load a voice (blocking, call from a thread). -> {voice, ms, already_loaded}."""
    t0 = time.time()
    already = voice in _engines
    _engine(voice)
    return {"voice": voice, "ms": int((time.time() - t0) * 1000), "already_loaded": already}


def _pcm16(samples) -> bytes:
    n = len(samples)
    return struct.pack(f"<{n}h", *(int(max(-1.0, min(1.0, float(s))) * 32767) for s in samples))


def wav_from_pcm(pcm: bytes, rate: int) -> bytes:
    return (b"RIFF" + struct.pack("<I", 36 + len(pcm)) + b"WAVEfmt " + struct.pack("<IHHIIHH", 16, 1, 1, rate, rate * 2, 2, 16)
            + b"data" + struct.pack("<I", len(pcm)) + pcm)


def synthesize_stream(text: str, voice: Optional[str] = None, speed: Optional[float] = None) -> Iterator[tuple]:
    """Yield (pcm16 bytes, sample_rate) per sentence piece. Blocking: run it in a thread.
    `text` is cleaned here (markdown, links, card numbers), so no caller can forget to."""
    spoken = clean_for_speech(text)[:MAX_TEXT_CHARS]
    if not spoken:
        return
    cfg = tts_cfg()
    for piece in split_for_tts(spoken):
        v = voice or voice_for(piece)
        engine, lock, sid = _engine(v)
        with lock:
            audio = engine.generate(piece, sid=sid, speed=float(speed or cfg["speed"]))
        if audio is None or not len(audio.samples):
            continue
        yield _pcm16(audio.samples), int(audio.sample_rate)


def synthesize_wav(text: str, voice: Optional[str] = None, speed: Optional[float] = None) -> tuple:
    """-> (wav bytes, {voice, chars, ms, audio_s}). Pieces share one rate (one voice); a text that mixes both
    languages is spoken in the dominant one, and the caller should send one language per request."""
    t0 = time.time()
    spoken = clean_for_speech(text)[:MAX_TEXT_CHARS]
    v = voice or voice_for(spoken)
    pcm, rate = [], 0
    for chunk, r in synthesize_stream(spoken, v, speed):
        pcm.append(chunk)
        rate = rate or r
    if not pcm:
        raise MediaError("There was nothing to say in that text.")
    data = b"".join(pcm)
    return wav_from_pcm(data, rate), {"voice": v, "chars": len(spoken), "ms": int((time.time() - t0) * 1000),
                                      "audio_s": round(len(data) / 2 / rate, 2)}
