"""tests/test_subagent_isolation.py - F3: sub-agent boundaries that must hold.

A child inherits the full tool set by default and runs with no approval modal,
so its containment is three gates: the tool set it is offered (schemas), the
execution gate for names the model emits anyway, and the no-further-delegation
rule. These tests prove all three, plus the workspace-pointer restore and the
PAN masking of the child's answer before it reaches the parent context.

Run: python -m unittest tests.test_subagent_isolation -v
"""

import asyncio
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.subagent import DENIED_TOOLS, resolve_subagent_tools, subagent_call_verdict
from core.subagent.runner import run_subagent, tool_spawn_agent, tool_spawn_parallel_agents


class ResolveToolsTests(unittest.TestCase):
    ALL = ["read_file", "run_shell", "run_python", "spawn_agent", "grep", "generate_image"]

    def test_denied_never_offered(self):
        got = resolve_subagent_tools(self.ALL, None, None, None)
        self.assertTrue({"read_file", "grep"} <= got)
        self.assertFalse(got & DENIED_TOOLS)

    def test_explicit_allowlist_cannot_re_admit_denied(self):
        got = resolve_subagent_tools(self.ALL, None, ["read_file", "run_shell", "spawn_agent"], None)
        self.assertEqual(got, {"read_file"})

    def test_role_tools_are_capped_by_parent_allowlist(self):
        got = resolve_subagent_tools(self.ALL, ["read_file", "grep"], None, frozenset({"read_file"}))
        self.assertEqual(got, {"read_file"})

    def test_empty_parent_allowlist_means_nothing(self):
        self.assertEqual(resolve_subagent_tools(self.ALL, None, None, frozenset()), set())

    def test_falsy_allowlist_is_no_constraint(self):
        self.assertEqual(resolve_subagent_tools(self.ALL, None, [], None),
                         resolve_subagent_tools(self.ALL, None, None, None))


class VerdictTests(unittest.TestCase):
    """The execution gate, independent of what was offered: denied-first."""

    def test_denied_refused_as_forbidden(self):
        for name in ("run_shell", "run_python", "spawn_agent", "spawn_parallel_agents",
                     "generate_image", "generate_video"):
            with self.subTest(name=name):
                self.assertIn("unavailable to sub-agents",
                              subagent_call_verdict(name, {"read_file", name}))

    def test_unlisted_refused_as_unlisted(self):
        self.assertIn("not enabled", subagent_call_verdict("browser_click", {"read_file"}))

    def test_listed_passes(self):
        self.assertIsNone(subagent_call_verdict("read_file", {"read_file"}))

    def test_denied_wins_over_unlisted(self):
        # the message must name the stronger reason so the model learns the
        # boundary, not the roster
        self.assertIn("unavailable",
                      subagent_call_verdict("run_shell", {"read_file", "grep"}))


class _FakeTarget:
    is_cloud = False

    def __init__(self, lane="executor"):
        self.lane = lane
        self.inst = None
        self.cm = None
        self.source = "local"

    def available(self):
        return True

    def describe(self):
        return f"local:{self.lane}"

    async def client(self):
        return object()


def _stream_script(*turns):
    """Fake _llm_chat_stream: each call yields the next scripted result."""
    state = {"n": 0}

    async def fake(client, msgs, tools, *a, **k):
        res = turns[min(state["n"], len(turns) - 1)]
        state["n"] += 1
        yield ("result", res)

    return fake


def _tc(name, args=None, tid=None):
    import json as _json
    return {"id": tid or f"c_{name}", "type": "function",
            "function": {"name": name, "arguments": _json.dumps(args or {})}}


