"""The final chat loop stays alive and honest (routes/chat/run.py and its helpers). One regression test per audited defect; each
drives the real chat_run with a fake model (same idea as tests/test_chat_media_flow.py and tests/test_3d_lane_fallback.py)."""
import asyncio
import copy
import io
import json
import threading
import time
import unittest
from contextlib import ExitStack, redirect_stderr
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import httpx

from core import context_budget, media_tools
from core.small_model import APP_CONFIG

PAGE = "<!DOCTYPE html><html><head><title>T</title></head><body>" + ("<p>Hello bakery lovers, fresh bread daily.</p>" * 40) + "</body></html>"
HUGE_PAGE = "Here is the site:\n```html\n" + PAGE * 2 + "\n```"
FIN = ("Project site: 5 files. Download the whole site as site-ab12cd34.zip [DOWNLOAD: site-ab12cd34.zip] and preview index.html as "
       "site-ab12cd34.html [DOWNLOAD: site-ab12cd34.html]. Reply now with a short summary and these two links.")
SITE_Q = "build me a multi-page website for my bakery with a home page, a menu page and a contact page"
SAVED = "saved a-1234abcd.txt [DOWNLOAD: a-1234abcd.txt]"
NOTHING = "I could not produce an answer this time. Press Continue to try again."


def rounds_cfg(n, **more):
    """chat config that allows n tool rounds (the thinking-level table decides the rounds of a normal turn)."""
    return {"thinking_budgets": {"medium": {"tool_calls": 8, "token_budget": 12000, "max_rounds": n}}, **more}


def res(content="", reasoning="", tool_calls=None, finish="stop"):
    return {"content": content, "reasoning": reasoning, "finish_reason": finish if not tool_calls else "tool_calls",
            "usage": {"completion_tokens": 5}, "timings": None, "aborted": None, "tool_calls": tool_calls or []}


def call(name, args, n=1):
    return {"id": f"c{n}", "type": "function", "function": {"name": name, "arguments": json.dumps(args)}}


def tool_round(name, args, n=1, content=""):
    return res(content=content, tool_calls=[call(name, args, n)])


def blocks(body):
    out = []
    for b in body.split("\n\n"):
        if "event: " in b and "data: " in b:
            ev = b.split("event: ", 1)[1].split("\n", 1)[0]
            try:
                out.append((ev, json.loads(b.split("data: ", 1)[1])))
            except ValueError:
                out.append((ev, None))
    return out


def shown(body):
    """The reply as the page ends up showing it (deltas, restarted by delta_reset, replaced by delta_replace)."""
    text = ""
    for ev, d in blocks(body):
        if ev == "delta_reset":
            text = ""
        elif ev == "delta_replace":
            text = d["text"]
        elif ev == "delta":
            text += d["text"]
    return text


class Harness(unittest.TestCase):
    def turn(self, query, rounds, history=None, cloud=True, cfg=None, extra=(), session_id=None, web=False):
        """rounds: one entry per model call (a result dict, or an exception to raise); the last one repeats.
        -> SimpleNamespace(body, final, sent (messages of every call), tools (names offered per call), err, events)."""
        from routes import chat as chat_route
        run = chat_route.run
        sent, tools_seen, clients = [], [], []
        self.live_sent = sent

        async def fake_stream(client, msgs, tools=None, **kw):
            sent.append(copy.deepcopy(msgs))
            tools_seen.append([t["function"]["name"] for t in (tools or [])])
            clients.append(client)
            r = rounds[min(len(sent) - 1, len(rounds) - 1)]
            if isinstance(r, BaseException):
                raise r
            if r["content"]:
                yield ("content_delta", r["content"])
            yield ("result", r)

        async def fake_fallback(client, fb, msgs, tools=None, *a, **kw):
            async for x in fake_stream(client, msgs, tools=tools):
                yield x

        lane = SimpleNamespace(model_id="cloud-x", display="cloud-x", provider_name="p", key="cloud:x")
        user = SimpleNamespace(id=1, is_super_admin=True, role_names=[])
        msgs = (history or []) + [{"role": "user", "content": query}]
        req = chat_route.ChatRunRequest(messages=msgs, web_search=web, kb="off", session_id=session_id)
        out = []
        main_client = object()
        cfg_all = {**APP_CONFIG, "chat": {**(APP_CONFIG.get("chat") or {}), **(cfg or {})}}

        async def main():
            with mock.patch.dict(APP_CONFIG, cfg_all):
                with (mock.patch.object(run.cloud, "cloud_lane", return_value=lane if cloud else None),
                      mock.patch.object(run.cloud, "CloudClient", return_value=main_client),
                      mock.patch.object(run.input_guard, "check_async", mock.AsyncMock(return_value=None)),
                      mock.patch.object(run.state, "is_running", return_value=True),
                      mock.patch.object(run.state, "process", SimpleNamespace(poll=lambda: None)),
                      mock.patch.object(run.state, "client", main_client if not cloud else object()),
                      mock.patch.object(run.state, "profile", {"model_path": "/m/Test-Main.gguf"}),
                      mock.patch.object(run.state, "profile_path", "/m/profile.json"),
                      mock.patch.object(run, "audit_log"), mock.patch.object(run, "monitor_begin", return_value=1),
                      mock.patch.object(run, "monitor_end", create=True), mock.patch.object(run, "db_record_request"),
                      mock.patch.object(run, "_llm_chat_stream", fake_stream),
                      mock.patch.object(run.common, "_llm_chat_stream_with_fallback", fake_fallback),
                      mock.patch.object(media_tools, "chat_image_tool_schema", return_value=None)):
                    with ExitStack() as st:
                        for p in extra:
                            st.enter_context(p)
                        resp = await chat_route.chat_run(req, user)
                        async for chunk in resp.body_iterator:
                            out.append(chunk)
        err = io.StringIO()
        with redirect_stderr(err):
            asyncio.run(main())
        body = "".join(c for c in out if isinstance(c, str))
        return SimpleNamespace(body=body, final=shown(body), sent=sent, tools=tools_seen, err=err.getvalue(), events=blocks(body),
                               clients=clients, main_client=main_client)




