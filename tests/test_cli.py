"""tests/test_cli.py - the terminal client (scripts/a770.py).

Pure rendering and approval logic first, then one real run over HTTP against the hermetic mock app.

Run: python -m unittest tests.test_cli -v
"""

import io
import json
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import a770 as cli  # noqa: E402


def sse(*events):
    lines = []
    for i, (name, data) in enumerate(events):
        lines += [f"id: {i}", f"event: {name}", f"data: {json.dumps(data)}", ""]
    return lines


class Sanitising(unittest.TestCase):
    def test_escape_sequences_and_control_characters_never_reach_the_terminal(self):
        evil = "ok\x1b[2J\x1b[31mred\x1b]52;c;QUJD\x07 done\x08\x00\r\nnext"
        out = cli.clean(evil)
        self.assertNotIn("\x1b", out)
        self.assertNotIn("\x07", out)
        self.assertNotIn("\x00", out)
        self.assertIn("red", out)
        self.assertIn("next", out)

    def test_one_line_collapses_and_truncates(self):
        self.assertEqual(cli.one_line("a\n\n  b\t c"), "a b c")
        self.assertTrue(cli.one_line("x" * 500, 50).endswith("…"))
        self.assertEqual(len(cli.one_line("x" * 500, 50)), 50)

    def test_args_are_summarised_and_content_is_measured_not_printed(self):
        s = cli.describe_args({"path": "src/a.py", "content": "z" * 5000, "n": 3, "opts": {"a": 1}})
        self.assertIn("path='src/a.py'", s)
        self.assertIn("content=<5000 chars>", s)
        self.assertNotIn("zzzz", s)
        self.assertIn("opts=<dict>", s)


class Parsing(unittest.TestCase):
    def test_frames_with_ids_comments_and_garbage(self):
        lines = [": ping", "id: 3", "event: delta", 'data: {"text": "hi"}', "", "event: x", "data: not json", "",
                 "event: done", 'data: {"state": "completed"}']
        got = list(cli.parse_sse(lines))
        self.assertEqual(got[0], (3, "delta", {"text": "hi"}))
        self.assertEqual(got[-1][1:], ("done", {"state": "completed"}))
        self.assertEqual(len(got), 2, "the malformed frame is skipped")

    def test_bytes_lines_are_accepted(self):
        self.assertEqual(len(list(cli.parse_sse([b"event: done", b'data: {"state": "x"}']))), 1)


class Rendering(unittest.TestCase):
    def render(self, *events):
        r, out = cli.Renderer(), []
        for e, d in events:
            for kind, text in r.feed(e, d):
                out.append(text if kind == "raw" else text + "\n")
        return r, "".join(out)

    def test_a_run(self):
        r, text = self.render(("delta", {"text": "Looking"}), ("tool_call", {"name": "read_file", "args": {"path": "a.py"}}),
                              ("tool_result", {"ok": True, "result": "line1\nline2"}), ("delta", {"text": "Done."}),
                              ("done", {"state": "completed"}))
        self.assertIn("Looking\n> read_file path='a.py'\n", text, "a streamed answer is closed before a tool line")
        self.assertIn("  ok  line1 line2", text)
        self.assertIn("Done.\n[completed]", text)
        self.assertEqual(r.final_state, "completed")

    def test_failures_and_reasons(self):
        r, text = self.render(("tool_result", {"ok": False, "result": "error: nope"}),
                              ("done", {"state": "stopped", "reason": "step_limit"}))
        self.assertIn("ERR error: nope", text)
        self.assertIn("[stopped] step_limit", text)
        self.assertEqual(cli.exit_code(r.final_state), 1)
        self.assertEqual(cli.exit_code("completed"), 0)
        self.assertEqual(cli.exit_code(None), 1)

    def test_server_text_with_escapes_is_cleaned_in_every_event(self):
        _, text = self.render(("tool_call", {"name": "x\x1b[2J", "args": {"p": "\x1b]52;c;AAAA\x07"}}),
                              ("tool_result", {"ok": True, "result": "\x1b[31mred"}), ("delta", {"text": "\x1b[0m!"}))
        self.assertNotIn("\x1b", text)


