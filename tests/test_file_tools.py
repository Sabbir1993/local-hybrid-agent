"""tests/test_file_tools.py - read/write/append/edit/insert tools, verify loop and undo.

Covers brief acceptance cases 1-4, 11, 12. Run: python -m unittest tests.test_file_tools -v
"""

import asyncio
import sqlite3
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core import agent_tools, companion_bridge
from core.agent_tools import edit_engine, file_state
from core.agent_tools.limits import effective_max_tokens
from core.request_context import set_current_device, set_current_user

WS = r"C:\Users\[PLACEHOLDER]\projects\demo"


def full(name):
    return str(Path(WS) / name)


class FakeCompanion:
    def __init__(self):
        self.files = {}
        self.ops = []
        self.js_ok = True

    async def call(self, uid, op, params, timeout=None):
        self.ops.append(op)
        if op == "fs.read":
            return {"content": self.files.get(params["path"])}
        if op == "fs.write":
            self.files[params["path"]] = params["content"]
            return {"existed": True}
        if op == "fs.remove":
            self.files.pop(params["path"], None)
            return {"removed": True}
        if op == "fs.verify":
            return {"checked": True, "ok": self.js_ok, "detail": "SyntaxError: Unexpected token"}
        if op == "fs.list":
            return {"files": ["a.py"]}
        if op == "fs.grep":
            return {"hits": ["src/a.py:3: needle", "src/b.js:9: needle"]}
        raise RuntimeError(f"unknown op {op}")


class Base(unittest.TestCase):
    def setUp(self):
        db = sqlite3.connect(":memory:")
        db.row_factory = sqlite3.Row
        db.execute("CREATE TABLE projects (name TEXT, user_id INTEGER, device_id TEXT, workspace_dir TEXT)")
        db.execute("INSERT INTO projects VALUES ('demo', 7, 'dev1', ?)", (WS,))
        self.fc = FakeCompanion()
        self._patches = [
            mock.patch.object(agent_tools, "_projects_db", db),
            mock.patch.object(companion_bridge, "is_connected", lambda uid: uid == 7),
            mock.patch.object(companion_bridge, "call", self.fc.call),
            mock.patch.dict(agent_tools._active_project, {"7:dev1": "demo"}, clear=True),
            mock.patch.dict(agent_tools._ws_changes, {}, clear=True),
            mock.patch.dict(agent_tools._file_diffs, {}, clear=True),
            mock.patch.dict(file_state._read_sets, {}, clear=True),
            mock.patch.dict(file_state._undo, {}, clear=True),
            mock.patch.dict(file_state._verify, {}, clear=True),
        ]
        for p in self._patches:
            p.start()
        set_current_user(7)
        set_current_device("dev1")

    def tearDown(self):
        for p in self._patches:
            p.stop()
        set_current_user(None)
        set_current_device(None)

    def run_(self, coro):
        return asyncio.run(coro)

    def call(self, name, **args):
        return self.run_(agent_tools.TOOL_IMPLS[name](args))


