"""Split GGUF models (name-00001-of-0000N.gguf ...): shown at their full size, always launched from part 1, and an
incomplete set is refused with a clear message instead of llama-server failing mid-load."""
import tempfile
import unittest
from pathlib import Path

from core.profiles import build_dynamic_profile
from core.profiles.paths import _entry
from core.vram.gguf_parser import first_part, model_files, model_size_bytes

GGUF_MAGIC = b"GGUF"


class SplitModelTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name) / "gpt-oss-120b"
        self.dir.mkdir()
        self.parts = [self.dir / f"gpt-oss-120b-MXFP4-0000{i}-of-00002.gguf" for i in (1, 2)]
        self.parts[0].write_bytes(GGUF_MAGIC + b"a" * 996)       # 1000 bytes
        self.parts[1].write_bytes(GGUF_MAGIC + b"b" * 1996)      # 2000 bytes
        self.single = Path(self.tmp.name) / "small.gguf"
        self.single.write_bytes(GGUF_MAGIC + b"c" * 496)

    def tearDown(self):
        self.tmp.cleanup()

    def test_all_parts_count_toward_the_size(self):
        self.assertEqual(model_files(self.parts[0]), self.parts)
        self.assertEqual(model_size_bytes(self.parts[0]), 3000)
        self.assertEqual(model_size_bytes(self.single), 500)
        self.assertEqual(model_files(self.single), [self.single])

    def test_wizard_entry_reports_full_size_and_parts(self):
        e = _entry(self.parts[0])
        self.assertEqual((e["size_gb"], e["parts"]), (round(3000 / 1e9, 2), 2))
        self.assertEqual(_entry(self.single)["parts"], 1)

    def test_any_part_launches_from_part_one(self):
        self.assertEqual(first_part(self.parts[1]), self.parts[0])
        self.assertEqual(first_part(self.single), self.single)
        self.assertEqual(build_dynamic_profile(self.parts[1])["model_path"], str(self.parts[0]))

    def test_missing_part_is_named(self):
        self.parts[1].unlink()
        with self.assertRaises(FileNotFoundError) as cm:
            first_part(self.parts[0])
        self.assertIn("part 2 is missing", str(cm.exception))

    def test_brackets_in_a_name_do_not_break_the_part_lookup(self):
        d = Path(self.tmp.name) / "odd"
        d.mkdir()
        parts = [d / f"m[q4]-0000{i}-of-00002.gguf" for i in (1, 2)]
        for p in parts:
            p.write_bytes(GGUF_MAGIC + b"x" * 96)
        self.assertEqual(model_files(parts[0]), parts)


class DisplayNameTests(unittest.TestCase):
    def test_part_suffix_is_not_part_of_the_name(self):
        from core.profiles.paths import model_display_name
        self.assertEqual(model_display_name("/m/gpt-oss-120b/gpt-oss-120b-MXFP4-00001-of-00002.gguf"), "gpt-oss-120b-MXFP4")
        self.assertEqual(model_display_name(r"E:\AI\Models\Ornith-1.5-35B-Q4_K_M.gguf"), "Ornith-1.5-35B-Q4_K_M")
        self.assertEqual(model_display_name("gpt-oss-120b-MXFP4-00001-of-00002"), "gpt-oss-120b-MXFP4")


if __name__ == "__main__":
    unittest.main()
