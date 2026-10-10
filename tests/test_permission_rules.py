"""tests/test_permission_rules.py - admin allow/ask/deny rules per tool and path (core/permission_rules.py).

Run: python -m unittest tests.test_permission_rules -v
"""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from core import permission_rules as pr  # noqa: E402

KEYS = {"id": "no-keys", "tool": "read_file|write_file|edit_file|grep", "path": "**/*.pem", "action": "deny",
        "reason": "private keys are off limits"}
ENV = {"id": "env", "tool": "read_file|edit_file|write_file", "path": ".env*", "unless": ".env.example",
       "action": "ask"}
PUSH = {"id": "push", "tool": "run_shell", "command": "git push*", "action": "ask"}


class Globs(unittest.TestCase):
    def test_double_star_crosses_folders_and_single_star_does_not(self):
        self.assertTrue(pr.path_matches("a/b/c/key.pem", "**/*.pem"))
        self.assertTrue(pr.path_matches("key.pem", "**/*.pem"))
        self.assertTrue(pr.path_matches("src/x.py", "src/*.py"))
        self.assertFalse(pr.path_matches("src/deep/x.py", "src/*.py"))
        self.assertTrue(pr.path_matches("src/deep/x.py", "src/**"))

    def test_a_name_pattern_matches_in_any_folder(self):
        self.assertTrue(pr.path_matches("config/prod/.env.local", ".env*"))
        self.assertFalse(pr.path_matches("config/environment.py", ".env*"))

    def test_slashes_case_and_dot_prefix_are_normalised(self):
        self.assertTrue(pr.path_matches(".\\Certs\\SERVER.PEM", "**/*.pem"))

    def test_glob_characters_are_literal_outside_the_wildcards(self):
        self.assertFalse(pr.path_matches("axb", "a.b"))
        self.assertTrue(pr.path_matches("a.b", "a.b"))
        self.assertFalse(pr.path_matches("", "*"))


class Decisions(unittest.TestCase):
    def test_deny_on_the_matching_tool_and_path(self):
        v = pr.evaluate("read_file", {"path": "certs/server.pem"}, [KEYS])
        self.assertEqual((v["action"], v["id"]), ("deny", "no-keys"))
        self.assertIn("off limits", pr.refusal_text(v))

    def test_other_tools_and_paths_are_untouched(self):
        self.assertIsNone(pr.evaluate("read_file", {"path": "src/app.py"}, [KEYS]))
        self.assertIsNone(pr.evaluate("list_files", {"path": "certs/server.pem"}, [KEYS]))

    def test_unless_exempts_a_template(self):
        self.assertEqual(pr.evaluate("read_file", {"path": ".env"}, [ENV])["action"], "ask")
        self.assertIsNone(pr.evaluate("read_file", {"path": ".env.example"}, [ENV]))

    def test_deny_beats_ask_whatever_the_order(self):
        both = [ENV, {"id": "d", "tool": "read_file", "path": ".env*", "action": "deny"}]
        self.assertEqual(pr.evaluate("read_file", {"path": ".env"}, both)["action"], "deny")
        self.assertEqual(pr.evaluate("read_file", {"path": ".env"}, both[::-1])["action"], "deny")

    def test_path_lists_and_every_path_key_are_checked(self):
        self.assertIsNotNone(pr.evaluate("git_commit", {"paths": ["a.py", "k/x.pem"]},
                                         [dict(KEYS, tool="git_*")]))
        self.assertIsNotNone(pr.evaluate("edit_file", {"file": "x.pem"}, [KEYS]))
        self.assertIsNotNone(pr.evaluate("write_file", {"file_path": "x.pem"}, [KEYS]))

    def test_shell_command_globs(self):
        self.assertEqual(pr.evaluate("run_shell", {"command": "git push origin main"}, [PUSH])["id"], "push")
        self.assertEqual(pr.evaluate("run_shell", {"command": "GIT PUSH --force"}, [PUSH])["action"], "ask")
        self.assertIsNone(pr.evaluate("run_shell", {"command": "git status"}, [PUSH]))
        self.assertIsNone(pr.evaluate("read_file", {"command": "git push"}, [PUSH]), "the rule names run_shell")

    def test_a_path_rule_on_shell_looks_at_every_word_of_the_command(self):
        rule = dict(KEYS, tool="run_shell")
        self.assertEqual(pr.evaluate("run_shell", {"command": "type certs\\server.pem"}, [rule])["action"], "deny")
        self.assertEqual(pr.evaluate("run_shell", {"command": 'cat "a/b/id.pem" | head'}, [rule])["action"], "deny")
        self.assertIsNone(pr.evaluate("run_shell", {"command": "echo hello"}, [rule]))

    def test_tool_globs_and_a_rule_with_only_a_tool_blocks_the_whole_tool(self):
        rule = {"id": "no-mcp", "tool": "mcp__*", "action": "deny"}
        self.assertIsNotNone(pr.evaluate("mcp__jira__create", {}, [rule]))
        self.assertIsNone(pr.evaluate("read_file", {}, [rule]))

    def test_tool_may_be_a_list(self):
        rule = {"id": "r", "tool": ["write_file", "edit_file"], "path": "dist/**", "action": "deny"}
        self.assertIsNotNone(pr.evaluate("edit_file", {"path": "dist/a.js"}, [rule]))

    def test_malformed_rules_are_ignored_not_raised(self):
        junk = [None, 5, "x", {}, {"action": "allow", "path": "*"}, {"action": "deny"}, {"action": "deny", "tool": 7}]
        self.assertEqual(pr.valid_rules(junk[:6]), [])
        self.assertIsNone(pr.evaluate("read_file", {"path": "a"}, junk[:6]))
        self.assertIsNone(pr.evaluate("read_file", {"path": "a"}, None))
        self.assertIsNone(pr.evaluate("read_file", None, [KEYS]))

    def test_no_rules_is_a_no_op(self):
        self.assertIsNone(pr.evaluate("run_shell", {"command": "rm -rf /"}, []))


