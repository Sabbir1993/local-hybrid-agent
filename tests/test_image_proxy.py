"""core/image_proxy.py: only URLs a search tool returned, only public hosts, only real raster images, size-capped."""
import unittest
from unittest import mock

from core import image_proxy, net_guard
from core.net_guard import FetchResult

PNG = b"\x89PNG\r\n\x1a\n" + b"0" * 32
URL = "https://tse1.mm.bing.net/th?id=abc"


def _result(content=PNG, status=200, truncated=False):
    return FetchResult(url=URL, status_code=status, headers={}, content=content, encoding="utf-8",
                       truncated=truncated, history=[])


class ImageProxy(unittest.TestCase):
    def setUp(self):
        image_proxy._allowed.clear()
        image_proxy._cache.clear()

    def test_only_urls_a_search_returned_for_this_user(self):
        with mock.patch.object(net_guard, "guarded_get", return_value=_result()) as g:
            with self.assertRaises(image_proxy.ProxyRefused):
                image_proxy.fetch(1, URL)                          # never remembered
            g.assert_not_called()
            image_proxy.remember(1, [URL])
            with self.assertRaises(image_proxy.ProxyRefused):
                image_proxy.fetch(2, URL)                          # another user's result
            self.assertEqual(image_proxy.fetch(1, URL), ("image/png", PNG))

    def test_exfiltration_url_is_refused(self):
        image_proxy.remember(1, [URL])
        with self.assertRaises(image_proxy.ProxyRefused):
            image_proxy.fetch(1, "https://evil.example/?q=secret")

    def test_https_only_and_expiry(self):
        image_proxy.remember(1, ["http://x.example/a.png", URL])
        with self.assertRaises(image_proxy.ProxyRefused):
            image_proxy.fetch(1, "http://x.example/a.png")
        image_proxy._allowed[1][URL] = 0                         # expired
        self.assertFalse(image_proxy.is_allowed(1, URL))

    def test_must_be_a_real_raster_image_within_the_size_cap(self):
        image_proxy.remember(1, [URL])
        for bad in (_result(b"<svg onload=alert(1)>"), _result(b"MZ\x90\x00"), _result(truncated=True),
                    _result(status=404), _result(b"")):
            image_proxy._cache.clear()
            with mock.patch.object(net_guard, "guarded_get", return_value=bad):
                with self.assertRaises(image_proxy.ProxyRefused):
                    image_proxy.fetch(1, URL)

    def test_private_hosts_are_blocked_by_the_ssrf_guard(self):
        u = "https://10.0.0.5/a.png"
        image_proxy.remember(1, [u])
        with self.assertRaises(image_proxy.ProxyRefused):
            image_proxy.fetch(1, u)

    def test_sniff_and_cache(self):
        self.assertEqual(image_proxy.sniff(b"\xff\xd8\xff\xe0abc"), "image/jpeg")
        self.assertEqual(image_proxy.sniff(b"GIF89a.."), "image/gif")
        self.assertEqual(image_proxy.sniff(b"RIFF\x00\x00\x00\x00WEBPVP8 "), "image/webp")
        image_proxy.remember(1, [URL])
        with mock.patch.object(net_guard, "guarded_get", return_value=_result()) as g:
            image_proxy.fetch(1, URL)
            image_proxy.fetch(1, URL)
            self.assertEqual(g.call_count, 1)


if __name__ == "__main__":
    unittest.main()
