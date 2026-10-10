"""tests/test_git_agent_tools.py - git_inspect / git_commit / git_branch against real git.

The companion is faked by running the real `git` in a temp repo (the same argument vector the tools send), so
what is verified is git's actual behaviour: what a commit includes, what gets unstaged, what is refused.

Run: python -m unittest tests.test_git_agent_tools -v
"""

import asyncio
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core import agent_tools, companion_bridge, git_tools
from core.agent_tools import git_agent_tools as gat
from core.request_context import set_current_device, set_current_user

# a published test number, built so no literal card number sits in the source
TEST_PAN = "4" + "1" * 15


@unittest.skipUnless(shutil.which("git"), "git is not installed")
class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = Path(self.tmp.name) / "repo"
        self.repo.mkdir()
        self.calls = []
        self.env = dict(os.environ, GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@t", GIT_COMMITTER_NAME="t",
                        GIT_COMMITTER_EMAIL="t@t")
        self.g("init", "-q")
        self.write("a.py", "print('a')\n")
        self.write("b.py", "print('b')\n")
        self.g("add", "-A")
        self.g("commit", "-q", "-m", "base")
        self._p = [
            mock.patch.object(companion_bridge, "call", self.fake_call),
            mock.patch.object(git_tools, "require_device_workspace", lambda: (7, self.repo)),
        ]
        for p in self._p:
            p.start()
        set_current_user(7)
        set_current_device("dev1")

    def tearDown(self):
        for p in self._p:
            p.stop()
        set_current_user(None)
        set_current_device(None)
        self.tmp.cleanup()

    def g(self, *args):
        return subprocess.run(["git", *args], cwd=self.repo, capture_output=True, text=True, check=True,
                              env=self.env).stdout.strip()

    def write(self, name, text):
        p = self.repo / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")

    async def fake_call(self, uid, op, params, timeout=None):
        assert op == "git.run", op
        self.calls.append(list(params["args"]))
        r = subprocess.run(["git", *params["args"]], cwd=params["cwd"], capture_output=True, text=True, env=self.env)
        return {"exit_code": r.returncode, "stdout": r.stdout, "stderr": r.stderr}

    def call(self, tool, **args):
        return asyncio.run(agent_tools.TOOL_IMPLS[tool](args))

    def head_files(self):
        return set(self.g("show", "--name-only", "--pretty=format:", "HEAD").split())

    def staged(self):
        return set(self.g("diff", "--cached", "--name-only").split())


class Inspect(Base):
    def test_status_clean_and_dirty(self):
        self.assertIn("working tree clean", self.call("git_inspect", action="status"))
        self.write("a.py", "print('changed')\n")
        self.write("new.py", "x = 1\n")
        out = self.call("git_inspect", action="status")
        self.assertIn("2 changed file(s)", out)
        self.assertRegex(out, r"M a\.py|M a\.py")
        self.assertIn("?? new.py", out)

    def test_diff_unstaged_and_staged(self):
        self.write("a.py", "print('changed')\n")
        self.assertIn("+print('changed')", self.call("git_inspect", action="diff", path="a.py"))
        self.assertEqual(self.call("git_inspect", action="diff", staged=True), "(no staged changes)")
        self.g("add", "a.py")
        self.assertIn("+print('changed')", self.call("git_inspect", action="diff", staged=True))

    def test_log_and_branches(self):
        out = self.call("git_inspect", action="log", count=5)
        self.assertIn("t: base", out)
        self.assertIn("branches:", self.call("git_inspect", action="branches"))

    def test_bad_action_and_unsafe_path_are_refused(self):
        self.assertIn("action must be one of", self.call("git_inspect", action="push"))
        self.assertIn("not allowed", self.call("git_inspect", action="diff", path="--output=x"))

    def test_not_a_repo(self):
        shutil.rmtree(self.repo / ".git", ignore_errors=True)
        self.assertIn("not a git repository", self.call("git_inspect", action="status"))