class ShippedDefaults(unittest.TestCase):
    def test_the_rules_in_app_json_are_valid_and_protect_key_files(self):
        import json
        cfg = json.loads((Path(__file__).resolve().parents[1] / "config" / "app.json").read_text(encoding="utf-8"))
        rules = (cfg.get("permissions") or {}).get("rules")
        self.assertTrue(rules, "defaults are shipped")
        self.assertEqual(len(pr.valid_rules(rules)), len(rules), "every shipped rule is usable")
        for key_file in ("certs/server.pem", "home/.ssh/id_rsa", "secrets/tls.key"):
            v = pr.evaluate("read_file", {"path": key_file}, rules)
            self.assertIsNotNone(v, key_file)
            self.assertEqual(v["action"], "deny", key_file)
        self.assertIsNone(pr.evaluate("read_file", {"path": "src/main.py"}, rules))
        self.assertIsNone(pr.evaluate("read_file", {"path": ".env.example"}, rules))


class LoopBehaviour(unittest.TestCase):
    """The real /agent/run loop with a scripted model: a deny holds in every mode, an ask is a card."""

    RULES = [{"id": "no-pem", "tool": "mcp__fake__*", "path": "**/*.pem", "action": "deny", "reason": "keys are off limits"},
             {"id": "notes", "tool": "mcp__fake__*", "path": "notes/**", "action": "ask", "reason": "notes are private"}]

    def setUp(self):
        import queue
        from core.registry import registry
        self.touched, self.cards = [], []

        async def touch(args):
            self.touched.append(args.get("path"))
            return "done"

        registry.register("mcp__fake__touch", touch, {"type": "function", "function": {
            "name": "mcp__fake__touch", "description": "[mcp:fake] touch",
            "parameters": {"type": "object", "properties": {"path": {"type": "string"}}}}},
            source="mcp:fake", meta={"label": "fake/touch", "read_only": True}, replace=True)
        self.registry, self.queue = registry, queue

    def tearDown(self):
        self.registry.unregister_source("mcp:fake")

    def run_script(self, script, answer=False, mode=None, rules=None):
        import json
        from unittest import mock
        import eval_mock as em
        from routes.agent import permissions, stream

        async def fake_permission_stream(req_id, ev, *a, **k):
            try:
                yield "done", (answer, "" if answer else "denied in test", "once")
            finally:
                permissions._perm_pending.pop(req_id, None)

        events = []
        cfg = mock.patch.dict(stream.APP_CONFIG, {"permissions": {"rules": self.RULES if rules is None else rules}})
        with em.MockEvalEnv() as env, cfg, mock.patch.object(stream, "_permission_stream", fake_permission_stream):
            em._current["fifo"] = self.queue.Queue()
            for turn in script:
                em._current["fifo"].put(dict(turn))
            em._current["extra_calls"] = 0
            em._current["requests"] = 0
            payload = {"messages": [{"role": "user", "content": "Touch the files."}], "mode": "all-local",
                       "max_steps": 8, "temperature": 0, "session_id": env.sid}
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
                            data = json.loads(line[6:])
                        except ValueError:
                            continue
                        events.append((event, data))
                        if event == "permission_request":
                            self.cards.append(data)
        return events

    def result_of(self, events):
        return [d for e, d in events if e == "tool_result"]

    def test_a_denied_path_never_runs_and_the_model_is_told_why(self):
        import eval_mock as em
        ev = self.run_script([em.T(tool_calls=[em.TC("mcp__fake__touch", path="certs/a.pem")]), em.T(content="ok")])
        self.assertEqual(self.touched, [])
        self.assertEqual(self.cards, [], "a deny is not a question")
        res = self.result_of(ev)[0]
        self.assertFalse(res["ok"])
        self.assertIn("no-pem", res["result"])
        self.assertIn("keys are off limits", res["result"])

    def test_a_deny_holds_even_in_bypass_mode(self):
        import eval_mock as em
        self.run_script([em.T(tool_calls=[em.TC("mcp__fake__touch", path="a.pem")]), em.T(content="ok")], mode="bypass")
        self.assertEqual(self.touched, [])

    def test_an_ask_rule_shows_a_card_and_runs_only_if_allowed(self):
        import eval_mock as em
        self.run_script([em.T(tool_calls=[em.TC("mcp__fake__touch", path="notes/a.txt")]), em.T(content="ok")])
        self.assertEqual(len(self.cards), 1)
        self.assertEqual(self.cards[0]["kind"], "rule")
        self.assertIn("notes are private", self.cards[0]["cmd"])
        self.assertEqual(self.touched, [], "denied in the card")
        self.cards.clear()
        self.run_script([em.T(tool_calls=[em.TC("mcp__fake__touch", path="notes/a.txt")]), em.T(content="ok")], answer=True)
        self.assertEqual(self.touched, ["notes/a.txt"])

    def test_bypass_mode_skips_an_ask_but_not_a_deny(self):
        import eval_mock as em
        self.run_script([em.T(tool_calls=[em.TC("mcp__fake__touch", path="notes/a.txt")]), em.T(content="ok")], mode="bypass")
        self.assertEqual(self.cards, [])
        self.assertEqual(self.touched, ["notes/a.txt"])

    def test_unrelated_calls_and_an_empty_rule_list_are_untouched(self):
        import eval_mock as em
        self.run_script([em.T(tool_calls=[em.TC("mcp__fake__touch", path="src/a.py")]), em.T(content="ok")])
        self.assertEqual(self.touched, ["src/a.py"])
        self.assertEqual(self.cards, [])
        self.run_script([em.T(tool_calls=[em.TC("mcp__fake__touch", path="a.pem")]), em.T(content="ok")], rules=[])
        self.assertEqual(self.touched[-1], "a.pem", "no rules: nothing is blocked")

    def test_a_denied_call_in_a_parallel_batch_does_not_run_and_the_others_do(self):
        import eval_mock as em
        ev = self.run_script([em.T(tool_calls=[em.TC("read_file", path="README.md"),
                                               em.TC("read_file", path="x.pem")]),
                              em.T(content="ok")], rules=[dict(self.RULES[0], tool="read_file")])
        res = self.result_of(ev)
        self.assertEqual(len(res), 2)
        blocked = [r for r in res if "no-pem" in str(r.get("result"))]
        self.assertEqual(len(blocked), 1, "only the .pem read was blocked")
        self.assertEqual(sum(1 for e, d in ev if e == "tool_call"), 2, "each call is announced once")


