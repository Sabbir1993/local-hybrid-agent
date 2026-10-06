"""tests/test_doc_refs.py - tracked markdown must not cite paths that moved.

The 2026-09-29/10-02 package split turned core/agent_loop.py etc. into packages,
and PROJECT_KNOWLEDGE / README kept dozens of stale core/*.py references for
weeks. A doc that points at a file that does not exist is a trap for the next
reader. This test walks the tracked .md files in the repo root and fails on
backticked paths ending in a known code/source suffix that do not exist.

Pure stdlib, no server, no DB. Run: python -m unittest tests.test_doc_refs -v
"""

import re
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

REPO = Path(__file__).resolve().parents[1]
_PATH_RX = re.compile(r"`([^`]{3,120})`")
_KNOWN_SUFFIXES = (".py", ".js", ".jsx", ".ts", ".css", ".html", ".md", ".json",
                   ".sh", ".ps1", ".lock", ".yaml", ".yml", ".svg")
# Files that legitimately do not exist in the tree because they are generated
# at runtime (per-session or per-user) or are intentionally git-ignored output.
# A path change still fails the test; these are only about presence in the repo.
_RUNTIME_PATHS = {"plan.md", "PLAN.md", "profile.md", "preferences.md", "lessons.md"}
_IGNORED_SUFFIX = (".chunks",)
# Intentional absences: git-ignored eval output (R5, 2026-10-06) is documented
# as ignored, and a plain `run.py`/`config.py` inside a module bullet is short
# for a path the doc already scoped with its parent.
_GITIGNORED_OUTPUT = {"eval_results.json", "eval_results_live.json",
                      "eval_live_baseline.json"}
_LEGACY_SRC_PATHS = {"config/providers.json", "config/providers.json.migrated"}
_IMPLIED_PARENTS = ("core", "config", "static/js")
_NEGATES_EXISTENCE = ("no `", "there is no ", "does not exist", "doesn't exist")
_BUILD_PLAN_VERBS = ("Build ", "Create ", "Add ", "Write ")


# Links inside markdown are `[text](target)`: the backticked capture grabs the
# whole thing for inline-code-only markup, but markdown links never sit inside
# backticks, so this list stays clean of link syntax.
_NOT_A_PATH = ("http://", "https://", "<", ">", " ", "{", "}")

def _doc_files() -> list:
    docs = sorted(REPO.glob("*.md"))
    if (REPO / "docs").is_dir():
        # archives are a frozen snapshot of an older tree; checking them for
        # freshness would fail forever on purpose, so only scan one level deep
        # under docs/ (the live docs), not docs/history/ archives
        docs += sorted((REPO / "docs").glob("*.md"))
    return sorted(set(docs))


class DocReferencesResolve(unittest.TestCase):
    def test_every_backticked_source_path_exists(self):
        bad = []
        for f in _doc_files():
            for line_no, line in enumerate(f.read_text(encoding="utf-8").splitlines(), 1):
                for hit in _PATH_RX.findall(line):
                    hit = hit.strip()
                    if any(hit.startswith(p) or p in hit for p in _NOT_A_PATH):
                        continue
                    if not hit.endswith(_KNOWN_SUFFIXES):
                        continue
                    # negations ("there is no core/agent_loop.py") and build
                    # plans ("Build core/browser_tools/playwright_runner.py")
                    # reference paths deliberately, not as existing files
                    if any(n in line for n in _NEGATES_EXISTENCE):
                        continue
                    if any(f"{v}`" in line or f" {v.lower()}`" in line.lower()
                           for v in _BUILD_PLAN_VERBS):
                        continue
                    if Path(hit).name in _RUNTIME_PATHS:
                        continue
                    if hit.endswith(_IGNORED_SUFFIX):
                        continue
                    if Path(hit).name in _GITIGNORED_OUTPUT:
                        continue
                    if hit in _LEGACY_SRC_PATHS:
                        continue  # imported-then-deleted, documented as such
                    if "*" in hit or "?" in hit:
                        continue  # glob (config/*.json, test_*guard*.py)
                    if hit.startswith((".", "/")):
                        continue  # dot-relative (./, .venv/, .agent/) or absolute
                    # strip surrounding sentence punctuation that the backticks
                    # did not: `core/vram.py: compute_tensor_split()` is a path
                    # plus a symbol, so test the path-only prefix.
                    cand = hit.split(":", 1)[0].split("(", 1)[0]
                    if cand == hit and "." not in Path(cand).name:
                        continue
                    path = REPO / cand
                    if path.exists() or path.is_dir():
                        continue
                    # A bare filename may be written inside a `core/` or
                    # `config/` bullet ("orchestration internals: `config.py`
                    # (paths...)"). Resolve against each implied parent.
                    # Anything still missing is either real drift or a
                    # never-existing bare name - rejected either way, because
                    # the doc could have written the full path.
                    for parent in _IMPLIED_PARENTS:
                        alt = REPO / parent / cand
                        if alt.exists() or alt.is_dir():
                            break
                    else:
                        bad.append(f"{f.relative_to(REPO)}:{line_no}: `{hit}` -> {cand}")
        self.assertEqual(bad, [], "\n".join(bad[:30]))


if __name__ == "__main__":
    unittest.main()