class Commit(Base):
    def test_commits_only_the_named_paths(self):
        self.write("a.py", "print('a2')\n")
        self.write("b.py", "print('b2')\n")
        self.g("add", "b.py")                       # staged by the user, not part of the agent's job
        out = self.call("git_commit", message="change a", paths=["a.py"])
        self.assertIn("committed 1 file(s)", out)
        self.assertEqual(self.head_files(), {"a.py"})
        self.assertEqual(self.staged(), {"b.py"}, "the user's staged change was not swept into the commit")

    def test_new_untracked_file_can_be_committed_by_name(self):
        self.write("src/new.py", "x = 1\n")
        self.call("git_commit", message="add new", paths=["src/new.py"])
        self.assertEqual(self.head_files(), {"src/new.py"})

    def test_all_tracked_takes_modified_tracked_files_but_not_untracked_ones(self):
        self.write("a.py", "print('a2')\n")
        self.write("untracked.txt", "scratch\n")
        out = self.call("git_commit", message="tidy", all_tracked=True)
        self.assertIn("committed 1 file(s)", out)
        self.assertEqual(self.head_files(), {"a.py"})
        self.assertTrue((self.repo / "untracked.txt").exists())

    def test_credential_files_are_refused_and_unstaged(self):
        self.write(".env", "API_KEY=[PLACEHOLDER]\n")
        self.write("a.py", "print('a2')\n")
        out = self.call("git_commit", message="oops", paths=["a.py", ".env"])
        self.assertIn("looks like a credential file", out)
        self.assertEqual(self.g("rev-list", "--count", "HEAD"), "1", "nothing was committed")
        self.assertNotIn(".env", self.staged(), "what the tool staged is put back")

    def test_a_folder_hiding_a_key_is_caught_after_staging(self):
        self.write("deploy/id_rsa", "-----BEGIN PRIVATE KEY-----\n")
        self.write("deploy/run.sh", "echo hi\n")
        out = self.call("git_commit", message="deploy", paths=["deploy"])
        self.assertIn("deploy/id_rsa", out)
        self.assertEqual(self.g("rev-list", "--count", "HEAD"), "1")

    def test_env_templates_are_not_secrets(self):
        self.assertFalse(gat.is_secret_path(".env.example"))
        self.assertFalse(gat.is_secret_path("config/.env.sample"))
        for bad in (".env", "app/.env.production", "certs/server.pem", "id_rsa", "~/.ssh/config", ".npmrc",
                    "aws/credentials.json", "secrets.yaml", "x/.aws/credentials"):
            self.assertTrue(gat.is_secret_path(bad), bad)
        self.assertFalse(gat.is_secret_path("src/credentials_helper.py"))

    def test_broad_or_unsafe_paths_are_refused(self):
        for bad in (".", "*", "../x", "/etc/passwd", "C:/x", "-A", ":(top)x", "a\nb"):
            with self.subTest(path=bad):
                self.assertIn("not allowed", self.call("git_commit", message="m", paths=[bad]))
        self.assertEqual(self.g("rev-list", "--count", "HEAD"), "1")

    def test_message_and_scope_are_required(self):
        self.assertIn("message is required", self.call("git_commit", message="  ", paths=["a.py"]))
        self.assertIn("say what to commit", self.call("git_commit", message="m"))

    def test_a_card_number_in_the_message_is_refused(self):
        self.write("a.py", "print('a2')\n")
        out = self.call("git_commit", message=f"fix for card {TEST_PAN}", paths=["a.py"])
        self.assertIn("payment card number", out)
        self.assertEqual(self.g("rev-list", "--count", "HEAD"), "1")

    def test_nothing_to_commit(self):
        self.assertIn("nothing to commit", self.call("git_commit", message="m", paths=["a.py"]))

    def test_the_result_says_pushing_is_the_users_job(self):
        self.write("a.py", "print('a2')\n")
        self.assertIn("Git panel", self.call("git_commit", message="m", paths=["a.py"]))


class Branch(Base):
    def test_create_switch_and_list(self):
        self.assertIn("created and switched to branch agent/fix", self.call("git_branch", action="create", name="agent/fix"))
        self.assertEqual(self.g("rev-parse", "--abbrev-ref", "HEAD"), "agent/fix")
        default = "master" if "master" in self.g("branch") else "main"
        self.assertIn(f"switched to branch {default}", self.call("git_branch", action="switch", name=default))
        self.assertIn("agent/fix", self.call("git_branch", action="list"))

    def test_bad_names_and_actions(self):
        for bad in ("-D", "a..b", "x.lock", "", "a b", "--orphan"):
            with self.subTest(name=bad):
                self.assertIn("invalid branch name", self.call("git_branch", action="create", name=bad))
        self.assertIn("action must be one of", self.call("git_branch", action="delete", name="x"))

    def test_a_conflicting_dirty_tree_refuses_instead_of_forcing(self):
        self.g("switch", "-q", "-c", "other")
        self.write("a.py", "print('on other')\n")
        self.g("commit", "-q", "-am", "other change")
        default = "master" if "master" in self.g("branch") else "main"
        self.g("switch", "-q", default)
        self.write("a.py", "print('dirty')\n")
        out = self.call("git_branch", action="switch", name="other")
        self.assertIn("error:", out)
        self.assertEqual((self.repo / "a.py").read_text(encoding="utf-8"), "print('dirty')\n", "work was not lost")


