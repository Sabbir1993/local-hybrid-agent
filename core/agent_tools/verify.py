"""Syntax check of a file's text after the agent writes it (the "verify loop").

verify_text() runs on the server from the content it just wrote, so it needs no device round
trip. JavaScript is the exception: it has no server-side parser, so callers may ask the
companion (`fs.verify`, node --check) - see core/agent_tools/verify_loop.py.

Result: (status, detail) with status "ok", "fail" or "skip". `detail` never contains file
content beyond one short error line.
"""
import ast
import json
import xml.etree.ElementTree as ET
from pathlib import PurePath

SERVER_CHECKED = {".py", ".pyw", ".json", ".toml", ".yaml", ".yml", ".xml", ".svg"}
COMPANION_CHECKED = {".js", ".mjs", ".cjs"}


def _short(msg: str) -> str:
    return " ".join(str(msg).split())[:240]


def verify_text(path: str, text: str) -> tuple[str, str]:
    ext = PurePath(path).suffix.lower()
    try:
        if ext in (".py", ".pyw"):
            ast.parse(text, filename=path)
            return "ok", "python syntax"
        if ext == ".json":
            json.loads(text)
            return "ok", "json"
        if ext == ".toml":
            import tomllib
            tomllib.loads(text)
            return "ok", "toml"
        if ext in (".yaml", ".yml"):
            try:
                import yaml
            except ImportError:
                return "skip", ""
            list(yaml.safe_load_all(text))
            return "ok", "yaml"
        if ext in (".xml", ".svg"):
            ET.fromstring(text)
            return "ok", "xml"
    except SyntaxError as e:
        where = f"line {e.lineno}" if e.lineno else "unknown line"
        return "fail", _short(f"{type(e).__name__} at {where}: {e.msg}")
    except json.JSONDecodeError as e:
        return "fail", _short(f"JSON error at line {e.lineno} col {e.colno}: {e.msg}")
    except ET.ParseError as e:
        return "fail", _short(f"XML error at {e.position}: {e}")
    except Exception as e:
        return "fail", _short(f"{type(e).__name__}: {e}")
    return "skip", ""
