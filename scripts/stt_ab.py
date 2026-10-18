"""A/B several whisper.cpp models on the OpenSLR 104 Bengali-English test split.

Starts one whisper-server per model on a spare port (the app is not touched), cuts a seeded sample of
sentence segments out of the long recordings, and reports word / character accuracy and speed.

  python scripts/stt_ab.py --data E:/AI/stt-ab/data/ex/test --n 150 \
      --model large-v3=E:/AI/Models/orchestrator/voice-models/ggml-large-v3.bin \
      --model turbo-bn=E:/AI/stt-ab/out/ggml-large-v3-turbo-bn-q5_0.bin

Audio stays on this machine. Scoring reuses scripts/eval_stt.py (same normalisation as the app's eval).
"""
import argparse
import io
import json
import os
import random
import subprocess
import sys
import time
import wave
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from eval_stt import score, transcribe  # noqa: E402


def load_segments(data: Path) -> list:
    texts = dict(l.rstrip("\n").split(" ", 1) for l in (data / "transcripts/text").read_text(encoding="utf-8").splitlines() if " " in l)
    segs = []
    for l in (data / "transcripts/segments").read_text(encoding="utf-8").splitlines():
        sid, rec, a, b = l.split()
        if sid in texts and 2.0 <= float(b) - float(a) <= 20.0 and (data / f"{rec}.wav").exists():
            segs.append((sid, rec, float(a), float(b), texts[sid]))
    return segs


def cut(path: Path, a: float, b: float) -> bytes:
    with wave.open(str(path)) as w:
        sr = w.getframerate()
        w.setpos(int(a * sr))
        frames = w.readframes(int((b - a) * sr))
        buf = io.BytesIO()
        with wave.open(buf, "wb") as o:
            o.setnchannels(w.getnchannels()); o.setsampwidth(w.getsampwidth()); o.setframerate(sr)
            o.writeframes(frames)
    return buf.getvalue()


def run_model(name: str, model: str, clips: list, args) -> dict:
    exe = args.server
    env = dict(os.environ, GGML_VK_VISIBLE_DEVICES=str(args.gpu))
    cmd = [exe, "-m", model, "--host", "127.0.0.1", "--port", str(args.port), "-t", "8",
           "--inference-path", "/v1/audio/transcriptions"] + (["-ng"] if args.gpu < 0 else [])
    p = subprocess.Popen(cmd, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    url = f"http://127.0.0.1:{args.port}"
    try:
        import httpx
        for _ in range(180):
            try:
                httpx.get(url, timeout=2); break
            except Exception:
                time.sleep(1)
        we = wr = ce = cr = 0
        audio = wall = 0.0
        hyps = []
        for sid, wav, dur, ref in clips:
            t0 = time.time()
            hyp = transcribe(url, wav, args.lang, "")
            wall += time.time() - t0
            audio += dur
            a, b, c, d = score(ref, hyp)
            we += a; wr += b; ce += c; cr += d
            hyps.append({"id": sid, "ref": ref, "hyp": hyp})
        return {"model": name, "wer": we / wr, "cer": ce / cr, "rtf": wall / audio, "n": len(clips), "hyps": hyps}
    finally:
        p.terminate()
        try:
            p.wait(10)
        except Exception:
            p.kill()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", required=True)
    ap.add_argument("--n", type=int, default=150)
    ap.add_argument("--model", action="append", required=True, help="name=path/to/ggml.bin")
    ap.add_argument("--server", default="E:/AI/vulkan-arc/whisper-vulkan/whisper-server.exe")
    ap.add_argument("--gpu", type=int, default=1, help="-1 = CPU")
    ap.add_argument("--port", type=int, default=8199)
    ap.add_argument("--lang", default="bn")
    ap.add_argument("--out", default="bench_out/stt_ab.json")
    args = ap.parse_args()

    data = Path(args.data)
    segs = load_segments(data)
    random.Random(7).shuffle(segs)
    clips = [(sid, cut(data / f"{rec}.wav", a, b), b - a, txt) for sid, rec, a, b, txt in segs[:args.n]]
    results = [run_model(*m.split("=", 1), clips, args) for m in args.model]
    print(f"{'model':<14}{'word acc':>10}{'char acc':>10}{'RTF':>8}")
    for r in results:
        print(f"{r['model']:<14}{1 - r['wer']:>10.3f}{1 - r['cer']:>10.3f}{r['rtf']:>8.2f}")
    Path(args.out).parent.mkdir(exist_ok=True)
    Path(args.out).write_text(json.dumps(results, ensure_ascii=False, indent=1), encoding="utf-8")


if __name__ == "__main__":
    main()