class IsolationRunTests(unittest.TestCase):
    """The model emits run_shell + spawn_agent + read_file: only read_file runs."""

    def setUp(self):
        from core.registry import bootstrap_builtin_tools
        bootstrap_builtin_tools()
        try:
            from core.shell_tools import register_shell_tools
            register_shell_tools()
        except Exception:
            pass
        self.calls = []

        async def fake_run_tool(name, args):
            self.calls.append(name)
            return f"ran {name}"

        patches = [
            mock.patch("core.agent_loop.run_tool", fake_run_tool),
            mock.patch("core.agent_tools.active_workspace", lambda: Path("/tmp/ws")),
            mock.patch("core.lanes.targets", lambda *a, **k: [_FakeTarget()]),
            mock.patch("core.lanes.registry",
                       lambda *a, **k: {"executor": {"kind": "chat", "label": "x"}}),
            mock.patch("core.cloud.cloud_lane", lambda *a, **k: None),
            mock.patch("core.cloud.role_map", lambda *a, **k: {}),
            mock.patch("core.db.db_record_request", lambda *a, **k: None),
            mock.patch("routes.common._llm_chat_stream", _stream_script(
                {"content": "", "tool_calls": [
                    _tc("run_shell", {"command": "rm -rf /"}),
                    _tc("spawn_agent", {"task": "grandchild"}),
                    _tc("read_file", {"path": "a.py"}),
                ], "usage": {}, "timings": {}},
                {"content": "done. card 4111111111111111.", "tool_calls": [],
                 "usage": {}, "timings": {}},
            )),
        ]
        for p in patches:
            p.start()
        self.addCleanup(lambda: [p.stop() for p in patches])

    def test_denied_tools_refused_allowed_tool_runs(self):
        out = asyncio.run(run_subagent("do the thing"))
        # refusals land in the child's transcript (tool results), not its answer;
        # the proof is what reached the executor: only read_file did
        self.assertEqual(self.calls, ["read_file"])
        self.assertIn("lane=executor", out)

    def test_child_answer_pans_masked_before_parent(self):
        out = asyncio.run(run_subagent("do the thing"))
        self.assertNotIn("4111111111111111", out)
        self.assertIn("1111", out)


class EntryValidationTests(unittest.TestCase):
    def test_empty_task_refused(self):
        self.assertIn("non-empty task", asyncio.run(run_subagent("  ")))
        self.assertIn("task is required", asyncio.run(tool_spawn_agent({})))

    def test_unknown_role_names_alternatives(self):
        out = asyncio.run(run_subagent("x", role="nope-not-a-role"))
        self.assertIn("unknown role", out)

    def test_parallel_specs_validated(self):
        self.assertIn("must be a list", asyncio.run(tool_spawn_parallel_agents({})))
        out = asyncio.run(tool_spawn_parallel_agents({"agents": [{"task": ""}, "nope"]}))
        self.assertIn("task is required", out)
        self.assertIn("invalid specification", out)


class ScopeTests(unittest.TestCase):
    def test_active_project_restored(self):
        from core.subagent.scope import _subagent_scope
        with mock.patch("core.agent_tools.get_active_project", return_value="A"), \
             mock.patch("core.agent_tools.set_active_project") as setp, \
             mock.patch("core.agent_tools.get_active_project",
                        side_effect=["A", "B", "B", "A"]):
            async def go():
                async with _subagent_scope():
                    pass
            asyncio.run(go())
            setp.assert_called_once_with("A")

    def test_untouched_project_is_not_reset(self):
        from core.subagent.scope import _subagent_scope
        with mock.patch("core.agent_tools.get_active_project", return_value="A"), \
             mock.patch("core.agent_tools.set_active_project") as setp:
            async def go():
                async with _subagent_scope():
                    pass
            asyncio.run(go())
            setp.assert_not_called()


class ChildWriteTrackingTests(unittest.TestCase):
    """R10: a child's file writes must not pollute the parent's _ws_changes
    (diff/undo set) after the child finishes."""

    def test_child_writes_are_dropped_sibling_and_parent_survive(self):
        import tempfile
        import core.agent_tools as at
        from core.subagent.scope import drop_child_write_tracking
        tmp = tempfile.TemporaryDirectory()
        root = Path(tmp.name).resolve()

        def fake_ws():
            return Path(tmp.name)

        # seed _ws_changes: parent file + the two child files + one sibling file
        with mock.patch.object(at, "active_workspace", fake_ws), \
             mock.patch.object(at, "_ws_changes", {7: {
                 str(root / "README.md"): {"before": None, "after": "written"},
                 str(root / "core" / "app.py"): {"before": None, "after": "written"},
                 str(root / "tests" / "test_app.py"): {"before": None, "after": "written"},
                 str(root / "sibling.txt"): {"before": None, "after": "written"},
             }}), \
             mock.patch("core.request_context.get_current_user_id", return_value=7):
            drop_child_write_tracking({"core/app.py", "tests/test_app.py"})
            changes = dict(at._ws_changes[7])
        tmp.cleanup()
        self.assertEqual(set(changes.keys()), {str(root / "README.md"), str(root / "sibling.txt")})

    def test_empty_child_set_touches_nothing(self):
        from core import agent_tools
        from core.subagent.scope import drop_child_write_tracking
        with mock.patch("core.agent_tools._ws_changes", {7: {"a": 1}}), \
             mock.patch("core.request_context.get_current_user_id", return_value=7):
            drop_child_write_tracking(set())
            self.assertEqual(agent_tools._ws_changes[7], {"a": 1})


if __name__ == "__main__":
    unittest.main()
