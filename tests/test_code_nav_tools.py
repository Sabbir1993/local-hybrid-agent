"""tests/test_code_nav_tools.py - find_symbol / find_references / file_outline over a device workspace.

The server holds no workspace: sources come from the companion's fs.read_many and live only in memory. A fake
device implements that op with the same contract as companion/fsops.js (stamps, `known` skipping, batching).

Run: python -m unittest tests.test_code_nav_tools -v
"""

import asyncio
import sqlite3
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core import agent_tools, companion_bridge
from core.code_intel import ast_index, remote
from core.request_context import set_current_device, set_current_user

WS = r"C:\Users\[PLACEHOLDER]\projects\demo"

AUTH_PY = '''class Api:
    def authenticate(self, token):
        """Check a token."""
        return check(token)


def check(token):
    return bool(token)


def login(user):
    return Api().authenticate(user)
'''
UTIL_TS = '''export function check(x: string) { return x.length > 0; }
export class Box {
  open() { return check("a"); }
}
'''


class FakeDevice:
    """fs.read_many with the companion's contract."""

    def __init__(self, files, batch=100):
        self.files = dict(files)
        self.mtime = {rel: 1000 for rel in files}
        self.batch = batch
        self.calls = []

    def put(self, rel, text):
        self.files[rel] = text
        self.mtime[rel] = self.mtime.get(rel, 1000) + 1

    def drop(self, rel):
        self.files.pop(rel, None)

    def stamp(self, rel):
        return [self.mtime[rel], len(self.files[rel])]

    async def call(self, uid, op, params, timeout=None):
        if op != "fs.read_many":
            raise RuntimeError(f"unknown op {op}")
        self.calls.append(params)
        exts = tuple(params["exts"])
        every = sorted(r for r in self.files if r.endswith(exts))
        out, more = [], False
        for rel in every:
            if params["known"].get(rel) == self.stamp(rel):
                continue
            if len(out) >= self.batch:
                more = True
                continue
            out.append({"rel": rel, "stamp": self.stamp(rel), "text": self.files[rel]})
        return {"files": out, "all": every, "more": more, "truncated": False, "skipped_large": 0}


class Base(unittest.TestCase):
    files = {"auth.py": AUTH_PY, "web/util.ts": UTIL_TS, "README.md": "# not code\n"}

    def setUp(self):
        db = sqlite3.connect(":memory:")
        db.row_factory = sqlite3.Row
        db.execute("CREATE TABLE projects (name TEXT, user_id INTEGER, device_id TEXT, workspace_dir TEXT)")
        db.execute("INSERT INTO projects VALUES ('demo', 7, 'dev1', ?)", (WS,))
        self.dev = FakeDevice(self.files)
        remote.clear()
        self._patches = [
            mock.patch.object(agent_tools, "_projects_db", db),
            mock.patch.object(companion_bridge, "is_connected", lambda uid: uid == 7),
            mock.patch.object(companion_bridge, "call", self.dev.call),
            mock.patch.dict(agent_tools._active_project, {"7:dev1": "demo"}, clear=True),
        ]
        for p in self._patches:
            p.start()
        set_current_user(7)
        set_current_device("dev1")

    def tearDown(self):
        for p in self._patches:
            p.stop()
        remote.clear()
        set_current_user(None)
        set_current_device(None)

    def call(self, tool, **args):
        return asyncio.run(agent_tools.TOOL_IMPLS[tool](args))


class FindSymbol(Base):
    def test_finds_a_function_with_line_range_and_signature(self):
        out = self.call("find_symbol", name="login")
        self.assertIn("1 definition of 'login'", out)
        self.assertIn("auth.py:11-12  function  login(user)", out)

    def test_class_method_by_dotted_name_and_docstring(self):
        out = self.call("find_symbol", name="Api.authenticate")
        self.assertIn("auth.py:2-4  method  Api.authenticate(self, token)", out)
        self.assertIn("Check a token.", out)

    def test_same_name_in_two_languages_and_path_narrowing(self):
        out = self.call("find_symbol", name="check")
        self.assertIn("2 definitions of 'check'", out)
        self.assertIn("auth.py:7-8", out)
        self.assertIn("web/util.ts:1-1", out)
        only = self.call("find_symbol", name="check", path="web/util.ts")
        self.assertIn("1 definition", only)
        self.assertNotIn("auth.py", only)

    def test_typescript_class_and_method(self):
        self.assertIn("web/util.ts:2-4  class  Box", self.call("find_symbol", name="Box"))
        self.assertIn("Box.open", self.call("find_symbol", name="Box.open"))

    def test_miss_suggests_similar_names_and_points_to_grep(self):
        out = self.call("find_symbol", name="authenticat")
        self.assertIn("no definition of 'authenticat'", out)
        self.assertIn("authenticate", out)
        self.assertIn("grep", out)

    def test_name_is_required(self):
        with self.assertRaises(ValueError):
            self.call("find_symbol")


