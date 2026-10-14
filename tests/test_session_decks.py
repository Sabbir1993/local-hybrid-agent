"""Several decks in one chat: model clutter never becomes slides, edits can target a specific deck, a new deck keeps the
session's look, and write_file (the other way a model can write a deck) gets the same pictures and checks as doc_create."""
import asyncio
import io
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import core.agent_tools  # noqa: F401,E402
from PIL import Image  # noqa: E402
from pptx import Presentation  # noqa: E402

from core import agent_tools, doc_store, doc_tools  # noqa: E402
from core.doc_ops import pptx_ops  # noqa: E402
from core.doc_ops.pptx_ops import outline as O  # noqa: E402
from core.doc_tools import operations as ops  # noqa: E402
from core.doc_tools import slide_images as si  # noqa: E402
from core.request_context import set_current_user  # noqa: E402
from routes.chat.file_intent import pick_target  # noqa: E402


def png() -> bytes:
    b = io.BytesIO()
    Image.new("RGB", (1200, 800), (30, 120, 200)).save(b, "PNG")
    return b.getvalue()


class ModelClutter(unittest.TestCase):
    def titles(self, md):
        return [s["title"] for s in O.parse_outline(md)["slides"]]

    def test_ascii_art_boxes_are_dropped(self):
        md = ("# D\nsubtitle: s\n---\n## Economy\n- Key sectors\n"
              "┌──────┐\n│ GARMENTS │\n└──────┘\n- Exports\n"
              "╔═══╗\n║ TOTAL ║\n╚═══╝\n").replace("\\n", "\n")
        deck = O.parse_outline(md)
        text = " ".join(" ".join(s["bullets"]) for s in deck["slides"])
        self.assertNotRegex(text, "[─-▟]")
        self.assertIn("Exports", text)

    def test_raw_xml_never_becomes_a_slide(self):
        md = ('# D\nsubtitle: s\n---\n<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
              '<presentation xmlns="http://schemas.openxmlformats.org/presentationml/2006/main">\n'
              '<p:sldLayout type="title" xmlns:r="x">\n---\n## Real\n- one\n- two\n')
        self.assertEqual(self.titles(md), ["D", "Real"])

    def test_slides_about_the_deck_itself_are_dropped(self):
        md = ("# D\nsubtitle: s\n---\n## History\n- a\n- b\n---\n## Design Notes for This Deck\n- Minimal palette\n- White background\n"
              "---\n## Image Sources Used\n- https://example.org/a.jpg\n---\n## Sources\n- WHO report 2024\n- World Bank data\n")
        self.assertEqual(self.titles(md), ["D", "History", "Sources"])           # a real "Sources" slide stays

    def test_the_first_slide_is_never_dropped(self):
        self.assertEqual(self.titles("# Design Notes\nsubtitle: s\n---\n## A\n- x\n- y\n")[0], "Design Notes")

    def test_the_attached_deck_shape_builds_clean(self):
        md = ("# Dhaka Deep Dive\nsubtitle: A city\n---\n<?xml version=\"1.0\"?>\n<p:sldLayout type=\"title\">\n---\n"
              "## Tourism\n- Lalbagh Fort\n\u250c\u2500\u2500\u2510\n\u2502 MUST SEE \u2502\n\u2514\u2500\u2500\u2518\n- Ahsan Manzil\n---\n"
              "## Design Notes for This Deck\n- Minimal palette\n---\n## Image Sources Used\n- url\n")
        prs = Presentation(io.BytesIO(pptx_ops.create(md)))
        texts = " ".join(sh.text_frame.text for sl in prs.slides for sh in sl.shapes if sh.has_text_frame)
        self.assertNotIn("<?xml", texts)
        self.assertNotIn("Design Notes", texts)
        self.assertNotIn("Image Sources", texts)
        self.assertNotRegex(texts, "[\u2500-\u259f]")
        self.assertEqual(len(prs.slides), 2)


