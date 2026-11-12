"""The start of a chat's prompt stays the same from turn to turn (core/prompt_sticky.py), so the model server can reuse
what it already read: per-turn text leaves the system message, manuals and document tools stay once used, tool order is
stable."""
import unittest
from unittest import mock

from core import prompt_sticky as ps


def tool(name):
    return {"type": "function", "function": {"name": name, "description": name}}


def names(tools):
    return [t["function"]["name"] for t in tools]


class ArrangeTests(unittest.TestCase):
    def setUp(self):
        ps.reset()
        self._on = mock.patch.object(ps, "enabled", return_value=True)
        self._on.start()
        self.addCleanup(self._on.stop)

    def run_turn(self, parts, tools, **kw):
        return ps.arrange(parts, tools, 7, 42, **kw)

    def test_turn_text_leaves_the_system_message(self):
        system, turn, _ = self.run_turn([("date", "D"), ("kb", "hits"), ("file-lean", "L"), ("prior-files", "P")],
                                        [tool("write_file")])
        self.assertEqual([l for l, _ in system], ["date", "file-lean"])
        self.assertEqual([l for l, _ in turn], ["kb", "prior-files"])

    def test_prefix_only_grows_at_the_end(self):
        base = [("date", "D"), ("memory", "M")]
        t1 = [tool("write_file"), tool("search_knowledge_base")]
        s1, _, tools1 = self.run_turn(base + [("file-lean", "L"), ("no-ascii", "N")], t1)
        # turn 2 asks for a deck: file manual, deck manual and document tools appear
        s2, _, tools2 = self.run_turn(base + [("file-manual", "FM"), ("pptx-manual", "PM"), ("no-ascii", "N")],
                                      [tool("write_file"), tool("doc_inspect"), tool("doc_create"), tool("search_knowledge_base")])
        # turn 3 is small talk again: the manuals and tools of turn 2 stay, in the same order
        s3, _, tools3 = self.run_turn(base + [("file-lean", "L"), ("no-ascii", "N")], t1)
        self.assertEqual(s3, s2)
        self.assertEqual(names(tools3), names(tools2))
        self.assertEqual(names(tools2)[:2], names(tools1))               # earlier tools keep their place
        self.assertNotIn("file-lean", [l for l, _ in s3])                # replaced by the kept file manual
        # turn 4 adds a workbook: only an addition at the end
        s4, _, _ = self.run_turn(base + [("file-manual", "FM"), ("xlsx-manual", "XM"), ("no-ascii", "N")],
                                 [tool("write_file"), tool("doc_create"), tool("search_knowledge_base")])
        # kept manuals come first (core/prompt_sticky.py): the new one is added at the END of that group
        kept3 = [x for x in s3 if x[0] in ps.STICKY_LABELS]
        kept4 = [x for x in s4 if x[0] in ps.STICKY_LABELS]
        self.assertEqual(kept4[:len(kept3)], kept3)
        self.assertEqual(kept4[-1], ("xlsx-manual", "XM"))
        self.assertEqual(s4[:len(kept4)], kept4)

    def test_web_tools_are_never_kept_on(self):
        self.run_turn([("date", "D"), ("web-manual", "W")], [tool("write_file"), tool("web_search")])
        system, _, tools = self.run_turn([("date", "D")], [tool("write_file")])
        self.assertEqual(names(tools), ["write_file"])
        self.assertEqual([l for l, _ in system], ["date"])

    def test_a_manual_is_dropped_while_its_tool_is_not_offered(self):
        self.run_turn([("date", "D"), ("pdf-tools", "PDF")], [tool("write_file"), tool("pdf_extract")])
        system, _, tools = self.run_turn([("date", "D")], [tool("write_file")], allow_tools=False)
        self.assertEqual(names(tools), ["write_file"])                   # a restricted turn keeps its own tool list
        self.assertNotIn("pdf-tools", [l for l, _ in system])

    def test_no_session_means_nothing_is_remembered(self):
        ps.arrange([("date", "D"), ("file-manual", "FM")], [tool("doc_create")], 7, None)
        system, _, tools = ps.arrange([("date", "D")], [tool("write_file")], 7, None)
        self.assertEqual(([l for l, _ in system], names(tools)), (["date"], ["write_file"]))

    def test_chats_do_not_share(self):
        ps.arrange([("date", "D"), ("file-manual", "FM")], [tool("doc_create")], 7, 1)
        system, _, _ = ps.arrange([("date", "D")], [tool("write_file")], 7, 2)
        self.assertEqual([l for l, _ in system], ["date"])

    def test_off_switch_changes_nothing(self):
        self._on.stop()
        with mock.patch.object(ps, "enabled", return_value=False):
            parts = [("date", "D"), ("kb", "hits")]
            self.assertEqual(ps.arrange(parts, [tool("a")], 7, 42), (parts, [], [tool("a")]))
        self._on.start()

    def test_turn_block_goes_to_the_end_of_the_user_message(self):
        msgs = [{"role": "user", "content": "first"}, {"role": "assistant", "content": "a"},
                {"role": "user", "content": "what is our refund policy?"}]
        ps.attach(msgs, ps.turn_block([("kb", "KB: refunds within 7 days")]))
        self.assertTrue(msgs[-1]["content"].startswith("what is our refund policy?"))
        self.assertIn("KB: refunds within 7 days", msgs[-1]["content"])
        self.assertEqual(msgs[0]["content"], "first")
        before = [dict(m) for m in msgs]
        ps.attach(msgs, ps.turn_block([]))
        self.assertEqual(msgs, before)


