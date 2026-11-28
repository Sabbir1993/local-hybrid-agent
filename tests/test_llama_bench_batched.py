"""llama-batched-bench command line, output parsing (jsonl and md) and failure handling.

The fixtures follow the output format of build b11429 (commit d81235049): the jsonl line
and markdown table layouts are the ones that build's batched-bench prints. The numbers in
them are illustrative, not a measurement.
"""

import unittest
from pathlib import Path
from unittest import mock

from core.llama_bench import (build_batched_cmd, format_batched_table, main,
                              parse_batched_markdown, parse_batched_output, run_batched_bench)

FIX = Path(__file__).parent / "fixtures"
JSONL = (FIX / "llama_batched_bench_b11429.jsonl.txt").read_text(encoding="utf-8")
MD = (FIX / "llama_batched_bench_b11429.md.txt").read_text(encoding="utf-8")


class Parsing(unittest.TestCase):
    def test_jsonl_rows_and_per_user_math(self):
        res = parse_batched_output(JSONL)
        self.assertTrue(res["ok"])
        self.assertEqual(res["format"], "jsonl")
        rows = res["rows"]
        self.assertEqual([r["npl"] for r in rows], [1, 4, 8, 15])
        last = rows[-1]
        self.assertEqual((last["npp"], last["ntg"], last["n_kv"]), (512, 128, 9600))
        self.assertAlmostEqual(last["prefill_tps"], 1228.8)
        self.assertAlmostEqual(last["decode_tps_total"], 150.0)
        self.assertAlmostEqual(last["decode_tps_per_user"], 10.0)
        self.assertAlmostEqual(rows[1]["decode_tps_per_user"], 20.0)
        self.assertIs(last["prompt_shared"], False)

    def test_markdown_table_matches_jsonl(self):
        res = parse_batched_output(MD)
        self.assertTrue(res["ok"])
        self.assertEqual(res["format"], "md")
        md_rows = res["rows"]
        js_rows = parse_batched_output(JSONL)["rows"]
        self.assertEqual(len(md_rows), 4)
        for a, b in zip(md_rows, js_rows):
            self.assertEqual(a["npl"], b["npl"])
            self.assertAlmostEqual(a["decode_tps_total"], b["decode_tps_total"], places=1)
            self.assertAlmostEqual(a["decode_tps_per_user"], b["decode_tps_per_user"], places=1)
            self.assertAlmostEqual(a["prefill_tps"], b["prefill_tps"], places=1)

    def test_garbage_is_not_ok(self):
        self.assertEqual(parse_batched_output("loading...\nsegfault\n"),
                         {"ok": False, "reason": "no_recognizable_rows"})
        self.assertEqual(parse_batched_markdown("| a | b |\n|---|---|\n| 1 | 2 |"), [])

    def test_table_renders(self):
        text = format_batched_table(parse_batched_output(JSONL)["rows"])
        self.assertIn("per user", text)
        self.assertEqual(len(text.splitlines()), 6)


class Command(unittest.TestCase):
    def test_flags(self):
        cmd = build_batched_cmd(Path("/rt/llama-batched-bench"), "/m.gguf", npl=[1, 4, 8, 15],
                                npp=[512], ntg=[128], ctx=131072, ngl=999,
                                device_ids=["Vulkan0", "Vulkan1"], split_mode="layer",
                                tensor_split="1/1", cache_type_k="q8_0", cache_type_v="q8_0",
                                flash_attn="on", kv_unified=True,
                                extra_args=["--kv-unified-per-slot", "131072"])
        val = lambda f: cmd[cmd.index(f) + 1]
        self.assertEqual(val("-npl"), "1,4,8,15")
        self.assertEqual(val("-npp"), "512")
        self.assertEqual(val("-ntg"), "128")
        self.assertEqual(val("-c"), "131072")
        self.assertEqual(val("-dev"), "Vulkan0,Vulkan1")
        self.assertEqual(val("-sm"), "layer")
        self.assertEqual(val("--tensor-split"), "1,1")      # common parser: comma form
        self.assertEqual(val("-ctk"), "q8_0")
        self.assertEqual(val("-ctv"), "q8_0")
        self.assertEqual(val("-fa"), "on")
        self.assertEqual(val("--kv-unified-per-slot"), "131072")
        self.assertEqual(val("--output-format"), "jsonl")
        self.assertIn("-kvu", cmd)
        self.assertNotIn("-pps", cmd)


def _proc(rc=0, out="", err=""):
    return mock.Mock(returncode=rc, stdout=out, stderr=err)


class Run(unittest.TestCase):
    def test_ok_and_auto_context(self):
        with mock.patch("core.llama_bench.subprocess.run", return_value=_proc(out=JSONL)) as run:
            res = run_batched_bench(Path("/rt/b"), "/m.gguf", npl=[1, 15], npp=[512], ntg=[128])
        self.assertTrue(res["ok"])
        self.assertEqual(res["ctx"], 15 * (512 + 128))
        cmd = run.call_args.args[0]
        self.assertEqual(cmd[cmd.index("-c") + 1], str(15 * 640))

    def test_oom_marker_wins_over_exit_code(self):
        with mock.patch("core.llama_bench.subprocess.run",
                        return_value=_proc(rc=1, err="ggml_vulkan: Device memory allocation of size 1 failed\n"
                                                     "vk::OutOfDeviceMemoryError")):
            res = run_batched_bench(Path("/rt/b"), "/m.gguf")
        self.assertEqual(res["reason"], "oom")

    def test_crash(self):
        with mock.patch("core.llama_bench.subprocess.run", return_value=_proc(rc=3, err="boom")):
            self.assertEqual(run_batched_bench(Path("/rt/b"), "/m.gguf")["reason"], "crash")

    def test_old_build_without_output_format_falls_back_to_markdown(self):
        old = _proc(rc=1, err="error: invalid argument: --output-format\nusage: ...")
        with mock.patch("core.llama_bench.subprocess.run", side_effect=[old, _proc(out=MD)]) as run:
            res = run_batched_bench(Path("/rt/b"), "/m.gguf")
        self.assertTrue(res["ok"])
        self.assertEqual(res["format"], "md")
        self.assertNotIn("--output-format", run.call_args_list[1].args[0])

    def test_timeout(self):
        import subprocess
        with mock.patch("core.llama_bench.subprocess.run",
                        side_effect=subprocess.TimeoutExpired("b", 1)):
            self.assertEqual(run_batched_bench(Path("/rt/b"), "/m.gguf")["reason"], "timeout")


class Cli(unittest.TestCase):
    def test_help_exits_zero(self):
        with self.assertRaises(SystemExit) as cm, mock.patch("sys.stdout"):
            main(["--help"])
        self.assertEqual(cm.exception.code, 0)


if __name__ == "__main__":
    unittest.main()
