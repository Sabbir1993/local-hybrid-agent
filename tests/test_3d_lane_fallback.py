"""A 3D page that the small 3D lane model fails to deliver is redone once on the main model (routes/chat/run.py), instead of
ending in the "file was not generated" dead end. Drives the real chat_run with fake model streams."""
import asyncio
import io
import unittest
from contextlib import redirect_stderr
from types import SimpleNamespace
from unittest import mock

QUERY = "caterpillar riding a bike. make a 3D html design"
PAGE = "```html\n<!DOCTYPE html><html><body>MAIN MODEL PAGE</body></html>\n```"
NOTE = "was not generated this time"


def result(content="", tool_calls=None, reasoning=""):
    return ("result", {"content": content, "reasoning": reasoning, "tool_calls": tool_calls or [], "usage": {"completion_tokens": 7},
                       "timings": None, "finish_reason": "stop", "aborted": None})


class Lane3dFallbackTests(unittest.TestCase):
    def drive(self, lane_reply, main_reply, ensure_error=None, project=True):
        """lane_reply / main_reply: lists of (ev, val) events one model call returns, or a list of such lists for several calls."""
        from routes import chat as chat_route
        from core.lanes.creative import CreativeLane
        run = chat_route.run
        lane_client, main_client = object(), object()
        calls = {"lane": 0, "main": 0}
        sent = {"lane": [], "main": []}

        async def fake_stream(client, msgs, tools=None, **kw):
            who = "lane" if client is lane_client else "main"
            replies = lane_reply if who == "lane" else main_reply
            n = calls[who]
            calls[who] += 1
            sent[who].append([dict(m) for m in msgs])
            for ev in replies[min(n, len(replies) - 1)]:
                if ev[0] == "result" and ev[1]["content"]:
                    yield ("content_delta", ev[1]["content"])           # what a live stream sends before its result
                yield ev

        user = SimpleNamespace(id=1, is_super_admin=True, role_names=[])
        req = chat_route.ChatRunRequest(messages=[{"role": "user", "content": QUERY}], web_search=False, kb="off", session_id=91)
        picked = CreativeLane("html3d", lane_client, "Test-3D-Coder", 4000)
        out = []
        ensure = mock.AsyncMock(side_effect=ensure_error)

        from core.small_model import APP_CONFIG
        cfg = {**APP_CONFIG, "chat": {**(APP_CONFIG.get("chat") or {}), "project_builder": project}}

        async def main():
            with (mock.patch.dict(APP_CONFIG, cfg), mock.patch.object(run.cloud, "cloud_lane", return_value=None),
                  mock.patch.object(run.creative, "pick", mock.AsyncMock(return_value=picked)),
                  mock.patch.object(run.input_guard, "check_async", mock.AsyncMock(return_value=None)),
                  mock.patch.object(run.state, "is_running", return_value=True),
                  mock.patch.object(run.state, "process", SimpleNamespace(poll=lambda: None)),
                  mock.patch.object(run.state, "client", main_client),
                  mock.patch.object(run.state, "profile", {"model_path": "/m/Test-Main.gguf"}),
                  mock.patch.object(run.state, "profile_path", "/m/profile.json"),
                  mock.patch.object(run.state, "ensure_running", ensure),
                  mock.patch.object(run, "audit_log"), mock.patch.object(run, "monitor_begin", return_value=1) as mb,
                  mock.patch.object(run, "monitor_end", create=True), mock.patch.object(run, "db_record_request") as rec,
                  mock.patch.object(run, "_llm_chat_stream", fake_stream)):
                resp = await chat_route.chat_run(req, user)
                async for chunk in resp.body_iterator:
                    out.append(chunk)
                out.append(("rec", rec.call_args))
                out.append(("mb", mb.call_args))
        err = io.StringIO()
        with redirect_stderr(err):
            asyncio.run(main())
        text = "".join(c for c in out if isinstance(c, str))
        return SimpleNamespace(text=text, calls=calls, sent=sent, err=err.getvalue(), ensure=ensure,
                               lanes=[c for c in text.split("\n\n") if "event: lane" in c],
                               rec=[c for c in out if isinstance(c, tuple) and c[0] == "rec"][0][1])

    def test_empty_lane_reply_is_redone_on_the_main_model(self):
        r = self.drive([[result("")]], [[result(PAGE)]], project=False)
        self.assertEqual(r.calls, {"lane": 1, "main": 1})                  # the nudge was not spent on the weak model
        self.assertNotIn(NOTE, r.text)
        self.assertIn("MAIN MODEL PAGE", r.text)
        self.assertEqual(len(r.lanes), 2)
        self.assertIn("Test-3D-Coder", r.lanes[0])
        self.assertIn("Test-Main", r.lanes[1])
        self.assertIn('"lane": "main"', r.lanes[1])
        self.assertEqual(r.sent["main"][0][-1], r.sent["lane"][0][-1])     # same user message, no failed assistant turn in between
        self.assertNotIn("assistant", [m["role"] for m in r.sent["main"][0]])
        self.assertIn("delivered nothing: finish=stop completion_tokens=7 reasoning_chars=0 text_markup=False", r.err)
        self.assertIn("3D lane failed - retrying on the main model", r.err)
        self.assertEqual(r.rec.args[1], "Test-Main")                        # usage is booked on the model that answered

    def test_project_builder_route_falls_back_before_its_own_nudges(self):
        # this request is a "single page" project build: the lane used to get three project nudges and one more before the note
        r = self.drive([[result("")]], [[result(PAGE)]])
        self.assertEqual(r.calls["lane"], 1)
        self.assertGreaterEqual(r.calls["main"], 1)
        self.assertEqual(len(r.lanes), 2)
        self.assertIn("MAIN MODEL PAGE", r.text)

    def test_main_may_answer_with_a_write_file_call(self):
        tc = {"id": "c1", "type": "function", "function": {"name": "write_file", "arguments": "{}"}}
        with mock.patch("routes.chat.run.chat_write_file", return_value="error: stub", create=True):
            r = self.drive([[result("")]], [[result("", tool_calls=[tc])], [result(PAGE)]], project=False)
        self.assertEqual(r.calls["lane"], 1)
        self.assertGreaterEqual(r.calls["main"], 1)
        self.assertNotIn(NOTE, r.text)

    def test_an_announcement_without_the_page_drops_its_text_and_falls_back(self):
        r = self.drive([[result("Let me build the 3D page now:")]], [[result(PAGE)]], project=False)
        self.assertEqual(r.calls, {"lane": 1, "main": 1})
        self.assertLess(r.text.index("event: delta_reset"), r.text.index("MAIN MODEL PAGE"))
        self.assertEqual(len(r.lanes), 2)

    def test_a_lane_that_delivers_is_left_alone(self):
        r = self.drive([[result(PAGE.replace("MAIN MODEL", "LANE"))]], [[result(PAGE)]], project=False)
        self.assertEqual(r.calls, {"lane": 1, "main": 0})
        self.assertEqual(len(r.lanes), 1)
        self.assertNotIn("retrying on the main model", r.err)
        self.assertNotIn("delivered nothing", r.err)

    def test_both_fail_gives_the_old_note_once(self):
        r = self.drive([[result("")]], [[result("Here is your design:")]], project=False)
        self.assertEqual(r.calls["lane"], 1)
        self.assertEqual(r.calls["main"], 2)                               # main gets its own single nudge, as before
        self.assertEqual(r.text.count(NOTE), 1)
        self.assertEqual(len(r.lanes), 2)                                  # switched exactly once

    def test_main_cannot_start_keeps_the_old_path(self):
        r = self.drive([[result("")]], [[result(PAGE)]], ensure_error=RuntimeError("no gpu"), project=False)
        self.assertEqual(r.calls, {"lane": 2, "main": 0})                  # nudge to the lane model, then the note
        self.assertEqual(r.text.count(NOTE), 1)
        self.assertEqual(len(r.lanes), 1)
        self.assertNotIn("retrying on the main model", r.err)
        self.assertEqual(r.ensure.await_count, 1)                          # asked once, not again on the second failure
        self.assertEqual(r.err.count("delivered nothing"), 2)


if __name__ == "__main__":
    unittest.main()
