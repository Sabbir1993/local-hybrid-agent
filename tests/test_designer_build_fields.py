import io
import unittest
import zipfile

from core.doc_ops.pptx_ops import designer


def _slide(title, bullets, kind="bullets", notes=""):
    return {"title": title, "kind": kind, "bullets": bullets, "table": [], "notes": notes, "subtitle": "",
            "chart": None, "image": None, "callout": "", "alert": "", "eyebrow": "", "layout": kind,
            "art": None, "source": "", "image_extra": None}


class DesignerStripsBuildFields(unittest.TestCase):
    def test_tone_text_never_reaches_the_picture_or_the_notes(self):
        deck = {"title": "Deck", "theme": "forest", "mode": "designer", "slides": [
            _slide("Deck", [], kind="cover"),
            _slide("The Week at a Glance", ["62 | Total activities logged | tone=green",
                                            "Weekly summary of activity",
                                            "117 hrs | Time invested | tone=amber"], notes="Start here.")]}
        data = designer.build(deck)
        if not data:   # no Chrome / Edge on this machine: the designer style is not available here
            self.skipTest("designer style unavailable")
        z = zipfile.ZipFile(io.BytesIO(data))
        for n in z.namelist():
            if n.endswith(".xml"):
                self.assertNotIn(b"tone=", z.read(n), n)


if __name__ == "__main__":
    unittest.main()
