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

SERVER_CHECKED = {".py", ".pyw", ".json", ".toml", ".yaml", ".yml", ".xml", ".svg", ".jsx"}
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
        if ext == ".jsx":
            try:
                import tree_sitter
                import tree_sitter_javascript as _tsjs
                lang = tree_sitter.Language(_tsjs.language())
                p = tree_sitter.Parser(lang)
                tree = p.parse(text.encode("utf-8"))
                if tree.root_node.has_error:
                    def _find_err(n):
                        if n.is_missing or n.type == "ERROR":
                            return n
                        for c in n.children:
                            if c.has_error:
                                res = _find_err(c)
                                if res:
                                    return res
                        return None
                    err = _find_err(tree.root_node)
                    where = f"line {err.start_point[0] + 1}" if err else "unknown line"
                    return "fail", f"JSX syntax error at {where}"
                return "ok", "jsx syntax"
            except ImportError:
                return "skip", ""
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


def validate_code_syntax(path: str, text: str) -> tuple[bool, str]:
    """Validate syntax in-memory before committing changes to disk.
    Supports Python (ast), JSON, TOML, YAML, XML/SVG, and JavaScript/JSX (tree-sitter).
    Returns (True, '') when valid or skipped, (False, error_msg) when invalid syntax.
    """
    ext = PurePath(path).suffix.lower()
    if ext in (".js", ".mjs", ".cjs"):
        try:
            import tree_sitter
            import tree_sitter_javascript as _tsjs
            lang = tree_sitter.Language(_tsjs.language())
            p = tree_sitter.Parser(lang)
            tree = p.parse(text.encode("utf-8"))
            if tree.root_node.has_error:
                def _find_err(n):
                    if n.is_missing or n.type == "ERROR":
                        return n
                    for c in n.children:
                        if c.has_error:
                            res = _find_err(c)
                            if res:
                                return res
                    return None
                err = _find_err(tree.root_node)
                where = f"line {err.start_point[0] + 1}" if err else "unknown line"
                return False, f"JavaScript syntax error at {where}"
            return True, ""
        except Exception:
            return True, ""
    status, detail = verify_text(path, text)
    if status == "fail":
        return False, detail
    return True, ""