# --------------------------------------------------------------------------------------------------------------------------
# 1. a file written from the reply never runs on the event-loop thread
# --------------------------------------------------------------------------------------------------------------------------
class NothingBlocksTheLoop(Harness):
    """core/agent_tools/common_writer._run_blocking + core/doc_tools/slide_images._on_main_loop wait for the event loop: a save
    made ON the loop thread (a deck with an `image:` line and a local image model) waits for itself for ever."""

    @staticmethod
    def recorder(seen, result=SAVED):
        def fake_write(args):
            seen.append((threading.get_ident(), dict(args)))
            return result
        return fake_write

    def test_a_download_tag_with_a_code_block_is_saved_off_the_loop(self):
        seen = []
        fake_write = self.recorder(seen)
        loop_ids = []

        async def stream_marker(*a, **k):
            loop_ids.append(threading.get_ident())
        r_rounds = [res("Here is the page:\n```html\n" + PAGE + "\n```\n\n[DOWNLOAD: bakery.html]")]
        with mock.patch("routes.chat.run.chat_write_file", fake_write), mock.patch("routes.chat.delivery.chat_write_file", fake_write), \
                mock.patch("routes.chat.delivery._file_exists", return_value=True), \
                mock.patch("routes.chat.run.run_in_executor_ctx", self.spy_executor(loop_ids)):
            self.turn("save the bakery page as bakery.html", r_rounds)
        self.assertTrue(seen, "the page was saved")
        self.assertTrue(loop_ids, "the loop thread id was taken")
        self.assertTrue(all(t != loop_ids[0] for t, _ in seen), "chat_write_file ran on the event-loop thread")

    @staticmethod
    def spy_executor(loop_ids):
        from core.request_context import run_in_executor_ctx as real

        async def spy(fn, *args):
            if not loop_ids:
                loop_ids.append(threading.get_ident())       # called from the loop thread: that is the loop's id
            return await real(fn, *args)
        return spy

    def test_the_file_intent_recovery_write_is_off_the_loop(self):
        seen, loop_ids = [], []
        fake_write = self.recorder(seen)
        table = "| a | b |\n|---|---|\n| 1 | 2 |\n| 3 | 4 |"
        with mock.patch("routes.chat.run.chat_write_file", fake_write), mock.patch("routes.chat.delivery.chat_write_file", fake_write), \
                mock.patch("routes.chat.delivery._file_exists", return_value=True), \
                mock.patch("routes.chat.run.run_in_executor_ctx", self.spy_executor(loop_ids)):
            self.turn("please create a csv file with two columns a and b", [res("Sure, here is the data:\n\n" + table)])
        self.assertTrue(seen)
        self.assertTrue(all(t != loop_ids[0] for t, _ in seen))

    def test_finalize_reply_files_runs_in_the_executor(self):
        """A page pasted into the reply is saved by finalize_reply_files: that call is made off the loop."""
        seen, loop_ids = [], []
        fake_write = self.recorder(seen, "saved cats-1234abcd.html [DOWNLOAD: cats-1234abcd.html]")
        threads = []
        from routes.chat import delivery
        real_finalize = delivery.finalize_reply_files

        def spy_finalize(*a, **k):
            threads.append(threading.get_ident())
            return real_finalize(*a, **k)
        with mock.patch("routes.chat.delivery.chat_write_file", fake_write), mock.patch("routes.chat.run.chat_write_file", fake_write), \
                mock.patch("routes.chat.run.finalize_reply_files", spy_finalize), \
                mock.patch("routes.chat.delivery._file_exists", return_value=True), \
                mock.patch("routes.chat.run.run_in_executor_ctx", self.spy_executor(loop_ids)):
            self.turn("tell me about cats, in cats.html", [res("```html\n" + PAGE + "\n```")], extra=())
        self.assertTrue(threads, "finalize_reply_files ran")
        self.assertTrue(all(t != loop_ids[0] for t in threads), "finalize_reply_files ran on the event-loop thread")
        self.assertTrue(all(t != loop_ids[0] for t, _ in seen))


