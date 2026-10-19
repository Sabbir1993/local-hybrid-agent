"""core/code_intel/lsp_client.py - Headless Language Server Protocol (LSP) Client.

Communicates over stdio using JSON-RPC 2.0 with standard language servers:
- Python: pyright-langserver, pylsp, jedi-language-server
- TypeScript / JavaScript: typescript-language-server
- Rust: rust-analyzer
- Go: gopls

Provides semantic, type-resolved code intelligence (cross-file definitions,
references, hover, and document symbols) with automatic discovery and graceful
fallback to Tree-sitter syntactic indexing when no language server is installed.
"""

import asyncio
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple, Union
from urllib.parse import unquote, urlparse
from urllib.request import pathname2url


# Language server command search candidates per language ID
LSP_SERVERS: Dict[str, List[str]] = {
    "python": ["pyright-langserver", "pylsp", "jedi-language-server"],
    "typescript": ["typescript-language-server"],
    "javascript": ["typescript-language-server"],
    "rust": ["rust-analyzer"],
    "go": ["gopls"],
}

# File extension to LSP language ID mapping
EXT_TO_LANG: Dict[str, str] = {
    ".py": "python",
    ".ts": "typescript",
    ".tsx": "typescriptreact",
    ".js": "javascript",
    ".jsx": "javascriptreact",
    ".rs": "rust",
    ".go": "go",
}


def file_path_to_uri(file_path: Union[str, Path]) -> str:
    """Convert a filesystem path to a file:// URI."""
    p = Path(file_path).resolve()
    return f"file://{pathname2url(str(p))}"


def uri_to_file_path(uri: str) -> str:
    """Convert a file:// URI to a filesystem path."""
    parsed = urlparse(uri)
    path = unquote(parsed.path)
    if os.name == "nt" and path.startswith("/") and len(path) > 2 and path[2] == ":":
        path = path[1:]
    return os.path.normpath(path)


def detect_language(path: Union[str, Path]) -> Optional[str]:
    ext = Path(path).suffix.lower()
    return EXT_TO_LANG.get(ext)


def find_lsp_executable(language: str) -> Optional[str]:
    """Check PATH for an available language server binary for the given language."""
    candidates = LSP_SERVERS.get(language, [])
    for cand in candidates:
        exe = shutil.which(cand)
        if exe:
            return exe
    return None


class JsonRpcMessage:
    """Encodes and decodes JSON-RPC 2.0 messages framed by Content-Length headers."""

    @staticmethod
    def encode(payload: dict) -> bytes:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        header = f"Content-Length: {len(data)}\r\n\r\n".encode("ascii")
        return header + data

    @classmethod
    async def read_from_stream(cls, reader: asyncio.StreamReader) -> Optional[dict]:
        content_length = None
        while True:
            line_bytes = await reader.readline()
            if not line_bytes:
                return None
            line = line_bytes.decode("ascii", errors="replace").strip()
            if not line:
                if content_length is not None:
                    break
                continue
            if line.lower().startswith("content-length:"):
                try:
                    content_length = int(line.split(":", 1)[1].strip())
                except ValueError:
                    content_length = None

        if content_length is None or content_length <= 0:
            return None

        body = await reader.readexactly(content_length)
        return json.loads(body.decode("utf-8", errors="replace"))


