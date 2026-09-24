"""Move files from the flat shared common space into COMMON_ROOT/user_<id>/.

Ownership is inferred from chat history: a file belongs to the user whose
session messages reference it ([DOWNLOAD: name] or ATTACHED FILE: name).
Files with no owner, or referenced by more than one user, go to
COMMON_ROOT/_unowned/ -- no route serves that folder.

Dry-run by default; pass --apply to move files. Writes a CSV report either way.
--default-owner <id> gives files with no traceable owner to that user instead.

    python scripts/migrate_common_per_user.py            # report only
    python scripts/migrate_common_per_user.py --apply
"""

import argparse
import csv
import re
import shutil
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.db import _projects_db  # noqa: E402
from core.small_model import COMMON_ROOT  # noqa: E402

_MARKERS = [
    re.compile(r"\[DOWNLOAD:\s*([^\]]+?)\s*\]"),
    re.compile(r"ATTACHED FILE:\s*([^\n(]+?)\s*(?:\(|---)"),
    re.compile(r"--- FILE:\s*([^\n]+?)\s*---"),
]


def _owners() -> dict[str, set[int]]:
    owners: dict[str, set[int]] = defaultdict(set)
    rows = _projects_db.execute(
        "SELECT s.user_id AS uid, m.content AS content FROM messages m "
        "JOIN sessions s ON s.id = m.session_id WHERE s.user_id IS NOT NULL")
    for r in rows:
        text = r["content"] or ""
        for rx in _MARKERS:
            for m in rx.finditer(text):
                owners[Path(m.group(1).strip()).name].add(int(r["uid"]))
    return owners


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--apply", action="store_true", help="actually move files (default: dry run)")
    ap.add_argument("--report", default=str(COMMON_ROOT / "_migration_report.csv"))
    ap.add_argument("--default-owner", type=int, default=None,
                    help="user id that receives files with no traceable owner (default: _unowned/)")
    args = ap.parse_args()

    root = COMMON_ROOT.resolve()
    owners = _owners()
    plan = []
    for f in sorted(root.iterdir()):
        if not f.is_file() or f.name.startswith("_migration_report"):
            continue
        uids = owners.get(f.name, set())
        if len(uids) == 1:
            dest_dir, reason = root / f"user_{next(iter(uids))}", "owner"
        elif not uids and args.default_owner is not None:
            dest_dir, reason = root / f"user_{args.default_owner}", "default-owner"
        else:
            dest_dir = root / "_unowned"
            reason = "no-owner" if not uids else f"shared:{','.join(map(str, sorted(uids)))}"
        dest = dest_dir / f.name
        n = 2
        while dest.exists():
            dest = dest_dir / f"{f.stem}-{n}{f.suffix}"
            n += 1
        plan.append((f, dest, reason))

    with open(args.report, "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["source", "destination", "reason", "moved"])
        for src, dest, reason in plan:
            moved = False
            if args.apply:
                dest.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(src), str(dest))
                moved = True
            w.writerow([src.name, str(dest.relative_to(root)), reason, moved])

    owned = sum(1 for p in plan if p[2] in ("owner", "default-owner"))
    print(f"{len(plan)} files: {owned} to user folders, {len(plan) - owned} to _unowned "
          f"({'moved' if args.apply else 'dry run'}). Report: {args.report}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