# --------------------------------------------------------------------------------------------------------------------------
# 2. a model server that never answers ends the turn with an error, it does not hang
# --------------------------------------------------------------------------------------------------------------------------
class WedgedServer(unittest.TestCase):
    def drive(self, client, **patches):
        from routes.common import llm_stream

        async def go():
            async def handle(r, w):
                await r.read(1000)
                await asyncio.sleep(30)            # accepts, then never answers
            srv = await asyncio.start_server(handle, "127.0.0.1", 0)
            port = srv.sockets[0].getsockname()[1]
            c = client(port)
            t0 = time.time()
            try:
                with (mock.patch.multiple(llm_stream, **patches) if patches else ExitStack()):
                    async for _ in llm_stream._llm_chat_stream_raw_inner(c, [{"role": "user", "content": "hi"}]):
                        pass
            except Exception as e:
                return e, time.time() - t0
            finally:
                srv.close()
                await c.aclose()
            return None, time.time() - t0
        return asyncio.run(go())

    def test_the_clients_own_read_timeout_applies(self):
        e, took = self.drive(lambda port: httpx.AsyncClient(base_url=f"http://127.0.0.1:{port}", timeout=httpx.Timeout(2, read=1)))
        self.assertIsInstance(e, httpx.ReadTimeout)
        self.assertLess(took, 5)

    def test_a_client_without_a_read_limit_gets_the_local_one(self):
        e, took = self.drive(lambda port: httpx.AsyncClient(base_url=f"http://127.0.0.1:{port}", timeout=None),
                             LOCAL_HTTP_READ_TIMEOUT_S=1.0)
        self.assertIsInstance(e, httpx.ReadTimeout)
        self.assertLess(took, 5)

    def test_the_timeout_arguments(self):
        from routes.common.llm_stream import _stream_timeout
        from core.config import LOCAL_HTTP_READ_TIMEOUT_S
        self.assertEqual(_stream_timeout(SimpleNamespace(is_cloud=True)), {})                      # the provider's 300 s
        self.assertEqual(_stream_timeout(SimpleNamespace(timeout=httpx.Timeout(5, read=600))), {})   # the client's own limits
        t = _stream_timeout(SimpleNamespace(timeout=httpx.Timeout(None)))["timeout"]
        self.assertGreaterEqual(LOCAL_HTTP_READ_TIMEOUT_S, 600)           # a long prefill is still waited for
        self.assertEqual(t.read, LOCAL_HTTP_READ_TIMEOUT_S)
        from core.config import CLOUD_TIMEOUT_S
        self.assertEqual(CLOUD_TIMEOUT_S, 300.0)
        src = (Path(__file__).resolve().parents[1] / "routes" / "common" / "llm_stream.py").read_text(encoding="utf-8")
        import re
        self.assertEqual(re.findall(r"\.stream\([^)\n]*timeout=None", src), [])

    def test_a_timeout_ends_the_turn_with_a_clear_message(self):
        h = Harness()
        r = h.turn("hello there", [httpx.ReadTimeout("")], cloud=False)
        self.assertIn("took too long", r.final)
        self.assertEqual([d for ev, d in r.events if ev == "done"][-1]["state"], "failed")


# --------------------------------------------------------------------------------------------------------------------------
# 3. a cloud-only chat does not wait for the local model
# --------------------------------------------------------------------------------------------------------------------------
class LazyLocalFallback(Harness):
    def drive(self, primary_fails):
        from routes import chat as chat_route
        run = chat_route.run
        local = object()
        cloud_client = object()
        ensure = mock.AsyncMock()
        lane = SimpleNamespace(model_id="cloud-x", display="cloud-x", provider_name="p", key="cloud:x")
        user = SimpleNamespace(id=1, is_super_admin=True, role_names=[])
        req = chat_route.ChatRunRequest(messages=[{"role": "user", "content": "hello there"}], web_search=False, kb="off", session_id=None)
        used = []

        async def fake_llm(client, msgs, tools=None, *a, **kw):
            used.append("cloud" if client is cloud_client else "local")
            if client is cloud_client and primary_fails:
                raise RuntimeError("provider down")
            yield ("content_delta", "ok then")
            yield ("result", res("ok then"))
        out = []

        async def main():
            with (mock.patch.object(run.cloud, "cloud_lane", return_value=lane), mock.patch.object(run.cloud, "CloudClient", return_value=cloud_client),
                  mock.patch.object(run.cloud, "cloud_bindings", return_value={"fallback_local": True}),
                  mock.patch.object(run.input_guard, "check_async", mock.AsyncMock(return_value=None)),
                  mock.patch.object(run.state, "is_running", return_value=False),
                  mock.patch.object(run.state, "process", None), mock.patch.object(run.state, "client", local),
                  mock.patch.object(run.state, "profile", {"model_path": "/m/Test-Main.gguf"}),
                  mock.patch.object(run.state, "profile_path", "/m/profile.json"), mock.patch.object(run.state, "ensure_running", ensure),
                  mock.patch("routes.common.fallback._llm_chat_stream", fake_llm),
                  mock.patch.object(run, "audit_log"), mock.patch.object(run, "monitor_begin", return_value=1),
                  mock.patch.object(run, "monitor_end", create=True), mock.patch.object(run, "db_record_request")):
                resp = await chat_route.chat_run(req, user)
                async for chunk in resp.body_iterator:
                    out.append(chunk)
        with redirect_stderr(io.StringIO()):
            asyncio.run(main())
        return SimpleNamespace(ensure=ensure, used=used, body="".join(c for c in out if isinstance(c, str)))

    def test_the_local_model_is_not_started_when_the_cloud_answers(self):
        r = self.drive(primary_fails=False)
        r.ensure.assert_not_awaited()
        self.assertEqual(r.used, ["cloud"])
        self.assertEqual(shown(r.body), "ok then")

    def test_the_local_model_is_started_only_when_the_cloud_call_fails(self):
        r = self.drive(primary_fails=True)
        r.ensure.assert_awaited_once()
        self.assertEqual(r.used, ["cloud", "local"])
        self.assertEqual(shown(r.body), "ok then")
        self.assertIn('"source": "local"', r.body)            # the page is told the local model answered


