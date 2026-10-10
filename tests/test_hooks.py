"""tests/test_hooks.py - admin command hooks around tool calls (core/hooks.py).

Run: python -m unittest tests.test_hooks -v
"""

import asyncio
import json
import queue
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from core import hooks  # noqa: E402


def cfg(rules, enabled=True):
    return mock.patch.dict(hooks.APP_CONFIG, {"hooks": {"enabled": enabled, "timeout_s": 5, "rules": rules}})


FMT = {"id": "fmt", "event": "post_tool", "tool": "edit_file|write_file", "command": "ruff format {path}"}
GATE = {"id": "gate", "event": "pre_tool", "tool": "run_shell", "command": "python check.py {tool}"}


class Matching(unittest.TestCase):
    def test_off_by_default_and_when_disabled(self):
        with cfg([FMT], enabled=False):
            self.assertEqual(hooks.matching("post_tool", "edit_file"), [])
        self.assertFalse(hooks.enabled() and hooks._cfg().get("rules"), "the shipped config has no rules")

    def test_event_and_tool_filters(self):
        with cfg([FMT, GATE]):
            self.assertEqual([r["id"] for r in hooks.matching("post_tool", "write_file")], ["fmt"])
            self.assertEqual([r["id"] for r in hooks.matching("pre_tool", "run_shell")], ["gate"])
            self.assertEqual(hooks.matching("pre_tool", "edit_file"), [])
            self.assertEqual(hooks.matching("stop", "edit_file"), [])

    def test_bad_rules_are_ignored_and_the_count_is_capped(self):
        many = [dict(GATE, id=f"g{i}") for i in range(10)]
        with cfg([None, {"event": "pre_tool"}, "x"] + many):
            self.assertEqual(len(hooks.matching("pre_tool", "run_shell")), hooks.MAX_HOOKS_PER_CALL)


class Commands(unittest.TestCase):
    def test_placeholders_are_filled_and_the_path_is_quoted(self):
        self.assertEqual(hooks.build_command(FMT, "edit_file", {"path": "src/my file.py"}), 'ruff format "src/my file.py"')
        self.assertEqual(hooks.build_command(GATE, "run_shell", {"command": "x"}), "python check.py run_shell")

    def test_a_value_that_could_become_shell_syntax_is_refused(self):
        for bad in ('a.py"; rm -rf /', "a.py & calc", "a`id`.py", "$(id).py", "a|b.py", "a%PATH%.py", "-rf",
                    "../secret.py", "a/../b.py", 'a"b.py', "a\nb.py", "x" * 300):
            self.assertIsNone(hooks.build_command(FMT, "edit_file", {"path": bad}), repr(bad))

    def test_a_command_without_placeholders_needs_no_values(self):
        rule = {"id": "r", "event": "pre_tool", "command": "python check.py"}
        self.assertEqual(hooks.build_command(rule, "weird tool!", {}), "python check.py")


class Running(unittest.TestCase):
    def run_(self, coro):
        return asyncio.run(coro)

    def test_pre_hook_passes_blocks_and_fails_closed(self):
        calls = []

        async def ok(cmd):
            calls.append(cmd)
            return 0, ""

        async def nope(cmd):
            return 2, "tests are red"

        with cfg([GATE]):
            with mock.patch.object(hooks, "_run", ok):
                self.assertIsNone(self.run_(hooks.run_pre("run_shell", {"command": "ls"})))
                self.assertEqual(calls, ["python check.py run_shell"])
            with mock.patch.object(hooks, "_run", nope):
                why = self.run_(hooks.run_pre("run_shell", {"command": "ls"}))
                self.assertIn("gate", why)
                self.assertIn("tests are red", why)
            self.assertIsNone(self.run_(hooks.run_pre("read_file", {"path": "a"})), "other tools are untouched")
        with cfg([dict(GATE, command="check {path}", tool="*")]):
            why = self.run_(hooks.run_pre("edit_file", {"path": "a;b.py"}))
            self.assertIn("characters a hook cannot take safely", why, "an unsafe value blocks, it does not skip")

    def test_post_hook_reports_failures_and_skips_unsafe_values(self):
        async def bad(cmd):
            return 1, "E501 line too long"

        with cfg([FMT]), mock.patch.object(hooks, "_run", bad):
            note = self.run_(hooks.run_post("edit_file", {"path": "a.py"}))
            self.assertIn("fmt", note)
            self.assertIn("E501", note)
            self.assertEqual(self.run_(hooks.run_post("edit_file", {"path": "a;b.py"})), "", "unsafe: skipped quietly")

    def test_a_hook_that_cannot_run_is_a_failure_not_a_crash(self):
        with cfg([GATE]), mock.patch("core.agent_tools.require_device_workspace", side_effect=PermissionError("no device")):
            code, out = self.run_(hooks._run("anything"))
            self.assertEqual(code, 1)
            self.assertIn("could not run", out)

    def test_the_command_goes_through_the_companion_in_the_workspace(self):
        sent = {}

        async def fake_call(uid, op, params, timeout=0):
            sent.update(uid=uid, op=op, params=params)
            return {"exit_code": 0, "stdout": "fine", "stderr": ""}

        with cfg([GATE]), mock.patch("core.agent_tools.require_device_workspace", return_value=(7, Path("/ws"))), \
                mock.patch("core.companion_bridge.call", fake_call):
            self.assertEqual(self.run_(hooks._run("echo hi")), (0, "fine"))
        self.assertEqual((sent["uid"], sent["op"]), (7, "shell.run"))
        self.assertEqual(sent["params"]["command"], "echo hi")
        self.assertEqual(sent["params"]["timeout"], 5)


