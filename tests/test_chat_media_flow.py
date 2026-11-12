"""A picture made in chat is made once, shown with a download link, and the turn always ends with a reply. Drives the real
chat_run with a fake model that calls generate_image (routes/chat/run.py, core/media_tools.py, routes/chat/delivery.py)."""
import asyncio
import io
import json
import tempfile
import unittest
from contextlib import redirect_stderr
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from core import media_tools

IMG_SCHEMA = {"type": "function", "function": {"name": "generate_image", "description": "make a picture",
                                               "parameters": {"type": "object", "properties": {"prompt": {"type": "string"}}}}}


def tool_call(prompt, n=1):
    return {"content": "", "reasoning": "", "finish_reason": "tool_calls", "usage": {"completion_tokens": 5}, "timings": None,
            "aborted": None, "tool_calls": [{"id": f"c{n}", "type": "function",
                                             "function": {"name": "generate_image", "arguments": json.dumps({"prompt": prompt})}}]}


def text(content="", reasoning=""):
    return {"content": content, "reasoning": reasoning, "finish_reason": "stop", "usage": {"completion_tokens": 5},
            "timings": None, "aborted": None, "tool_calls": []}


def made(n):
    path = f"generated/pic{n}.png"
    return {"markdown": f"![pic {n}](/agent/raw?path=generated%2Fpic{n}.png)\n\n[DOWNLOAD: {path}]",
            "files": [{"path": path, "mime": "image/png"}], "model": "m", "where": "this PC"}