class FindReferences(Base):
    def test_lists_call_sites_with_the_calling_function(self):
        out = self.call("find_references", name="check")
        self.assertIn("2 call sites of 'check'", out)
        self.assertIn("auth.py:4  in Api.authenticate", out)
        self.assertIn("web/util.ts:3  in Box.open", out)
        self.assertIn("syntactic calls only", out)

    def test_defined_but_never_called(self):
        out = self.call("find_references", name="login")
        self.assertIn("no call sites of 'login'", out)
        self.assertIn("defined at auth.py:11", out)

    def test_unknown_symbol(self):
        self.assertIn("No definition of 'ghost' was found", self.call("find_references", name="ghost"))


class FileOutline(Base):
    def test_outline_shows_structure_without_bodies(self):
        out = self.call("file_outline", path="auth.py")
        self.assertIn("auth.py: 4 symbols", out)
        self.assertIn("1-4  class  Api", out)
        self.assertIn("  2-4  method  authenticate(self, token)", out)
        self.assertNotIn("return bool", out)

    def test_absolute_device_path_is_accepted(self):
        self.assertIn("auth.py: 4 symbols", self.call("file_outline", path=WS + "\\auth.py"))

    def test_unindexed_file_is_explained(self):
        out = self.call("file_outline", path="README.md")
        self.assertIn("not indexed", out)

    def test_syntax_error_file_is_reported_not_guessed(self):
        self.dev.put("broken.py", "def oops(:\n")
        self.assertIn("syntax errors", self.call("file_outline", path="broken.py"))


class IncrementalSync(Base):
    def test_second_query_transfers_no_text_and_edits_are_seen(self):
        self.call("find_symbol", name="login")
        first = self.dev.calls[0]
        self.assertEqual(first["known"], {})
        self.assertEqual(sorted(first["exts"]), sorted(ast_index.SUPPORTED_SUFFIXES))

        self.call("find_symbol", name="login")
        self.assertEqual(set(self.dev.calls[1]["known"]), {"auth.py", "web/util.ts"},
                         "the stamps already held are sent, so the device returns no text")

        # the agent edits a file: the next query sees the new symbol without a restart
        self.dev.put("auth.py", AUTH_PY + "\n\ndef logout(user):\n    return None\n")
        self.assertIn("1 definition of 'logout'", self.call("find_symbol", name="logout"))

    def test_deleted_file_leaves_the_index(self):
        self.assertIn("web/util.ts", self.call("find_symbol", name="Box"))
        self.dev.drop("web/util.ts")
        self.assertIn("no definition of 'Box'", self.call("find_symbol", name="Box"))

    def test_batches_are_followed_until_done(self):
        self.dev.batch = 1                      # one file per round trip
        out = self.call("find_symbol", name="check")
        self.assertIn("2 definitions", out)
        self.assertGreaterEqual(len(self.dev.calls), 2)

    def test_indexes_are_per_user_and_root(self):
        async def go():
            a = await remote.sync(7, WS)
            b = await remote.sync(8, WS)
            c = await remote.sync(7, WS + "2")
            return a.index, b.index, c.index
        a, b, c = asyncio.run(go())
        self.assertIsNot(a, b)
        self.assertIsNot(a, c)

    def test_cache_is_bounded(self):
        async def go():
            for i in range(remote.MAX_ENTRIES + 5):
                await remote.sync(7, f"{WS}{i}")
        asyncio.run(go())
        self.assertLessEqual(len(remote._CACHE), remote.MAX_ENTRIES)


