"""tests/test_repo_hygiene.py - repo state that silently misleads whoever runs this next.

Run: python -m unittest tests.test_repo_hygiene -v
"""

import json
import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def git(*args) -> str:
    return subprocess.run(["git", *args], cwd=ROOT, capture_output=True, text=True).stdout


def tracked(path: str) -> list:
    return [ln for ln in git("ls-files", path).splitlines() if ln.strip()]


class GitignoreCoversLocalOnlyState(unittest.TestCase):
    """.kilo/ and friends were excluded only by .git/info/exclude, which is a local file that
    is never committed. A fresh clone therefore saw a whole duplicate tree as untracked,
    including a stale 100 KB server_manager.py.bak."""

    def test_the_worktree_state_is_in_the_committed_gitignore(self):
        ignore = (ROOT / ".gitignore").read_text(encoding="utf-8")
        for entry in (".kilo/", ".kilocode/"):
            self.assertIn(entry, ignore, f"{entry} must be in the committed .gitignore")

    def test_git_actually_ignores_them_now(self):
        for path in (".kilo", "db_backups"):
            rc = subprocess.run(["git", "check-ignore", "-q", path], cwd=ROOT).returncode
            self.assertEqual(rc, 0, f"{path} is not ignored")

    def test_installed_copies_are_not_committed(self):
        """plugins/ is copied from plugin_catalog/ to activate a plugin, and load_plugins()
        already tolerates the directory being absent. All 10 files were byte-identical."""
        self.assertEqual(tracked("plugins"), [])
        self.assertNotIn("plugins/", (ROOT / ".gitignore").read_text(encoding="utf-8")
                         .split("Installed copies")[0])

    def test_the_oversized_diagram_png_is_not_committed(self):
        self.assertEqual(tracked("diagram/system_architecture_full.png"), [])
        # the SVG is the source of truth and is still tracked
        self.assertTrue(tracked("diagram/system_architecture_full.svg"))


class NoDuplicatedSkillTrees(unittest.TestCase):
    """.claude/skills held 65 byte-identical copies of .agents/skills - the same skill content
    committed twice. Only .claude/launch.json is unique."""

    def test_claude_skills_are_untracked(self):
        self.assertEqual(tracked(".claude/skills"), [])

    def test_launch_json_is_kept(self):
        self.assertIn(".claude/launch.json", tracked(".claude"))

    def test_agents_skills_stay_tracked(self):
        """Deliberate for now: every installed skill must exist in skill_catalog/ before
        .agents can be untracked, because that is what a fresh clone installs from."""
        self.assertTrue(tracked(".agents/skills"), ".agents/skills must stay tracked")

    def test_every_installed_skill_is_in_the_catalog(self):
        """The precondition for ever untracking .agents/. If one of these is missing from the
        catalog, untracking it would silently drop the skill from every fresh clone."""
        from core import skills
        installed = sorted(d.name for d in skills.SKILLS_DIR.iterdir() if d.is_dir())
        self.assertTrue(installed, "expected installed skills to exist")
        missing = [n for n in installed if not skills._catalog_dir(n)]
        self.assertEqual(missing, [], f"installed but not in skill_catalog/: {missing}")


class LicensingIsRecorded(unittest.TestCase):
    """The repo redistributes third-party components. A project licence that silently implied
    MIT over them would be wrong, and two of them are not open source at all."""

    def test_the_project_has_a_licence(self):
        lic = ROOT / "LICENSE"
        self.assertTrue(lic.is_file(), "root LICENSE is required")
        text = lic.read_text(encoding="utf-8")
        self.assertIn("MIT License", text)
        self.assertIn("Copyright (c)", text)

    def test_the_companion_declares_the_same_licence(self):
        import json
        pkg = json.loads((ROOT / "companion" / "package.json").read_text(encoding="utf-8"))
        self.assertEqual(pkg.get("license"), "MIT")

    def test_there_is_a_notice_listing_third_party_components(self):
        notice = ROOT / "NOTICE.md"
        self.assertTrue(notice.is_file(), "NOTICE.md is required once third-party code ships")
        text = notice.read_text(encoding="utf-8")
        self.assertIn("Anthropic", text)
        self.assertIn("NOT open source", text.replace("not open source", "NOT open source"))

    def test_non_mit_skills_keep_their_licence_file_and_are_recorded(self):
        """docx and pdf are (c) Anthropic, PBC, all rights reserved. frontend-design is
        Apache-2.0. All three must keep their licence text and a PROVENANCE record."""
        for name, lic in (("docx", "Anthropic"), ("pdf", "Anthropic"),
                          ("frontend-design", "Apache")):
            d = ROOT / "skill_catalog" / name
            self.assertTrue((d / "LICENSE.txt").is_file(), f"{name} lost its licence file")
            self.assertTrue((d / "PROVENANCE.json").is_file(), f"{name} has no PROVENANCE.json")
            prov = json.loads((d / "PROVENANCE.json").read_text(encoding="utf-8"))
            self.assertIn(lic, prov["plugin_license"])
            self.assertTrue(prov["files"], f"{name} PROVENANCE has no file hashes")
            self.assertIn("LICENSE.txt", prov["files"])

    def test_first_party_skills_have_catalog_metadata(self):
        """A catalog entry with no title shows a bare slug in the Customize list."""
        from core.skills import parse_skill_md
        for name in ("code-review", "commit-style", "docx", "frontend-design",
                     "goal", "grill-me", "pdf", "web-research"):
            sk = parse_skill_md(ROOT / "skill_catalog" / name / "SKILL.md")
            self.assertIsNotNone(sk, f"{name} frontmatter does not parse")
            self.assertTrue(sk["title"], f"{name} has no title")
            self.assertTrue(sk["version"], f"{name} has no version")
            self.assertTrue(sk["description"], f"{name} has no description")

    def test_every_moved_skill_is_installed_and_unmodified(self):
        """install_from_catalog refuses to clobber a locally-modified install, so an
        installed tree that drifted from the catalog would block reinstalls forever."""
        from core import skills
        for name in ("code-review", "commit-style", "docx", "frontend-design",
                     "goal", "grill-me", "pdf", "web-research"):
            self.assertTrue(skills.is_installed(name), f"{name} is not installed")
            self.assertFalse(skills.is_modified(name),
                             f"{name} differs from its catalog copy")