class SubAgents(unittest.TestCase):
    """Delegating must not get around an administrator rule."""

    def refusal(self, name, args, rules, hook_rules=None):
        import asyncio
        from unittest import mock
        from core import hooks
        from core.subagent.runner import subagent_policy_refusal
        cfg = {"permissions": {"rules": rules}, "hooks": {"enabled": bool(hook_rules), "rules": hook_rules or []}}
        with mock.patch.dict(hooks.APP_CONFIG, cfg):
            return asyncio.run(subagent_policy_refusal(name, args))

    def test_deny_applies_to_a_sub_agent(self):
        r = self.refusal("read_file", {"path": "k/server.pem"}, [KEYS])
        self.assertIn("no-keys", r)

    def test_ask_cannot_be_answered_so_it_is_a_refusal_that_says_so(self):
        r = self.refusal("read_file", {"path": ".env"}, [ENV])
        self.assertIn("cannot ask", r)
        self.assertTrue(r.startswith("error:"))

    def test_unrelated_calls_pass(self):
        self.assertIsNone(self.refusal("read_file", {"path": "src/a.py"}, [KEYS, ENV]))
        self.assertIsNone(self.refusal("read_file", {"path": "src/a.py"}, []))

    def test_a_pre_hook_applies_to_a_sub_agent(self):
        import asyncio
        from unittest import mock
        from core import hooks

        async def nope(cmd):
            return 2, "frozen"
        hook = {"id": "freeze", "event": "pre_tool", "tool": "edit_file", "command": "python freeze.py"}
        with mock.patch.object(hooks, "_run", nope):
            self.assertIn("freeze", self.refusal("edit_file", {"path": "a.py"}, [], [hook]))


class Brace(unittest.TestCase):
    def test_brace_alternatives(self):
        self.assertTrue(pr.path_matches("a/b.pem", "**/{*.pem,*.key}"))
        self.assertTrue(pr.path_matches("b.key", "**/{*.pem,*.key}"))
        self.assertFalse(pr.path_matches("b.txt", "**/{*.pem,*.key}"))


if __name__ == "__main__":
    unittest.main()
