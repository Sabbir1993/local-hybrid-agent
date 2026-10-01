import json
import re
from typing import Optional

try:
    import numpy as _np
except Exception:
    _np = None

CHUNK_CHARS = 900
CHUNK_OVERLAP = 150
# R5 experiment knob: snap chunk cuts to paragraph/line/word boundaries instead
# of slicing mid-sentence. R5 grid (lexical) + real-vector validation: snap is
# the difference between recall 0.833 and 1.0 at 900/150 (a boundary-split
# marker falls below the min_cos gate without it), and 900+snap ties 500 on
# quality at less than half the chunk count. Default ON; the eval vector
# cache pins the boundary policy alongside size/overlap.
CHUNK_SNAP = True
CHUNK_SNAP_LOOKBACK = 200
MAX_FILE_BYTES = 512 * 1024
MAX_FILES = 400
MAX_CHUNKS_PER_DOC = 40
# Company knowledge arrives as long PDFs: cap the KB path an order of magnitude
# higher (~300KB). Workspace/session callers keep the default, which bounds
# index size and agent context -- F decides those separately.
MAX_KB_CHUNKS_PER_DOC = 400
MAX_SESSIONS = 60
EMBED_BATCH = 16
EMBEDDER_RETRY_S = 600  # retry the embedder 10 min after a failure
TEXT_EXTS = {
    ".py", ".js", ".ts", ".jsx", ".tsx", ".html", ".css", ".json", ".md",
    ".txt", ".csv", ".yml", ".yaml", ".xml", ".sh", ".bat", ".ps1", ".php",
    ".sql", ".ini", ".toml", ".log",
}
SKIP_DIRS = {".git", "__pycache__", "node_modules", ".venv", "venv"}


def _chunk_text(text: str, max_chunks: int = MAX_CHUNKS_PER_DOC) -> list:
    text = text.strip()
    if not text:
        return []
    if len(text) <= CHUNK_CHARS:
        return [text]
    chunks = []
    step = CHUNK_CHARS - CHUNK_OVERLAP
    i = 0
    while i < len(text) and len(chunks) < max_chunks:
        end = min(i + CHUNK_CHARS, len(text))
        tail = end >= len(text)
        if CHUNK_SNAP and not tail:
            end = _snap_cut(text, i, end)
        part = text[i:end]
        if part.strip():
            chunks.append(part)
        if tail:
            break
        if CHUNK_SNAP:
            i = max(end - CHUNK_OVERLAP, i + 1)
        else:
            i += step
    return chunks


def _snap_cut(text: str, start: int, end: int) -> int:
    """Pull a chunk cut back to a paragraph, line, or word boundary.

    Searches back up to CHUNK_SNAP_LOOKBACK chars for the first boundary of
    the most-preferred kind available. The floor sits one past a full overlap
    (start + CHUNK_OVERLAP + 1), which guarantees the next step advances with
    overlap intact: end - CHUNK_OVERLAP > start always, so the crawler can
    never degrade to one-char steps. Falls back to the hard cut when nothing
    is found past the floor (progress is non-negotiable).
    """
    floor = start + CHUNK_OVERLAP + 1
    if end <= floor:
        return end
    # Prefer nearby boundaries: only look back CHUNK_SNAP_LOOKBACK chars, so a
    # paragraph break 800 chars back does not beat a line break 50 back. The
    # floor still holds below the window (progress over prose).
    lo = max(floor, end - CHUNK_SNAP_LOOKBACK)
    for sep in ("\n\n", "\n", " "):
        j = text.rfind(sep, lo, end)
        if j != -1:
            cut = j + len(sep) if sep != " " else j
            if cut > start:
                return cut
    return end


def _knowledge_id_from_path(path: str) -> Optional[int]:
    if path.startswith("kb:"):
        try:
            return int(path.split(":", 1)[1])
        except ValueError:
            return None
    return None


def _session_id_from_path(path: str) -> Optional[int]:
    if path.startswith("session:"):
        try:
            return int(path.split(":", 1)[1])
        except ValueError:
            return None
    return None


def _decode_vec(vec):
    if vec is None:
        return None
    try:
        raw = bytes(vec)
        if _np is not None:
            return _np.frombuffer(raw, dtype=_np.float32)
        return json.loads(raw.decode())
    except Exception:
        return None


def _norm(vals: list) -> list:
    if not vals:
        return []
    lo, hi = min(vals), max(vals)
    if hi - lo < 1e-9:
        return [0.0] * len(vals)
    return [(v - lo) / (hi - lo) for v in vals]


# Words that carry no retrieval signal: interrogatives, auxiliaries, pronouns,
# prepositions/conjunctions, and quantity words that appear in every question
# ("how many ..."). Content nouns stay however frequent - frequency is the
# ranker's job, and cutting a content word blinds whole question classes.
STOPWORDS = frozenset({
    "the", "a", "an", "and", "or", "not", "no", "all", "any",
    "are", "is", "was", "were", "be", "been", "being",
    "do", "does", "did", "done", "have", "has", "had",
    "can", "could", "should", "would", "may", "might", "shall", "will",
    "how", "what", "when", "where", "which", "who", "whom", "whose", "why",
    "many", "much", "more", "most", "some", "such", "than", "then",
    "i", "we", "you", "he", "she", "it", "they", "them", "us",
    "my", "our", "your", "his", "her", "its", "their",
    "of", "to", "in", "on", "for", "with", "at", "by", "from", "as",
})


def _stem_word(w: str) -> str:
    """Lightweight English stemmer (stdlib-only, no new dependency).

    Only what lexical substring-matching needs: the stem must remain a
    substring of the inflected forms ("receipt" matches "receipt" and
    "receipts" via str.count). Over-stemming is therefore cheap ("stag"
    still matches "staging"); under-stemming ("receipts" vs "receipt")
    is the failure this exists to fix. Guards: words of length <= 3 and
    ss-endings pass through untouched.
    """
    if len(w) <= 3 or w.endswith("ss"):
        return w
    if w.endswith("ies") and len(w) > 4:
        return w[:-3] + "y"  # policies -> policy
    if w.endswith(("sses", "xes", "zes", "ches", "shes")) and len(w) > 4:
        return w[:-2]  # classes -> class, watches -> watch
    if w.endswith("s") and not w.endswith("us"):
        return w[:-1]  # receipts -> receipt, days -> day
    if w.endswith("eed"):
        return w  # need, agreed - stripping eats the root
    if w.endswith("ed") and len(w) > 4:
        w = w[:-2]  # needed -> need, accepted -> accept
    elif w.endswith("ing") and len(w) > 5:
        w = w[:-3]  # running -> runn, shipping -> shipp (doubled below)
    else:
        return w
    # Collapse doubled consonants from the strip (runn -> run) - but keep
    # fall/pass spellings: only non-vowel, non-l/s endings reduce.
    if len(w) > 3 and w[-1] == w[-2] and w[-1] not in "aeiouls":
        w = w[:-1]
    return w


def _normalize_query_words(text: str, limit: int = 10) -> list:
    """Lowercased, stopword-filtered, stemmed query words for lexical scoring.

    One function so query words, title words and the title-match set all
    normalize identically - matching is substring counting, so any skew
    between the two sides silently blinds whole question classes.
    """
    out = []
    for w in re.findall(r"\w{3,}", (text or "").lower()):
        if w in STOPWORDS:
            continue
        out.append(_stem_word(w))
        if len(out) >= limit:
            break
    return out
