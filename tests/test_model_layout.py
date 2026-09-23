"""tests/test_model_layout.py - Models/<model>/ discovery + companion header checks.

Run: python -m unittest tests.test_model_layout -v
Uses tiny fake GGUF headers (metadata only, no tensors).
"""

import json
import struct
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core import profiles


def write_gguf(path: Path, kv: dict) -> Path:
    """Minimal GGUF v3: header + string/uint32 metadata, zero tensors."""
    path.parent.mkdir(parents=True, exist_ok=True)
    out = [b"GGUF", struct.pack("<I", 3), struct.pack("<Q", 0), struct.pack("<Q", len(kv))]
    for k, v in kv.items():
        kb = k.encode()
        out.append(struct.pack("<Q", len(kb)) + kb)
        if isinstance(v, str):
            vb = v.encode()
            out.append(struct.pack("<I", 8) + struct.pack("<Q", len(vb)) + vb)
        else:
            out.append(struct.pack("<I", 4) + struct.pack("<I", v))
    path.write_bytes(b"".join(out))
    return path


def model(path, n_embd, arch="qwen35"):
    return write_gguf(path, {"general.architecture": arch,
                             f"{arch}.block_count": 32,
                             f"{arch}.attention.head_count": 16,
                             f"{arch}.attention.head_count_kv": 4,
                             f"{arch}.embedding_length": n_embd,
                             f"{arch}.attention.key_length": 256,
                             f"{arch}.context_length": 262144,
                             f"{arch}.expert_count": 0})


def mtp(path, n_embd, arch="qwen35", nextn=1):
    p = model(path, n_embd, arch)
    # re-write with the MTP marker key appended
    return write_gguf(p, {"general.architecture": arch,
                          f"{arch}.embedding_length": n_embd,
                          f"{arch}.nextn_predict_layers": nextn})


def mmproj(path, proj_dim):
    return write_gguf(path, {"general.architecture": "clip",
                             "clip.vision.projection_dim": proj_dim})


class ModelLayoutTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self._saved = (profiles.MODELS_DIR, list(profiles._extra_roots))
        profiles.MODELS_DIR = self.root
        profiles._extra_roots.clear()

    def tearDown(self):
        profiles.MODELS_DIR, roots = self._saved
        profiles._extra_roots[:] = roots
        self.tmp.cleanup()

    def test_mismatched_companions_rejected(self):
        """The reported bug: a 9B must not get the 27B's MTP draft."""
        m9 = model(self.root / "Qwen3.8-9B" / "Qwen3.8-9B-Q4.gguf", 4096)
        mtp(self.root / "Qwen3.8-9B" / "mtp-Qwen3.8-27B-Q4_0.gguf", 5120)
        mmproj(self.root / "Qwen3.8-9B" / "mmproj-BF16.gguf", 4096)
        c = profiles.companions(m9)
        self.assertIsNone(c["mtp"])
        self.assertIn("5120-wide", c["mtp_note"])
        self.assertEqual(c["mmproj"].name, "mmproj-BF16.gguf")   # any name inside the folder

    def test_companions_never_cross_folders(self):
        m27 = model(self.root / "Qwen3.8-27B" / "Qwen3.8-27B-Q4.gguf", 5120)
        good = mtp(self.root / "Qwen3.8-27B" / "mtp-Qwen3.8-27B-Q4_0.gguf", 5120)
        mmproj(self.root / "Qwen3.8-9B" / "mmproj-Qwen3.8-9B-BF16.gguf", 4096)
        c = profiles.companions(m27)
        self.assertEqual(c["mtp"], good)
        self.assertIsNone(c["mmproj"])
        self.assertEqual(c["mmproj_note"], "")

    def test_wrong_projector_size_rejected(self):
        m27 = model(self.root / "Big" / "big.gguf", 5120)
        mmproj(self.root / "Big" / "mmproj-small.gguf", 4096)
        c = profiles.companions(m27)
        self.assertIsNone(c["mmproj"])
        self.assertIn("4096-wide", c["mmproj_note"])

    def test_draft_without_mtp_layers_rejected(self):
        m = model(self.root / "M" / "m.gguf", 4096)
        mtp(self.root / "M" / "mtp-m.gguf", 4096, nextn=0)
        self.assertIn("no MTP layers", profiles.companions(m)["mtp_note"])

    def test_flat_layout_exact_names_only(self):
        m = model(self.root / "Qwen3.8-9B-Q4.gguf", 4096)
        mmproj(self.root / "mmproj-Qwen3.8-other.gguf", 4096)      # family-only match
        self.assertIsNone(profiles.companions(m)["mmproj"])
        exact = mmproj(self.root / "mmproj-Qwen3.8-9B-Q4.gguf", 4096)
        self.assertEqual(profiles.companions(m)["mmproj"], exact)

    def test_manifest_pins_and_rejects_paths(self):
        folder = self.root / "V"
        m = model(folder / "v.gguf", 4096)
        mmproj(folder / "mmproj-F16.gguf", 4096)
        pinned = mmproj(folder / "mmproj-BF16.gguf", 4096)
        (folder / "model.json").write_text(json.dumps({"mmproj": "mmproj-F16.gguf"}))
        self.assertEqual(profiles.companions(m)["mmproj"].name, "mmproj-F16.gguf")
        (folder / "model.json").write_text(json.dumps({"mmproj": ""}))
        self.assertIsNone(profiles.companions(m)["mmproj"])
        (folder / "model.json").write_text(json.dumps({"mmproj": "../V/" + pinned.name}))
        c = profiles.companions(m)
        self.assertIsNone(c["mmproj"])
        self.assertIn("not a file in this folder", c["mmproj_note"])

    def test_discovery_skips_helpers_companions_and_shards(self):
        model(self.root / "A" / "a.gguf", 4096)
        mmproj(self.root / "A" / "mmproj-a.gguf", 4096)
        model(self.root / "S" / "s-00001-of-00002.gguf", 4096)
        model(self.root / "S" / "s-00002-of-00002.gguf", 4096)
        model(self.root / "orchestrator" / "helper.gguf", 2048)
        model(self.root / "flat.gguf", 4096)
        found = sorted((f.name, folder) for f, folder in profiles.discover_models())
        self.assertEqual(found, [("a.gguf", "A"), ("flat.gguf", None),
                                 ("s-00001-of-00002.gguf", "S")])

    def test_in_models_dir(self):
        m = model(self.root / "A" / "a.gguf", 4096)
        self.assertTrue(profiles.in_models_dir(m))
        self.assertFalse(profiles.in_models_dir(self.root.parent / "elsewhere.gguf"))


if __name__ == "__main__":
    unittest.main()