class EngineTests(unittest.TestCase):
    def test_prefixes_stripped_only_when_every_line_has_one(self):
        s, hit = edit_engine.strip_line_prefixes("    12\tfoo\n    13\tbar")
        self.assertEqual((s, hit), ("foo\nbar", True))
        s, hit = edit_engine.strip_line_prefixes("12 apples\nthree")
        self.assertFalse(hit)

    def test_unique_and_ambiguous_and_missing(self):
        text = "a = 1\nb = 2\na = 1\n"
        with self.assertRaises(edit_engine.EditError) as cm:
            edit_engine.apply_edit(text, "a = 1", "a = 9")
        self.assertIn("2 times", str(cm.exception))
        self.assertIn("lines 1, 3", str(cm.exception))
        self.assertEqual(edit_engine.apply_edit(text, "a = 1", "a = 9", True)["count"], 2)
        with self.assertRaises(edit_engine.EditError) as cm:
            edit_engine.apply_edit(text, "b = 3", "b = 4")
        self.assertIn("not found", str(cm.exception))
        self.assertIn("line 2", str(cm.exception), "closest candidate is suggested")

    def test_noop_rejected(self):
        with self.assertRaises(edit_engine.EditError):
            edit_engine.apply_edit("x\n", "x", "x")

    def test_whitespace_fallback_reindents(self):
        text = "class A:\n    def f(self):\n        return 1\n"
        # the model dropped the indentation of the block it copied
        res = edit_engine.apply_edit(text, "def f(self):\n    return 1", "def f(self):\n    return 2")
        self.assertEqual(res["method"], "whitespace")
        self.assertEqual(res["text"], "class A:\n    def f(self):\n        return 2\n")

    def test_whitespace_fallback_refuses_ambiguity(self):
        text = "  x = 1\n\tx = 1\n"
        with self.assertRaises(edit_engine.EditError) as cm:
            edit_engine.apply_edit(text, "x  =  1", "x = 2")
        self.assertIn("matches 2 places", str(cm.exception))

    def test_crlf_files_keep_their_endings(self):
        text = "one\r\ntwo\r\nthree\r\n"
        res = edit_engine.apply_edit(text, "one\ntwo", "ONE\nTWO")
        self.assertEqual(res["text"], "ONE\r\nTWO\r\nthree\r\n")

    def test_numbered_window_and_cap(self):
        text = "\n".join(f"line {i}" for i in range(1, 501)) + "\n"
        v = edit_engine.numbered(text, 1, 200, 20000)
        self.assertEqual((v["first"], v["last"], v["total"], v["next_offset"]), (1, 200, 500, 201))
        self.assertTrue(v["text"].startswith("     1\tline 1"))
        v = edit_engine.numbered(text, 481, 200, 20000)
        self.assertEqual((v["last"], v["next_offset"]), (500, None))
        v = edit_engine.numbered(text, 1, 200, 300)
        self.assertLess(v["last"], 200, "size cap shortens the window")
        self.assertEqual(v["next_offset"], v["last"] + 1)

    def test_append_boundary(self):
        self.assertEqual(edit_engine.append_text("a", "b"), "a\nb")
        self.assertEqual(edit_engine.append_text("a\n", "b"), "a\nb")
        self.assertEqual(edit_engine.append_text(None, "b"), "b")

    def test_insert_positions(self):
        self.assertEqual(edit_engine.insert_at_line("a\nb\n", 1, "X")["text"], "X\na\nb\n")
        self.assertEqual(edit_engine.insert_at_line("a\nb\n", 3, "X")["text"], "a\nb\nX\n")
        with self.assertRaises(edit_engine.EditError):
            edit_engine.insert_at_line("a\nb\n", 9, "X")


class DiffEngineTests(unittest.TestCase):
    """E-edit tier 4: models write unified diffs more reliably than they copy
    blocks. apply_diff parses diff -u / git patches and applies each hunk; it
    never falls back silently - a hunk that does not apply fails the WHOLE
    call (atomicity), telling the model which hunk drifted."""

    FILE = ("import os\n"
            "import sys\n"
            "\n"
            "def alpha():\n"
            "    return 1\n"
            "\n"
            "def beta():\n"
            "    return 2\n"
            "\n"
            "def gamma():\n"
            "    return 3\n")

    DIFF = ("--- a/m.py\n"
            "+++ b/m.py\n"
            "@@ -1,5 +1,5 @@\n"
            " import os\n"
            "-import sys\n"
            "+import math\n"
            " \n"
            " def alpha():\n"
            "     return 1\n"
            "@@ -7,3 +7,4 @@\n"
            " def beta():\n"
            "     return 2\n"
            "+    return 21\n")

    def test_simple_diff_applies(self):
        res = edit_engine.apply_diff(self.FILE, self.DIFF)
        self.assertIn("import math", res["text"])
        self.assertNotIn("import sys", res["text"])
        self.assertIn("return 21", res["text"])
        self.assertEqual(res["hunks"], 2)
        self.assertEqual(res["method"], "diff")

    def test_diff_without_file_headers_applies(self):
        # models routinely omit the ---/+++ lines; @@ hunks alone are enough
        res = edit_engine.apply_diff(self.FILE, "@@ -2 +2 @@\n-import sys\n+import math\n")
        self.assertIn("import math", res["text"])

    def test_diff_crlf_file_keeps_endings(self):
        crlf = self.FILE.replace("\n", "\r\n")
        res = edit_engine.apply_diff(crlf, self.DIFF)
        self.assertIn("import math\r\n", res["text"])
        # every CR must be part of a CRLF pair: no stray \r left from a
        # LF-joined edit spliced into a CRLF file (blank lines legitimately
        # read "\r\n\r\n", so the invariant is per-CR, not per-substring)
        self.assertEqual(res["text"].count("\r"), res["text"].count("\r\n"))

    def test_diff_context_drift_fails_loudly(self):
        # the file changed since the model read it (line drifted): the hunk's
        # context lines don't match. Must raise, never guess.
        drifted = self.FILE.replace("    return 1\n", "    return 100\n")
        with self.assertRaises(edit_engine.EditError) as cm:
            edit_engine.apply_diff(drifted, self.DIFF)
        self.assertIn("hunk 1", str(cm.exception))
        self.assertIn("drifted", str(cm.exception))

    def test_diff_partial_failure_is_atomic(self):
        # the two clean hunks verify, the appended garbage hunk does not: the
        # call fails as a whole, so the file is never left half-patched
        bad = self.DIFF + "@@ -99,2 +99,2 @@\n totally\n absent\n"
        before = self.FILE
        with self.assertRaises(edit_engine.EditError) as cm:
            edit_engine.apply_diff(self.FILE, bad)
        self.assertIn("hunk 3", str(cm.exception))
        self.assertEqual(self.FILE, before, "atomic: failure leaves the input untouched")

    def test_diff_rejects_empty_and_non_diff(self):
        with self.assertRaises(edit_engine.EditError):
            edit_engine.apply_diff(self.FILE, "")
        with self.assertRaises(edit_engine.EditError) as cm:
            edit_engine.apply_diff(self.FILE, "just some text\nwith no hunks\n")
        self.assertIn("no hunks", str(cm.exception))

    def test_diff_rejects_hunk_that_only_adds_without_context(self):
        # a hunk with zero context and zero removed lines is an insert at a
        # line number - supported via context verification; garbage numbers fail
        with self.assertRaises(edit_engine.EditError):
            edit_engine.apply_diff(self.FILE, "@@ -99,0 +99,1 @@\n+ghost\n")

    def test_diff_hunk_at_exact_line_without_full_context(self):
        # hunk claims to start at line 4 but context is only 1 line: line
        # numbers verify the anchor when the trimmed context is short
        res = edit_engine.apply_diff(self.FILE, "@@ -4,2 +4,2 @@\n def alpha():\n-    return 1\n+    return 10\n")
        self.assertIn("return 10", res["text"])

    def test_diff_strips_line_number_prefixes(self):
        # model pasted read_file output as the diff; prefix stripper already
        # exists for old_string - the diff path must benefit too
        prefixed = "\n".join(f"  {i + 1}\t{ln}" for i, ln in enumerate(self.DIFF.split("\n")))
        res = edit_engine.apply_diff(self.FILE, prefixed)
        self.assertIn("import math", res["text"])