class SpecificDeck(unittest.TestCase):
    FILES = ["Dhaka_Deep_Dive-aaaa1111.pptx", "Cyber_Risk_Briefing-bbbb2222.pptx", "Onboarding_Guide-cccc3333.pdf",
             "Cyber_Risk_Briefing-v2-dddd4444.pptx"]

    def test_default_is_the_newest(self):
        self.assertEqual(pick_target("make the title bigger", self.FILES), self.FILES[-1])

    def test_a_named_file_wins(self):
        self.assertEqual(pick_target("change slide 2 in Dhaka_Deep_Dive-aaaa1111.pptx", self.FILES), self.FILES[0])

    def test_name_without_the_id_suffix_gives_the_newest_version(self):
        self.assertEqual(pick_target("edit Cyber Risk Briefing slide 3", self.FILES), "Cyber_Risk_Briefing-v2-dddd4444.pptx")

    def test_a_word_only_one_deck_has(self):
        self.assertEqual(pick_target("update the dhaka deck", self.FILES), self.FILES[0])
        self.assertEqual(pick_target("fix the onboarding one", self.FILES), self.FILES[2])

    def test_type_words(self):
        self.assertEqual(pick_target("add a section to the pdf", self.FILES), self.FILES[2])
        self.assertEqual(pick_target("change the presentation colours", self.FILES), self.FILES[-1])

    def test_previous_picks_the_one_before(self):
        files = ["A_Deck-aaaa1111.pptx", "B_Deck-bbbb2222.pptx"]
        self.assertEqual(pick_target("change the previous presentation", files), files[0])

    def test_nothing_to_pick(self):
        self.assertEqual(pick_target("edit it", []), "")
        self.assertEqual(pick_target("edit it", ["only-aaaa1111.pptx"]), "only-aaaa1111.pptx")


class StyleMemory(unittest.TestCase):
    def setUp(self):
        ops._LAST_STYLE.clear()

    def tearDown(self):
        ops._LAST_STYLE.clear()

    def test_next_deck_without_a_theme_inherits_the_last_one(self):
        first = "theme: forest\naccent: D97706\n# One\nsubtitle: s\n---\n## A\n- x\n- y\n"
        self.assertEqual(ops._inherit_style(1, ".pptx", first), first)           # explicit: recorded, unchanged
        second = ops._inherit_style(1, ".pptx", "# Two\nsubtitle: s\n---\n## B\n- x\n- y\n")
        self.assertTrue(second.startswith("theme: forest\naccent: D97706\n"))
        deck = O.parse_outline(second)
        self.assertEqual(O.resolve_theme(deck)["name"], "forest")
        self.assertEqual(O.resolve_theme(deck)["accent"], "D97706")

    def test_an_explicit_theme_wins_and_becomes_the_memory(self):
        ops._inherit_style(1, ".pptx", "theme: forest\n# One\nsubtitle: s\n---\n## A\n- x\n- y\n")
        out = ops._inherit_style(1, ".pptx", "theme: royal\n# Two\nsubtitle: s\n---\n## B\n- x\n- y\n")
        self.assertEqual(O.resolve_theme(O.parse_outline(out))["name"], "royal")
        third = ops._inherit_style(1, ".pptx", "# Three\nsubtitle: s\n---\n## C\n- x\n- y\n")
        self.assertEqual(O.resolve_theme(O.parse_outline(third))["name"], "royal")

    def test_font_is_inherited_too(self):
        ops._inherit_style(1, ".pptx", "theme: minimal\nfont: Arial\n# One\nsubtitle: s\n---\n## A\n- x\n- y\n")
        out = ops._inherit_style(1, ".pptx", "# Two\nsubtitle: s\n---\n## B\n- x\n- y\n")
        self.assertEqual(O.resolve_theme(O.parse_outline(out))["body"], "Arial")

    def test_users_do_not_share_a_look(self):
        ops._inherit_style(1, ".pptx", "theme: forest\n# One\nsubtitle: s\n---\n## A\n- x\n- y\n")
        md = "# Two\nsubtitle: s\n---\n## B\n- x\n- y\n"
        self.assertEqual(ops._inherit_style(2, ".pptx", md), md)

    def test_memory_expires(self):
        ops._inherit_style(1, ".pptx", "theme: forest\n# One\nsubtitle: s\n---\n## A\n- x\n- y\n")
        ops._LAST_STYLE[1]["ts"] = time.time() - ops._STYLE_TTL_S - 5
        md = "# Two\nsubtitle: s\n---\n## B\n- x\n- y\n"
        self.assertEqual(ops._inherit_style(1, ".pptx", md), md)

    def test_switch_off_and_other_formats_are_untouched(self):
        ops._inherit_style(1, ".pptx", "theme: forest\n# One\nsubtitle: s\n---\n## A\n- x\n- y\n")
        md = "# Two\nsubtitle: s\n---\n## B\n- x\n- y\n"
        with mock.patch.object(ops, "_flag", side_effect=lambda n, d=True: n != "deck_keep_style"):
            self.assertEqual(ops._inherit_style(1, ".pptx", md), md)
        self.assertEqual(ops._inherit_style(1, ".pdf", md), md)
        self.assertEqual(ops._inherit_style(None, ".pptx", md), md)