class ChatMediaTests(unittest.TestCase):
    def turn(self, query, rounds, history=None):
        """rounds: the model's replies, one per call. -> text of the stream, tools offered per round, prompts the generator got."""
        from routes import chat as chat_route
        run = chat_route.run
        tools_seen, prompts, results = [], [], []
        n_made = [0]

        async def fake_stream(client, msgs, tools=None, **kw):
            tools_seen.append([t["function"]["name"] for t in (tools or [])])
            results.append([m["content"] for m in msgs if m.get("role") == "tool"])
            r = rounds[min(len(tools_seen) - 1, len(rounds) - 1)]
            if r["content"]:
                yield ("content_delta", r["content"])
            yield ("result", r)

        async def fake_fallback(client, fb, msgs, tools=None, *a, **kw):
            async for x in fake_stream(client, msgs, tools=tools):
                yield x

        async def fake_events(kind, args):
            prompts.append(args.get("prompt"))
            n_made[0] += 1
            yield ("progress", {"text": "x", "pct": 5, "elapsed": 1})
            d = made(n_made[0])
            yield ("result", media_tools.result_text(kind, d), d)

        lane = SimpleNamespace(model_id="cloud-x", display="cloud-x", provider_name="p")
        user = SimpleNamespace(id=1, is_super_admin=True, role_names=[])
        msgs = (history or []) + [{"role": "user", "content": query}]
        req = chat_route.ChatRunRequest(messages=msgs, web_search=False, kb="off", session_id=None)
        out = []

        async def main():
            with (mock.patch.object(run.cloud, "cloud_lane", return_value=lane), mock.patch.object(run.cloud, "CloudClient"),
                  mock.patch.object(run.input_guard, "check_async", mock.AsyncMock(return_value=None)),
                  mock.patch.object(run.state, "is_running", return_value=True), mock.patch.object(run, "audit_log"),
                  mock.patch.object(run, "monitor_begin", return_value=1), mock.patch.object(run, "monitor_end", create=True),
                  mock.patch.object(run, "db_record_request"), mock.patch.object(run, "_llm_chat_stream", fake_stream),
                  mock.patch.object(run.common, "_llm_chat_stream_with_fallback", fake_fallback),
                  mock.patch.object(media_tools, "chat_image_tool_schema", return_value=IMG_SCHEMA),
                  mock.patch.object(media_tools, "generate_events", fake_events)):
                resp = await chat_route.chat_run(req, user)
                async for chunk in resp.body_iterator:
                    out.append(chunk)
        err = io.StringIO()
        with redirect_stderr(err):
            asyncio.run(main())
        body = "".join(c for c in out if isinstance(c, str))
        done = [json.loads(b.split("data: ", 1)[1]) for b in body.split("\n\n") if "event: tool_result" in b]
        return SimpleNamespace(body=body, tools=tools_seen, prompts=prompts, err=err.getvalue(), tool_results=done,
                               model_saw=results, final=self.final_text(body))

    @staticmethod
    def final_text(body):
        """The reply as the page ends up showing it: deltas, with a delta_reset / delta_replace restarting it."""
        text_ = ""
        for block in body.split("\n\n"):
            if "event: delta_reset" in block:
                text_ = ""
            elif "event: delta_replace" in block:
                text_ = json.loads(block.split("data: ", 1)[1])["text"]
            elif "event: delta\n" in block:
                text_ += json.loads(block.split("data: ", 1)[1])["text"]
        return text_

    # (a) the model calls the tool again after it worked
    def test_second_call_is_not_run_and_reply_has_picture_once_with_download(self):
        r = self.turn("draw a red bicycle", [tool_call("a red bicycle"), tool_call("a red bicycle on a road", 2), text("Done.")])
        self.assertEqual(len(r.prompts), 1)
        self.assertEqual(r.tool_results[1]["result"], media_tools.CHAT_ALREADY_MADE)
        self.assertIn("image tool called again - skipped (already made 1)", r.err)
        self.assertNotIn("bicycle", r.err)                                   # the log line carries no prompt text
        self.assertEqual(r.final.count("/agent/raw?path=generated%2Fpic1.png"), 1)
        self.assertEqual(r.final.count("[DOWNLOAD: generated/pic1.png]"), 1)
        self.assertIn("Done.", r.final)

    # (b) no tools after the last allowed picture
    def test_round_after_the_picture_has_no_tools(self):
        r = self.turn("draw a red bicycle", [tool_call("a red bicycle"), text("Here you go.")])
        self.assertIn("generate_image", r.tools[0])
        self.assertEqual(r.tools[1], [])
        self.assertEqual(len(r.tools), 2)                                   # the turn ended after that round
        self.assertNotIn("budget_stop", r.body)
        saw = r.model_saw[1][-1]
        self.assertTrue(saw.startswith("Made the image"))
        self.assertNotIn("generated/", saw)                                  # the model no longer sees a path to copy
        self.assertNotIn("markdown", saw)

    # (c) an empty reply after the picture
    def test_empty_reply_gets_a_fixed_sentence_the_picture_and_the_link(self):
        for reply in (text(""), text("", reasoning="Let me think about what the user wants, step by step ...")):
            r = self.turn("draw a red bicycle", [tool_call("a red bicycle"), reply])
            self.assertTrue(r.final.startswith("Here is your image."), r.final)
            self.assertIn("/agent/raw?path=generated%2Fpic1.png", r.final)
            self.assertIn("[DOWNLOAD: generated/pic1.png]", r.final)
            self.assertNotIn("I searched for information", r.body)
            self.assertNotIn("Made the image", r.final)                      # the tool's own text is never shown
            self.assertNotIn("You announced the deliverable", json.dumps(r.model_saw))

    # (d) the number of pictures
    def test_two_images_asked_two_different_prompts_run_a_third_is_blocked(self):
        r = self.turn("make 2 images of a cat", [tool_call("a grey cat on a sofa"), tool_call("a black kitten in the garden", 2),
                                                 tool_call("an orange cat sleeping", 3), text("Both are ready.")])
        self.assertEqual(r.prompts, ["a grey cat on a sofa", "a black kitten in the garden"])
        self.assertIn("generate_image", r.tools[1])                     # one more is still allowed after the first
        self.assertEqual(r.tools[2], [])
        self.assertIn("pic1.png", r.final)
        self.assertIn("pic2.png", r.final)

    def test_the_same_prompt_twice_is_blocked_even_when_two_are_allowed(self):
        r = self.turn("make 2 images of a cat", [tool_call("a grey cat on a sofa"), tool_call("a grey cat on a sofa!", 2),
                                                 text("ok")])
        self.assertEqual(len(r.prompts), 1)
        self.assertEqual(r.tool_results[1]["result"], media_tools.CHAT_ALREADY_MADE)
        self.assertIn("generate_image", r.tools[1])                     # still one allowed: the model may try another

    def test_copies_wanted(self):
        w = media_tools.copies_wanted
        self.assertEqual([w("draw a cat"), w("make 2 images of a cat"), w("three pictures please"), w("a couple of logos"),
                          w("draw a few variations"), w("make 9 images"), w("make two different logo designs"),
                          w("an image 1080 pixels wide")], [1, 2, 3, 2, 3, 4, 2, 1])
        self.assertEqual(w("make 3 images", "video"), 1)

    def test_repeat_blocked(self):
        b = media_tools.repeat_blocked
        self.assertTrue(b(1, 1, "anything else", ["a cat"]))                  # as many as asked for
        self.assertTrue(b(1, 2, "A red bicycle, on a road", ["a red bicycle on a road"]))
        self.assertFalse(b(1, 2, "a blue whale under the sea", ["a red bicycle on a road"]))
        self.assertFalse(b(0, 1, "", []))

    # (e) the download tag survives the real finalize functions
    def test_download_tag_survives_finalize_reply_files(self):
        from routes.chat.delivery import finalize_reply_files, _finalize_media
        with tempfile.TemporaryDirectory() as td:
            root = Path(td).resolve()
            (root / "generated").mkdir()
            (root / "generated" / "pic1.png").write_bytes(b"\x89PNG")
            with (mock.patch("core.agent_tools.workspace.common_workspace", return_value=root),
                  mock.patch("routes.agent.download.common_workspace", return_value=root)):
                out, _ = finalize_reply_files("Here it is.\n\n[DOWNLOAD: generated/pic1.png]", [], "draw a cat", [], True)
                self.assertIn("[DOWNLOAD: generated/pic1.png]", out)
                self.assertNotIn("not created", out)
                out, _ = finalize_reply_files(_finalize_media("Ready.", [made(1)["markdown"]])[0], [], "draw a cat", [], True)
                self.assertEqual(out.count("[DOWNLOAD: generated/pic1.png]"), 1)
                # a tag for a file that is not there is still dropped
                out, _ = finalize_reply_files("[DOWNLOAD: generated/nope.png]", [], "draw a cat", [], True)
                self.assertNotIn("[DOWNLOAD: generated/nope.png]", out)

    def test_the_tag_path_resolves_for_the_download_route(self):
        from routes.agent.download import _resolve_requested_file
        with tempfile.TemporaryDirectory() as td:
            root = Path(td).resolve()
            (root / "generated").mkdir()
            (root / "generated" / "pic1.png").write_bytes(b"\x89PNG")
            with (mock.patch("core.agent_tools.workspace.common_workspace", return_value=root),
                  mock.patch("routes.agent.download.common_workspace", return_value=root)):
                p = _resolve_requested_file("generated/pic1.png")
                self.assertEqual(p, root / "generated" / "pic1.png")

    # (f) markdown_for
    def test_markdown_for(self):
        from core.media.storage import markdown_for
        img = markdown_for([{"path": "generated/a b.png", "mime": "image/png"}], "a [red] cat")
        self.assertEqual(img.split("\n\n"), ["![a  red  cat](/agent/raw?path=generated/a%20b.png)", "[DOWNLOAD: generated/a b.png]"])
        vid = markdown_for([{"path": "generated/v.mp4", "mime": "video/mp4"}], "x")
        self.assertEqual(vid.split("\n\n"), ["[VIDEO: generated/v.mp4]", "[DOWNLOAD: generated/v.mp4]"])

    # (g) a normal chat turn
    def test_normal_chat_is_unaffected(self):
        r = self.turn("what is the capital of France?", [text("Paris.")])
        self.assertEqual(r.final, "Paris.")
        self.assertEqual(r.prompts, [])
        self.assertNotIn("generate_image", r.tools[0])

    # (h) agent mode keeps the text it relies on
    def test_agent_result_text_is_unchanged_and_chat_text_is_short(self):
        res = made(1) | {"model": "sd", "where": "this PC"}
        agent = media_tools.result_text("image", res)
        self.assertIn("Include this markdown exactly in your reply:", agent)
        self.assertIn(res["markdown"], agent)
        chat = media_tools.chat_result_text("image", res)
        self.assertEqual(chat, "Made the image with sd (this PC); it is saved and already shown to the user with a download "
                               "link. Reply with one short sentence; do not repeat the picture or the link.")


if __name__ == "__main__":
    unittest.main()