class FuzzyTierTests(unittest.TestCase):
    """E-edit tier 3: whitespace tolerance already exists; CONTENT drift is the
    44% failure mode. A near-miss block (line changed since the model read it)
    applies when exactly one candidate is close enough (>= 0.85) - and fails
    LOUDLY when candidates are ambiguous or nothing is close."""

    FILE = ("def handler(req):\n"
            "    user = db.get_user(req.id)\n"
            "    if not user:\n"
            "        raise HttpError(404)\n"
            "    return render(user)\n")

    OLD = ("user = db.get_user(req.id)\n"
           "if not user:\n"
           "    raise HttpError(404, 'no user')\n")  # model misremembered the raise line

    def test_drifted_block_applies_with_similarity(self):
        res = edit_engine.apply_edit(self.FILE, self.OLD,
                                     "user = db.get_user(req.id)\n"
                                     "if not user:\n"
                                     "    raise HttpError(404)\n"
                                     "    log.warning('missing')\n")
        self.assertEqual(res["method"], "fuzzy")
        self.assertIn("log.warning", res["text"])
        self.assertNotIn("'no user'", res["text"], "the drifted old line is replaced, not kept")
        self.assertIn("        log.warning('missing')", res["text"],
                      "replacement is reindented into the file's block")

    def test_fuzzy_refuses_two_similar_candidates(self):
        twin = ("def handler(req):\n"
                "    user = db.get_user(req.id)\n"
                "    if not user:\n"
                "        raise HttpError(404)\n"
                "    return render(user)\n"
                "def handler2(req):\n"
                "    user = db.get_user(req.id)\n"
                "    if not user:\n"
                "        raise HttpError(404)\n"
                "    return render(user)\n")
        with self.assertRaises(edit_engine.EditError) as cm:
            edit_engine.apply_edit(twin, self.OLD, "replaced\n")
        self.assertIn("2 places", str(cm.exception))

    def test_fuzzy_refuses_distant_block(self):
        with self.assertRaises(edit_engine.EditError) as cm:
            edit_engine.apply_edit(self.FILE, "def totally_other(a, b, c):\n    return zzz\n", "x\n")
        self.assertIn("not found", str(cm.exception))
        self.assertIn("read_file", str(cm.exception), "the message says how to recover")

    def test_near_miss_below_threshold_suggests_close_lines(self):
        # right shape, wrong code: mean similarity well under the 0.85 gate, so
        # NOT applied - but the closest real lines are named so the model can
        # copy them verbatim instead of guessing again
        with self.assertRaises(edit_engine.EditError) as cm:
            edit_engine.apply_edit(self.FILE,
                                   "def handler(req):\n"
                                   "    account = db.lookup(req.owner)\n"
                                   "    if account is None:\n"
                                   "    return render(account)\n",
                                   "x\n")
        msg = str(cm.exception)
        self.assertIn("not found", msg)
        self.assertIn("Closest lines", msg)

    def test_fuzzy_never_applies_to_single_line_drift_silently(self):
        # single-line old_string has no structure: typo tolerance on one line
        # would silently corrupt code. Only >=2-line blocks qualify.
        with self.assertRaises(edit_engine.EditError):
            edit_engine.apply_edit(self.FILE, "user = db.get_user(req.ids)\n", "x = 1\n")

    def test_fuzzy_reindents_like_whitespace_tier(self):
        # the model wrote the block unindented AND drifted the last line:
        # whitespace tier can't match (content), fuzzy must, and reindent
        res = edit_engine.apply_edit(self.FILE,
                                     "user = db.get_user(req.id)\nif not user:\n    raise HttpError(404, 'gone')\n",
                                     "user = db.get_user(req.id)\nif not user:\n    raise PermissionError()\n")
        self.assertEqual(res["method"], "fuzzy")
        self.assertIn("        raise PermissionError()", res["text"],
                      "replacement lands inside the file's indentation")