# --------------------------------------------------------------------------------------------------------------------------
# 4. a website build that stops before project_finish is packed by the server, cleanly
# --------------------------------------------------------------------------------------------------------------------------
class ProjectAutoFinish(Harness):
    CFG = {"project_builder": True}
    EXISTS = mock.patch("routes.chat.delivery._file_exists", return_value=True)

    def build(self, rounds, fin=FIN, cfg=None, extra=()):
        """-> result with .fin_calls (how many model calls had been made when finish_if_possible ran) and .fin_threads."""
        fin_calls, fin_threads, loop_ids = [], [], []
        from core.request_context import run_in_executor_ctx as real

        def fake_finish(project=None):
            fin_threads.append(threading.get_ident())
            fin_calls.append(len(self.live_sent))
            return fin

        async def spy(fn, *a):
            if not loop_ids:
                loop_ids.append(threading.get_ident())
            return await real(fn, *a)
        with mock.patch("core.agent_tools.project_files.finish_if_possible", fake_finish), \
                mock.patch("routes.chat.run.run_in_executor_ctx", spy), self.EXISTS:
            r = self.turn(SITE_Q, rounds, cloud=False, cfg={**self.CFG, **(cfg or {})}, extra=extra)
        r.fin_calls, r.fin_threads, r.loop_id = fin_calls, fin_threads, (loop_ids[0] if loop_ids else None)
        return r

    def test_text_only_replies_end_in_a_clean_packed_message(self):
        # three nudges are spent on text-only replies; the fourth text-only reply is packed by the server
        r = self.build([res("Now let me write the css file next:")])
        self.assertEqual(len(r.sent), 4, "three nudges and the fourth reply")
        self.assertEqual(r.fin_calls, [4], "packed once, only after the nudges were spent")
        final = r.final
        self.assertIn("[DOWNLOAD: site-ab12cd34.zip]", final)
        self.assertIn("[DOWNLOAD: site-ab12cd34.html]", final)
        self.assertNotIn("Reply now with", final)                      # the tool's own wording is not shown
        self.assertNotIn("Project site:", final)
        self.assertIn("The build did not finish by itself", final)
        self.assertNotIn("Now let me write", final)
        self.assertEqual(final.count("[DOWNLOAD: site-ab12cd34.zip]"), 1)
        self.assertTrue(all(t != r.loop_id for t in r.fin_threads), "finish_if_possible ran on the event-loop thread")
        # (a) the saved links count as delivered: no "announced but not produced" nudge, the ZIP message is not reset
        self.assertEqual([ev for ev, _ in r.events].count("delta_reset"), 3, "only the three nudges reset the text")
        self.assertNotIn("announced the deliverable", json.dumps(r.sent))

    def test_a_pasted_page_is_not_left_in_chat_when_nothing_can_be_packed(self):
        r = self.build([res(HUGE_PAGE)], fin=None)
        self.assertEqual(r.final.strip(), "The build did not finish; press Continue.")
        self.assertNotIn("<html", r.final.lower())
        self.assertEqual(len(r.sent), 4, "no extra nudge after the status sentence")

    def test_two_nudges_are_not_enough_to_pack(self):
        """Only the third spent nudge arms the server's packing: a reply with the ZIP link in it just ends the turn."""
        r = self.build([res("Let me write the next file:"), res("Done. [DOWNLOAD: site-ab12cd34.zip]")],
                       extra=(mock.patch("routes.chat.run._common_resolve", return_value=SimpleNamespace(is_file=lambda: True)),))
        self.assertEqual(r.fin_calls, [], "finish_if_possible was not needed")
        self.assertIn("[DOWNLOAD: site-ab12cd34.zip]", r.final)

    def test_an_unfinished_project_is_packed_when_the_rounds_run_out(self):
        writes = []

        def fake_project_write(args):
            writes.append(args)
            return "saved index.html (1 of 3 files)"
        rounds = [tool_round("project_write", {"project": "site", "path": "index.html", "content": "<p>x</p>"}),
                  tool_round("project_write", {"project": "site", "path": "menu.html", "content": "<p>y</p>"}, 2),
                  res("The site is half built: the index page exists, the other pages still need to be written.")]
        r = self.build(rounds, cfg=rounds_cfg(2, project_max_rounds=2), extra=(mock.patch("routes.chat.run.tool_project_write", fake_project_write),))
        self.assertEqual(len(r.fin_calls), 1)
        self.assertIn("[DOWNLOAD: site-ab12cd34.zip]", r.final)
        self.assertNotIn("Reply now with", r.final)
        done = [d for ev, d in r.events if ev == "done"][-1]
        self.assertEqual((done.get("state"), done.get("note")), ("stopped", "budget"))


# --------------------------------------------------------------------------------------------------------------------------
# 5. nothing the reader must not see is shown as the answer
# --------------------------------------------------------------------------------------------------------------------------
class EmptyReplies(Harness):
    def tool_then_empty(self, reasoning, cfg=None):
        def fake_tool(name, args):
            return "RAW TOOL OUTPUT secret-looking text"
        fut = [tool_round("lookup_thing", {"q": "x"}), res("", reasoning=reasoning)]

        async def run_tool(name, args):
            return "RAW TOOL OUTPUT secret-looking text"
        return self.turn("tell me a story about a fox", fut, cloud=False, cfg=cfg, extra=(mock.patch("routes.chat.run.run_tool", run_tool),))

    def test_an_empty_reply_after_a_tool_is_one_fixed_sentence(self):
        r = self.tool_then_empty("The user wants a story. I think hidden thoughts about the fox and the hen, step by step ...")
        self.assertEqual(r.final.strip(), NOTHING)
        self.assertNotIn("hidden thoughts", r.body.replace('"thought_delta"', "").split("event: tool_result")[-1])
        deltas = "".join(d["text"] for ev, d in r.events if ev in ("delta", "delta_replace"))
        self.assertNotIn("RAW TOOL OUTPUT", deltas)
        self.assertNotIn("hidden thoughts", deltas)
        self.assertNotIn("I searched for information", r.body)

    def test_the_wrap_up_after_the_rounds_say_the_same_sentence(self):
        r = self.tool_then_empty("some private reasoning that is long enough to pass the old length check", cfg=rounds_cfg(1))
        deltas = "".join(d["text"] for ev, d in r.events if ev in ("delta", "delta_replace"))
        self.assertIn("Continue", r.final)
        self.assertNotIn("RAW TOOL OUTPUT", deltas)
        self.assertNotIn("private reasoning", deltas)


