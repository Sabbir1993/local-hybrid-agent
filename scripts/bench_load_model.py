"""Load a local GGUF through the running app (POST /control/switch) using BENCH_USER / BENCH_PASSWORD, then wait until it answers.

Credentials come from the environment or from a dotenv file (--env-file); they are never printed.
    python scripts/bench_load_model.py --env-file companion/.env --model E:/AI/Models/Ornith-1.5-35B-A3B-GGUF/Ornith-1.5-35B-Q4_K_M.gguf
"""
import argparse
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def load_env(path):
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            if k.strip().startswith("BENCH_"):
                os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="http://127.0.0.1:8000")
    ap.add_argument("--env-file")
    ap.add_argument("--model", required=True)
    ap.add_argument("--wait", type=int, default=900)
    a = ap.parse_args()
    if a.env_file:
        load_env(a.env_file)
    from tests import eval_agent
    c = eval_agent._live_login(a.base, os.environ["BENCH_USER"], os.environ["BENCH_PASSWORD"])
    r = c.post(f"{a.base}/control/switch", json={"target": a.model}, timeout=a.wait)
    print("switch:", r.status_code, r.text[:300])
    t0 = time.time()
    while time.time() - t0 < a.wait:
        st = c.get(f"{a.base}/control/status", timeout=30).json()
        print({k: st.get(k) for k in ("status", "running", "loaded", "model", "profile", "context_size", "ctx") if k in st})
        if st.get("running") or st.get("loaded") or st.get("status") in ("running", "ready"):
            break
        time.sleep(10)


if __name__ == "__main__":
    main()
