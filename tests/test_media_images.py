"""Pictures given to the image model (core/media_images.py): type / size checks,
EXIF stripped, downscaled, paths only inside the user's own folder.

Run: python -m unittest tests.test_media_images -v
"""

import base64
import io
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core import agent_tools, media_images  # noqa: E402
from core.request_context import set_current_user  # noqa: E402


def _img(fmt="PNG", size=(64, 48), mode="RGB", **save):
    b = io.BytesIO()
    Image.new(mode, size, (200, 30, 30) if mode == "RGB" else (200, 30, 30, 128)).save(b, format=fmt, **save)
    return b.getvalue()


def _b64(data):
    return base64.b64encode(data).decode()


class CleanPictureTests(unittest.TestCase):
    def test_png_jpeg_webp_become_png(self):
        for fmt in ("PNG", "JPEG", "WEBP"):
            out = media_images.clean_picture(_img(fmt))
            self.assertTrue(out["png"].startswith(b"\x89PNG"), fmt)
            self.assertEqual((out["w"], out["h"]), (64, 48))

    def test_alpha_is_kept(self):
        out = media_images.clean_picture(_img("PNG", mode="RGBA"))
        self.assertEqual(Image.open(io.BytesIO(out["png"])).mode, "RGBA")

    def test_other_types_and_fakes_are_refused(self):
        for bad in (_img("GIF"), _img("BMP"), b"<svg xmlns='http://www.w3.org/2000/svg'/>",
                    b"\x89PNG\r\n\x1a\n" + b"not really a png" * 10):
            with self.assertRaises(media_images.InputError):
                media_images.clean_picture(bad)

    def test_exif_location_is_dropped(self):
        exif = Image.Exif()
        exif[0x010F] = "SecretCam"                  # Make
        exif[0x8825] = {1: "N", 2: (23.0, 48.0, 0.0)}   # GPS info (Dhaka-ish)
        src = _img("JPEG", exif=exif.tobytes())
        self.assertIn(b"SecretCam", src)
        out = media_images.clean_picture(src)
        self.assertNotIn(b"SecretCam", out["png"])
        self.assertNotIn(b"eXIf", out["png"])
        self.assertFalse(Image.open(io.BytesIO(out["png"])).getexif())

    def test_big_pictures_are_scaled_down(self):
        out = media_images.clean_picture(_img("PNG", size=(3000, 1500)))
        self.assertEqual((out["w"], out["h"]), (1536, 768))

    def test_too_many_pixels_or_too_small(self):
        with mock.patch.object(media_images, "MAX_PIXELS", 1000):
            with self.assertRaises(media_images.InputError):
                media_images.clean_picture(_img("PNG", size=(64, 48)))
        with self.assertRaises(media_images.InputError):
            media_images.clean_picture(_img("PNG", size=(8, 8)))

    def test_too_many_bytes(self):
        with mock.patch.object(media_images, "MAX_BYTES", 100):
            with self.assertRaises(media_images.InputError):
                media_images.clean_picture(_img("PNG"))


class PrepareInputsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.p = mock.patch.object(agent_tools, "COMMON_ROOT", Path(self.tmp.name))
        self.p.start()
        set_current_user(1)
        mine = Path(self.tmp.name) / "user_1" / "generated"
        mine.mkdir(parents=True)
        (mine / "a.png").write_bytes(_img("PNG"))
        other = Path(self.tmp.name) / "user_2" / "generated"
        other.mkdir(parents=True)
        (other / "secret.png").write_bytes(_img("PNG"))

    def tearDown(self):
        set_current_user(None)
        self.p.stop()
        self.tmp.cleanup()

    def test_b64_data_url_and_own_path(self):
        out = media_images.prepare_inputs([{"b64": "data:image/jpeg;base64," + _b64(_img("JPEG"))},
                                           {"path": "generated/a.png"}])
        self.assertEqual(len(out), 2)

    def test_other_users_files_are_out_of_reach(self):
        for path in ("../user_2/generated/secret.png", str(Path(self.tmp.name) / "user_2/generated/secret.png")):
            with self.assertRaises(media_images.InputError, msg=path):
                media_images.prepare_inputs([{"path": path}])

    def test_limits(self):
        with self.assertRaises(media_images.InputError):
            media_images.prepare_inputs([{"path": "generated/a.png"}] * 3, limit=2)
        with self.assertRaises(media_images.InputError):
            media_images.prepare_inputs([{"b64": "!!!not base64!!!"}])
        with self.assertRaises(media_images.InputError):
            media_images.prepare_inputs([{}])
        with mock.patch.object(media_images, "MAX_TOTAL", 100):
            with self.assertRaises(media_images.InputError):
                media_images.prepare_inputs([{"path": "generated/a.png"}])


if __name__ == "__main__":
    unittest.main()
