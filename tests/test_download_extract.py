"""scripts/download_models.py _safe_extract: llama.cpp's Linux tarball holds symlinks (libllama.so -> libllama.so.0)."""
import importlib.util
import io
import os
import tarfile
import tempfile
import unittest
from pathlib import Path

_spec = importlib.util.spec_from_file_location("download_models", Path(__file__).resolve().parents[1] / "scripts" / "download_models.py")
dl = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(dl)


def make_tar(path: Path, links: list) -> None:
    with tarfile.open(path, "w:gz") as t:
        data = b"x"
        info = tarfile.TarInfo("llama-b1/libllama.so.0")
        info.size = len(data)
        t.addfile(info, io.BytesIO(data))
        for name, target in links:
            info = tarfile.TarInfo(name)
            info.type = tarfile.SYMTYPE
            info.linkname = target
            t.addfile(info)


class SafeExtractTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())

    def test_link_pointing_outside_is_refused(self):
        for target in ("../../etc/passwd", "/etc/passwd"):
            make_tar(self.tmp / "a.tgz", [("llama-b1/libllama.so", target)])
            with self.assertRaises(ValueError):
                dl._safe_extract(self.tmp / "a.tgz", self.tmp / "out")

    @unittest.skipIf(os.name == "nt", "creating symlinks needs a privilege on Windows")
    def test_link_inside_the_folder_is_extracted(self):
        make_tar(self.tmp / "b.tgz", [("llama-b1/libllama.so", "libllama.so.0")])
        dl._safe_extract(self.tmp / "b.tgz", self.tmp / "out")
        self.assertTrue((self.tmp / "out" / "llama-b1" / "libllama.so").is_symlink())


if __name__ == "__main__":
    unittest.main()
