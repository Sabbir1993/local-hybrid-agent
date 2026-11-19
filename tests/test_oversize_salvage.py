"""A write cut at the size cap keeps its complete part, the history never shows the model a look-alike or empty call,
and the same oversized mistake on one file is caught on the second try (the naafco/build_report.py loop: 7+ failed
append_file calls of ~11k tokens each, all thrown away)."""
import ast
import asyncio
import unittest
from unittest import mock

from core.agent_loop import loop_guard
from core.agent_loop.history_args import slim_write_args, stub_content_error
from core.agent_loop.repair import validate_and_repair_tool_args
from core.agent_tools import file_ops, file_state, limits, salvage, verify_loop
from tests.test_file_not_found_hint import FOLDER, FakeCompanion

HEAD = "import os\n\n\ndef first():\n    return 1\n"
FUNCS = "".join(f"\n\ndef f{i}(x):\n    y = x + {i}\n    return y * 2\n" for i in range(6))
CUT_MID_FUNC = FUNCS + "\n\ndef f_last(x):\n    if x:\n        return [\n            1,\n"   # cut inside f_last


class Base(unittest.TestCase):
    def setUp(self):
        file_state.reset_session()
        self.addCleanup(file_state.reset_session)
        file_ops._SYNTAX_FAILS.clear()
        self.fake = FakeCompanion({})

    def go(self, coro_fn):
        with mock.patch("core.agent_tools.require_device_workspace", lambda: (7, FOLDER)), \
                mock.patch.object(file_ops, "_cb", lambda: self.fake), \
                mock.patch.object(verify_loop, "_cb", lambda: self.fake):
            return asyncio.run(coro_fn())


class SalvagePrefix(unittest.TestCase):
    def test_python_keeps_every_complete_function_and_still_parses(self):
        pre = salvage.salvage_prefix(HEAD, CUT_MID_FUNC, "r.py")
        self.assertIn("def f5", pre)
        self.assertNotIn("f_last", pre)
        ast.parse(HEAD + pre)

    def test_python_cut_inside_the_only_block_saves_nothing(self):
        self.assertEqual(salvage.salvage_prefix(HEAD, "\n\ndef big(x):\n    return [\n        1,\n        2,\n", "r.py"), "")

    def test_unparsed_code_is_cut_between_blocks(self):
        php = "function a() {\n  return 1;\n}\n\nfunction b() {\n  return 2;\n}\n\nfunction c() {\n  if (x) {\n"
        pre = salvage.salvage_prefix("<?php\n", php, "a.php")
        self.assertTrue(pre.endswith("}\n\n"))
        self.assertNotIn("function c", pre)

    def test_plain_text_keeps_the_last_complete_line(self):
        self.assertEqual(salvage.salvage_prefix("# T\n", "a\nb\nc\nd\nhalf", "n.md"), "a\nb\nc\nd\n")


class AppendSalvage(Base):
    def test_cut_python_append_saves_the_complete_part(self):
        self.fake.files["build_report.py"] = HEAD

        async def run():
            return await file_ops.tool_append_file({"path": "build_report.py", "content": "",
                                                    "_oversize_tokens": 9000, "_partial": CUT_MID_FUNC})

        out = self.go(run)
        self.assertTrue(out.startswith("partly saved"), out)
        self.assertIn("return y * 2", out)                      # the last saved line, so the model knows where to go on
        ast.parse(self.fake.files["build_report.py"])
        self.assertIn("def f5", self.fake.files["build_report.py"])

    def test_nothing_safe_to_save_gives_the_size_error_and_leaves_the_file(self):
        self.fake.files["build_report.py"] = HEAD

        async def run():
            return await file_ops.tool_append_file({"path": "build_report.py", "content": "", "_oversize_tokens": 9000,
                                                    "_partial": "\n\ndef big(x):\n    return [\n        1,\n"})

        out = self.go(run)
        self.assertIn("one call may carry at most", out)
        self.assertEqual(self.fake.files["build_report.py"], HEAD)

    def test_markdown_is_still_salvaged(self):
        self.fake.files["notes.md"] = "# Notes\n"

        async def run():
            return await file_ops.tool_append_file({"path": "notes.md", "content": "", "_oversize_tokens": 9000,
                                                    "_partial": "one\ntwo\nthree\nfour\nfive\nsix\nhal"})

        self.assertTrue(self.go(run).startswith("partly saved"))
        self.assertEqual(self.fake.files["notes.md"], "# Notes\none\ntwo\nthree\nfour\nfive\nsix\n")

    def test_append_syntax_failures_end_with_stop(self):
        self.fake.files["a.py"] = HEAD

        async def run():
            return [await file_ops.tool_append_file({"path": "a.py", "content": f"def broken{i}(:\n"}) for i in range(n)]

        n = limits.agent_limit("verify_max_retries")
        outs = self.go(run)
        self.assertIn("pre-save syntax validation failed", outs[0])
        self.assertIn("Stop writing it", outs[-1])