class Approvals(unittest.TestCase):
    CARD = {"req_id": "r1", "cmd": "git push origin main", "kind": "shell"}

    def test_decisions(self):
        self.assertEqual(cli.decide("y", "shell"), "allow")
        self.assertEqual(cli.decide("YES", "mcp"), "allow")
        self.assertEqual(cli.decide("", "shell"), "deny")
        self.assertEqual(cli.decide("anything else", "shell"), "deny")
        self.assertEqual(cli.decide("r", "rule"), "project")
        self.assertEqual(cli.decide("r", "shell"), "deny", "a shell command cannot be allowed for the run")
        for saved in ("always", "user", "project"):
            self.assertEqual(cli.decide(saved, "shell"), "deny", "saved patterns are never offered")

    def test_the_card_shows_what_will_happen_and_the_taint_warning(self):
        text = cli.card_prompt(dict(self.CARD, tainted=True, cmd="rm -rf build\x1b[2J"))
        self.assertIn("rm -rf build", text)
        self.assertIn("outside content", text)
        self.assertNotIn("\x1b", text)
        self.assertIn("POLICY", cli.card_prompt({"kind": "rule", "cmd": "x"}))

    def follow(self, answers, interactive=True):
        posts = []

        class FakeApi:
            def post(self, path, **kw):
                posts.append((path, kw.get("json")))

        events = sse(("run", {"run_id": "r"}), ("permission_request", self.CARD), ("tool_call", {"name": "run_shell", "args": {}}),
                     ("done", {"state": "completed"}))
        resp = mock.Mock(iter_lines=lambda: iter(events))
        out = io.StringIO()
        asked = []

        def ask(prompt):
            asked.append(prompt)
            return answers.pop(0)
        ren = cli.follow(FakeApi(), resp, interactive, out=out, ask=ask)
        return ren, posts, out.getvalue(), asked

    def test_yes_is_sent_to_the_server(self):
        ren, posts, out, asked = self.follow(["y"])
        self.assertEqual(posts, [("/agent/permission", {"req_id": "r1", "decision": "allow"})])
        self.assertEqual(len(asked), 1)
        self.assertIn("allowed", out)
        self.assertEqual(ren.final_state, "completed")

    def test_enter_and_eof_deny(self):
        _, posts, _, _ = self.follow([""])
        self.assertEqual(posts[0][1]["decision"], "deny")

    def test_without_a_terminal_everything_is_denied_and_nothing_is_asked(self):
        _, posts, out, asked = self.follow([], interactive=False)
        self.assertEqual(asked, [])
        self.assertEqual(posts[0][1]["decision"], "deny")
        self.assertIn("no terminal to ask on", out)


class Main(unittest.TestCase):
    def test_a_token_is_required_and_is_not_a_flag(self):
        with mock.patch.dict("os.environ", {}, clear=True), mock.patch("sys.stderr", new_callable=io.StringIO):
            self.assertEqual(cli.main(["runs"]), 2)
        with self.assertRaises(SystemExit):
            cli.build_parser().parse_args(["--token", "x", "runs"])

    def test_permission_mode_bypass_is_not_offered(self):
        with self.assertRaises(SystemExit), mock.patch("sys.stderr", new_callable=io.StringIO):
            cli.build_parser().parse_args(["run", "x", "--permission-mode", "bypass"])

    def test_base_must_be_http(self):
        with mock.patch.dict("os.environ", {"A770_TOKEN": "a770_pat_x"}), mock.patch("sys.stderr", new_callable=io.StringIO):
            self.assertEqual(cli.main(["--base", "file:///etc", "runs"]), 2)


class RealRun(unittest.TestCase):
    """One run through the real HTTP stack with the scripted model."""

    def test_run_attach_and_runs_against_the_mock_app(self):
        import queue
        import test_run_detached_http as h
        import eval_mock as em

        class T(h.Base):
            slow_s = 0.2

            def runTest(self):
                pass
        t = T()
        t.setUp()
        try:
            api = cli.Api(t.base, "a770_pat_test", em.DEVICE)
            args = cli.build_parser().parse_args(["run", "Use the slow connector once, then answer.", "--no-input"])
            out = io.StringIO()
            with mock.patch("sys.stdout", out):
                code = cli.cmd_run(api, args)
            text = out.getvalue()
            self.assertEqual(code, 0, text)
            self.assertIn("> mcp__slow__wait", text)
            self.assertIn("All done.", text)
            self.assertIn("[completed]", text)

            # the finished run is still held: list it, then replay it
            runs = api.get("/agent/runs").json()["runs"]
            self.assertEqual(len(runs), 1)
            self.assertTrue(runs[0]["id"].startswith("cli-"))
            out2 = io.StringIO()
            with mock.patch("sys.stdout", out2):
                code2 = cli.cmd_attach(api, cli.build_parser().parse_args(["attach", runs[0]["id"]]))
            self.assertEqual(code2, 0)
            self.assertIn("All done.", out2.getvalue())
            self.assertIn(runs[0]["id"], _capture(cli.cmd_runs, api))
        finally:
            t.tearDown()


def _capture(fn, api):
    buf = io.StringIO()
    with mock.patch("sys.stdout", buf):
        fn(api, None)
    return buf.getvalue()


if __name__ == "__main__":
    unittest.main()