class DiffToolTests(Base):
    def test_edit_file_accepts_diff_argument(self):
        self.fc.files[full("m.py")] = "a = 1\nb = 2\n"
        self.call("read_file", path="m.py")
        out = self.call("edit_file", path="m.py",
                        diff="--- a/m.py\n+++ b/m.py\n@@ -1,2 +1,2 @@\n-a = 1\n+a = 11\n b = 2\n")
        self.assertIn("edited", out)
        self.assertEqual(self.fc.files[full("m.py")], "a = 11\nb = 2\n")

    def test_edit_file_diff_and_old_string_are_exclusive(self):
        self.fc.files[full("m.py")] = "a = 1\nb = 2\n"
        self.call("read_file", path="m.py")
        out = self.call("edit_file", path="m.py", old_string="a = 1", new_string="a = 2",
                        diff="@@ -1 +1 @@\n-a = 1\n+a = 11\n")
        self.assertTrue(out.startswith("error:"))
        self.assertIn("either", out)

    def test_edit_file_diff_requires_read_first(self):
        self.fc.files[full("m.py")] = "a = 1\n"
        out = self.call("edit_file", path="m.py", diff="@@ -1 +1 @@\n-a = 1\n+a = 2\n")
        self.assertTrue(out.startswith("error:"))
        self.assertIn("read", out)

    def test_edit_file_diff_error_is_reported_not_raised(self):
        self.fc.files[full("m.py")] = "a = 1\nb = 2\n"
        self.call("read_file", path="m.py")
        out = self.call("edit_file", path="m.py", diff="@@ -9 +9 @@\n-ghost\n+x\n")
        self.assertTrue(out.startswith("error:"))
        self.assertIn("hunk", out)


class SchemaContractTests(unittest.TestCase):
    def test_edit_file_schema_documents_diff(self):
        schema = next(t for t in agent_tools.AGENT_TOOLS
                      if t["function"]["name"] == "edit_file")
        props = schema["function"]["parameters"]["properties"]
        self.assertIn("diff", props, "diff must be an advertised argument")
        self.assertNotIn("diff", schema["function"]["parameters"].get("required", []),
                         "diff is optional - old/new path must keep working")
        self.assertIn("diff", schema["function"]["description"])

    def test_repair_accepts_diff_only_edit(self):
        from core.agent_loop.repair import validate_and_repair_tool_args
        args, err = validate_and_repair_tool_args(
            "edit_file", {"path": "m.py", "diff": "@@ -1 +1 @@\n-a\n+b\n"})
        self.assertIsNone(err)
        self.assertEqual(args["diff"], "@@ -1 +1 @@\n-a\n+b\n")

    def test_repair_still_requires_content_without_diff(self):
        from core.agent_loop.repair import validate_and_repair_tool_args
        _, err = validate_and_repair_tool_args("edit_file", {"path": "m.py"})
        self.assertIsNotNone(err)
        self.assertIn("old_string", err)


