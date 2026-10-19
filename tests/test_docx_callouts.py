import unittest
import io
from docx import Document
from core.doc_ops.docx_ops import creation


class TestDocxCallouts(unittest.TestCase):
    def test_callout_rendering(self):
        md = """# Executive Brief
> [!NOTE] This is an important strategic initiative.

> [!WARNING] Urgent intervention required before sprint.

Regular text follows.
"""
        docx_bytes = creation.create(md, title="Executive Brief")
        self.assertIsNotNone(docx_bytes)
        doc = Document(io.BytesIO(docx_bytes))
        texts = [p.text for p in doc.paragraphs]
        self.assertTrue(any("Note: " in t for t in texts))
        self.assertTrue(any("Warning: " in t for t in texts))


if __name__ == "__main__":
    unittest.main()
