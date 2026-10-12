"""Transcribe the eval clips with a local Hugging Face Whisper model (CPU) -> hyps.jsonl for eval_stt.py --hyp.

  python scripts/stt_hf_hyps.py --model E:/AI/Models/.../BanglaASR --dir recordings --out hyps_bn.jsonl [--lang bn]

Lets you score a fine-tune BEFORE converting it to ggml. Audio and model stay local.
"""
import argparse
import json
import wave
from pathlib import Path

import numpy as np

PROMPTS = Path(__file__).with_name("stt_eval_prompts.json")


def load_wav16k(path: Path) -> np.ndarray:
    with wave.open(str(path), "rb") as w:
        if w.getframerate() != 16000 or w.getnchannels() != 1 or w.getsampwidth() != 2:
            raise SystemExit(f"{path.name}: expected 16 kHz mono 16-bit (use scripts/record_stt_eval.py)")
        return np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16).astype(np.float32) / 32768.0


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--model", required=True, help="local folder of the HF model")
    ap.add_argument("--dir", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--lang", choices=["bn", "en"], help="only this prompt language")
    ap.add_argument("--force-lang", help="force decoder language for every clip (e.g. bn); default: each prompt's own")
    a = ap.parse_args()
    if hasattr(__import__("sys").stdout, "reconfigure"):
        __import__("sys").stdout.reconfigure(encoding="utf-8")   # Bangla on a cp1252 Windows console

    import torch
    from transformers import WhisperForConditionalGeneration, WhisperProcessor
    proc = WhisperProcessor.from_pretrained(a.model, local_files_only=True)
    model = WhisperForConditionalGeneration.from_pretrained(a.model, local_files_only=True).eval()
    with open(a.out, "w", encoding="utf-8") as out:
        for p in json.loads(PROMPTS.read_text(encoding="utf-8")):
            if a.lang and p["lang"] != a.lang:
                continue
            wav = Path(a.dir) / f"{p['id']}.wav"
            if not wav.exists():
                continue
            feats = proc(load_wav16k(wav), sampling_rate=16000, return_tensors="pt").input_features
            lang = {"bn": "bengali", "en": "english"}.get(a.force_lang or p["lang"], a.force_lang or p["lang"])
            with torch.no_grad():
                ids = model.generate(feats, language=lang, task="transcribe", num_beams=1, max_new_tokens=128)
            hyp = proc.batch_decode(ids, skip_special_tokens=True)[0].strip()
            out.write(json.dumps({"id": p["id"], "hyp": hyp}, ensure_ascii=False) + "\n")
            print(p["id"], hyp)


if __name__ == "__main__":
    main()
