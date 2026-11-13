import os
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SKIP_DIRS = {"node_modules", ".git", "vendor", "dist", "__pycache__", ".venv", "venv", "build"}
SUFFIXES = (".py", ".js", ".ts", ".tsx", ".md", ".json", ".html", ".css")
MAX_BYTES = 2 * 1024 * 1024
ALLOWED = {9, 10, 13}  # tab, LF, CR


def control_byte_lines(data: bytes):
    """1-based line numbers holding a byte < 32 other than tab/LF/CR."""
    return [n for n, line in enumerate(data.split(b"\n"), 1)
            if any(b < 32 and b not in ALLOWED for b in line)]


def find_control_byte_hits(root: str):
    hits = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
        for name in filenames:
            if not name.endswith(SUFFIXES):
                continue
            path = os.path.join(dirpath, name)
            try:
                if os.path.getsize(path) > MAX_BYTES:
                    continue
                with open(path, "rb") as f:
                    data = f.read()
            except OSError:
                continue
            rel = os.path.relpath(path, root).replace(os.sep, "/")
            hits += [f"{rel}:{n}" for n in control_byte_lines(data)]
    return hits


class ControlByteHelperTest(unittest.TestCase):
    def test_detects_backspace_and_nul(self):
        self.assertEqual(control_byte_lines(b"ok\tfine\r\nbad\x08here\n"), [2])
        self.assertEqual(control_byte_lines(b"a\x00\nb"), [1])

    def test_allows_tab_lf_cr(self):
        self.assertEqual(control_byte_lines(b"a\tb\r\nc\r\n"), [])


class NoControlBytesTest(unittest.TestCase):
    def test_repo_has_no_control_bytes(self):
        hits = find_control_byte_hits(ROOT)
        self.assertEqual(hits, [], "raw control bytes (use an escaped form such as backslash-b):\n" + "\n".join(hits))


if __name__ == "__main__":
    unittest.main()
