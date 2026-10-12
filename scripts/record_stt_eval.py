"""Record the STT eval sentences from your microphone (uses ffmpeg, no extra installs).

  python scripts/record_stt_eval.py --list-devices
  python scripts/record_stt_eval.py --device "Microphone (Realtek ...)" --out recordings [--lang bn|en]

For each sentence: read it, press Enter, speak naturally, it records a few seconds and saves
<out>/<id>.wav (16 kHz mono). Type 'r' to redo, 's' to skip, 'q' to quit. Re-running skips clips
that already exist. Use the mic you actually use for voice chat, at your normal distance.
"""
import argparse
import json
import re
import subprocess
import sys
from pathlib import Path

PROMPTS = Path(__file__).with_name("stt_eval_prompts.json")


def list_devices() -> None:
    r = subprocess.run(["ffmpeg", "-hide_banner", "-list_devices", "true", "-f", "dshow", "-i", "dummy"],
                       capture_output=True, text=True, encoding="utf-8", errors="replace")
    names = re.findall(r'"([^"]+)" \(audio\)', r.stderr)
    print("Audio devices:\n  " + "\n  ".join(names) if names else r.stderr)


def record(device: str, path: Path, seconds: float) -> bool:
    cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-f", "dshow", "-i", f"audio={device}",
           "-t", str(seconds), "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le", str(path)]
    return subprocess.run(cmd).returncode == 0 and path.exists()


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--list-devices", action="store_true")
    ap.add_argument("--device", help="exact name from --list-devices")
    ap.add_argument("--out", default="recordings")
    ap.add_argument("--lang", choices=["bn", "en"], help="only this language")
    ap.add_argument("--seconds", type=float, default=6.0, help="length of each recording")
    a = ap.parse_args()
    if a.list_devices:
        list_devices()
        return 0
    if not a.device:
        ap.error("--device is required (see --list-devices)")
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    for p in json.loads(PROMPTS.read_text(encoding="utf-8")):
        if a.lang and p["lang"] != a.lang:
            continue
        path = out / f"{p['id']}.wav"
        if path.exists():
            continue
        while True:
            print(f"\n[{p['id']}]  {p['text']}")
            cmd = input("Enter = record (s skip, q quit): ").strip().lower()
            if cmd == "q":
                return 0
            if cmd == "s":
                break
            print(f"  recording {a.seconds:g}s - speak now...")
            if not record(a.device, path, a.seconds):
                print("  recording failed - check --device")
                return 1
            if input("  saved. Enter = keep, r = redo: ").strip().lower() != "r":
                break
    print(f"\nDone. Next: python scripts/eval_stt.py --dir {out} --url http://127.0.0.1:8199")
    return 0


if __name__ == "__main__":
    sys.exit(main())
