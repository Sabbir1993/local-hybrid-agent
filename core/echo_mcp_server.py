"""Minimal stdio MCP server for testing: echoes text, adds numbers.

Speaks newline-delimited JSON-RPC 2.0 on stdin/stdout per the MCP stdio transport.
Run from config/app.json:
  "mcp_servers": {"echo": {"transport": "stdio", "command": "python",
                           "args": ["core/echo_mcp_server.py"]}}
"""
import json
import sys

TOOLS = [
    {
        "name": "ping",
        "description": "Return the text sent, prefixed with pong.",
        "inputSchema": {"type": "object",
                        "properties": {"text": {"type": "string"}},
                        "required": ["text"]},
    },
    {
        "name": "add",
        "description": "Add two integers.",
        "inputSchema": {"type": "object",
                        "properties": {"a": {"type": "integer"}, "b": {"type": "integer"}},
                        "required": ["a", "b"]},
    },
]


def handle(method, params):
    if method == "initialize":
        return {"protocolVersion": "2025-03-26",
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "echo", "version": "1.0"}}
    if method == "tools/list":
        return {"tools": TOOLS}
    if method == "tools/call":
        name = (params or {}).get("name")
        args = (params or {}).get("arguments") or {}
        if name == "ping":
            return {"content": [{"type": "text", "text": "pong: " + str(args.get("text", ""))}]}
        if name == "add":
            return {"content": [{"type": "text", "text": str(int(args.get("a", 0)) + int(args.get("b", 0)))}]}
        return {"content": [{"type": "text", "text": f"error: unknown tool {name}"}]}
    return None


def main():
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            continue
        method = msg.get("method")
        rid = msg.get("id")
        if rid is None:
            continue   # notification from client -> no response needed
        try:
            result = handle(method, msg.get("params"))
            out = {"jsonrpc": "2.0", "id": rid, "result": result or {}}
        except Exception as e:
            out = {"jsonrpc": "2.0", "id": rid,
                   "error": {"code": -32603, "message": str(e)}}
        sys.stdout.write(json.dumps(out) + "\n")
        sys.stdout.flush()


if __name__ == "__main__":
    main()