# --------------------------------------------------------------------------------------------------------------------------
# 6/7. the text of a length-cut reply is carried once; a call mentioned in the reasoning is not run
# --------------------------------------------------------------------------------------------------------------------------
class ReplyHandling(Harness):
    def test_cont_prefix_is_used_once(self):
        async def run_tool(name, args):
            return "tool says hi"
        rounds = [res("PART-ONE-OF-THE-ANSWER ", finish="length"), tool_round("lookup_thing", {"q": "x"}), res("Final words.")]
        r = self.turn("tell me about foxes", rounds, cloud=False, extra=(mock.patch("routes.chat.run.run_tool", run_tool),))
        self.assertGreaterEqual(len(r.sent), 3)
        third = r.sent[2]
        asst = [m for m in third if m.get("role") == "assistant"]
        self.assertEqual(sum(1 for m in asst if "PART-ONE" in str(m.get("content"))), 1, "the earlier part is in the history once")
        with_calls = [m for m in asst if m.get("tool_calls")]
        self.assertEqual(with_calls[-1]["content"], "")       # not the earlier part again
        self.assertNotIn("PART-ONE", r.final.split("Final words.")[-1])

    def test_cont_prefix_does_not_leak_into_the_answer_after_a_discarded_turn(self):
        """A length-cut reply is continued; the continuation is then redone (nudge). The final answer is the last turn's text only."""
        seen = []

        async def spy(*args, **kw):
            seen.append(args[3])
            if False:
                yield ""
        self.turn("save a note as a.txt", [res("PART-ONE-OF-THE-ANSWER ", finish="length"), res("Let me write the file now:"),
                                               res("FINAL ANSWER about the note.")], cloud=False,
                      extra=(mock.patch("routes.chat.run.verifier.sse_events", spy),))
        self.assertEqual(len(seen), 1)
        self.assertTrue(seen[0].startswith("FINAL ANSWER about the note."), seen)
        self.assertNotIn("PART-ONE", seen[0])

    def test_a_call_mentioned_in_the_reasoning_does_not_run_beside_an_answer(self):
        writes = []
        reasoning = ('The user wants a haiku. I could save it, e.g. <tool_call>{"name": "write_file", "arguments": '
                     '{"path": "a.txt", "content": "hi"}}</tool_call> but no, just answer.')
        r = self.turn("tell me a haiku about rain", [res("Spring rain falls softly, the quiet pond wakes up, frogs sing in the dusk.",
                                                          reasoning=reasoning)], cloud=False,
                      extra=(mock.patch("routes.chat.run.chat_write_file", lambda a: writes.append(a) or SAVED),))
        self.assertEqual(writes, [])
        self.assertNotIn("tool_call", [ev for ev, _ in r.events])
        self.assertIn("Spring rain", r.final)

    def test_a_call_in_the_reasoning_still_runs_when_the_reply_is_empty(self):
        async def run_tool(name, args):
            return "found it"
        reasoning = '<tool_call>{"name": "lookup_thing", "arguments": {"q": "x"}}</tool_call>'
        r = self.turn("tell me a haiku about rain", [res("", reasoning=reasoning), res("Rain on the roof.")], cloud=False,
                      extra=(mock.patch("routes.chat.run.run_tool", run_tool),))
        self.assertIn("tool_call", [ev for ev, _ in r.events])


# --------------------------------------------------------------------------------------------------------------------------
# 8. files already saved are linked when the run fails or is stopped
# --------------------------------------------------------------------------------------------------------------------------
class FailedRuns(Harness):
    def saved_then(self, boom):
        return self.turn("save a text file with hello world as a.txt", [tool_round("write_file", {"path": "a.txt", "content": "hello world"}), boom],
                         cloud=False, extra=(mock.patch("routes.chat.run.chat_write_file", lambda a: SAVED),
                                             mock.patch("routes.chat.delivery._file_exists", return_value=True)))

    def test_a_failure_after_a_save_still_links_the_file(self):
        r = self.saved_then(RuntimeError("model server error"))
        self.assertIn("[DOWNLOAD: a-1234abcd.txt]", r.final)
        done = [d for ev, d in r.events if ev == "done"][-1]
        self.assertEqual(done["state"], "failed")
        self.assertEqual(done["note"], "RuntimeError")

    def test_a_stop_after_a_save_still_links_the_file(self):
        r = self.saved_then(asyncio.CancelledError())
        self.assertIn("[DOWNLOAD: a-1234abcd.txt]", r.final)


