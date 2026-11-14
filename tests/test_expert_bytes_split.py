import tempfile
import unittest
from pathlib import Path
from unittest import mock

from core.vram import gguf_parser as g


class SplitExpertBytes(unittest.TestCase):
    def _call(self, parts):
        with tempfile.TemporaryDirectory() as d:
            files = [Path(d) / f"m-{i + 1:05d}-of-{len(parts):05d}.gguf" for i in range(len(parts))]
            for f in files:
                f.write_bytes(b"x")
            by_name = {f: p for f, p in zip(files, parts)}
            with mock.patch.object(g, "_expert_bytes_file", side_effect=lambda f: by_name[Path(f)]):
                return g.expert_layer_bytes(files[0])

    def test_sums_shards_and_merges_a_block_split_across_files(self):
        self.assertEqual(self._call([{0: 10, 1: 5}, {1: 5, 2: 7}, {}]), {0: 10, 1: 10, 2: 7})

    def test_unreadable_shard_gives_empty_not_partial(self):
        self.assertEqual(self._call([{0: 10}, None]), {})


if __name__ == "__main__":
    unittest.main()
