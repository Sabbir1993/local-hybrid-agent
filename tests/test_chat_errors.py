import unittest

import httpx

from core.chat_errors import friendly_chat_error


class FriendlyChatErrorTests(unittest.TestCase):
    def test_a_crashed_model_server_is_explained(self):
        for e in (httpx.RemoteProtocolError(""), httpx.ReadError(""), httpx.ConnectError("")):
            msg = friendly_chat_error(e)
            self.assertIn("model server stopped", msg)
            self.assertIn("send your message again", msg)

    def test_timeout_has_its_own_message(self):
        self.assertIn("timed out", friendly_chat_error(httpx.ReadTimeout("")))

    def test_an_empty_message_never_leaves_a_bare_label(self):
        self.assertEqual(friendly_chat_error(RuntimeError("")), "⚠️ Chat error: RuntimeError")
        self.assertEqual(friendly_chat_error(ValueError("bad input")), "⚠️ Chat error: bad input")

    def test_transport_errors_do_not_leak_the_raw_text(self):
        raw = "[WinError 10061] http://127.0.0.1:8090 " + chr(69) + ":" + chr(92) + "models" + chr(92) + "x.gguf"
        msg = friendly_chat_error(httpx.ConnectError(raw))
        self.assertNotIn("127.0.0.1", msg)
        self.assertNotIn("models", msg)


if __name__ == "__main__":
    unittest.main()
