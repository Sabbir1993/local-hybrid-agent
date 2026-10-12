"""Measure speech-to-text accuracy (WER / CER) for Bangla and English.

Record each sentence from scripts/stt_eval_prompts.json on your own mic, save it as
<dir>/<id>.wav (any sample rate, mono or stereo), then:

  # score a whisper-server you started yourself (the app is not touched)
  whisper-server -m <model.bin> --port 8199 --inference-path /v1/audio/transcriptions
  python scripts/eval_stt.py --dir recordings --url http://127.0.0.1:8199 --label large-v3-q8

  # score existing transcripts ({"id": "...", "hyp": "..."} per line) without any server
  python scripts/eval_stt.py --dir recordings --hyp hyps.jsonl

Accuracy = 1 - WER (word level) and 1 - CER (character level; fairer for Bangla compounds).
The target is >= 0.80 word accuracy per language. Audio stays on this machine.
"""
import argparse
import json
import re
import sys
import unicodedata
from pathlib import Path

PROMPTS = Path(__file__).with_name("stt_eval_prompts.json")


def normalize(text: str) -> str:
    t = unicodedata.normalize("NFC", str(text or "")).lower()
    t = t.replace("‌", "").replace("‍", "")           # ZWNJ / ZWJ vary between engines
    t = re.sub(r"[।॥.,;:!?\"'()\[\]{}‘’“”\-]", " ", t)   # includes the danda
    return re.sub(r"\s+", " ", t).strip()


def edit_distance(a: list, b: list) -> int:
    prev = list(range(len(b) + 1))
    for i, x in enumerate(a, 1):
        cur = [i]
        for j, y in enumerate(b, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (x != y)))
        prev = cur
    return prev[-1]


def score(ref: str, hyp: str) -> tuple:
    """-> (word errors, ref words, char errors, ref chars)"""
    r, h = normalize(ref), normalize(hyp)
    rw, hw = r.split(), h.split()
    return edit_distance(rw, hw), len(rw), edit_distance(list(r), list(h)), len(r)


def transcribe(url: str, wav: bytes, lang: str, prompt: str) -> str:
    import httpx
    data = {"response_format": "json", "language": lang, "temperature": "0"}
    if prompt:
        data["prompt"] = prompt
    r = httpx.post(url.rstrip("/") + "/v1/audio/transcriptions", files={"file": ("a.wav", wav, "audio/wav")},
                   data=data, timeout=600)
    r.raise_for_status()
    return str(r.json().get("text") or "")


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")      # Bangla on a cp1252 Windows console
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dir", required=True, help="folder with <id>.wav recordings")
    ap.add_argument("--url", help="base URL of a whisper-server (OpenAI-style /v1/audio/transcriptions)")
    ap.add_argument("--hyp", help="JSONL of {id, hyp} instead of calling a server")
    ap.add_argument("--lang", choices=["en", "bn", "auto"], default=None,
                    help="force this language for every clip (default: each prompt's own language)")
    ap.add_argument("--bn-prompt", default="", help="initial prompt sent with Bangla clips")
    ap.add_argument("--label", default="run")
    args = ap.parse_args()
    if bool(args.url) == bool(args.hyp):
        ap.error("give exactly one of --url or --hyp")

    prompts = json.loads(PROMPTS.read_text(encoding="utf-8"))
    hyps = {}
    if args.hyp:
        for ln in Path(args.hyp).read_text(encoding="utf-8").splitlines():
            if ln.strip():
                o = json.loads(ln)
                hyps[o["id"]] = o["hyp"]

    totals = {}
    missing = 0
    for p in prompts:
        wav_path = Path(args.dir) / f"{p['id']}.wav"
        if args.url:
            if not wav_path.exists():
                missing += 1
                continue
            lang = args.lang or p["lang"]
            hyp = transcribe(args.url, wav_path.read_bytes(), lang, args.bn_prompt if p["lang"] == "bn" else "")
        else:
            if p["id"] not in hyps:
                missing += 1
                continue
            hyp = hyps[p["id"]]
        we, wn, ce, cn = score(p["text"], hyp)
        t = totals.setdefault(p["lang"], [0, 0, 0, 0, 0])
        t[0] += we; t[1] += wn; t[2] += ce; t[3] += cn; t[4] += 1
        flag = "" if we == 0 else "  <-"
        print(f"[{p['lang']}] {p['id']}  WER {we}/{wn}{flag}\n    ref: {p['text']}\n    hyp: {hyp}")

    print(f"\n== {args.label} ==")
    ok = True
    for lang, (we, wn, ce, cn, n) in sorted(totals.items()):
        wa, ca = 1 - we / max(wn, 1), 1 - ce / max(cn, 1)
        ok &= wa >= 0.80
        print(f"{lang}: {n} clips  word accuracy {wa:.1%}  char accuracy {ca:.1%}  "
              f"{'PASS' if wa >= 0.80 else 'below 80%'}")
    if missing:
        print(f"({missing} prompt(s) had no recording / transcript and were skipped)")
    return 0 if totals and ok else 1


if __name__ == "__main__":
    sys.exit(main())
