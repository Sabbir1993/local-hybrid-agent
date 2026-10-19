"""tests/test_lsp_client.py - Unit tests for headless LSP client and JSON-RPC framing."""

import asyncio
import io
import unittest
from unittest import mock

from core.code_intel.lsp_client import (
    JsonRpcMessage,
    LSPClient,
    detect_language,
    file_path_to_uri,
    find_lsp_executable,
    uri_to_file_path,
)


class TestLspFramingAndPaths(unittest.TestCase):
    def test_path_uri_conversion(self):
        sample_path = "e:/AI/vulkan-arc/a770-dual-runtime/core/tokens.py"
        uri = file_path_to_uri(sample_path)
        self.assertTrue(uri.startswith("file://"))
        self.assertIn("tokens.py", uri)

        reverted = uri_to_file_path(uri)
        self.assertTrue(reverted.lower().endswith("tokens.py"))

    def test_detect_language(self):
        self.assertEqual(detect_language("server.py"), "python")
        self.assertEqual(detect_language("client.ts"), "typescript")
        self.assertEqual(detect_language("component.tsx"), "typescriptreact")
        self.assertEqual(detect_language("index.js"), "javascript")
        self.assertEqual(detect_language("app.jsx"), "javascriptreact")
        self.assertEqual(detect_language("main.rs"), "rust")
        self.assertIsNone(detect_language("README.md"))

    def test_json_rpc_encode_decode(self):
        async def _test():
            msg = {"jsonrpc": "2.0", "id": 1, "method": "test", "params": {"key": "value"}}
            encoded = JsonRpcMessage.encode(msg)
            self.assertTrue(encoded.startswith(b"Content-Length: "))
            self.assertIn(b"\r\n\r\n", encoded)

            reader = asyncio.StreamReader()
            reader.feed_data(encoded)
            reader.feed_eof()

            decoded = await JsonRpcMessage.read_from_stream(reader)
            self.assertEqual(decoded, msg)

        asyncio.run(_test())


class TestLspClientProtocol(unittest.TestCase):
    def test_mock_lsp_session(self):
        async def _test():
            client = LSPClient(command="mock-langserver")

            # Mock subprocess with simulated reader and writer
            mock_proc = mock.MagicMock()
            reader = asyncio.StreamReader()
            writer = mock.MagicMock()
            written_chunks = []

            def _write(data):
                written_chunks.append(data)

            writer.write.side_effect = _write
            writer.drain = mock.AsyncMock()

            mock_proc.stdin = writer
            mock_proc.stdout = reader
            mock_proc.returncode = None

            with mock.patch("asyncio.create_subprocess_exec", return_value=mock_proc):
                ok = await client.start()
                self.assertTrue(ok)
                self.assertTrue(client.is_running)

                # Feed initialize response into reader
                init_resp = {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "result": {
                        "capabilities": {
                            "definitionProvider": True,
                            "referencesProvider": True,
                        }
                    },
                }
                reader.feed_data(JsonRpcMessage.encode(init_resp))

                res = await client.initialize("e:/AI/workspace")
                self.assertIn("capabilities", res)

                # Test get_definition request
                def_resp = {
                    "jsonrpc": "2.0",
                    "id": 2,
                    "result": [
                        {
                            "uri": "file:///e:/AI/workspace/auth.py",
                            "range": {
                                "start": {"line": 10, "character": 4},
                                "end": {"line": 10, "character": 16},
                            },
                        }
                    ],
                }
                reader.feed_data(JsonRpcMessage.encode(def_resp))

                defs = await client.get_definition("e:/AI/workspace/main.py", 5, 2)
                self.assertEqual(len(defs), 1)
                self.assertEqual(defs[0]["line"], 11)
                self.assertEqual(defs[0]["character"], 5)

                await client.shutdown()
                self.assertFalse(client.is_running)

        asyncio.run(_test())


if __name__ == "__main__":
    unittest.main()