class ToolTests(Base):
    def test_write_refuses_existing_unless_overwrite(self):
        self.call("write_file", path="a.py", content="x = 1\n")
        out = self.call("write_file", path="a.py", content="x = 2\n")
        self.assertTrue(out.startswith("error:"))
        self.assertIn("append_file", out)
        self.assertEqual(self.fc.files[full("a.py")], "x = 1\n")
        self.assertIn("overwrote", self.call("write_file", path="a.py", content="x = 2\n", overwrite=True))

    def test_write_size_cap_points_to_skeleton(self):
        out = self.call("write_file", path="big.py", content="x = 1\n" * 4000)
        self.assertTrue(out.startswith("error:"))
        self.assertIn("skeleton", out)
        self.assertNotIn(full("big.py"), self.fc.files)

    def test_chunked_build_of_a_large_file(self):
        # acceptance 1: a 1500-line file built only from small calls
        self.call("write_file", path="app.py", content="import os\n\n\n# TODO: body\n")
        for i in range(30):
            body = "".join(f"def f{i}_{j}():\n    return {j}\n\n" for j in range(17))
            out = self.call("append_file", path="app.py", content=body)
            self.assertIn("verify: OK", out)
        text = self.fc.files[full("app.py")]
        self.assertGreater(text.count("\n"), 1500)
        compile(text, "app.py", "exec")

    def test_append_needs_existing_file(self):
        out = self.call("append_file", path="nope.py", content="x = 1\n")
        self.assertTrue(out.startswith("error:"))
        self.assertIn("write_file", out)

    def test_legacy_append_flag_still_builds_files(self):
        self.call("write_file", path="l.txt", content="one")
        self.call("write_file", path="l.txt", content="two", append=True)
        self.assertEqual(self.fc.files[full("l.txt")], "one\ntwo")

    def test_edit_requires_read_first(self):
        self.fc.files[full("old.py")] = "x = 1\n"
        out = self.call("edit_file", path="old.py", old_string="x = 1", new_string="x = 2")
        self.assertTrue(out.startswith("error:"))
        self.assertIn("read", out)
        self.call("read_file", path="old.py")
        out = self.call("edit_file", path="old.py", old_string="x = 1", new_string="x = 2")
        self.assertIn("edited old.py", out)
        self.assertEqual(self.fc.files[full("old.py")], "x = 2\n")

    def test_file_created_this_session_can_be_edited(self):
        self.call("write_file", path="n.py", content="a = 1\n")
        self.assertIn("edited", self.call("edit_file", path="n.py", old_string="a = 1", new_string="a = 2"))

    def test_edit_in_5000_line_file_stays_small(self):
        # acceptance 2: the tool result for one edit is a few lines, not the file
        self.fc.files[full("big.py")] = "".join(f"v{i} = {i}\n" for i in range(5000))
        self.call("read_file", path="big.py", offset=2500, limit=5)
        out = self.call("edit_file", path="big.py", old_string="v2501 = 2501", new_string="v2501 = 0")
        self.assertLess(len(out), 1500)
        self.assertIn("line 2502", out)

    def test_non_unique_edit_recovers(self):
        # acceptance 3
        self.fc.files[full("d.py")] = "x = 1\ny = 2\nx = 1\n"
        self.call("read_file", path="d.py")
        out = self.call("edit_file", path="d.py", old_string="x = 1", new_string="x = 5")
        self.assertIn("2 times", out)
        out = self.call("edit_file", path="d.py", old_string="y = 2\nx = 1", new_string="y = 2\nx = 5")
        self.assertIn("edited", out)
        self.assertEqual(self.fc.files[full("d.py")], "x = 1\ny = 2\nx = 5\n")

    def test_wrong_indentation_edit_succeeds(self):
        # acceptance 4
        self.fc.files[full("i.py")] = "def f():\n    if x:\n        return 1\n"
        self.call("read_file", path="i.py")
        out = self.call("edit_file", path="i.py", old_string="if x:\n    return 1", new_string="if x:\n    return 2")
        self.assertIn("ignoring whitespace", out)
        self.assertEqual(self.fc.files[full("i.py")], "def f():\n    if x:\n        return 2\n")

    def test_read_pagination_footer(self):
        self.fc.files[full("r.txt")] = "".join(f"l{i}\n" for i in range(1, 451))
        out = self.call("read_file", path="r.txt")
        self.assertIn("lines 1-450 of 450", out)    # the default slice is 500 lines
        out = self.call("read_file", path="r.txt", limit=200)
        self.assertIn("lines 1-200 of 450", out)
        self.assertIn("offset=201", out)
        out = self.call("read_file", path="r.txt", offset=401)
        self.assertIn("lines 401-450 of 450", out)
        self.assertNotIn("more", out.rsplit("[", 1)[1])

    def test_traversal_rejected(self):
        # acceptance 11
        for name in ("write_file", "append_file", "read_file", "insert_at_line"):
            with self.assertRaises(PermissionError):
                self.call(name, path="../../etc/passwd", content="x", text="x", line=1)
        self.assertFalse([k for k in self.fc.files if "passwd" in k])

    def test_insert_at_line(self):
        self.fc.files[full("t.txt")] = "a\nb\n"
        self.call("read_file", path="t.txt")
        out = self.call("insert_at_line", path="t.txt", line=2, text="X")
        self.assertIn("inserted 1 line", out)
        self.assertEqual(self.fc.files[full("t.txt")], "a\nX\nb\n")

    def test_list_and_grep_take_path_and_glob(self):
        out = self.call("grep", pattern="needle", glob="*.py")
        self.assertEqual(out, "src/a.py:3: needle")
        out = self.call("grep", pattern="needle", path="pkg", glob="*.js")
        self.assertEqual(out, "pkg/src/b.js:9: needle")
        self.assertEqual(self.call("list_files", path="sub"), "sub/a.py")