class NeverDestructiveOrOutward(Base):
    def test_no_tool_ever_sends_a_blocked_or_publishing_argument(self):
        self.write("a.py", "print('a2')\n")
        self.call("git_inspect", action="status")
        self.call("git_commit", message="m", paths=["a.py"])
        self.call("git_branch", action="create", name="agent/x")
        flat = {a for c in self.calls for a in c}
        for banned in ("push", "reset", "--hard", "clean", "-f", "--force", "update-ref", "--delete", "-D",
                       "remote", "fetch", "pull", "checkout"):
            self.assertNotIn(banned, flat)


class ApprovalAndWiring(unittest.TestCase):
    def test_command_for_shows_exactly_what_will_happen(self):
        self.assertEqual(gat.command_for("git_commit", {"message": "fix login\n\nlong detail", "paths": ["a.py", "src/b.py"]}),
                         'git commit -m "fix login" -- a.py src/b.py')
        self.assertEqual(gat.command_for("git_commit", {"message": "tidy", "all_tracked": True}), 'git commit -m "tidy" -a')
        self.assertEqual(gat.command_for("git_branch", {"action": "create", "name": "agent/x"}), "git switch -c agent/x")
        self.assertEqual(gat.command_for("git_branch", {"action": "switch", "name": "main"}), "git switch main")

    def test_invalid_calls_have_no_card_and_reads_have_none_either(self):
        self.assertIsNone(gat.command_for("git_commit", {"message": "m"}))
        self.assertIsNone(gat.command_for("git_commit", {"message": "", "paths": ["a.py"]}))
        self.assertIsNone(gat.command_for("git_commit", {"message": "m", "paths": ["."]}))
        self.assertIsNone(gat.command_for("git_branch", {"action": "create", "name": "-D"}))
        self.assertIsNone(gat.command_for("git_branch", {"action": "list"}))
        self.assertIsNone(gat.command_for("git_inspect", {"action": "status"}))

    def test_where_each_tool_is_allowed(self):
        from core import tool_surface
        from core.request_context import PERSONAL_BLOCKED_TOOLS
        from core.subagent.constants import DENIED_TOOLS
        from routes.agent import constants
        self.assertEqual(gat.GIT_GATED_TOOLS, {"git_commit", "git_branch"})
        self.assertIn("git_inspect", constants.PLAN_MODE_TOOLS)
        self.assertIn("git_inspect", constants.PARALLEL_READ_TOOLS)
        for n in ("git_commit", "git_branch"):
            self.assertNotIn(n, constants.PLAN_MODE_TOOLS, "plan mode is read-only")
            self.assertNotIn(n, constants.PARALLEL_READ_TOOLS)
            self.assertIn(n, PERSONAL_BLOCKED_TOOLS)
            self.assertIn(n, DENIED_TOOLS)
        self.assertEqual({t["function"]["name"] for t in agent_tools.AGENT_TOOLS if t["function"]["name"].startswith("git_")},
                         {"git_inspect", "git_commit", "git_branch"})
        self.assertIn("git", tool_surface.needed_families("commit this on a new branch"))
        self.assertNotIn("git", tool_surface.needed_families("fix the failing test in core/db.py"),
                         "an ordinary coding request does not pay for the git schemas")

    def test_the_loop_gates_git_commands_like_shell_commands(self):
        src = (Path(__file__).resolve().parents[1] / "routes" / "agent" / "stream.py").read_text(encoding="utf-8")
        self.assertIn("elif name in GIT_GATED_TOOLS and not personal:", src)
        self.assertIn("_gate_cmd = _git_command_for(name, args)", src)


if __name__ == "__main__":
    unittest.main()