class BackupRetention(unittest.TestCase):
    """Every repair wrote a snapshot and nothing ever removed one, which is how
    db_backups/ reached 50+ files / 31 MB."""

    def test_pruning_keeps_the_newest_per_database(self):
        import tempfile

        from core import db_repair
        d = Path(tempfile.mkdtemp())
        original = db_repair.BACKUP_DIR
        db_repair.BACKUP_DIR = d
        try:
            for i in range(12):
                (d / f"projects.pre-fk-202601{i:02d}-101010.db").write_bytes(b"x")
            for i in range(3):
                (d / f"memory.pre-fk-202601{i:02d}-101010.db").write_bytes(b"x")
            removed = db_repair.prune_backups(keep=5)
            self.assertEqual(len(list(d.glob("projects*.db"))), 5)
            self.assertEqual(len(list(d.glob("memory*.db"))), 3, "a quiet db must not be trimmed")
            self.assertEqual(removed, 7)
        finally:
            db_repair.BACKUP_DIR = original

    def test_the_newest_is_the_one_kept(self):
        import tempfile

        from core import db_repair
        d = Path(tempfile.mkdtemp())
        original = db_repair.BACKUP_DIR
        db_repair.BACKUP_DIR = d
        try:
            for i in range(9):
                (d / f"projects.pre-fk-20260101-{i:02d}0000.db").write_bytes(b"x")
            db_repair.prune_backups(keep=2)
            kept = sorted(p.name for p in d.glob("*.db"))
            self.assertEqual(kept, ["projects.pre-fk-20260101-070000.db",
                                    "projects.pre-fk-20260101-080000.db"])
        finally:
            db_repair.BACKUP_DIR = original

    def test_zero_keeps_everything(self):
        import tempfile

        from core import db_repair
        d = Path(tempfile.mkdtemp())
        original = db_repair.BACKUP_DIR
        db_repair.BACKUP_DIR = d
        try:
            for i in range(7):
                (d / f"projects.pre-fk-20260101-{i:02d}0000.db").write_bytes(b"x")
            self.assertEqual(db_repair.prune_backups(keep=0), 0)
            self.assertEqual(len(list(d.glob("*.db"))), 7)
        finally:
            db_repair.BACKUP_DIR = original


class WorkspaceDbDiscoveryIsUserScoped(unittest.TestCase):
    """_discover_workspace_dbs called db_list_projects() with no arguments, which raises
    TypeError; the bare `except Exception` swallowed it, so registered project workspaces were
    never scanned at all. Defaulting the missing argument to None would have turned that
    no-op into a cross-tenant scan of every workspace on the machine."""

    def test_the_user_argument_is_required(self):
        import inspect

        from routes.db_explorer.helpers import _discover_workspace_dbs, _resolve_db
        for fn in (_discover_workspace_dbs, _resolve_db):
            sig = inspect.signature(fn)
            self.assertIn("user_id", sig.parameters, f"{fn.__name__} must take user_id")
            self.assertIs(sig.parameters["user_id"].default, inspect.Parameter.empty,
                          f"{fn.__name__}.user_id must have NO default, or it can be forgotten")
            self.assertEqual(str(sig.parameters["user_id"].annotation), "typing.Optional[int]")

    def test_the_bare_except_is_gone_from_the_project_loop(self):
        """The swallow is what hid the TypeError. A per-project failure should not take out
        the whole scan, but the call itself must be able to fail loudly in review."""
        from pathlib import Path as P
        src = (P(__file__).resolve().parent.parent / "routes" / "db_explorer" / "helpers.py"
               ).read_text(encoding="utf-8")
        self.assertIn("db_list_projects(user_id)", src)

    def test_every_call_site_passes_the_caller(self):
        from pathlib import Path as P
        base = P(__file__).resolve().parent.parent / "routes" / "db_explorer" / "endpoints"
        for name in ("browse.py", "query.py"):
            src = (base / name).read_text(encoding="utf-8")
            self.assertNotIn("_resolve_db(db_id)", src,
                             f"{name} still calls _resolve_db without a user")
            self.assertIn("_resolve_db(db_id, user.id)", src)


class NoSecretsOrDatabasesTracked(unittest.TestCase):
    def test_no_sqlite_files_are_committed(self):
        for pat in ("*.db", "*.db-wal", "*.db-shm"):
            found = git("ls-files", pat).split()
            self.assertEqual([f for f in found if f.strip()], [], f"{pat} is tracked")

    def test_provider_files_are_not_committed(self):
        self.assertEqual([f for f in git("ls-files", "config/providers").split() if f.strip()], [])


if __name__ == "__main__":
    unittest.main()