class VerifyAndUndoTests(Base):
    def setUp(self):
        super().setUp()
        # These tests assert "Attempt N/M" wording, so they pin the M they count against.
        # Reading ambient config here made them fail when the shipped default moved 3 -> 5;
        # product config must never be dictated by test wording.
        from core.small_model import APP_CONFIG
        self._agent_saved = dict(APP_CONFIG.get("agent") or {})
        APP_CONFIG.setdefault("agent", {})["verify_max_retries"] = 3

    def tearDown(self):
        from core.small_model import APP_CONFIG
        APP_CONFIG["agent"] = self._agent_saved
        super().tearDown()

    def test_syntax_error_reported_and_fixed(self):
        out = self.call("write_file", path="bad.py", content="def f(:\n    pass\n")
        self.assertIn("verify: FAILED", out)
        self.assertIn("line 1", out)
        out = self.call("edit_file", path="bad.py", old_string="def f(:", new_string="def f():")
        self.assertIn("verify: OK", out)

    def test_json_checked(self):
        self.assertIn("verify: FAILED", self.call("write_file", path="c.json", content='{"a": 1,}'))
        self.assertIn("verify: OK", self.call("write_file", path="d.json", content='{"a": 1}'))

    def test_javascript_uses_companion(self):
        self.fc.js_ok = False
        self.assertIn("verify: FAILED", self.call("write_file", path="a.js", content="function ("))
        self.assertIn("fs.verify", self.fc.ops)

    def test_jsx_checked(self):
        self.assertIn("verify: FAILED", self.call("write_file", path="b.jsx", content="function B() { return <button>; }"))
        self.assertIn("verify: OK", self.call("write_file", path="c.jsx", content="function B() { return <button>OK</button>; }"))

    def test_unknown_types_are_not_checked(self):
        self.assertNotIn("verify", self.call("write_file", path="n.txt", content="hello"))

    def test_stops_after_max_retries_and_restores_last_good(self):
        self.call("write_file", path="g.py", content="x = 1\n")
        outs = []
        for i in range(3):
            outs.append(self.call("edit_file", path="g.py", old_string="x = 1" if i == 0 else f"x = ({i}",
                                  new_string=f"x = ({i + 1}"))
        self.assertIn("Attempt 1/3", outs[0])
        self.assertIn("Attempt 2/3", outs[1])
        self.assertIn("restored", outs[2])
        self.assertEqual(self.fc.files[full("g.py")], "x = 1\n")

    def test_no_auto_restore_for_chunked_writes(self):
        self.call("write_file", path="h.py", content="class A:\n")           # incomplete but only a first chunk
        out = ""
        for _ in range(3):
            out = self.call("append_file", path="h.py", content="    def f(self):")
        self.assertIn("Stop and tell the user", out)
        self.assertIn("def f", self.fc.files[full("h.py")])

    def test_revert_undoes_last_edit_then_earlier_ones(self):
        # acceptance 12
        self.call("write_file", path="u.py", content="a = 1\n")
        self.call("edit_file", path="u.py", old_string="a = 1", new_string="a = 2")
        self.call("edit_file", path="u.py", old_string="a = 2", new_string="a = 3")
        self.assertIn("reverted", self.call("revert", path="u.py"))
        self.assertEqual(self.fc.files[full("u.py")], "a = 2\n")
        self.assertIn("reverted", self.call("revert", path="u.py", steps=2))
        self.assertNotIn(full("u.py"), self.fc.files, "a file the agent created is deleted by undoing its creation")

    def test_revert_to_start(self):
        self.fc.files[full("s.py")] = "orig = 1\n"
        self.call("read_file", path="s.py")
        self.call("edit_file", path="s.py", old_string="orig = 1", new_string="orig = 2")
        self.call("edit_file", path="s.py", old_string="orig = 2", new_string="orig = 3")
        self.assertIn("before this session", self.call("revert", path="s.py", to_start=True))
        self.assertEqual(self.fc.files[full("s.py")], "orig = 1\n")

    def test_undo_stack_is_bounded(self):
        for i in range(file_state.MAX_UNDO_PER_FILE + 10):
            file_state.push_undo("p", f"v{i}")
        self.assertEqual(file_state.undo_depth("p"), file_state.MAX_UNDO_PER_FILE)


