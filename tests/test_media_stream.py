"""Live (chunked) STT: core/media/streaming.py sessions + overlap join + routes."""

import sys
import unittest
from pathlib import Path
from unittest import mock

from _patching import patch_in_package  # noqa: E402

import httpx  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from core import lanes, media, small_model  # noqa: E402
from core.media import streaming  # noqa: E402
from test_media import MediaBase, _wav, run  # noqa: E402


def _loud_wav(n=1600, amp=2000):
    """Audible WAV (constant tone-ish level): passes the silence gate."""
    import struct
    raw = struct.pack("<%dh" % n, *([amp] * n))
    return (b"RIFF" + struct.pack("<I", 36 + n * 2) + b"WAVEfmt "
            + struct.pack("<IHHIIHH", 16, 1, 1, 16000, 32000, 2, 16) + b"data" + struct.pack("<I", n * 2)
            + raw)


class JoinTests(unittest.TestCase):
    def test_exact_overlap(self):
        self.assertEqual(media.join_interims(["hello world", "world this is", "is a test"]),
                         "hello world this is a test")

    def test_case_insensitive_overlap(self):
        self.assertEqual(media.join_interims(["see you Hello", "hello again"]),
                         "see you Hello again")

    def test_no_overlap_just_joins(self):
        self.assertEqual(media.join_interims(["one", "two three"]), "one two three")

    def test_empty_and_blank_windows(self):
        self.assertEqual(media.join_interims(["", "hi there", "  ", "there friend"]),
                         "hi there friend")


class StreamSessionTests(MediaBase):
    def _whisper(self, texts):
        with mock.patch("core.profiles.in_helper_dir", lambda p: True), \
                mock.patch("pathlib.Path.exists", lambda self: True):
            lanes.save_local_lane("stt", {"kind": "stt", "model": "orchestrator/voice-models/ggml-large-v3-turbo-q5_0.bin",
                                          "gpu": -1, "port": 8095, "language": "en"})
        inst = small_model.small_models.instances["stt"]
        lanes.set_role_map(1, {"transcribe": "stt"})
        queue = list(texts)

        def h(req):
            return httpx.Response(200, json={"text": queue.pop(0) if queue else ""})
        inst.client = httpx.AsyncClient(base_url="http://127.0.0.1:8095",
                                        transport=httpx.MockTransport(h))

        async def _up():
            return None
        self._patches = [
            mock.patch.object(small_model, "find_whisper_server", lambda: Path("C:/w/whisper-server.exe")),
            mock.patch("pathlib.Path.exists", lambda self: True),
            mock.patch.object(inst, "ensure_loaded", _up),
        ]
        for p in self._patches:
            p.start()
        self.addCleanup(lambda: [p.stop() for p in self._patches])

    def test_happy_path_with_overlap(self):
        self._whisper(["hello world", "world this is", "is a test"])
        s = run(media.stream_start(self.user, "en"))
        r0 = run(media.stream_chunk(self.user, s["stream_id"], 0, _loud_wav()))
        self.assertEqual(r0["interim"], "hello world")
        r1 = run(media.stream_chunk(self.user, s["stream_id"], 1, _loud_wav()))
        self.assertEqual(r1["interim"], "hello world this is")
        r2 = run(media.stream_chunk(self.user, s["stream_id"], 2, _loud_wav()))
        self.assertEqual(r2["interim"], "hello world this is a test")
        fin = run(media.stream_finish(self.user, s["stream_id"]))
        self.assertEqual(fin["text"], "hello world this is a test")
        self.assertEqual(fin["chunks"], 3)
        self.assertIn("audio_s", fin)
        # consumed: finishing twice is gone
        with self.assertRaises(media.MediaError):
            run(media.stream_finish(self.user, s["stream_id"]))

    def test_out_of_order_rejected(self):
        self._whisper(["a", "b"])
        s = run(media.stream_start(self.user, None))
        run(media.stream_chunk(self.user, s["stream_id"], 0, _loud_wav()))
        with self.assertRaises(media.MediaError):
            run(media.stream_chunk(self.user, s["stream_id"], 0, _loud_wav()))
        with self.assertRaises(media.MediaError):
            run(media.stream_chunk(self.user, s["stream_id"], 5, _loud_wav()))

    def test_unknown_and_foreign_streams(self):
        self._whisper(["a"])
        with self.assertRaises(media.MediaError):
            run(media.stream_chunk(self.user, "nope-nope-nope", 0, _wav()))
        s = run(media.stream_start(self.user, None))
        other = mock.Mock(id=2, username="u2", role_names=["user"])
        with self.assertRaises(media.MediaError):
            run(media.stream_finish(other, s["stream_id"]))
        with self.assertRaises(media.MediaError):
            run(media.stream_chunk(other, s["stream_id"], 0, _wav()))

    def test_cancel_is_idempotent(self):
        self._whisper(["a"])
        s = run(media.stream_start(self.user, None))
        self.assertTrue(run(media.stream_cancel(self.user, s["stream_id"]))["ok"])
        self.assertTrue(run(media.stream_cancel(self.user, s["stream_id"]))["ok"])
        with self.assertRaises(media.MediaError):
            run(media.stream_finish(self.user, s["stream_id"]))

    def test_oversize_chunk_rejected(self):
        self._whisper(["a"])
        s = run(media.stream_start(self.user, None))
        with self.assertRaises(media.MediaError):
            run(media.stream_chunk(self.user, s["stream_id"], 0, _wav(2_000_000)))

    def test_expired_session_purged(self):
        self._whisper(["a"])
        s = run(media.stream_start(self.user, None))
        streaming._STREAMS[s["stream_id"]]["created"] -= streaming.MAX_AGE_S + 60
        with self.assertRaises(media.MediaError):
            run(media.stream_chunk(self.user, s["stream_id"], 0, _wav()))
        self.assertNotIn(s["stream_id"], streaming._STREAMS)

    def test_start_without_setup(self):
        with self.assertRaises(media.MediaError):
            run(media.stream_start(self.user, None))

    def test_wav_rms(self):
        self.assertEqual(streaming.wav_rms(_wav()), 0.0)
        self.assertGreater(streaming.wav_rms(_loud_wav()), 0.05)
        self.assertIsNone(streaming.wav_rms(b"not audio"))

    def test_silent_window_skipped_without_whisper(self):
        self._whisper(["hello"])   # must stay queued: whisper is never called
        s = run(media.stream_start(self.user, None))
        r = run(media.stream_chunk(self.user, s["stream_id"], 0, _wav()))
        self.assertEqual(r["text"], "")
        self.assertEqual(r["interim"], "")
        fin = run(media.stream_finish(self.user, s["stream_id"]))
        self.assertEqual(fin["text"], "")
        self.assertEqual(fin["chunks"], 1)

    def test_silence_between_speech_leaves_no_trace(self):
        self._whisper(["go", "stop"])
        s = run(media.stream_start(self.user, None))
        run(media.stream_chunk(self.user, s["stream_id"], 0, _loud_wav()))
        run(media.stream_chunk(self.user, s["stream_id"], 1, _wav()))   # room noise
        r = run(media.stream_chunk(self.user, s["stream_id"], 2, _loud_wav()))
        self.assertEqual(r["interim"], "go stop")