class Degradation(Base):
    def test_old_companion_gets_an_actionable_error(self):
        async def old(uid, op, params, timeout=None):
            raise RuntimeError("unknown op fs.read_many")
        with mock.patch.object(companion_bridge, "call", old):
            with self.assertRaises(RuntimeError) as cm:
                self.call("find_symbol", name="x")
        self.assertIn("update it", str(cm.exception))

    def test_workspace_without_supported_code(self):
        self.dev.files = {"notes.txt": "hi"}
        self.dev.mtime = {"notes.txt": 1}
        self.assertIn("no indexable code", self.call("find_symbol", name="x"))


class SourceIndexUnit(unittest.TestCase):
    def test_update_remove_retain_and_outline(self):
        ix = ast_index.SourceIndex()
        ix.update("a.py", (1, 10), b"def f():\n    return g()\n")
        ix.update("b.py", (1, 10), b"def g():\n    pass\n")
        d = ix.data()
        self.assertEqual({s["name"] for s in d["symbols"]}, {"f", "g"})
        self.assertEqual(ast_index.callers_in(d, "g")[0]["caller"], "f")
        ix.update("a.py", (2, 12), b"def h():\n    pass\n")      # edit replaces, not appends
        self.assertEqual({s["name"] for s in ix.data()["symbols"]}, {"h", "g"})
        ix.retain(["b.py"])
        self.assertIsNone(ix.status("a.py"))
        self.assertEqual([s["name"] for s in ix.outline("b.py")], ["g"])

    def test_parse_errors_are_skipped_and_named(self):
        ix = ast_index.SourceIndex()
        ix.update("bad.py", (1, 1), b"def (:\n")
        self.assertEqual(ix.status("bad.py"), "parse_error")
        self.assertIn("bad.py (parse error)", ix.data()["skipped"])
        self.assertIsNone(ix.outline("bad.py"))


class RepoMap(Base):
    def setUp(self):
        super().setUp()
        inner = self.dev.call

        async def with_listing(uid, op, params, timeout=None):
            if op == "fs.list":
                return {"files": sorted(self.dev.files)}
            if op == "fs.read":
                return {"content": None}
            return await inner(uid, op, params, timeout)
        self._wl = mock.patch.object(companion_bridge, "call", with_listing)
        self._wl.start()

    def tearDown(self):
        self._wl.stop()
        super().tearDown()

    def test_overview_ends_with_a_code_map_ranked_by_definitions(self):
        out = self.call("project_overview")
        self.assertIn("Code map", out)
        self.assertIn("auth.py: Api, check, login", out)
        self.assertLess(out.index("auth.py: Api"), out.index("web/util.ts: check, Box"),
                        "the file defining more symbols comes first")
        self.assertNotIn("authenticate", out.split("Code map")[1].splitlines()[1],
                         "methods are not listed in the map")

    def test_path_prefix_limits_the_map(self):
        out = self.call("project_overview", path="web")
        self.assertIn("web/util.ts", out.split("Code map")[1])
        self.assertNotIn("auth.py", out.split("Code map")[1])

    def test_overview_still_works_when_the_index_is_unavailable(self):
        async def listing_only(uid, op, params, timeout=None):
            if op == "fs.list":
                return {"files": sorted(self.dev.files)}
            if op == "fs.read":
                return {"content": None}
            raise RuntimeError(f"unknown op {op}")
        with mock.patch.object(companion_bridge, "call", listing_only):
            out = self.call("project_overview")
        self.assertIn("Project overview", out)
        self.assertNotIn("Code map", out)


class Wiring(unittest.TestCase):
    def test_tools_are_registered_and_exposed_where_read_tools_belong(self):
        from core.agent_loop import action_guard
        from routes.agent import constants
        for n in ("find_symbol", "find_references", "file_outline"):
            self.assertIn(n, agent_tools.TOOL_IMPLS)
            self.assertIn(n, {t["function"]["name"] for t in agent_tools.AGENT_TOOLS})
            self.assertIn(n, constants.PLAN_MODE_TOOLS, "read-only, so allowed in plan mode")
            self.assertIn(n, constants.PARALLEL_READ_TOOLS)
            self.assertIn(n, action_guard.PASSIVE_TOOLS)


if __name__ == "__main__":
    unittest.main()