class LaneCapTests(unittest.TestCase):
    def test_executor_is_capped_even_when_settings_are_unlimited(self):
        self.assertEqual(effective_max_tokens(-1, "executor"), 4096)
        self.assertEqual(effective_max_tokens(0, "executor"), 4096)

    def test_user_limit_can_only_lower_the_cap(self):
        self.assertEqual(effective_max_tokens(2000, "executor"), 2000)
        self.assertEqual(effective_max_tokens(30000, "executor"), 4096)
        self.assertEqual(effective_max_tokens(-1, "main"), 16000)

    def test_cloud_models_are_left_as_set(self):
        self.assertEqual(effective_max_tokens(-1, "main", cloud=True), -1)
        self.assertEqual(effective_max_tokens(8000, "executor", cloud=True), 8000)

    def test_executor_thinking_is_capped(self):
        from core import reasoning
        from core.agent_tools.limits import executor_effort_ceiling
        from core.small_model import APP_CONFIG
        # pin the default: ambient config now ships "medium", which is a product choice
        with mock.patch.dict(APP_CONFIG, {"agent": {}}):
            self.assertEqual(executor_effort_ceiling(), "low")
        self.assertEqual(reasoning.cap("high", "low"), "low")
        self.assertEqual(reasoning.cap("none", "low"), "none")
        self.assertEqual(reasoning.cap("low", "medium"), "low")
        self.assertIsNone(reasoning.cap(None, "low"))
        with mock.patch.dict("core.small_model.APP_CONFIG", {"agent": {"executor_max_effort": "bogus"}}):
            self.assertEqual(executor_effort_ceiling(), "low")

    def test_unknown_lane_is_untouched(self):
        self.assertEqual(effective_max_tokens(-1, "direct"), -1)
        self.assertEqual(effective_max_tokens(500, "direct"), 500)


class ToolArgValidationTests(Base):
    """run_tool() is the single choke point every lane passes through (main loop, router,
    sub-agents, chat), so argument validation belongs there -- before the registry dispatch.

    The registry mirrors every builtin (core/registry.py:bootstrap_builtin_tools, called at
    startup), so validating after the registry branch made the check unreachable for every
    builtin tool: a call whose arguments were cut off mid-JSON reached the file tools anyway
    and could create an empty file, defeating the 'nothing was changed' promise. These tests
    drive run_tool() rather than TOOL_IMPLS directly, and assert the registry really does hold
    the builtins, so the old ordering cannot come back unnoticed.
    """

    def setUp(self):
        super().setUp()
        from core.registry import bootstrap_builtin_tools, registry
        bootstrap_builtin_tools()

    def call_tool(self, name, **args):
        from core.agent_loop.execution import run_tool
        return self.run_(run_tool(name, args))

    def test_builtins_are_registered_so_this_test_covers_the_registry_path(self):
        # if this fails, run_tool() would have taken the TOOL_IMPLS fallback instead and
        # these tests would no longer prove anything about the registry dispatch
        from core.registry import registry
        for name in ("write_file", "append_file", "edit_file", "insert_at_line"):
            self.assertIsNotNone(registry.get(name), f"{name} must be registered for this test to mean anything")

    def test_arguments_cut_off_mid_json_change_nothing(self):
        from core.agent_loop.repair import INVALID_JSON_KEY
        out = self.call_tool("write_file", **{INVALID_JSON_KEY: '{"path": "cut.py", "content": "def f()'})
        self.assertTrue(out.startswith("error:"), out)
        self.assertIn("Nothing was changed", out)
        self.assertNotIn(full("cut.py"), self.fc.files)
        self.assertEqual([op for op in self.fc.ops if op == "fs.write"], [])

    def test_missing_path_is_reported_rather_than_written(self):
        # read_file and revert have no content-shape guess (repair.py only guesses for
        # write_file), so a missing path is a hard error naming the field
        for tool in ("read_file", "revert"):
            out = self.call_tool(tool)
            self.assertTrue(out.startswith("error:"), out)
            self.assertIn("path required", out)
            self.assertIn(tool, out)

    def test_write_file_filename_guess_is_visible_not_silent(self):
        # repair.py:26-33 does guess a name from the content shape when write_file has no
        # path, because the agent targets small local models that routinely omit it. That is
        # only acceptable because the chosen name is reported back in the tool result, so the
        # model and the user both see which file was created. Pinned here so it can never
        # become a silent write. Whether the guess should exist at all is a measurement
        # question, not a correctness one -- see the eval harness phase.
        out = self.call_tool("write_file", content="x = 1\n")
        self.assertEqual(self.fc.files[full("output.txt")], "x = 1\n")
        self.assertIn("output.txt", out)
        self.assertIn("created", out)

    def test_missing_content_is_reported(self):
        out = self.call_tool("write_file", path="empty.py")
        self.assertTrue(out.startswith("error:"), out)
        self.assertIn("content required", out)
        self.assertNotIn(full("empty.py"), self.fc.files)

    def test_edit_file_missing_new_string_is_reported(self):
        self.call_tool("write_file", path="e.py", content="a = 1\n")
        out = self.call_tool("edit_file", path="e.py", old_string="a = 1")
        self.assertTrue(out.startswith("error:"), out)
        self.assertIn("new_string required", out)
        self.assertEqual(self.fc.files[full("e.py")], "a = 1\n")

    def test_traversal_is_refused_at_the_choke_point(self):
        out = self.call_tool("write_file", path="../escape.py", content="x = 1\n")
        self.assertTrue(out.startswith("error:"), out)
        self.assertIn("traverse", out)

    def test_a_valid_call_still_executes(self):
        out = self.call_tool("write_file", path="ok.py", content="x = 1\n")
        self.assertIn("verify: OK", out)
        self.assertEqual(self.fc.files[full("ok.py")], "x = 1\n")

    def test_aliases_are_still_normalised(self):
        # validate_and_repair_tool_args is what collapses file->path and code->content;
        # the fix must not have made it a reject-only check
        out = self.call_tool("write_file", file="alias.py", code="y = 2\n")
        self.assertIn("verify: OK", out)
        self.assertEqual(self.fc.files[full("alias.py")], "y = 2\n")