class StreamRouteTests(MediaBase):
    def setUp(self):
        super().setUp()
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        from core import deps
        from routes import lanes as lanes_routes, media as media_routes
        self.admin = False
        app = FastAPI()
        app.include_router(lanes_routes.router)
        app.include_router(media_routes.router)
        app.dependency_overrides[deps.get_current_user] = lambda: self.user
        self.rp = [*patch_in_package(lanes_routes, "user_has_permission", lambda u, k: self.admin),
                   *patch_in_package(lanes_routes, "audit_log", lambda *a, **k: None),
                   mock.patch.object(media_routes, "audit_log", lambda *a, **k: None)]
        for p in self.rp:
            p.start()
        self.client = TestClient(app)

    def tearDown(self):
        self.client.close()
        for p in self.rp:
            p.stop()
        super().tearDown()

    def _whisper(self, texts):
        with mock.patch("core.profiles.in_helper_dir", lambda p: True), \
                mock.patch("pathlib.Path.exists", lambda self: True):
            lanes.save_local_lane("stt", {"kind": "stt", "model": "orchestrator/voice-models/ggml-large-v3-turbo-q5_0.bin",
                                          "gpu": -1, "port": 8095, "language": "en"})
        inst = small_model.small_models.instances["stt"]
        lanes.set_role_map(1, {"transcribe": "stt"})
        queue = list(texts)

        def h(req):
            return httpx.Response(200, json={"text": queue.pop(0) if queue else ""})
        inst.client = httpx.AsyncClient(base_url="http://127.0.0.1:8095",
                                        transport=httpx.MockTransport(h))

        async def _up():
            return None
        patches = [
            mock.patch.object(small_model, "find_whisper_server", lambda: Path("C:/w/whisper-server.exe")),
            mock.patch("pathlib.Path.exists", lambda self: True),
            mock.patch.object(inst, "ensure_loaded", _up),
        ]
        for p in patches:
            p.start()
        self.addCleanup(lambda: [p.stop() for p in patches])

    def _post_chunk(self, sid, seq):
        return self.client.post("/media/transcribe_stream/chunk",
                                files={"file": ("win.wav", _loud_wav(), "audio/wav")},
                                data={"stream_id": sid, "seq": str(seq)})

    def test_http_flow(self):
        self._whisper(["good morning", "morning everyone"])
        r = self.client.post("/media/transcribe_stream/start", json={"language": "en"})
        self.assertEqual(r.status_code, 200, r.text)
        sid = r.json()["stream_id"]
        c0 = self._post_chunk(sid, 0)
        self.assertEqual(c0.status_code, 200, c0.text)
        self.assertEqual(c0.json()["interim"], "good morning")
        c1 = self._post_chunk(sid, 1)
        self.assertEqual(c1.json()["interim"], "good morning everyone")
        bad = self._post_chunk(sid, 1)
        self.assertEqual(bad.status_code, 400)
        fin = self.client.post("/media/transcribe_stream/finish", json={"stream_id": sid})
        self.assertEqual(fin.status_code, 200, fin.text)
        self.assertEqual(fin.json()["text"], "good morning everyone")
        gone = self.client.post("/media/transcribe_stream/finish", json={"stream_id": sid})
        self.assertEqual(gone.status_code, 404)

    def test_http_cancel(self):
        self._whisper(["x"])
        sid = self.client.post("/media/transcribe_stream/start", json={}).json()["stream_id"]
        self.assertEqual(self.client.post("/media/transcribe_stream/cancel",
                                          json={"stream_id": sid}).status_code, 200)
        self.assertEqual(self.client.post("/media/transcribe_stream/finish",
                                          json={"stream_id": sid}).status_code, 404)


if __name__ == "__main__":
    unittest.main()