# --------------------------------------------------------------------------------------------------------------------------
# 9. endings by rounds / time / repeated failures
# --------------------------------------------------------------------------------------------------------------------------
class Endings(Harness):
    def test_a_level_with_zero_rounds_still_answers_once(self):
        # max_rounds 0 with tools offered used to mean a turn that never ran; it is one normal round now
        r = self.turn("tell me a story about a fox", [res("Once upon a time a fox crossed the river and found a quiet field.")],
                      cloud=False, cfg=rounds_cfg(0),
                      extra=(mock.patch.object(media_tools, "chat_image_tool_schema", return_value=IMG_SCHEMA),))
        self.assertNotIn("request failed", r.err)
        self.assertIn("Once upon a time", r.final)
        self.assertNotIn("budget_stop", [ev for ev, _ in r.events])

    def test_the_time_limit_says_time_and_ends_stopped(self):
        async def run_tool(name, args):
            return "fine"
        r = self.turn("tell me a story about a fox", [tool_round("lookup_thing", {"q": "x"}), res("")], cloud=False,
                      cfg={"run_timeout_s": 1e-9}, extra=(mock.patch("routes.chat.run.run_tool", run_tool),))
        stop = [d for ev, d in r.events if ev == "budget_stop"][-1]
        self.assertEqual(stop["reason"], "timeout")
        self.assertIn("ran out of time", r.final)
        done = [d for ev, d in r.events if ev == "done"][-1]
        self.assertEqual((done.get("state"), done.get("note")), ("stopped", "timeout"))

    def test_status_texts(self):
        from routes.chat.wrapup import status_text
        for reason in ("budget", "timeout", "circuit_breaker"):
            self.assertNotIn("infinite loop", status_text(5, ["a.txt"], reason))
            self.assertIn("Continue", status_text(5, [], reason))
        self.assertIn("5 steps", status_text(5, [], "budget"))
        self.assertIn("time", status_text(5, [], "timeout"))
        self.assertIn("failed", status_text(5, [], "circuit_breaker"))

    def test_a_normal_file_turn_is_not_marked_stopped(self):
        r = self.turn("save a text file with hello world as a.txt",
                      [tool_round("write_file", {"path": "a.txt", "content": "hello world"}), res("All done, the file a.txt is saved with your text.")],
                      cloud=False, extra=(mock.patch("routes.chat.run.chat_write_file", lambda a: SAVED),
                                          mock.patch("routes.chat.delivery._file_exists", return_value=True)))
        self.assertIsNone([d for ev, d in r.events if ev == "done"][-1].get("state"))


# --------------------------------------------------------------------------------------------------------------------------
# 10. the window and the learned ratio belong to one model
# --------------------------------------------------------------------------------------------------------------------------
class BudgetLanes(Harness):
    def test_lane_keys(self):
        self.assertEqual(context_budget.lane_key("main"), "main")
        self.assertEqual(context_budget.lane_key("main", cloud_key="p:m"), "main:cloud:p:m")
        self.assertEqual(context_budget.lane_key("main", lane3d=True), "main:lane3d")

    def test_a_cloud_lane_does_not_inherit_the_local_window(self):
        context_budget.reset()
        self.addCleanup(context_budget.reset)
        context_budget.set_window("main", 8192)
        context_budget.record_usage("main", 1000, 3000, [{"role": "user", "content": "x" * 5000}], None)

        class Boom:
            async def get(self, *a, **k):
                raise AssertionError("probed")
        cloud_lane = context_budget.lane_key("main", cloud_key="p:m")
        self.assertIsNone(asyncio.run(context_budget.probe_window(cloud_lane, None)))
        self.assertEqual(context_budget.factor(cloud_lane), 1.0)
        self.assertEqual(asyncio.run(context_budget.probe_window("main", Boom())), 8192)      # the local lane keeps its own

    def test_a_cloud_turn_after_a_local_one_gets_the_cloud_budget(self):
        context_budget.reset()
        self.addCleanup(context_budget.reset)
        context_budget.set_window("main", 8192)               # a local model with a small window answered before
        for _ in range(30):                                   # ... and its estimates were far too low (factor -> 2.0)
            context_budget.record_usage("main", 1000, 5000, [{"role": "user", "content": "x"}], None)
        self.assertGreater(context_budget.factor("main"), 1.9)
        seen = []

        def fake_shrink(msgs, budget, *a, **k):
            seen.append(budget)
        self.turn("tell me about foxes", [res("Foxes are wild dogs.")], cloud=True,
                      extra=(mock.patch("routes.chat.run.shrink_old_tool_results", fake_shrink),))
        self.assertTrue(seen)
        self.assertGreater(seen[0], 20000, "the cloud request was budgeted with the local model's window / learned ratio")


# --------------------------------------------------------------------------------------------------------------------------
# 11. token waste: big arguments are not sent again
# --------------------------------------------------------------------------------------------------------------------------
class HistoryStaysSmall(Harness):
    def test_doc_create_content_is_trimmed_in_the_history(self):
        body = "word " * 3000
        impls = {"doc_create": lambda a: "created report-1234abcd.pdf [DOWNLOAD: report-1234abcd.pdf]"}
        with mock.patch.dict("routes.chat.run._DOC_CHAT_IMPLS", impls):
            r = self.turn("write me a report about foxes",
                          [tool_round("doc_create", {"file": "report.pdf", "content": body, "total": 2}), res("Your report is ready.")],
                          cloud=False, extra=(mock.patch("routes.chat.delivery._file_exists", return_value=True),))
        later = [m for m in r.sent[-1] if m.get("role") == "assistant" and m.get("tool_calls")]
        self.assertTrue(later)
        args = later[-1]["tool_calls"][0]["function"]["arguments"]
        self.assertLess(len(args), 700)
        self.assertIn("chars saved to the file", args)

    def test_the_project_nudge_does_not_keep_the_whole_page(self):
        r = self.turn(SITE_Q, [res(HUGE_PAGE), res("Done. [DOWNLOAD: site-ab12cd34.zip]")], cloud=False, cfg={"project_builder": True})
        second = r.sent[1]
        asst = [m for m in second if m.get("role") == "assistant"]
        self.assertTrue(asst)
        self.assertLess(len(asst[-1]["content"]), 1700)


