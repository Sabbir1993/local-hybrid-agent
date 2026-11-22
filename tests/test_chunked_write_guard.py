"""Building a file in chunks must not loop back to the skeleton: restarting a half-built file is refused, and the
oversize errors for an existing file say 'send the next section', never 'write a skeleton first'."""
import asyncio
import unittest
from unittest import mock

from core import request_context as rc
from core.agent_loop import execution
from core.agent_tools import file_ops, file_state, verify_loop
from core.agent_tools.limits import agent_limit, write_cap_tokens
from tests.test_file_not_found_hint import FOLDER, FakeCompanion

OVER = agent_limit("write_file_max_tokens") + 1000     # past the configured cap, whatever this machine's config says

SKELETON = ('<!DOCTYPE html>\n<html>\n<head><meta charset="UTF-8"><title>System Report</title></head>\n'
            '<body>\n<h1>System Health Report</h1>\n</body>\n</html>\n')
CSS = "<style>\n" + "".join(f".c{i} {{ color: #{i:06x}; margin: {i}px; }}\n" for i in range(60)) + "</style>\n</head>"


class Base(unittest.TestCase):
    def setUp(self):
        file_state.reset_session()
        self.addCleanup(file_state.reset_session)
        self.fake = FakeCompanion({})

    def go(self, coro_fn):
        with mock.patch("core.agent_tools.require_device_workspace", lambda: (7, FOLDER)), \
                mock.patch.object(file_ops, "_cb", lambda: self.fake), \
                mock.patch.object(verify_loop, "_cb", lambda: self.fake):
            return asyncio.run(coro_fn())


class OversizeMessages(Base):
    def test_append_and_edit_oversize_never_tell_the_model_to_write_a_skeleton(self):
        for tool in ("append_file", "edit_file"):
            out = file_ops._too_big("x" * (OVER * 4), tool)
            self.assertIn("do NOT rewrite or restart", out)
            self.assertNotIn("skeleton", out)

    def test_write_file_oversize_on_a_new_file_still_says_skeleton(self):
        self.assertIn("skeleton", file_ops._too_big("x" * (OVER * 4), "write_file"))

    def test_write_file_oversize_over_an_existing_file_says_continue_not_restart(self):
        self.fake.files["a.html"] = SKELETON

        async def run():
            return await file_ops.tool_write_file({"path": "a.html", "content": "", "_oversize_tokens": OVER})

        out = self.go(run)
        self.assertIn("do NOT rewrite or restart", out)

    def test_write_file_oversize_for_a_new_file_keeps_the_skeleton_advice(self):
        async def run():
            return await file_ops.tool_write_file({"path": "new.html", "content": "", "_oversize_tokens": OVER})

        self.assertIn("skeleton", self.go(run))