class LoopBehaviour(unittest.TestCase):
    """The real /agent/run loop: a failing pre hook stops the tool, a failing post hook talks back."""

    def setUp(self):
        from core.registry import registry
        self.ran = []

        async def touch(args):
            self.ran.append(args.get("path"))
            return "touched"

        registry.register("mcp__fake__touch", touch, {"type": "function", "function": {
            "name": "mcp__fake__touch", "description": "[mcp:fake] touch",
            "parameters": {"type": "object", "properties": {"path": {"type": "string"}}}}},
            source="mcp:fake", meta={"label": "fake/touch", "read_only": True}, replace=True)
        self.registry = registry

    def tearDown(self):
        self.registry.unregister_source("mcp:fake")

    def run_script(self, rules, exit_code, mode=None):
        import eval_mock as em
        from routes.agent import stream
        seen = []

        async def fake_run(cmd):
            seen.append(cmd)
            return exit_code, "policy says no" if exit_code else ""

        events = []
        with em.MockEvalEnv() as env, cfg(rules), mock.patch.object(hooks, "_run", fake_run):
            em._current["fifo"] = queue.Queue()
            for turn in [em.T(tool_calls=[em.TC("mcp__fake__touch", path="a.py")]), em.T(content="ok")]:
                em._current["fifo"].put(dict(turn))
            em._current["extra_calls"] = 0
            em._current["requests"] = 0
            payload = {"messages": [{"role": "user", "content": "Touch a.py."}], "mode": "all-local",
                       "max_steps": 6, "temperature": 0, "session_id": env.sid}
            if mode:
                payload["permission_mode"] = mode
            headers = {"User-Agent": "A770NativeApp/1.0", "X-Device-Id": em.DEVICE}
            with env.client.stream("POST", "/agent/run", json=payload, headers=headers, timeout=120) as resp:
                event = None
                for line in resp.iter_lines():
                    line = line.strip() if isinstance(line, str) else line.decode("utf-8", "replace").strip()
                    if line.startswith("event: "):
                        event = line[7:].strip()
                    elif line.startswith("data: ") and event:
                        try:
                            events.append((event, json.loads(line[6:])))
                        except ValueError:
                            pass
        return events, seen

    def result(self, events):
        return [d for e, d in events if e == "tool_result"][0]

    def test_a_failing_pre_hook_blocks_the_tool_even_in_bypass_mode(self):
        pre = {"id": "gate", "event": "pre_tool", "tool": "mcp__fake__*", "command": "python check.py {path}"}
        events, seen = self.run_script([pre], exit_code=2, mode="bypass")
        self.assertEqual(self.ran, [])
        res = self.result(events)
        self.assertFalse(res["ok"])
        self.assertIn("blocked by hook 'gate'", res["result"])
        self.assertIn("policy says no", res["result"])
        self.assertEqual(seen, ['python check.py "a.py"'])

    def test_a_passing_pre_hook_lets_it_run(self):
        pre = {"id": "gate", "event": "pre_tool", "tool": "mcp__fake__*", "command": "python check.py"}
        events, _ = self.run_script([pre], exit_code=0)
        self.assertEqual(self.ran, ["a.py"])
        self.assertTrue(self.result(events)["ok"])

    def test_a_failing_post_hook_adds_feedback_to_the_result(self):
        post = {"id": "lint", "event": "post_tool", "tool": "mcp__fake__*", "command": "python lint.py {path}"}
        events, _ = self.run_script([post], exit_code=1)
        self.assertEqual(self.ran, ["a.py"], "the tool ran")
        res = self.result(events)
        self.assertTrue(res["ok"])
        self.assertIn("hook 'lint' failed", res["result"])
        self.assertIn("policy says no", res["result"])

    def test_no_hooks_no_extra_calls(self):
        events, seen = self.run_script([], exit_code=1)
        self.assertEqual(seen, [])
        self.assertEqual(self.ran, ["a.py"])


if __name__ == "__main__":
    unittest.main()