# --------------------------------------------------------------------------------------------------------------------------
# 12/13. no fake archives; no empty assistant messages; cleanup when the start-up raises
# --------------------------------------------------------------------------------------------------------------------------
class SmallHoles(Harness):
    def test_a_zip_tag_with_a_code_block_is_not_saved_as_text(self):
        writes = []
        self.turn("give me my files", [res("Here you go [DOWNLOAD: site.zip]\n```html\n" + PAGE + "\n```")], cloud=False,
                      extra=(mock.patch("routes.chat.run.chat_write_file", lambda a: writes.append(a) or SAVED),
                             mock.patch("routes.chat.delivery.chat_write_file", lambda a: writes.append(a) or SAVED)))
        self.assertFalse([w for w in writes if str(w.get("path")).lower().endswith(".zip")], writes)

    def test_a_discarded_empty_turn_leaves_a_placeholder_not_an_empty_message(self):
        r = self.turn(SITE_Q, [res(""), res("Done. [DOWNLOAD: site-ab12cd34.zip]")], cloud=False, cfg={"project_builder": True})
        second = r.sent[1]
        empties = [m for m in second if m.get("role") == "assistant" and not m.get("tool_calls") and not str(m.get("content") or "").strip()]
        self.assertEqual(empties, [])
        self.assertTrue(any(m.get("content") == "(no reply)" for m in second if m.get("role") == "assistant"))

    def test_a_failure_while_the_run_is_registered_still_ends_it(self):
        from routes.chat import run
        ended = []
        with mock.patch.object(run.runs, "end", lambda *a, **k: ended.append(a)):
            r = self.turn("hello there", [res("hi")], cloud=False,
                          extra=(mock.patch.object(run.output_guard, "OutputRedactor", side_effect=RuntimeError("boom")),))
        self.assertTrue(ended, "the runs row was left 'running'")
        self.assertEqual(ended[-1][1], "failed")
        self.assertIn("done", [ev for ev, _ in r.events])

    def test_a_failing_monitor_does_not_leave_a_crash_in_the_cleanup(self):
        from routes.chat import run
        r = self.turn("hello there", [res("hi")], cloud=False,
                      extra=(mock.patch.object(run, "monitor_begin", side_effect=RuntimeError("boom")),))
        self.assertIn("done", [ev for ev, _ in r.events])
        self.assertNotIn("NameError", r.err)
        self.assertNotIn("UnboundLocalError", r.err)


# --------------------------------------------------------------------------------------------------------------------------
# 14. pictures: follow-ups, mixed requests, counts, repeats, delivery
# --------------------------------------------------------------------------------------------------------------------------
IMG_MD = "![a red cat](/agent/raw?path=generated/20250101_120000_red-cat.png)\n\n[DOWNLOAD: generated/20250101_120000_red-cat.png]"
IMG_SCHEMA = {"type": "function", "function": {"name": "generate_image", "description": "make a picture",
                                               "parameters": {"type": "object", "properties": {"prompt": {"type": "string"}}}}}


class PictureFollowUps(Harness):
    HISTORY = [{"role": "user", "content": "draw a red cat"}, {"role": "assistant", "content": "Here is your image.\n\n" + IMG_MD}]

    def test_session_files_do_not_hold_pictures(self):
        from routes.chat.file_intent import session_files, session_media
        self.assertEqual(session_files(self.HISTORY), [])
        self.assertEqual(session_media(self.HISTORY), ["generated/20250101_120000_red-cat.png"])
        docs = self.HISTORY + [{"role": "assistant", "content": "[DOWNLOAD: report-1234abcd.pdf]"}]
        self.assertEqual(session_files(docs), ["report-1234abcd.pdf"])

    def test_where_is_the_image_returns_the_right_link_without_a_model_call(self):
        for q in ("where is the image?", "where is the file?", "give me the link again", "where can I download it"):
            r = self.turn(q, [res("should not be asked")], history=self.HISTORY)
            self.assertEqual(r.sent, [], q)
            self.assertIn("[DOWNLOAD: generated/20250101_120000_red-cat.png]", r.final, q)
            self.assertNotIn("[DOWNLOAD: 20250101", r.final, q)

    def test_make_it_blue_is_not_a_file_edit(self):
        for q in ("make it blue", "redo it darker", "add a sunset behind it"):
            r = self.turn(q, [res("Making it blue now.")], history=self.HISTORY)
            self.assertEqual(len(r.sent), 1, q)                              # the model decides (a new picture), no edit-the-file route
            self.assertNotIn("previous file", json.dumps(r.sent[0]).lower(), q)

    def test_a_picture_followup_after_a_deck_does_not_edit_the_deck(self):
        hist = [{"role": "user", "content": "make a deck"}, {"role": "assistant", "content": "[DOWNLOAD: deck-1234abcd.pptx]"}] + self.HISTORY
        inspected = []
        self.turn("make it blue", [res("Making it blue now.")], history=hist,
                      extra=(mock.patch("routes.chat.run.tool_doc_inspect_common", lambda a: inspected.append(a) or "outline"),))
        self.assertEqual(inspected, [], "the deck was opened for an edit")
        self.turn("make it blue", [res("Making it blue now.")], history=hist[:2],
                            extra=(mock.patch("routes.chat.run.tool_doc_inspect_common", lambda a: inspected.append(a) or "outline"),))
        self.assertTrue(inspected, "without the picture in between, the same words do edit the deck")

    def test_a_new_picture_request_is_not_a_where_question(self):
        r = self.turn("draw a logo with a download link on it", [res("ok")], history=self.HISTORY)
        self.assertEqual(len(r.sent), 1)


