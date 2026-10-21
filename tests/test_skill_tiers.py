"""tests/test_skill_tiers.py - P6-B: provenance tracking and trust tiers for skills.

The trust rule in core/skills.py is structural (nothing is downloaded; the
catalog is the only install source; stewards guard install endpoints behind
RBAC capabilities.install).  What it could not answer before: once installed, a
catalog skill and a hand-written one are IDENTICAL on disk - nobody can tell
which SKILL.md went through review.  P6-B adds the record, not the behavior:

- installing writes a .provenance.json sidecar INSIDE the installed dir that
  records the catalog tree hash at install time (plus source and timestamp),
- trust_of(name) classifies every installed skill into exactly three tiers:
  reviewed (provenance present and the tree still matches the recorded hash),
  modified (provenance present but the tree moved since the install),
  handwritten (no usable provenance - unreadable or absent),
- the tier rides on every load_skills() entry as a plain "trust" key, and the
  prompt fragment stays byte-stable (provenance is metadata, never prompt).
"""
import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core import skills  # noqa: E402  (module is imported for monkeypathcing)


def _make_catalog(root: Path, name: str = "demo", body: str = "# Demo\n\nstep one\n") -> Path:
    d = root / name
    d.mkdir(parents=True)
    (d / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: demo skill\n---\n{body}", encoding="utf-8")
    return d


class TierBase(unittest.TestCase):
    def setUp(self):
        self._tmp = Path(tempfile.mkdtemp(prefix="tiers_"))
        self._cat = self._tmp / "catalog"
        self._run = self._tmp / "run"
        self._cat.mkdir()
        self.patches = [
            patch.object(skills, "CATALOG_DIR", self._cat),
            patch.object(skills, "SKILLS_DIR", self._run),
        ]
        for p in self.patches:
            p.start()
            self.addCleanup(p.stop)

    def tearDown(self):
        shutil.rmtree(self._tmp, ignore_errors=True)


class TestInstallProvenance(TierBase):
    def test_install_writes_the_sidecar(self):
        _make_catalog(self._cat)
        skills.install_from_catalog("demo")
        side = self._run / "demo" / ".provenance.json"
        self.assertTrue(side.is_file())
        data = json.loads(side.read_text(encoding="utf-8"))
        self.assertEqual(data["source"], "skill_catalog")
        self.assertEqual(data["catalog_sha256"], skills._tree_hash(self._cat / "demo"))
        self.assertTrue(data["installed_at"])

    def test_reinstall_does_not_touch_the_sidecar(self):
        _make_catalog(self._cat)
        skills.install_from_catalog("demo")
        first = json.loads((self._run / "demo" / ".provenance.json")
                           .read_text(encoding="utf-8"))
        skills.install_from_catalog("demo")          # identical: no-op by the old rule
        second = json.loads((self._run / "demo" / ".provenance.json")
                            .read_text(encoding="utf-8"))
        self.assertEqual(first, second)


class TestTrustOf(TierBase):
    def test_reviewed_after_install(self):
        _make_catalog(self._cat)
        skills.install_from_catalog("demo")
        self.assertEqual(skills.trust_of("demo"), "reviewed")

    def test_modified_when_the_tree_moves_after_install(self):
        _make_catalog(self._cat)
        skills.install_from_catalog("demo")
        p = self._run / "demo" / "SKILL.md"
        p.write_text(p.read_text(encoding="utf-8") + "\nextra line\n", encoding="utf-8")
        self.assertEqual(skills.trust_of("demo"), "modified")
        # a NEW sidecar file counts too: extra files change the tree hash
        (self._run / "demo" / "extra.txt").write_text("x", encoding="utf-8")
        self.assertEqual(skills.trust_of("demo"), "modified")

    def test_handwritten_when_no_sidecar(self):
        _make_catalog(self._cat)
        (self._run / "byhand").mkdir(parents=True)
        (self._run / "byhand" / "SKILL.md").write_text(
            "---\nname: byhand\ndescription: mine\n---\nbody", encoding="utf-8")
        self.assertEqual(skills.trust_of("byhand"), "handwritten")

    def test_corrupt_sidecar_is_not_trusted(self):
        _make_catalog(self._cat)
        skills.install_from_catalog("demo")
        (self._run / "demo" / ".provenance.json").write_text("{not json", encoding="utf-8")
        self.assertEqual(skills.trust_of("demo"), "handwritten")

    def test_uninstalled_and_bad_names(self):
        _make_catalog(self._cat)
        self.assertEqual(skills.trust_of("never-installed"), "")
        self.assertEqual(skills.trust_of("Bad Name!"), "")


class TestListingStability(TierBase):
    def test_load_skills_carries_the_tier(self):
        _make_catalog(self._cat)
        skills.install_from_catalog("demo")
        loaded = skills.load_skills()
        self.assertEqual(loaded["demo"]["trust"], "reviewed")

    def test_prompt_fragment_is_byte_stable_across_tiers(self):
        _make_catalog(self._cat)
        body = "---\nname: demo\ndescription: demo skill\n---\n# Demo\nbody"
        d = self._run / "demo"
        d.mkdir(parents=True)
        (d / "SKILL.md").write_text(body, encoding="utf-8")   # same content, no sidecar
        from core.small_model import APP_CONFIG
        with patch.dict(APP_CONFIG, {"capabilities": {"skills": True}}):
            handwritten_frag = skills.skills_prompt_fragment()
            shutil.rmtree(d)
            skills.install_from_catalog("demo")               # same content, reviewed
            reviewed_frag = skills.skills_prompt_fragment()
        self.assertEqual(handwritten_frag, reviewed_frag)
        self.assertNotIn("trust", reviewed_frag)              # metadata never enters the prompt


if __name__ == "__main__":
    unittest.main()