class CompleteOversizeSalvage(Base):
    """A write that arrives complete but past the cap used to be a hard refusal; the model then
    regenerated the same giant file and looped. Cut writes already keep their prefix - do the same here."""

    def test_complete_oversized_new_file_saves_a_prefix_and_says_continue(self):
        from core.agent_tools.limits import CHARS_PER_TOKEN
        need = int(write_cap_tokens() * CHARS_PER_TOKEN * 1.4) + 500   # past the cap, under MAX_EDIT_BYTES
        body = ""
        while len(body) < need:
            body += f"line {len(body)}: the quick brown fox jumps over the lazy dog\n"

        async def run():
            return await file_ops.tool_write_file({"path": "big.txt", "content": body})

        out = self.go(run)
        self.assertIn("partly saved", out)
        self.assertIn("append_file", out)
        self.assertIn("big.txt", self.fake.files)
        self.assertLess(len(self.fake.files["big.txt"]), len(body))
        self.assertGreater(len(self.fake.files["big.txt"]), 100)

    def test_complete_oversized_append_saves_a_prefix_and_continues(self):
        from core.agent_tools.limits import CHARS_PER_TOKEN
        need = int(write_cap_tokens() * CHARS_PER_TOKEN * 1.4) + 500
        body = ("line: the quick brown fox jumps over the lazy dog\n" * (need // 50 + 1))
        self.fake.files["notes.txt"] = "existing header\n"

        async def run():
            return await file_ops.tool_append_file({"path": "notes.txt", "content": body})

        out = self.go(run)
        self.assertIn("partly saved", out)
        saved = self.fake.files["notes.txt"]
        self.assertTrue(saved.startswith("existing header\n"))
        self.assertLess(len(saved), len("existing header\n") + len(body))

    def test_oversized_existing_file_is_still_refused_without_recovery(self):
        """Never truncate over work the agent already wrote: that path says 'send the next section'."""
        self.fake.files["big.txt"] = "already here\n"

        async def run():
            # overwrite=true gets past the "already exists" guard to the size check
            return await file_ops.tool_write_file({"path": "big.txt", "content": "x" * (OVER * 4),
                                                   "overwrite": True})

        out = self.go(run)
        self.assertIn("do NOT rewrite or restart", out)
        self.assertEqual(self.fake.files["big.txt"], "already here\n")


class RestartIsRefused(Base):
    def _loop(self, overwrite_args):
        async def run():
            await file_ops.tool_write_file({"path": "system_report.html", "content": SKELETON})
            await file_ops.tool_edit_file({"path": "system_report.html", "old_string": "</head>", "new_string": CSS})
            built = self.fake.files["system_report.html"]
            out = await file_ops.tool_write_file({"path": "system_report.html", "content": SKELETON, **overwrite_args})
            return built, out

        return self.go(run)

    def test_overwriting_a_built_up_file_with_its_skeleton_is_refused(self):
        built, out = self._loop({"overwrite": True})
        self.assertGreater(len(built), 600)
        self.assertIn("deletes the sections you already added", out)
        self.assertIn("append_file", out)
        self.assertEqual(self.fake.files["system_report.html"], built)          # nothing was lost

    def test_the_shorter_file_is_possible_when_it_is_deliberate(self):
        _, out = self._loop({"overwrite": True, "shrink_ok": True})
        self.assertEqual(self.fake.files["system_report.html"], SKELETON)
        self.assertIn("overwrote", out)

    def test_a_small_file_can_still_be_rewritten(self):
        async def run():
            await file_ops.tool_write_file({"path": "s.html", "content": SKELETON})
            return await file_ops.tool_write_file({"path": "s.html", "content": "<p>x</p>", "overwrite": True})

        self.assertIn("overwrote", self.go(run))                                # nothing worth keeping was lost

    def test_a_rewrite_that_is_not_much_smaller_is_allowed(self):
        async def run():
            await file_ops.tool_write_file({"path": "s.html", "content": SKELETON})
            await file_ops.tool_edit_file({"path": "s.html", "old_string": "</head>", "new_string": CSS})
            bigger = self.fake.files["s.html"] + "<p>more</p>\n"
            return bigger, await file_ops.tool_write_file({"path": "s.html", "content": bigger, "overwrite": True})

        bigger, out = self.go(run)
        self.assertIn("overwrote", out)

    def test_the_guard_is_for_files_the_agent_built_not_for_other_files(self):
        self.fake.files["old.html"] = "x" * 2000                                # exists on disk, not written by this agent

        async def run():
            return await file_ops.tool_write_file({"path": "old.html", "content": "short", "overwrite": True})

        self.assertIn("overwrote", self.go(run))                                # Agent Task mode rules, as before


class PersonalAgentLoop(Base):
    def test_the_screenshot_loop_keeps_the_built_file(self):
        """skeleton -> add css -> restart with the skeleton: the css survives and the model is told how to continue."""
        tok = rc.set_personal_scope(True)
        self.addCleanup(lambda: rc._personal_scope.reset(tok))

        async def run():
            await execution.run_tool("write_file", {"path": "system_report.html", "content": SKELETON})
            await execution.run_tool("edit_file", {"path": "system_report.html", "old_string": "</head>", "new_string": CSS})
            return await execution.run_tool("write_file", {"path": "system_report.html", "content": SKELETON,
                                                           "overwrite": True})

        out = self.go(run)
        self.assertIn("deletes the sections you already added", out)
        self.assertIn(".c59", self.fake.files["system_report.html"])
        self.assertEqual(list(self.fake.files), ["system_report.html"])


class CutOffAndChunkSize(unittest.TestCase):
    def test_the_cut_off_notices_never_send_a_model_back_to_a_skeleton_for_an_existing_file(self):
        from core.agent_loop import truncation
        for text in (truncation.CUT_CALL_NOTICE, truncation.INVALID_ARGS_ERROR):
            self.assertIn("already exists", text)
            self.assertIn("do NOT recreate or rewrite", text)
            self.assertIn("append_file", text)
            self.assertIn("skeleton", text)                       # still offered, but only for a new file
            self.assertLess(text.index("already exists"), text.index("skeleton"))
        self.assertTrue(truncation.CUT_CALL_NOTICE.startswith("[cut off]"))   # executor_view keys on this prefix

    def _chunk(self, **cfg):
        from core.agent_tools import limits
        with mock.patch.dict("core.small_model.APP_CONFIG", {"agent": cfg}):
            return limits.effective_chunk_tokens()

    def test_a_chunk_target_the_executor_lane_cannot_generate_is_clamped(self):
        self.assertEqual(self._chunk(executor_max_tokens=4096, chunk_target_tokens=8000), 2048)

    def test_a_target_that_already_fits_is_kept(self):
        self.assertEqual(self._chunk(executor_max_tokens=4096, chunk_target_tokens=1500), 1500)
        self.assertEqual(self._chunk(executor_max_tokens=12000, chunk_target_tokens=3000), 3000)

    def test_the_main_lane_is_not_clamped_to_the_executor_cap(self):
        from core.agent_tools import limits
        cfg = {"executor_max_tokens": 4096, "main_max_tokens": 32000, "chunk_target_tokens": 8000}
        with mock.patch.dict("core.small_model.APP_CONFIG", {"agent": cfg}):
            self.assertEqual(limits.effective_chunk_tokens("main"), 8000)       # a 32000-token step easily carries it
            self.assertEqual(limits.effective_chunk_tokens("executor"), 2048)   # the 9B lane cannot

    def test_the_running_lane_decides_what_the_tools_quote(self):
        cfg = {"executor_max_tokens": 4096, "main_max_tokens": 32000, "chunk_target_tokens": 8000,
               "write_file_max_tokens": 500}
        tok = rc.set_current_lane("main")
        try:
            with mock.patch.dict("core.small_model.APP_CONFIG", {"agent": cfg}):
                main_msg = file_ops._too_big("x" * 20000, "append_file")
            rc._current_lane.reset(tok)
            tok = rc.set_current_lane("executor")
            with mock.patch.dict("core.small_model.APP_CONFIG", {"agent": cfg}):
                exec_msg = file_ops._too_big("x" * 20000, "append_file")
        finally:
            rc._current_lane.reset(tok)
        self.assertIn("8000 tokens", main_msg)
        self.assertIn("2048 tokens", exec_msg)

    def test_the_too_big_messages_quote_the_clamped_size(self):
        with mock.patch.dict("core.small_model.APP_CONFIG",
                             {"agent": {"executor_max_tokens": 4096, "chunk_target_tokens": 8000,
                                        "write_file_max_tokens": 500}}):
            out = file_ops._too_big("x" * 20000, "append_file")
            new = file_ops._too_big("x" * 20000, "write_file")
        self.assertIn("2048 tokens", out)
        self.assertNotIn("8000", out)
        self.assertIn("2048 tokens", new)


if __name__ == "__main__":
    unittest.main()
