"""One instruction per job for PDF / Word / deck requests in chat: the manual stops telling the model to write_file them when doc_create is on
offer, and a write_file of one is answered once with the exact doc_create call."""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core import prompt_scope
from core.agent_loop import doc_redirect


class FileManualTests(unittest.TestCase):
    def test_without_doc_tools_the_manual_is_unchanged(self):
        self.assertEqual(prompt_scope.file_manual(False), prompt_scope.FILE_MANUAL)

    def test_with_doc_tools_rule_3_points_at_doc_create(self):
        text = prompt_scope.file_manual(True)
        self.assertIn("do NOT use `write_file`", text)
        self.assertIn("doc_create(file='name.pdf'", text)
        self.assertNotIn("using `write_file(path='filename.pdf'", text)
        self.assertIn("see rule 3", text)
        # the rules around it survive: csv/xlsx wording, format choice, download tag, edit rule
        for keep in ("2. For Excel spreadsheets", "3b. CHOOSING THE FORMAT", "5. When you generate", "7. CHANGING AN EXISTING"):
            self.assertIn(keep, text)

    def test_stays_a_valid_manual_when_reworded(self):
        saved = prompt_scope.FILE_MANUAL
        try:
            prompt_scope.FILE_MANUAL = saved.replace("3. For PDF files", "3. Something else")
            self.assertEqual(prompt_scope.file_manual(True), prompt_scope.FILE_MANUAL)    # cannot find rule 3: leave it whole
        finally:
            prompt_scope.FILE_MANUAL = saved


class WriteRedirectTests(unittest.TestCase):
    OFFERED = {"write_file", "doc_create", "doc_append"}

    def test_first_pdf_write_is_redirected_once(self):
        already = set()
        msg = doc_redirect.write_note("write_file", {"path": "checklist.pdf", "content": "x"}, self.OFFERED, already)
        self.assertIn("doc_create(file='checklist.pdf'", msg)
        self.assertIn("doc_append", msg)
        self.assertTrue(msg.startswith("error:"))
        self.assertIsNone(doc_redirect.write_note("write_file", {"path": "checklist.pdf"}, self.OFFERED, already))

    def test_docx_and_pptx_are_covered_others_are_not(self):
        for name in ("a.docx", "A.PPTX"):
            self.assertIsNotNone(doc_redirect.write_note("write_file", {"path": name}, self.OFFERED, set()), name)
        for name in ("data.csv", "notes.md", "rows.xlsx", "page.html", ""):
            self.assertIsNone(doc_redirect.write_note("write_file", {"path": name}, self.OFFERED, set()), name)

    def test_not_offered_or_other_tool_is_never_redirected(self):
        self.assertIsNone(doc_redirect.write_note("write_file", {"path": "a.pdf"}, {"write_file"}, set()))
        self.assertIsNone(doc_redirect.write_note("doc_create", {"path": "a.pdf"}, self.OFFERED, set()))
        self.assertIsNone(doc_redirect.write_note("write_file", None, self.OFFERED, set()))

    def test_quotes_in_a_name_cannot_break_the_example_call(self):
        msg = doc_redirect.write_note("write_file", {"path": "it's.pdf"}, self.OFFERED, set())
        self.assertIn("doc_create(file='its.pdf'", msg)


