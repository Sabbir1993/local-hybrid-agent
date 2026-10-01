"""tests/test_eval_live.py - F1-lite: live-runner auth, sandbox prompts, env record.

The live suite needs a server+GPU, so these tests pin everything around it:
the destructive task is defused, unauthenticated runs fail with a hint (not a
mystery 401), MFA eval accounts fail fast, and the env record never raises.

Run: python -m unittest tests.test_eval_live -v
"""

import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import eval_agent as ev


class _StreamResp:
    def __init__(self, status=200, lines=()):
        self.status_code = status
        self._lines = list(lines)

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def iter_lines(self):
        return iter(self._lines)


class _Client:
    def __init__(self, stream_resp=None, post_resp=None, get_resp=None):
        self._stream = stream_resp or _StreamResp()
        self._post = post_resp
        self._get = get_resp
        self.closed = False

    def stream(self, *a, **k):
        return self._stream

    def post(self, *a, **k):
        return self._post

    def get(self, *a, **k):
        return self._get

    def close(self):
        self.closed = True


class _Resp:
    def __init__(self, status=200, payload=None):
        self.status_code = status
        self._payload = payload or {}

    def json(self):
        return self._payload


def _sse(*events):
    lines = []
    for ev_name, data in events:
        import json as _json
        lines.append(f"event: {ev_name}")
        lines.append(f"data: {_json.dumps(data)}")
    return lines


class TaskSafetyTests(unittest.TestCase):
    def test_no_task_addresses_the_whole_workspace(self):
        for t in ev.LIVE_TASKS:
            with self.subTest(task=t["name"]):
                self.assertNotIn("every python file", t["prompt"].lower())

    def test_escalation_task_is_bounded_to_scratch(self):
        t = next(t for t in ev.LIVE_TASKS if t["name"] == "escalation_task")
        self.assertIn("eval_tmp", t["prompt"])
        self.assertTrue(t.get("soft"))


class AuthTests(unittest.TestCase):
    def test_unauthenticated_run_explains_itself(self):
        rec = ev.run_live_task("http://x", ev.LIVE_TASKS[0], "auto",
                               client=_Client(_StreamResp(401, [])))
        self.assertFalse(rec["ok"])
        self.assertIn("--live-user", rec["error"])

    def test_success_path_records_tools(self):
        lines = _sse(("tool_call", {"name": "list_files"}),
                     ("delta", {"text": "here they are"}),
                     ("done", {}))
        rec = ev.run_live_task("http://x", ev.LIVE_TASKS[0], "auto",
                               client=_Client(_StreamResp(200, lines)))
        self.assertTrue(rec["ok"], rec.get("problems"))
        self.assertEqual(rec["tools"], ["list_files"])

    def test_mfa_eval_account_fails_fast(self):
        import httpx
        with mock.patch.object(httpx, "Client",
                               lambda *a, **k: _Client(post_resp=_Resp(200, {"mfa_required": True}))):
            with self.assertRaises(SystemExit) as cm:
                ev._live_login("http://x", "eval", "pw")
        self.assertIn("MFA", str(cm.exception))

    def test_bad_credentials_fail_fast(self):
        import httpx
        with mock.patch.object(httpx, "Client",
                               lambda *a, **k: _Client(post_resp=_Resp(401, {"detail": "nope"}))):
            with self.assertRaises(SystemExit):
                ev._live_login("http://x", "eval", "pw")


class EnvTests(unittest.TestCase):
    def test_env_record_never_raises(self):
        class Boom:
            def get(self, *a, **k):
                raise RuntimeError("down")
        env = ev._live_env("http://x", Boom(), "auto", 0.3)
        self.assertEqual((env["mode"], env["temperature"]), ("auto", 0.3))
        self.assertIn("ts", env)
        self.assertIn("git_sha", env)  # real sha or None, never an exception

    def test_env_captures_status_fields(self):
        c = _Client(get_resp=_Resp(200, {"profile": "qwen", "loaded": True, "zzz": 1}))
        env = ev._live_env("http://x", c, "main", 0.3)
        self.assertEqual(env.get("profile"), "qwen")
        self.assertNotIn("zzz", env)


if __name__ == "__main__":
    unittest.main()