class ChatRouteTests(unittest.TestCase):
    """Three turns of one chat through the real chat route: the system message and the tool list of a later turn start
    with the earlier turn's, and the knowledge-base text of a turn sits in that turn's user message."""
    KB = "KB BLOCK: refunds are paid within 7 days"

    def setUp(self):
        ps.reset()

    def turn(self, messages, session_id=77, hits=True):
        import asyncio
        from types import SimpleNamespace
        from routes import chat as chat_route
        seen = {}

        async def fake_stream(client, msgs, tools=None, **kw):
            seen["msgs"] = [dict(m) for m in msgs]
            seen["tools"] = [t["function"]["name"] for t in (tools or [])]
            raise RuntimeError("stop after capturing the payload")
            yield  # pragma: no cover

        async def fake_fallback(client, fb, msgs, tools=None, *a, **kw):
            async for x in fake_stream(client, msgs, tools=tools):
                yield x

        lane = SimpleNamespace(model_id="cloud-x", display="cloud-x", provider_name="p")
        user = SimpleNamespace(id=1, is_super_admin=True, role_names=[])
        req = chat_route.ChatRunRequest(messages=messages, web_search=False, kb="auto", session_id=session_id)
        hit = ([{"title": "Policy", "cos": 0.9, "text": "KB TEXT"}], self.KB) if hits else ([], "")

        async def main():
            run = chat_route.run
            with (mock.patch.object(run.cloud, "cloud_lane", return_value=lane), mock.patch.object(run.cloud, "CloudClient"),
                  mock.patch.object(run.input_guard, "check_async", mock.AsyncMock(return_value=None)),
                  mock.patch.object(run, "allowed_source_ids_for", return_value={1}),
                  mock.patch("core.knowledge_router._get_active_source_titles", return_value={1: "Policy"}),
                  mock.patch("core.knowledge_router.fetch_company_knowledge", mock.AsyncMock(return_value=hit)),
                  mock.patch.object(run.state, "is_running", return_value=True), mock.patch.object(run, "audit_log"),
                  mock.patch.object(run, "monitor_begin", return_value=1), mock.patch.object(run, "monitor_end", create=True),
                  mock.patch.object(run, "db_record_request"), mock.patch.object(run, "_llm_chat_stream", fake_stream),
                  mock.patch.object(run.common, "_llm_chat_stream_with_fallback", fake_fallback)):
                resp = await chat_route.chat_run(req, user)
                try:
                    n = 0
                    async for _ in resp.body_iterator:
                        n += 1
                        if n > 20:
                            break
                except Exception:
                    pass
        asyncio.run(main())
        return seen

    def test_prefix_is_stable_across_turns(self):
        from core.small_model import APP_CONFIG
        saved = APP_CONFIG.get("knowledge")
        APP_CONFIG["knowledge"] = {"cloud_policy": "allow", "company_keywords": ["refund"]}
        try:
            h = [{"role": "user", "content": "hello there, how are you today?"}]
            t1 = self.turn(h, hits=False)
            h += [{"role": "assistant", "content": "Fine, thanks."}, {"role": "user", "content": "what is our company refund policy?"}]
            t2 = self.turn(h)
            h += [{"role": "assistant", "content": "Seven days."}, {"role": "user", "content": "thanks, and what day is it?"}]
            t3 = self.turn(h, hits=False)
        finally:
            if saved is None:
                APP_CONFIG.pop("knowledge", None)
            else:
                APP_CONFIG["knowledge"] = saved
        self.assertTrue(t1.get("msgs") and t2.get("msgs") and t3.get("msgs"), "the model was not called")
        sys1, sys2, sys3 = (t["msgs"][0]["content"] for t in (t1, t2, t3))
        self.assertEqual(t1["msgs"][0]["role"], "system")
        self.assertEqual(sys1, sys2)                                  # the KB hit of turn 2 did not touch the system message
        self.assertEqual(sys2, sys3)
        self.assertNotIn(self.KB, sys2)
        self.assertIn(self.KB, t2["msgs"][-1]["content"])             # it travels with the question it answers
        self.assertTrue(t2["msgs"][-1]["content"].startswith("what is our company refund policy?"))
        self.assertEqual(t1["tools"], t2["tools"])
        self.assertEqual(t2["tools"], t3["tools"])
        self.assertEqual(t3["msgs"][1]["content"], "hello there, how are you today?")    # history is sent as written

    def test_an_attached_sheet_keeps_the_file_tools(self):
        sheet = ("summarise this data set\n\n--- FILE: sales.xlsx ---\nData file: sales.xlsx - 1 sheet, 300 data rows\n"
                 "[Spreadsheet profile above: not every row is shown. For exact counts call data_query(file=\"sales.xlsx\").]\n"
                 "--- END sales.xlsx ---")
        seen = self.turn([{"role": "user", "content": sheet}], session_id=78, hits=False)
        self.assertIn("data_query", seen["tools"])
        self.assertIn("write_file", seen["tools"])
        # a later small-talk turn still has the data tools (the chat's attachment), in the same order
        later = self.turn([{"role": "user", "content": sheet}, {"role": "assistant", "content": "Done."},
                           {"role": "user", "content": "thanks"}], session_id=78, hits=False)
        self.assertEqual(later["tools"][:len(seen["tools"])], seen["tools"])


if __name__ == "__main__":
    unittest.main()
