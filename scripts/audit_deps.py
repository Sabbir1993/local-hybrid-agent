"""scripts/audit_deps.py - hash-locked dependencies + vulnerability audit (PCI DSS 6.3).

  python scripts/audit_deps.py --lock   regenerate requirements.lock from requirements.txt
                                        (exact versions + sha256 hashes, via pip-compile)
  python scripts/audit_deps.py          audit requirements.lock for known CVEs (pip-audit)

Needs the dev tools, installed once:  pip install pip-tools pip-audit
Production install from the lock:     pip install --require-hashes -r requirements.lock
Exit code is non-zero when the audit finds a vulnerability, so it can gate CI.
"""

import argparse
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
REQS = ROOT / "requirements.txt"
LOCK = ROOT / "requirements.lock"


def _tool(module: str, hint: str) -> list:
    try:
        __import__(module)
    except ImportError:
        sys.exit(f"{hint} is not installed - run: pip install pip-tools pip-audit")
    return [sys.executable, "-m", module]


def lock() -> int:
    cmd = _tool("piptools", "pip-tools") + [
        "compile", "--generate-hashes", "--allow-unsafe", "--strip-extras", "--quiet",
        "-o", str(LOCK), str(REQS)]
    print("+", " ".join(cmd))
    return subprocess.call(cmd, cwd=ROOT)


def audit() -> int:
    if not LOCK.exists():
        sys.exit("requirements.lock not found - run: python scripts/audit_deps.py --lock")
    cmd = _tool("pip_audit", "pip-audit") + ["-r", str(LOCK), "--require-hashes", "--strict", "--desc", "off"]
    print("+", " ".join(cmd))
    return subprocess.call(cmd, cwd=ROOT)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--lock", action="store_true", help="regenerate requirements.lock")
    args = ap.parse_args()
    rc = lock() if args.lock else 0
    return rc or audit()


if __name__ == "__main__":
    sys.exit(main())