class ChatRouteBuildTests(unittest.TestCase):
    """The chat route in the build phase (the user has answered our PDF card), with a scripted model."""
    ASK = "Create a PDF compliance checklist mapping PCI DSS requirement areas to controls and owners."
    ANSWERS = "How long should the PDF be?: Short (4-5 sections)\nWho is it for?: Technical team\nStyle?: Formal report"

    def _run(self, replies, force=False):
        import asyncio
        import json
        from types import SimpleNamespace
        from unittest import mock
        from routes import chat as chat_route
        lane = SimpleNamespace(model_id="cloud-x", display="cloud-x", provider_name="p")
        calls = []

        async def fake_stream(client, msgs, **kw):
            calls.append({"msgs": json.loads(json.dumps(msgs)), "tools": [t.get("function", {}).get("name") for t in (kw.get("tools") or [])],
                          "tool_choice": kw.get("tool_choice")})
            if len(calls) > len(replies):
                raise RuntimeError("script ended")
            r = replies[len(calls) - 1]
            if r.get("content"):
                yield "content_delta", r["content"]
            yield "result", {"content": r.get("content", ""), "tool_calls": r.get("tool_calls", []), "finish_reason": "stop"}

        user = SimpleNamespace(id=1, is_super_admin=True, role_names=[])
        from core import prompt_scope
        history = [{"role": "user", "content": self.ASK}, {"role": "assistant", "content": prompt_scope.brief_card("pdf")}]
        req = chat_route.ChatRunRequest(messages=history + [{"role": "user", "content": self.ANSWERS}], web_search=False, kb="off")

        from core.small_model import APP_CONFIG

        async def main():
            with (mock.patch.object(chat_route.run.cloud, "cloud_lane", return_value=lane),
                  mock.patch.object(chat_route.run.cloud, "CloudClient"),
                  mock.patch.object(chat_route.run.input_guard, "check_async", mock.AsyncMock(return_value=None)),
                  mock.patch.object(chat_route.run, "allowed_source_ids_for", return_value={1}),
                  mock.patch.object(chat_route.run.state, "is_running", return_value=True),
                  mock.patch.object(chat_route.run, "audit_log"),
                  mock.patch.object(chat_route.run, "monitor_begin", return_value=1),
                  mock.patch.object(chat_route.run, "monitor_end", create=True),
                  mock.patch.object(chat_route.run, "db_record_request"),
                  mock.patch.dict(APP_CONFIG.setdefault("chat", {}), {"force_doc_tool": force}),
                  mock.patch.object(chat_route.run, "_llm_chat_stream", fake_stream)):
                resp = await chat_route.chat_run(req, user)
                try:
                    async for _ in resp.body_iterator:
                        pass
                except Exception:
                    pass

        asyncio.run(main())
        return calls

    @staticmethod
    def _text(call):
        return "\n".join(str(m.get("content") or "") for m in call["msgs"])

    def test_the_build_phase_manual_names_doc_create_not_write_file(self):
        calls = self._run([{"content": "ok"}])
        self.assertTrue(calls, "the model was never called")
        self.assertIn("doc_create", calls[0]["tools"])
        self.assertIn("do NOT use `write_file`", self._text(calls[0]))
        self.assertNotIn("using `write_file(path='filename.pdf'", self._text(calls[0]))

    def test_a_card_asked_again_is_nudged_to_build(self):
        card = "```questionnaire\n" + '{"title":"again","questions":[]}' + "\n```"
        calls = self._run([{"content": card}, {"content": "ok"}])
        self.assertGreaterEqual(len(calls), 2, "no second turn: the re-asked card was accepted as the answer")
        self.assertIn("Call doc_create now", self._text(calls[1]))

    def test_a_text_only_reply_in_the_build_phase_is_nudged_too(self):
        calls = self._run([{"content": "Here is the checklist:\n```\n| a | b |\n```"}, {"content": "ok"}])
        self.assertGreaterEqual(len(calls), 2)
        self.assertIn("Call doc_create now", self._text(calls[1]))

    def test_a_tool_call_is_required_on_the_first_build_turn_only_when_the_flag_is_on(self):
        off = self._run([{"content": "ok"}])
        self.assertIsNone(off[0]["tool_choice"])
        on = self._run([{"content": "ok"}], force=True)
        self.assertEqual(on[0]["tool_choice"], "required")
        self.assertFalse({"web_search", "web_fetch"} & set(on[0]["tools"]), "research tools must not be on offer when a call is forced")

    def test_after_a_tool_result_the_model_chooses_again(self):
        import json
        call = {"id": "c1", "type": "function", "function": {"name": "doc_create",
                                                             "arguments": json.dumps({"file": "a.pdf", "content": "# T", "total": 4})}}
        calls = self._run([{"content": "", "tool_calls": [call]}, {"content": "ok"}], force=True)
        self.assertEqual(calls[0]["tool_choice"], "required")
        if len(calls) > 1:
            self.assertIsNone(calls[1]["tool_choice"])

    def test_write_file_of_a_pdf_gets_the_doc_create_instruction(self):
        import json
        call = {"id": "c1", "type": "function", "function": {"name": "write_file",
                                                             "arguments": json.dumps({"path": "checklist.pdf", "content": "# T"})}}
        calls = self._run([{"content": "", "tool_calls": [call]}, {"content": "ok"}])
        self.assertGreaterEqual(len(calls), 2)
        self.assertIn("A PDF file is built with doc_create", self._text(calls[1]))


if __name__ == "__main__":
    unittest.main()
