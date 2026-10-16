"""Quality benchmark: a fixed set of Word documents (incident report, policy, proposal, minutes, Bengali) must build,
open, keep their headings / tables / footer page number and carry no leftover markdown.

Offline counterpart of the live run in scripts/bench_chat_files.py: the outlines in tests/file_bench_prompts.py stand in for what
a model writes, so a regression in core/doc_ops/docx_ops shows up here as a named finding without a GPU.
"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from core.doc_ops import docx_ops
from file_bench_checks import check_docx
from file_bench_prompts import DOCX_PROMPTS


class DocxBenchmark(unittest.TestCase):
    def test_every_outline_builds_clean(self):
        bad, total = [], 0
        for name, spec in DOCX_PROMPTS.items():
            data = docx_ops.create(spec["outline"], title=name.replace("_", " ").title())
            total += 1
            bad += [f"{name}: {f}" for f in check_docx(data, spec["outline"])]
        self.assertEqual(bad, [], f"{len(bad)} findings over {total} documents")


if __name__ == "__main__":
    unittest.main()