class RunPythonIsolationTests(Base):
    """D5: run_python wrote every script to ws/_agent_run.py, so two concurrent
    runs in one workspace overwrote each other's code -- and the companion's
    on-disk approval check (script bytes == approved code) raced the overwrite.
    Each call now gets its own script, and the script is removed afterwards."""

    def setUp(self):
        super().setUp()
        self.shell_cmds = []

        async def fake_call(uid, op, params, timeout=None):
            self.fc.ops.append(op)
            if op == "fs.write":
                self.fc.files[params["path"]] = params["content"]
                return {"existed": True}
            if op == "fs.remove":
                self.fc.files.pop(params["path"], None)
                return {"removed": True}
            if op == "shell.run":
                self.shell_cmds.append(params["command"])
                return {"stdout": "ok", "stderr": "", "exit_code": 0}
            raise RuntimeError(f"unknown op {op}")

        self._bridge = mock.patch.object(companion_bridge, "call", fake_call)
        self._bridge.start()

    def tearDown(self):
        # inner patch off FIRST (restores Base's file-ops fake), then Base's own
        # teardown. Stopping in the other order (addCleanup runs after tearDown)
        # would resurrect this test's fake under later test modules.
        self._bridge.stop()
        super().tearDown()

    def test_concurrent_calls_get_distinct_scripts(self):
        from core.agent_tools.file_ops import tool_run_python

        async def both():
            return await asyncio.gather(
                tool_run_python({"code": "print('aaa')"}),
                tool_run_python({"code": "print('bbb')"}),
            )
        a, b = self.run_(both())
        self.assertIn("exit code 0", a)
        self.assertIn("exit code 0", b)
        # the fixed shared name must never be written anymore
        self.assertNotIn(full("_agent_run.py"), self.fc.files)
        # each shell.run names its own script, and the two differ
        self.assertEqual(len(self.shell_cmds), 2)
        self.assertNotEqual(self.shell_cmds[0], self.shell_cmds[1])
        for cmd in self.shell_cmds:
            self.assertRegex(cmd, r'^python "_agent_run_[0-9a-f]{8}\.py"$')

    def test_script_removed_after_run(self):
        from core.agent_tools.file_ops import tool_run_python
        self.run_(tool_run_python({"code": "print('x')"}))
        leftovers = [p for p in self.fc.files if Path(p).name.startswith("_agent_run_")]
        self.assertEqual(leftovers, [], f"script litter left behind: {leftovers}")
        self.assertIn("fs.remove", self.fc.ops)


class PreSaveSyntaxValidationTests(Base):
    def test_pre_save_syntax_validation_rejects_broken_python(self):
        res = self.call("write_file", path="broken.py", content="def invalid(:\n  pass", pre_save_check=True)
        self.assertIn("error: pre-save syntax validation failed", res)
        self.assertNotIn(full("broken.py"), self.fc.files, "broken file must not be committed to disk")

    def test_pre_save_syntax_validation_passes_valid_code(self):
        res = self.call("write_file", path="valid.py", content="def valid():\n  pass\n", pre_save_check=True)
        self.assertIn("wrote", res)
        self.assertIn(full("valid.py"), self.fc.files)

    def test_pre_save_syntax_validation_rejects_broken_edit(self):
        self.call("write_file", path="test_edit.py", content="a = 1\nb = 2\n")
        res = self.call("edit_file", path="test_edit.py", old_string="b = 2", new_string="b = (", pre_save_check=True)
        self.assertIn("error: pre-save syntax validation failed", res)
        self.assertEqual(self.fc.files[full("test_edit.py")], "a = 1\nb = 2\n", "file on disk must remain pristine")


if __name__ == "__main__":
    unittest.main()