class WriteFileGetsTheSamePipeline(unittest.TestCase):
    def setUp(self):
        import tests.test_doc_ops as T
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self._patches = [mock.patch.object(agent_tools, "COMMON_ROOT", self.root),
                         mock.patch.object(doc_store, "_projects_db", T.doc_files_db()),
                         mock.patch.object(si.doc_store, "audit")]
        for p in self._patches:
            p.start()
        set_current_user(8)
        si._cache.clear()
        ops._LAST_STYLE.clear()
        self.addCleanup(self._cleanup)

    def _cleanup(self):
        for p in self._patches:
            p.stop()
        set_current_user(None)
        ops._LAST_STYLE.clear()
        self.tmp.cleanup()

    def _pictures(self, name):
        prs = Presentation(io.BytesIO((self.root / "user_8" / name).read_bytes()))
        return sum(1 for sl in prs.slides for sh in sl.shapes if sh.shape_type == 13)

    MD = "# Dhaka\nsubtitle: s\n---\n## Landmarks\nimage: Lalbagh Fort Dhaka\n- Lalbagh Fort\n- Ahsan Manzil\n- Star Mosque\n"

    def _fake_web(self):
        async def fake(uid, q, why, anchors=()):
            return {"data": png(), "credit_host": "example.org", "credit_url": "https://example.org/p", "via": "web"}
        return mock.patch.object(si, "_from_web", fake)

    def test_write_file_pptx_gets_pictures(self):
        with self._fake_web():
            out = agent_tools.tool_write_file_common({"path": "dhaka.pptx", "content": self.MD})
        name = out.split("[DOWNLOAD: ")[1].split("]")[0]
        self.assertEqual(self._pictures(name), 1)
        self.assertIn("Pictures: 1 added", out)

    def test_it_also_works_from_inside_a_running_event_loop(self):
        async def call():
            return agent_tools.tool_write_file_common({"path": "loop.pptx", "content": self.MD})
        with self._fake_web():
            out = asyncio.run(call())
        self.assertEqual(self._pictures(out.split("[DOWNLOAD: ")[1].split("]")[0]), 1)

    def test_write_file_deck_keeps_the_session_look(self):
        agent_tools.tool_write_file_common({"path": "one.pptx", "content": "theme: forest\n" + self.MD.replace("image: Lalbagh Fort Dhaka\n", "")})
        out = agent_tools.tool_write_file_common({"path": "two.pptx", "content": self.MD.replace("image: Lalbagh Fort Dhaka\n", "")})
        from pptx.opc.constants import RELATIONSHIP_TYPE as RT
        name = out.split("[DOWNLOAD: ")[1].split("]")[0]
        prs = Presentation(io.BytesIO((self.root / "user_8" / name).read_bytes()))
        theme = prs.slide_master.part.part_related_by(RT.THEME).blob.decode("utf-8")
        self.assertIn('typeface="Georgia"', theme)                                # forest = Georgia headings

    def test_write_file_deck_ignores_pasted_xml_and_meta_slides(self):
        md = self.MD.replace("image: Lalbagh Fort Dhaka\n", "") + "---\n## Image Sources Used\n- https://x.org/a.jpg\n"
        out = agent_tools.tool_write_file_common({"path": "clean.pptx", "content": md})
        name = out.split("[DOWNLOAD: ")[1].split("]")[0]
        prs = Presentation(io.BytesIO((self.root / "user_8" / name).read_bytes()))
        self.assertEqual(len(prs.slides), 2)

    def test_pictures_off_leaves_the_lines_out_and_still_writes(self):
        with mock.patch.object(ops, "_flag", side_effect=lambda n, d=True: n != "deck_images"):
            out = agent_tools.tool_write_file_common({"path": "off.pptx", "content": self.MD})
        self.assertEqual(self._pictures(out.split("[DOWNLOAD: ")[1].split("]")[0]), 0)

    def test_a_failing_picture_step_never_blocks_the_file(self):
        with mock.patch.object(ops, "prepare_build", side_effect=RuntimeError("boom")):
            out = agent_tools.tool_write_file_common({"path": "safe.pptx", "content": self.MD})
        self.assertIn("[DOWNLOAD:", out)


if __name__ == "__main__":
    unittest.main()
