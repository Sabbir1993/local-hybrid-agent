"""scripts/audit_deps.py - hash-locked dependencies + vulnerability audit (PCI DSS 6.3).

  python scripts/audit_deps.py --lock   regenerate requirements.lock from requirements.txt
                                        (exact versions + sha256 hashes, via pip-compile)
  python scripts/audit_deps.py          audit requirements.lock for known CVEs (pip-audit)
  python scripts/audit_deps.py --check-lock
                                        verify requirements.txt floors are satisfied by
                                        requirements.lock pins (catches "edited txt, forgot
                                        to re-lock": CI would otherwise install stale pins
                                        while devs test against newer ones)

Needs the dev tools, installed once:  pip install pip-tools pip-audit
Production install from the lock:     pip install --require-hashes -r requirements.lock
Exit code is non-zero when the audit finds a vulnerability, so it can gate CI.
"""

import argparse
import re
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


def check_lock() -> int:
    """Every requirements.txt floor must be satisfied by a requirements.lock pin.

    Parses floors (`name>=x.y`, `name==x.y`, bare `name`) and pins (`name==x.y`) with
    packaging-free version comparison good enough for lock files (numeric segments).
    Returns 0 when the lock covers the txt, 1 with details otherwise.
    """
    def split_req(line: str):
        line = line.split("#", 1)[0].strip().rstrip("\\").strip()
        if not line or line.startswith(("-", " ", "\t")) or "://" in line:
            return None
        m = re.match(r"^([A-Za-z0-9_.\-]+)\s*(.*)$", line)
        if not m:
            return None
        name, rest = m.group(1).lower().replace("_", "-").replace(".", "-"), m.group(2).strip()
        specs = []
        for part in rest.split(","):
            part = part.strip().rstrip(";").strip()
            mm = re.match(r"^(==|>=|<=|>|<|~=|!=)\s*([0-9][0-9A-Za-z.]*)([a-zA-Z0-9.]*)\s*$", part)
            if mm:
                specs.append((mm.group(1), mm.group(2)))
        return name, specs

    def parse_version(v: str) -> tuple:
        parts = []
        for seg in re.split(r"[.]", str(v)):
            m = re.match(r"(\d+)", seg)
            parts.append(int(m.group(1)) if m else 0)
        return tuple(parts)

    def satisfies(pin: tuple, op: str, want: tuple) -> bool:
        if op == "==":
            return pin == want
        if op == ">=":
            return pin >= want
        if op == "<=":
            return pin <= want
        if op == ">":
            return pin > want
        if op == "<":
            return pin < want
        if op == "!=":
            return pin != want
        if op == "~=":  # compatible release: >= want, == want-major
            return pin >= want and pin[: max(1, len(want) - 1)] == want[: max(1, len(want) - 1)]
        return True

    pins = {}
    for line in LOCK.read_text(encoding="utf-8").splitlines():
        m = re.match(r"^([A-Za-z0-9_.\-]+)==([0-9][0-9A-Za-z.]*)", line.strip())
        if m:
            pins[m.group(1).lower().replace("_", "-").replace(".", "-")] = parse_version(m.group(2))
    problems = []
    checked = 0
    for line in REQS.read_text(encoding="utf-8").splitlines():
        parsed = split_req(line)
        if not parsed:
            continue
        name, specs = parsed
        checked += 1
        if name not in pins:
            # environment markers (e.g. sys_platform) and -r includes have no pin by design
            if ";" in line or line.strip().startswith("-"):
                continue
            problems.append(f"{name}: no pin in requirements.lock (re-lock needed)")
            continue
        for op, want in specs:
            if not satisfies(pins[name], op, parse_version(want)):
                problems.append(f"{name}: lock pins "
                                f"{'.'.join(map(str, pins[name]))}, txt requires {op}{want}")
    if problems:
        print("requirements.lock is stale:")
        for p in problems:
            print(f"  - {p}")
        print("run: python scripts/audit_deps.py --lock")
        return 1
    print(f"lock covers requirements.txt ({checked} floors checked)")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--lock", action="store_true", help="regenerate requirements.lock")
    ap.add_argument("--check-lock", action="store_true",
                    help="verify the lock satisfies requirements.txt floors")
    args = ap.parse_args()
    if args.check_lock:
        return check_lock()
    rc = lock() if args.lock else 0
    return rc or audit()


if __name__ == "__main__":
    sys.exit(main())