class MixedRequests(unittest.TestCase):
    """The first picture must not cut the turn short when the message asks for more (tools stay on)."""

    def turn(self, query, web=False):
        h = Harness()
        n = [0]

        def made(i):
            return {"markdown": f"![p](/agent/raw?path=generated%2Fpic{i}.png)\n\n[DOWNLOAD: generated/pic{i}.png]",
                    "files": [{"path": f"generated/pic{i}.png", "mime": "image/png"}], "model": "m", "where": "this PC"}

        async def fake_events(kind, args):
            n[0] += 1
            d = made(n[0])
            yield ("result", media_tools.result_text(kind, d), d)
        r = h.turn(query, [tool_round("generate_image", {"prompt": "a cat"}), res("Done.")], cloud=True, web=web,
                   extra=(mock.patch.object(media_tools, "chat_image_tool_schema", return_value=IMG_SCHEMA),
                          mock.patch.object(media_tools, "generate_events", fake_events)))
        return r

    def test_a_picture_alone_still_ends_the_turn_without_tools(self):
        r = self.turn("draw a red bicycle")
        self.assertEqual(r.tools[1], [])

    def test_a_picture_with_other_work_keeps_the_tools(self):
        for q in ("Write a blog post about coffee with a header picture of a coffee cup",
                  "make an image of a cat and also tell me the weather in Dhaka",
                  "draw 3 logos then write me a document explaining them"):
            r = self.turn(q)
            self.assertNotEqual(r.tools[1], [], q)


class PictureCounts(unittest.TestCase):
    def test_copies_wanted(self):
        w = media_tools.copies_wanted
        cases = {"draw a logo, a poster and a banner": 3, "2 logos and a poster": 3, "draw 10 logos": 4, "draw 2 cats and 3 dogs": 4,
                 "first an image of a cat, then another image of a dog": 2, "\u09e9\u099f\u09bf \u099b\u09ac\u09bf \u0986\u0981\u0995\u09cb": 3,
                 "make me a 3 page poster": 1, "make a 3 slide deck with a logo": 1, "make an image: 3d render of a house": 1,
                 "make 5 different logos": 4, "generate an image of a cat with 2 eyes": 1, "a cat image and a dog image": 2,
                 "draw a cat": 1, "make 2 images of a cat": 2, "a couple of logos": 2, "an image of 4 pictures frames": 1,
                 "make a poster for 3 days sale": 1, "generate three images of mountains": 3, "create 2 variations of a dog": 2,
                 "\u099b\u09ac\u09bf \u0986\u0981\u0995\u09cb \u09e9\u099f\u09bf": 3, "draw a cat and a dog": 2}
        for q, n in cases.items():
            self.assertEqual(w(q), n, q)

    def test_repeat_blocked_overlap_only_counts_when_nothing_more_is_wanted(self):
        b = media_tools.repeat_blocked
        self.assertTrue(b(1, 1, "anything else", ["a cat"]))
        # two wanted: two styles of one subject are not a repeat (the old 80% rule blocked this)
        self.assertFalse(b(1, 2, "a red cat on a sofa, watercolor style", ["a red cat on a sofa, oil painting style"]))
        self.assertFalse(b(1, 2, "a red cat on a sofa photorealistic", ["a red cat on a sofa"]))
        self.assertTrue(b(1, 2, "A red bicycle, on a road", ["a red bicycle on a road"]))          # the same prompt
        self.assertFalse(b(0, 1, "", []))

    def test_the_refusal_text_depends_on_how_many_are_still_wanted(self):
        self.assertIn("different", media_tools.CHAT_SAME_AGAIN)
        self.assertNotIn("already made", media_tools.CHAT_SAME_AGAIN)
        self.assertIn("already made", media_tools.CHAT_ALREADY_MADE)


class PictureDelivery(unittest.TestCase):
    def test_media_is_added_by_kind_and_never_twice(self):
        from routes.chat.delivery import _finalize_media
        md = "![a red cat](/agent/raw?path=generated/x.png)\n\n[DOWNLOAD: generated/x.png]"
        out, _ = _finalize_media("Done. [DOWNLOAD: generated/x.png]", [md])
        self.assertEqual(out.count("[DOWNLOAD: generated/x.png]"), 1)
        self.assertIn("![a red cat](/agent/raw?path=generated/x.png)", out)                  # the picture itself is added
        out, _ = _finalize_media("Done [DOWNLOAD:  generated/x.png ]", [md])
        self.assertEqual(out.lower().count("download:"), 1)                                  # no second chip for the padded tag
        self.assertIn("![a red cat]", out)
        out, _ = _finalize_media("Done ![x](/agent/raw?path=generated%2Fx.png)", [md])
        self.assertEqual(out.count("![") , 1)
        self.assertEqual(out.count("[DOWNLOAD: generated/x.png]"), 1)
        out, changed = _finalize_media("Done.", [md])
        self.assertTrue(changed)
        out2, changed2 = _finalize_media(out, [md])
        self.assertFalse(changed2)


if __name__ == "__main__":
    unittest.main()


class RoundsNeverZero(unittest.TestCase):
    def test_a_level_with_zero_max_rounds_still_gets_one_round_when_tools_are_offered(self):
        import re
        src = open("routes/chat/run.py", encoding="utf-8").read()
        self.assertIn("max(1, int(level_max_rounds))", src)
        self.assertIsNone(re.search(r"max_turns = int\(level_max_rounds\) if", src))
