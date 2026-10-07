"""Reading attached videos: ffmpeg frame sampling (core/media/video.py), POST /media/video,
and the describe_image_bytes import-order regression."""

import asyncio
import io
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core import media, small_model  # noqa: E402
from core.small_model import find_ffmpeg  # noqa: E402

FFMPEG = find_ffmpeg()


def _clip(seconds=4, audio=True) -> bytes:
    tmp = Path(tempfile.mkdtemp())
    out = tmp / "c.mp4"
    cmd = [str(FFMPEG), "-y", "-loglevel", "error", "-f", "lavfi", "-i", f"testsrc=duration={seconds}:size=320x240:rate=10"]
    if audio:
        cmd += ["-f", "lavfi", "-i", f"sine=frequency=440:duration={seconds}"]
    cmd += ["-pix_fmt", "yuv420p", "-shortest", str(out)]
    subprocess.run(cmd, check=True)
    try:
        return out.read_bytes()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


class PickTimesTests(unittest.TestCase):
    def test_caps_and_spreads(self):
        ts = media.pick_times(120, [3, 3.5, 40, 41, 90], 8)
        self.assertLessEqual(len(ts), 8)
        self.assertEqual(ts, sorted(ts))
        self.assertLess(ts[0], 1)
        self.assertGreater(ts[-1], 60)

    def test_scene_and_grid_one_second_apart_merge(self):
        ts = media.pick_times(10, [0.5], 16)
        self.assertTrue(all(b - a >= 1.0 for a, b in zip(ts, ts[1:])))

    def test_very_short_clip_still_has_a_frame(self):
        self.assertEqual(len(media.pick_times(0.5, [], 16)) >= 1, True)


@unittest.skipIf(FFMPEG is None, "ffmpeg not installed")
class ExtractTests(unittest.TestCase):
    def test_frames_and_audio(self):
        res = media.video.extract(_clip(4), 6, 10)
        self.assertGreaterEqual(len(res["frames"]), 2)
        self.assertLessEqual(len(res["frames"]), 6)
        self.assertTrue(all(f[:2] == b"\xff\xd8" for _t, f in res["frames"]))
        self.assertAlmostEqual(res["duration"], 4, delta=0.6)
        self.assertEqual(res["wav"][:4], b"RIFF")

    def test_silent_video_has_no_audio(self):
        self.assertIsNone(media.video.extract(_clip(2, audio=False), 4, 10)["wav"])

    def test_too_long_rejected(self):
        with self.assertRaises(media.MediaError) as cm:
            media.video.extract(_clip(70, audio=False), 4, 1)
        self.assertIn("minutes", str(cm.exception))

    def test_not_a_video(self):
        with self.assertRaises(media.MediaError):
            media.video.extract(b"hello, this is text" * 50, 4, 10)

    def test_temp_folder_removed_even_on_failure(self):
        made = []
        real = tempfile.mkdtemp

        def spy(*a, **k):
            d = real(*a, **k)
            made.append(d)
            return d
        with mock.patch("tempfile.mkdtemp", spy):
            with self.assertRaises(media.MediaError):
                media.video.extract(b"junk" * 100, 4, 10)
        self.assertTrue(made)
        self.assertFalse(any(Path(d).exists() for d in made))


class NoFfmpegTests(unittest.TestCase):
    def test_clear_error(self):
        with mock.patch.object(small_model, "find_ffmpeg", lambda: None):
            with self.assertRaises(media.MediaError) as cm:
                media.video.extract(b"x", 4, 10)
        self.assertIn("isn't set up", str(cm.exception))


class RouteTests(unittest.TestCase):
    def _call(self, data, **patch):
        import routes.media as rm

        class _Up:
            def __init__(self, d): self._b = io.BytesIO(d)
            async def read(self, n=-1): return self._b.read(n)
        user = mock.Mock(id=1)
        with mock.patch.object(rm, "audit_log", lambda *a, **k: None):
            return asyncio.run(rm.media_video(_Up(data), user))

    def test_too_large_is_413(self):
        with mock.patch.dict(small_model.APP_CONFIG, {"media": {"max_video_mb": 1}}):
            r = self._call(b"0" * (1024 * 1024 + 5))
        self.assertEqual(r.status_code, 413)

    def test_no_ffmpeg_is_409(self):
        with mock.patch.object(small_model, "find_ffmpeg", lambda: None):
            r = self._call(b"abc")
        self.assertEqual(r.status_code, 409)

    @unittest.skipIf(FFMPEG is None, "ffmpeg not installed")
    def test_ok_returns_frames_and_transcript(self):
        async def fake_transcribe(user, wav, language=None):
            return {"text": "hello there"}
        with mock.patch.object(media, "transcribe", fake_transcribe):
            r = self._call(_clip(3))
        self.assertGreaterEqual(len(r["frames"]), 2)
        self.assertEqual(r["transcript"], "hello there")
        self.assertTrue(r["frames"][0]["b64"])

    @unittest.skipIf(FFMPEG is None, "ffmpeg not installed")
    def test_no_speech_model_is_a_note_not_an_error(self):
        async def no_stt(user, wav, language=None):
            raise media.MediaError("Speech to text isn't set up yet")
        with mock.patch.object(media, "transcribe", no_stt):
            r = self._call(_clip(2))
        self.assertIsNone(r["transcript"])
        self.assertIn("isn't set up", r["transcript_note"])


class DescribeImageRegression(unittest.TestCase):
    def test_describe_image_bytes_runs(self):
        from core import lanes
        png = (b"\x89PNG\r\n\x1a\n" + b"0" * 20)

        async def fake_post_chat(job, payload, **kw):
            self.assertEqual(job, "vision")
            self.assertGreater(payload["max_tokens"], 400 - 1)
            return {"choices": [{"message": {"content": "a cat"}}]}, None
        with mock.patch.object(lanes, "post_chat", fake_post_chat), \
                mock.patch("core.state.state") as st:
            st.process = None
            st.client = None
            st.profile = {}
            out = asyncio.run(small_model.describe_image_bytes(png))
        self.assertEqual(out, "a cat")


if __name__ == "__main__":
    unittest.main()