class LSPClient:
    """Asynchronous headless LSP client managing one language server subprocess over stdio."""

    def __init__(self, command: str, args: Optional[List[str]] = None, cwd: Optional[str] = None):
        self.command = command
        self.args = args or ["--stdio"]
        self.cwd = cwd
        self.process: Optional[asyncio.subprocess.Process] = None
        self._seq = 0
        self._pending_requests: Dict[int, asyncio.Future] = {}
        self._reader_task: Optional[asyncio.Task] = None
        self._server_capabilities: dict = {}
        self._running = False

    @property
    def is_running(self) -> bool:
        return self._running and self.process is not None and self.process.returncode is None

    async def start(self) -> bool:
        """Spawn the language server process and start the background stdio listener."""
        if self.is_running:
            return True
        try:
            cmd = [self.command] + self.args
            self.process = await asyncio.create_subprocess_exec(
                *cmd,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
                cwd=self.cwd,
            )
            self._running = True
            self._reader_task = asyncio.create_task(self._read_loop())
            return True
        except Exception:
            self._running = False
            self.process = None
            return False

    async def _read_loop(self) -> None:
        """Background reader reading JSON-RPC responses from the server's stdout."""
        while self._running and self.process and self.process.stdout:
            try:
                msg = await JsonRpcMessage.read_from_stream(self.process.stdout)
                if msg is None:
                    break
                if "id" in msg and msg["id"] in self._pending_requests:
                    fut = self._pending_requests.pop(msg["id"])
                    if not fut.done():
                        if "error" in msg:
                            fut.set_exception(RuntimeError(f"LSP error: {msg['error']}"))
                        else:
                            fut.set_result(msg.get("result"))
                # Notifications / server requests are ignored or logged
            except asyncio.CancelledError:
                break
            except Exception:
                break
        self._running = False

    async def request(self, method: str, params: Optional[dict] = None, timeout: float = 10.0) -> Any:
        """Send a JSON-RPC request and wait for the response."""
        if not self.is_running or not self.process or not self.process.stdin:
            raise RuntimeError("LSP server is not running")

        self._seq += 1
        req_id = self._seq
        payload = {
            "jsonrpc": "2.0",
            "id": req_id,
            "method": method,
            "params": params or {},
        }

        loop = asyncio.get_running_loop()
        fut = loop.create_future()
        self._pending_requests[req_id] = fut

        msg_bytes = JsonRpcMessage.encode(payload)
        try:
            self.process.stdin.write(msg_bytes)
            await self.process.stdin.drain()
            return await asyncio.wait_for(fut, timeout=timeout)
        except Exception:
            self._pending_requests.pop(req_id, None)
            raise

    async def notify(self, method: str, params: Optional[dict] = None) -> None:
        """Send a JSON-RPC notification (no response expected)."""
        if not self.is_running or not self.process or not self.process.stdin:
            return
        payload = {
            "jsonrpc": "2.0",
            "method": method,
            "params": params or {},
        }
        msg_bytes = JsonRpcMessage.encode(payload)
        try:
            self.process.stdin.write(msg_bytes)
            await self.process.stdin.drain()
        except Exception:
            pass

    async def initialize(self, root_path: str) -> dict:
        """Perform LSP initialize handshake."""
        root_uri = file_path_to_uri(root_path)
        params = {
            "processId": os.getpid(),
            "rootUri": root_uri,
            "rootPath": str(root_path),
            "capabilities": {
                "textDocument": {
                    "definition": {"dynamicRegistration": False},
                    "references": {"dynamicRegistration": False},
                    "hover": {"contentFormat": ["markdown", "plaintext"]},
                    "documentSymbol": {"hierarchicalDocumentSymbolSupport": True},
                },
                "workspace": {"applyEdit": False},
            },
            "initializationOptions": {},
        }
        res = await self.request("initialize", params)
        await self.notify("initialized", {})
        if isinstance(res, dict):
            self._server_capabilities = res.get("capabilities", {})
        return res or {}

    async def did_open(self, file_path: str, text: str, language_id: Optional[str] = None) -> None:
        """Notify the server that a document was opened or updated."""
        uri = file_path_to_uri(file_path)
        lang = language_id or detect_language(file_path) or "plaintext"
        await self.notify("textDocument/didOpen", {
            "textDocument": {
                "uri": uri,
                "languageId": lang,
                "version": 1,
                "text": text,
            }
        })

    async def get_definition(self, file_path: str, line: int, character: int) -> List[dict]:
        """Request textDocument/definition. Line and character are 0-indexed per LSP spec."""
        uri = file_path_to_uri(file_path)
        params = {
            "textDocument": {"uri": uri},
            "position": {"line": max(0, line), "character": max(0, character)},
        }
        try:
            raw = await self.request("textDocument/definition", params)
        except Exception:
            return []
        if not raw:
            return []
        items = raw if isinstance(raw, list) else [raw]
        results = []
        for item in items:
            t_uri = item.get("uri") or item.get("targetUri")
            t_range = item.get("range") or item.get("targetRange") or item.get("targetSelectionRange") or {}
            start = t_range.get("start", {})
            end = t_range.get("end", {})
            if t_uri:
                results.append({
                    "file": uri_to_file_path(t_uri),
                    "line": (start.get("line", 0)) + 1,
                    "character": (start.get("character", 0)) + 1,
                    "end_line": (end.get("line", 0)) + 1,
                    "end_character": (end.get("character", 0)) + 1,
                })
        return results

    async def get_references(self, file_path: str, line: int, character: int, include_decl: bool = False) -> List[dict]:
        """Request textDocument/references."""
        uri = file_path_to_uri(file_path)
        params = {
            "textDocument": {"uri": uri},
            "position": {"line": max(0, line), "character": max(0, character)},
            "context": {"includeDeclaration": include_decl},
        }
        try:
            raw = await self.request("textDocument/references", params)
        except Exception:
            return []
        if not isinstance(raw, list):
            return []
        results = []
        for item in raw:
            t_uri = item.get("uri")
            t_range = item.get("range", {})
            start = t_range.get("start", {})
            end = t_range.get("end", {})
            if t_uri:
                results.append({
                    "file": uri_to_file_path(t_uri),
                    "line": (start.get("line", 0)) + 1,
                    "character": (start.get("character", 0)) + 1,
                    "end_line": (end.get("line", 0)) + 1,
                    "end_character": (end.get("character", 0)) + 1,
                })
        return results

    async def get_hover(self, file_path: str, line: int, character: int) -> Optional[str]:
        """Request textDocument/hover."""
        uri = file_path_to_uri(file_path)
        params = {
            "textDocument": {"uri": uri},
            "position": {"line": max(0, line), "character": max(0, character)},
        }
        try:
            raw = await self.request("textDocument/hover", params)
        except Exception:
            return None
        if not raw or not isinstance(raw, dict):
            return None
        contents = raw.get("contents")
        if isinstance(contents, str):
            return contents
        if isinstance(contents, dict) and "value" in contents:
            return str(contents["value"])
        if isinstance(contents, list):
            parts = [c.get("value", str(c)) if isinstance(c, dict) else str(c) for c in contents]
            return "\n".join(parts)
        return None

    async def shutdown(self) -> None:
        """Gracefully shut down the server."""
        if not self.is_running:
            return
        try:
            await self.request("shutdown", {}, timeout=2.0)
            await self.notify("exit", {})
        except Exception:
            pass
        if self.process:
            try:
                self.process.terminate()
            except Exception:
                pass
        self._running = False
        if self._reader_task and not self._reader_task.done():
            self._reader_task.cancel()


class LSPManager:
    """Manages active LSPClient instances per workspace root and language."""

    def __init__(self):
        self._clients: Dict[Tuple[str, str], LSPClient] = {}
        self._lock = asyncio.Lock()

    async def get_client(self, workspace_root: str, language: str) -> Optional[LSPClient]:
        key = (os.path.normpath(workspace_root), language)
        async with self._lock:
            client = self._clients.get(key)
            if client and client.is_running:
                return client

            exe = find_lsp_executable(language)
            if not exe:
                return None

            client = LSPClient(command=exe, cwd=workspace_root)
            started = await client.start()
            if not started:
                return None
            try:
                await client.initialize(workspace_root)
                self._clients[key] = client
                return client
            except Exception:
                await client.shutdown()
                return None

    async def close_all(self) -> None:
        async with self._lock:
            for client in self._clients.values():
                await client.shutdown()
            self._clients.clear()


_MANAGER = LSPManager()


async def get_lsp_client(workspace_root: str, language: str) -> Optional[LSPClient]:
    """Retrieve or spawn an active LSP client for the given workspace and language."""
    return await _MANAGER.get_client(workspace_root, language)
