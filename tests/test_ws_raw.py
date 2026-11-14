"""GET /agent/ws/raw - streaming a workspace file for the preview modal.

Why this endpoint exists: /agent/raw only serves the server-side common space, so
previews of project files (src/shop.js and friends) from an agent tool card 404'd
with {"error":"file not found"}. This reads the user's own machine through the
companion, with the same workspace guards agent_ws_file uses.

The security surface is the point of the tests: the path must never escape the
active workspace, bytes must never come from the server's disk.

Run: python -m unittest tests.test_ws_raw -v
"""

import asyncio
import base64
import sqlite3
import sys
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core import agent_tools, companion_bridge  # noqa: E402
from core.request_context import set_current_device, set_current_user  # noqa: E402
from routes import agent as agent_routes  # noqa: E402
sys.path.insert(0, str(Path(__file__).resolve().parent))
from _patching import patch_in_package  # noqa: E402

USER = 5
WS = Path(r"C:\Users\tester\proj")


class _Principal:
    id = USER
    role_names = {"user"}


def _enter(patches):
    stack = ExitStack()
    for p in patches:
        stack.enter_context(p)
    return stack


class WsRawTests(unittest.TestCase):
    def setUp(self):
        self.files = {
            str(WS / "src" / "shop.js"): b"export const shop = 1;",
            str(WS / "logo.png"): b"\x89PNG\r\n\x1a\n\x00\x01",
        }
        self.calls = []
        self._db = sqlite3.connect(":memory:", check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        self._db.execute("CREATE TABLE projects (name TEXT, user_id INTEGER, "
                         "device_id TEXT, workspace_dir TEXT)")
        self._db.execute("INSERT INTO projects VALUES ('p', ?, 'dev', ?)", (USER, str(WS)))

    async def _call(self, uid, op, params, timeout=None):
        self.calls.append((op, params["path"]))
        if op == "fs.read":
            b = self.files.get(params["path"])
            return {"content": b.decode("utf-8", "replace") if b is not None else None}
        if op == "fs.read_b64":
            b = self.files.get(params["path"])
            return {"data": base64.b64encode(b).decode() if b is not None else None}
        raise AssertionError(f"unexpected companion op {op}")

    def _get(self, path, proj="p", call=None, fn=None, **kw):
        patches = [
            mock.patch.object(agent_tools, "_projects_db", self._db),
            mock.patch.object(companion_bridge, "is_connected", lambda uid: uid == USER),
            mock.patch.object(companion_bridge, "call", call or self._call),
            mock.patch.dict(agent_tools._active_project,
                            {f"{USER}:dev": proj} if proj else {}, clear=True),
            *patch_in_package(agent_routes, "_remote_uid", lambda: USER),
            *patch_in_package(agent_routes, "_ws_resolve", agent_tools._ws_resolve),
            *patch_in_package(agent_routes, "get_active_project", lambda uid: proj),
        ]
        with _enter(patches):
            set_current_user(USER)
            set_current_device("dev")
            return asyncio.run((fn or agent_routes.agent_ws_raw)(path, user=_Principal(), **kw))

    def _body(self, r):
        return r.body.decode("utf-8", "replace")

    # --- text -------------------------------------------------------------
    def test_text_file_is_streamed(self):
        r = self._get("src/shop.js")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.body, b"export const shop = 1;")
        self.assertIn("text/plain", r.media_type)
        self.assertIn(("fs.read", str(WS / "src" / "shop.js")), self.calls,
                      "text must be read as utf-8 text, not base64")

    def test_binary_file_is_streamed_as_bytes(self):
        r = self._get("logo.png")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.body, b"\x89PNG\r\n\x1a\n\x00\x01")
        self.assertEqual(r.media_type, "image/png")
        self.assertIn("fs.read_b64", [c[0] for c in self.calls], "binary must use fs.read_b64")

    # --- security ---------------------------------------------------------
    def test_path_escape_is_refused(self):
        for bad in ("../../etc/passwd", "..\\..\\secrets.txt", "src/../../../outside.txt"):
            self.calls.clear()
            r = self._get(bad)
            self.assertIn(r.status_code, (400, 403, 404), f"{bad} must not be served")
            # the 403 may echo the requested path back (it is the caller's own input);
            # what must never appear is file CONTENT
            self.assertNotIn("root:", self._body(r)[:200])
            self.assertEqual(self.calls, [], f"{bad} must never reach the companion")

    def test_nothing_is_read_from_the_servers_disk(self):
        r = self._get("src/shop.js")
        self.assertEqual(r.body, self.files[str(WS / "src" / "shop.js")])
        self.assertTrue(all(c[0].startswith("fs.") for c in self.calls),
                        "every byte must come through the companion")

    def test_response_is_not_sniffable_or_scriptable(self):
        r = self._get("logo.png")
        self.assertEqual(r.headers.get("x-content-type-options"), "nosniff")
        self.assertIn("sandbox", r.headers.get("content-security-policy", ""))

    def test_a_pdf_is_not_sandboxed_so_the_browsers_viewer_can_open_it(self):
        self.files[str(WS / "report.pdf")] = b"%PDF-1.4\n%fake\n"
        r = self._get("report.pdf")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.media_type, "application/pdf")
        self.assertNotIn("content-security-policy", {k.lower() for k in r.headers.keys()})
        self.assertEqual(r.headers.get("x-content-type-options"), "nosniff")

    def test_everything_else_stays_sandboxed(self):
        self.files[str(WS / "page.html")] = b"<script>1</script>"
        self.files[str(WS / "deck.pptx")] = b"PK\x03\x04"
        for name in ("page.html", "logo.png", "src/shop.js", "deck.pptx"):
            r = self._get(name)
            self.assertIn("sandbox", r.headers.get("content-security-policy", ""), name)

    # --- slides and page pictures of a file on the user's machine ----------------------------------------
    def _deck(self):
        import io
        from pptx import Presentation
        prs = Presentation()
        prs.slides.add_slide(prs.slide_layouts[5]).shapes.title.text = "Quarterly review"
        buf = io.BytesIO()
        prs.save(buf)
        return buf.getvalue()

    def test_slides_of_a_deck_in_the_workspace(self):
        import json
        self.files[str(WS / "deck.pptx")] = self._deck()
        r = self._get("deck.pptx", fn=agent_routes.agent_ws_slides)
        self.assertEqual(r.status_code, 200)
        model = json.loads(r.body)
        self.assertEqual(len(model["slides"]), 1)
        self.assertIn("fs.read_b64", [c[0] for c in self.calls])

    def test_slides_refuses_other_files_without_reading_them(self):
        r = self._get("src/shop.js", fn=agent_routes.agent_ws_slides)
        self.assertEqual(r.status_code, 400)
        self.assertEqual(self.calls, [])

    def test_slides_of_a_missing_deck_and_of_a_path_escape(self):
        self.assertEqual(self._get("nope.pptx", fn=agent_routes.agent_ws_slides).status_code, 404)
        self.calls.clear()
        r = self._get("../../secret.pptx", fn=agent_routes.agent_ws_slides)
        self.assertIn(r.status_code, (400, 403, 404))
        self.assertEqual(self.calls, [])

    def test_a_deck_too_big_to_read_whole_is_not_previewed_from_a_cut_file(self):
        self.files[str(WS / "huge.pptx")] = b"PK" + b"a" * (agent_routes._WS_RAW_MAX + 10)
        r = self._get("huge.pptx", fn=agent_routes.agent_ws_slides)
        self.assertEqual(r.status_code, 413)

    def test_page_count_is_zero_without_a_renderer_and_for_other_files(self):
        import json
        self.files[str(WS / "deck.pptx")] = self._deck()
        with mock.patch("core.doc_tools.render.to_pdf", lambda data, ext: None):
            r = self._get("deck.pptx", fn=agent_routes.agent_ws_doc_pages)
        self.assertEqual(json.loads(r.body), {"pages": 0})
        self.calls.clear()
        r = self._get("src/shop.js", fn=agent_routes.agent_ws_doc_pages)
        self.assertEqual(json.loads(r.body), {"pages": 0})
        self.assertEqual(self.calls, [], "a file LibreOffice cannot draw is not even read")

    def test_page_count_comes_from_the_rendered_deck(self):
        import json
        self.files[str(WS / "deck.pptx")] = self._deck()

        async def fake_pages(data, ext):
            self.assertEqual(ext, ".pptx")
            self.assertTrue(data.startswith(b"PK"))
            return b"%PDF", 3
        with mock.patch("routes.agent.preview._pages_of_bytes", fake_pages):
            r = self._get("deck.pptx", fn=agent_routes.agent_ws_doc_pages)
            bad = self._get("deck.pptx", fn=agent_routes.agent_ws_doc_page, n=9)
        self.assertEqual(json.loads(r.body), {"pages": 3})
        self.assertEqual(bad.status_code, 404)               # page 9 of 3

    # --- errors -----------------------------------------------------------
    def test_missing_file_is_404(self):
        r = self._get("src/nope.js")
        self.assertEqual(r.status_code, 404)
        self.assertIn("file not found", self._body(r))

    def test_no_project_selected(self):
        r = self._get("src/shop.js", proj=None)
        self.assertEqual(r.status_code, 400)
        self.assertIn("No project selected", self._body(r))

    def test_companion_failure_is_502(self):
        async def boom(uid, op, params, timeout=None):
            raise ConnectionError("companion not running")
        r = self._get("src/shop.js", call=boom)
        self.assertEqual(r.status_code, 502)
        self.assertIn("SSL Local Agent", self._body(r))

    def test_oversize_file_is_truncated(self):
        self.files[str(WS / "big.txt")] = b"a" * (agent_routes._WS_RAW_MAX + 5000)
        r = self._get("big.txt")
        self.assertEqual(len(r.body), agent_routes._WS_RAW_MAX)


if __name__ == "__main__":
    unittest.main()