class WriteFileSalvage(Base):
    def test_cut_write_of_a_new_file_keeps_the_valid_part(self):
        async def run():
            return await file_ops.tool_write_file({"path": "new.py", "content": "", "_oversize_tokens": 9000,
                                                   "_partial": HEAD + CUT_MID_FUNC})

        out = self.go(run)
        self.assertTrue(out.startswith("partly saved"), out)
        ast.parse(self.fake.files["new.py"])

    def test_cut_write_over_an_existing_file_never_touches_it(self):
        self.fake.files["old.py"] = HEAD

        async def run():
            return await file_ops.tool_write_file({"path": "old.py", "content": "", "_oversize_tokens": 9000,
                                                   "_partial": HEAD + CUT_MID_FUNC})

        self.assertIn("do NOT rewrite or restart", self.go(run))
        self.assertEqual(self.fake.files["old.py"], HEAD)


class WriteCap(unittest.TestCase):
    def test_cap_is_twice_the_chunk_on_main_even_with_a_10k_setting(self):
        cfg = {"agent": {"write_file_max_tokens": 10000, "chunk_target_tokens": 3000, "main_max_tokens": 12000}}
        with mock.patch.dict(limits.APP_CONFIG, cfg):
            self.assertEqual(limits.write_cap_tokens("main"), 6000)
            self.assertEqual(limits.write_cap_tokens("executor"), 4096)     # chunk = 4096 // 2

    def test_a_cut_call_is_always_refused_even_under_the_cap(self):
        self.assertIn("one call may carry at most", file_ops._too_big("", "append_file", {"_oversize_tokens": 10}))


class HistoryArgs(unittest.TestCase):
    def test_saved_call_drops_the_text_and_says_so(self):
        h = slim_write_args({"path": "a.py", "content": "x" * 5800})
        self.assertNotIn("content", h)
        self.assertIn("5800 chars", h["content_omitted"])

    def test_cut_call_has_no_empty_content_and_no_partial(self):
        h = slim_write_args({"path": "a.py", "content": "", "_oversize_tokens": 9000, "_partial": "x" * 30000})
        self.assertEqual(set(h), {"path", "content_note"})

    def test_small_calls_are_kept_as_sent(self):
        args = {"path": "a.py", "content": "print(1)\n"}
        self.assertEqual(slim_write_args(args), args)

    def test_copied_stubs_are_rejected_before_anything_is_written(self):
        for tool, args in (("append_file", {"path": "a.py", "content": "<5800 chars written to naafco/build_report.py>"}),
                           ("append_file", {"path": "n.md", "content": "<5800 chars written to n.md>"}),
                           ("edit_file", {"path": "a.py", "old_string": "x", "new_string": "<900 chars replaced in a.py>"}),
                           ("append_file", {"path": "a.py", "content_omitted": "5800 chars - already saved in the file"}),
                           ("append_file", {"path": "a.py", "content": "5800 chars - already saved in the file, not shown again"})):
            _, err = validate_and_repair_tool_args(tool, args)
            self.assertIsNotNone(err, args)
            self.assertIn("history", err)

    def test_real_content_and_secret_placeholders_pass(self):
        self.assertIsNone(stub_content_error("append_file", {"content": "API_KEY = \"[PLACEHOLDER]\"\n"}))
        self.assertIsNone(stub_content_error("append_file", {"content": "def f():\n    return '<b>'\n"}))


class LoopGuardOversize(unittest.TestCase):
    def test_second_oversize_on_the_same_file_nudges_at_once(self):
        g = loop_guard.LoopGuard()
        err = "error: this append_file content is about 9000 tokens; one call may carry at most 6000. The file ..."
        g.record("append_file", {"content": "", "_partial": "a" * 9000, "_oversize_tokens": 9000,
                                 "path": "naafco/build_report.py"}, err, False)
        self.assertEqual(g.check().level, loop_guard.OK)
        g.record("append_file", {"content": "", "_partial": "b" * 9500, "_oversize_tokens": 9500,
                                 "path": "naafco/build_report.py"}, err.replace("9000", "9500"), False)
        self.assertEqual(g.check().level, loop_guard.NUDGE)


if __name__ == "__main__":
    unittest.main()
