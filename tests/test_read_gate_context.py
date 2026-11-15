"""A file only counts as 'read' while its text is still in the prompt the model sees (clearing, compaction, fresh view)."""
import asyncio
import json
import unittest
from unittest import mock

from core.agent_loop.clearing import clear_old_results
from core.agent_loop.compaction import compact_messages
from core.agent_loop.context_files import paths_in_view, sync_reads
from core.agent_tools import file_ops, file_state, verify_loop
from tests.test_file_not_found_hint import FOLDER, FakeCompanion

BIG = "line of file text\n" * 120                       # well over the 600-char floor for clearing


def call(i, name, **args):
    return {"role": "assistant", "content": "", "tool_calls": [
        {"id": f"c{i}", "type": "function", "function": {"name": name, "arguments": json.dumps(args)}}]}


def result(i, content):
    return {"role": "tool", "tool_call_id": f"c{i}", "content": content}


def history(*reads):
    msgs = [{"role": "system", "content": "sys"}, {"role": "user", "content": "build the report"}]
    for i, path in enumerate(reads):
        msgs += [call(i, "read_file", path=path), result(i, BIG)]
    return msgs


class Base(unittest.TestCase):
    def setUp(self):
        file_state.reset_session()
        self.addCleanup(file_state.reset_session)
        patch = mock.patch("core.agent_tools.require_device_workspace", lambda: (7, FOLDER))
        patch.start()
        self.addCleanup(patch.stop)

    def mark(self, *names):
        for n in names:
            file_state.mark_read(str(FOLDER / n))

    def was_read(self, name):
        return file_state.was_read(str(FOLDER / name))


class PathsInView(Base):
    def test_a_read_result_and_a_write_call_count_as_text_in_view(self):
        msgs = history("a.html") + [call(9, "write_file", path="b.html", content="<p>x</p>"), result(9, "wrote 8 chars")]
        self.assertEqual(paths_in_view(msgs), {"a.html", "b.html"})

    def test_a_cleared_read_result_does_not(self):
        msgs = history("a.html")
        msgs[-1] = result(0, "[cleared to save context: read_file(a.html), about 480 tokens omitted. Top of it:\n...]")
        self.assertEqual(paths_in_view(msgs), set())

    def test_a_digest_line_is_not_text(self):
        msgs = [{"role": "system", "content": "s"},
                {"role": "user", "content": "[CONVERSATION DIGEST ...]\ntool read_file(a.html): line of file text\n\nbuild"}]
        self.assertEqual(paths_in_view(msgs), set())


class RealRewrites(Base):
    def test_clearing_old_read_results_revokes_those_files_but_not_the_newest(self):
        names = [f"f{i}.html" for i in range(6)]
        self.mark(*names)
        msgs = history(*names)
        with mock.patch.dict("core.small_model.APP_CONFIG",
                             {"context": {"clear_trigger_tokens": 100, "clear_keep_results": 3, "clear_at_least_tokens": 10}}):
            freed = clear_old_results(msgs, prompt_tokens=10 ** 6)
        self.assertGreater(freed, 0)
        sync_reads(msgs)
        self.assertEqual([self.was_read(n) for n in names], [False, False, False, True, True, True])

    def test_a_file_the_model_wrote_stays_read_while_its_write_call_is_kept(self):
        self.mark("mine.html", "f0.html", "f1.html", "f2.html", "f3.html", "f4.html")
        msgs = [{"role": "system", "content": "sys"}, {"role": "user", "content": "go"},
                call(90, "write_file", path="mine.html", content="<html>all of it</html>"), result(90, "wrote 21 chars")]
        for i in range(5):
            msgs += [call(i, "read_file", path=f"f{i}.html"), result(i, BIG)]
        with mock.patch.dict("core.small_model.APP_CONFIG",
                             {"context": {"clear_trigger_tokens": 100, "clear_keep_results": 3, "clear_at_least_tokens": 10}}):
            clear_old_results(msgs, prompt_tokens=10 ** 6)
        sync_reads(msgs)
        self.assertTrue(self.was_read("mine.html"))                       # clearing keeps the assistant's call
        self.assertFalse(self.was_read("f0.html"))
        self.assertTrue(self.was_read("f4.html"))

    def test_compaction_into_a_digest_revokes_the_files_it_swallowed(self):
        names = [f"f{i}.html" for i in range(12)]
        self.mark(*names)
        msgs = history(*names)
        out = compact_messages(msgs, budget_tokens=2500)
        self.assertLess(len(out), len(msgs))                              # it really rewrote the history
        sync_reads(out)
        self.assertFalse(self.was_read("f0.html"))                        # only a digest line is left of it
        self.assertTrue(self.was_read("f11.html"))                        # the newest read is still verbatim

    def test_a_fresh_view_without_the_file_text_revokes_it(self):
        self.mark("old.html")
        sync_reads([{"role": "system", "content": "s"}, {"role": "user", "content": "try again"}])
        self.assertFalse(self.was_read("old.html"))


class EditGate(Base):
    def test_edit_after_the_text_was_dropped_asks_for_a_read_instead_of_failing_blind(self):
        fake = FakeCompanion({"page.html": "<p>real</p>\n"})
        file_state.mark_read(str(FOLDER / "page.html"))                  # e.g. written or read long ago
        sync_reads([{"role": "system", "content": "s"}, {"role": "user", "content": "continue"}])

        async def go():
            return await file_ops.tool_edit_file({"path": "page.html", "old_string": "<p>invented</p>", "new_string": "x"})

        with mock.patch.object(file_ops, "_cb", lambda: fake), mock.patch.object(verify_loop, "_cb", lambda: fake):
            out = asyncio.run(go())
        self.assertIn("read page.html before editing it", out)           # no 'old_string not found' round trip

    def test_with_the_text_in_view_the_edit_goes_straight_through(self):
        fake = FakeCompanion({"page.html": "<p>real</p>\n"})
        file_state.mark_read(str(FOLDER / "page.html"))
        view = history("page.html")
        sync_reads(view)

        async def go():
            return await file_ops.tool_edit_file({"path": "page.html", "old_string": "<p>real</p>", "new_string": "<p>new</p>"})

        with mock.patch.object(file_ops, "_cb", lambda: fake), mock.patch.object(verify_loop, "_cb", lambda: fake):
            out = asyncio.run(go())
        self.assertIn("edited", out)
        self.assertEqual(fake.files["page.html"], "<p>new</p>\n")


if __name__ == "__main__":
    unittest.main()
