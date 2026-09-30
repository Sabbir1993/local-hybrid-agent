import json
import re
from typing import Optional, Union

from ..tool_args import SHELL_COMMAND_KEYS, shell_command


def validate_and_repair_tool_args(tool_name: str, args: dict, query_hint: str = "") -> tuple[dict, Optional[str]]:
    repaired = dict(args) if isinstance(args, dict) else {}

    if tool_name in ("write_file", "edit_file", "read_file", "revert"):
        p = repaired.get("path") or repaired.get("file") or repaired.get("filename")
        if not p:
            if query_hint:
                m = re.search(r'\b([\w\-./\\]+\.(?:html|htm|py|js|ts|json|css|txt|md|php|sh|bat|ps1))\b', query_hint, re.IGNORECASE)
                if m:
                    p = m.group(1)
            if not p and tool_name == "write_file" and repaired.get("content"):
                c_low = str(repaired["content"])[:300].lower()
                if "<!doctype html" in c_low or "<html" in c_low:
                    p = "index.html"
                elif "def " in c_low or "import " in c_low:
                    p = "main.py"
                else:
                    p = "output.txt"

        if not p:
            return repaired, f"error: path required for {tool_name}"

        p_str = str(p).replace("\\", "/").strip().lstrip("/")
        if p_str.startswith("workspace/"):
            p_str = p_str[10:]
        if ".." in p_str.split("/"):
            return repaired, "error: sandbox violation: path cannot traverse outside workspace root (..)"
        repaired["path"] = p_str

    if tool_name == "run_shell":
        cmd = shell_command(repaired)
        if not cmd:
            return repaired, ('error: command required - pass the shell command line as "command", '
                              'e.g. {"command": "dir"}')
        repaired["command"] = cmd
        for alias in SHELL_COMMAND_KEYS[1:]:
            repaired.pop(alias, None)

    if tool_name == "write_file":
        if "content" not in repaired:
            for alt in ("code", "text", "body", "source"):
                if alt in repaired:
                    repaired["content"] = repaired[alt]
                    break
        if "content" not in repaired or repaired["content"] is None:
            return repaired, "error: content required for write_file"
        repaired["content"] = str(repaired["content"])

    if tool_name == "edit_file":
        if not repaired.get("old_string"):
            return repaired, "error: old_string required for edit_file"
        if "new_string" not in repaired or repaired["new_string"] is None:
            return repaired, "error: new_string required for edit_file"

    if tool_name == "run_python":
        if not repaired.get("code"):
            for alt in ("command", "content", "script"):
                if alt in repaired:
                    repaired["code"] = repaired[alt]
                    break
        if not repaired.get("code"):
            return repaired, "error: code required for run_python"

    return repaired, None


def safe_parse_and_repair_args(raw: Union[str, dict], tool_name: str = "", query_hint: str = "") -> dict:
    if isinstance(raw, dict):
        out = dict(raw)
    elif not isinstance(raw, str) or not raw.strip():
        out = {}
    else:
        raw = raw.strip()
        out = None

        try:
            res = json.loads(raw)
            if isinstance(res, dict):
                out = res
        except Exception:
            pass

        if out is None:
            try:
                res = json.loads(raw, strict=False)
                if isinstance(res, dict):
                    out = res
            except Exception:
                pass

        if out is None:
            for suffix in ['"}', '"\n}', '}', '"]}', '"]', '"']:
                try:
                    res = json.loads(raw + suffix, strict=False)
                    if isinstance(res, dict):
                        out = res
                        break
                except Exception:
                    pass

        if out is None:
            out = {}
            m_path = re.search(r'"(?:path|file|filename)"\s*:\s*"([^"]+)"', raw)
            if m_path:
                out["path"] = m_path.group(1)

            m_cont = re.search(r'"content"\s*:\s*"?([\s\S]*)$', raw)
            if m_cont:
                c = m_cont.group(1)
                if c.startswith('"'):
                    c = c[1:]
                if c.endswith('"}'):
                    c = c[:-2]
                elif c.endswith('"'):
                    c = c[:-1]
                c = c.replace(r'\"', '"').replace(r'\n', '\n').replace(r'\t', '\t').replace(r'\\', '\\')
                out["content"] = c

            m_code = re.search(r'"(?:code|command)"\s*:\s*"?([\s\S]*)$', raw)
            if m_code and "content" not in out:
                c = m_code.group(1)
                if c.startswith('"'):
                    c = c[1:]
                if c.endswith('"}'):
                    c = c[:-2]
                elif c.endswith('"'):
                    c = c[:-1]
                c = c.replace(r'\"', '"').replace(r'\n', '\n').replace(r'\t', '\t').replace(r'\\', '\\')
                key = "code" if "code" in raw else "command"
                out[key] = c

            if not out:
                out = {"raw": raw}

    if tool_name in ("write_file", "edit_file", "read_file") and not out.get("path") and not out.get("file") and not out.get("filename"):
        if query_hint:
            m_fn = re.search(r'\b([\w\-./\\]+\.[a-zA-Z0-9_]+)\b', query_hint)
            if m_fn and not m_fn.group(1).endswith((".png", ".jpg", ".jpeg", ".gguf")):
                out["path"] = m_fn.group(1)
        if not out.get("path") and out.get("content"):
            c_low = out["content"][:300].lower()
            if "<!doctype html" in c_low or "<html" in c_low:
                out["path"] = "index.html"
            elif "def " in c_low or "import " in c_low:
                out["path"] = "main.py"

    if out.get("content") and isinstance(out["content"], str):
        c_text = out["content"]
        c_low = c_text.lower()
        if "<!doctype html" in c_low or "<html" in c_low:
            if "<script" in c_low and "</script>" not in c_low.split("<script")[-1]:
                c_text += "\n</script>"
            if "</body>" not in c_low:
                c_text += "\n</body>"
            if "</html>" not in c_low:
                c_text += "\n</html>"
            out["content"] = c_text

    return out


def _registered_tool_names() -> set:
    """Names of every registered tool (built-in, MCP, plugin) - what a text call may name."""
    try:
        from ..registry import registry
        return {str((t.get("function") or {}).get("name") or "") for t in registry.schemas()} - {""}
    except Exception:
        return set()


# "Tool Call: read_skill({...})", "Calling read_file({...})", "Action: list_files()"
_CALL_LINE_RX = re.compile(
    r"(?im)^[ \t>*`_-]*(?:tool[ _-]?call|calling(?: tool)?|call|action|using tool|invoke)"
    r"[ \t*_]*[:\-]?[ \t`*_]*([A-Za-z_][\w.\-]*)[`*]*[ \t]*\([ \t]*(\{[\s\S]*?\})?[ \t]*\)")
# GLM-style call: <tool_call>name<arg_key>k</arg_key><arg_value>v</arg_value>...</tool_call>
# (the closing tag is sometimes missing when the model stops right after the last value)
_KV_CALL_RX = re.compile(r"<tool_call>\s*([A-Za-z_][\w.\-]*)\s*((?:<arg_key>[\s\S]*?</arg_key>\s*<arg_value>[\s\S]*?</arg_value>\s*)*)(?:</tool_call>|$)")
_KV_PAIR_RX = re.compile(r"<arg_key>([\s\S]*?)</arg_key>\s*<arg_value>([\s\S]*?)</arg_value>")
# a line that is only  name({...})
_BARE_CALL_RX = re.compile(r"(?m)^[ \t]*`?([A-Za-z_][\w.\-]*)`?\(\s*(\{[^\n]*\})?\s*\)[ \t]*$")


def _extract_text_tool_calls(text: str, known: set = None) -> list:
    if not text:
        return []
    calls = []
    
    for m in re.finditer(r"<tool_call>([\s\S]*?)</tool_call>", text):
        raw = m.group(1).strip()
        d = safe_parse_and_repair_args(raw)
        if isinstance(d, dict) and "name" in d:
            args = d.get("arguments", {})
            calls.append({
                "id": f"call_txt_{len(calls)}",
                "type": "function",
                "function": {
                    "name": d["name"],
                    "arguments": json.dumps(args) if isinstance(args, dict) else str(args)
                }
            })

    if not calls:
        for m in re.finditer(r"```(?:json)?\s*(\{\s*\"name\"\s*:[\s\S]*?\})\s*```", text):
            raw = m.group(1).strip()
            d = safe_parse_and_repair_args(raw)
            if isinstance(d, dict) and "name" in d:
                args = d.get("arguments", {})
                calls.append({
                    "id": f"call_txt_{len(calls)}",
                    "type": "function",
                    "function": {
                        "name": d["name"],
                        "arguments": json.dumps(args) if isinstance(args, dict) else str(args)
                    }
                })

    if not calls:
        for m in re.finditer(r'<function\s+name=["\']([^"\']+)["\']>([\s\S]*?)</function>', text):
            fname = m.group(1).strip()
            body = m.group(2).strip()
            args = {}
            for pm in re.finditer(r'<param\s+name=["\']([^"\']+)["\']>(.*?)</param>', body):
                args[pm.group(1).strip()] = pm.group(2).strip()
            calls.append({
                "id": f"call_txt_{len(calls)}",
                "type": "function",
                "function": {
                    "name": fname,
                    "arguments": json.dumps(args)
                }
            })

    if not calls:
        names = known if known is not None else _registered_tool_names()
        for m in _KV_CALL_RX.finditer(text):
            fname = m.group(1).strip()
            if fname not in names:
                continue
            args = {}
            for km in _KV_PAIR_RX.finditer(m.group(2) or ""):
                key, val = km.group(1).strip(), km.group(2).strip()
                try:
                    parsed = json.loads(val) if val[:1] in "{[" or val in ("true", "false", "null") \
                        or re.fullmatch(r"-?\d+(\.\d+)?", val) else val
                except ValueError:
                    parsed = val
                args[key] = parsed
            calls.append({"id": f"call_txt_{len(calls)}", "type": "function",
                          "function": {"name": fname, "arguments": json.dumps(args)}})

    if not calls:
        # Some providers hand back a call as prose: "Tool Call: read_skill({...})". Accepted
        # only for names that are real registered tools, so ordinary text with parentheses
        # (an explanation of print(...)) never turns into an action.
        names = known if known is not None else _registered_tool_names()
        for rx in (_CALL_LINE_RX, _BARE_CALL_RX):
            for m in rx.finditer(text):
                fname = m.group(1).strip()
                if fname not in names:
                    continue
                args = safe_parse_and_repair_args(m.group(2) or "{}")
                calls.append({
                    "id": f"call_txt_{len(calls)}",
                    "type": "function",
                    "function": {"name": fname,
                                 "arguments": json.dumps(args if isinstance(args, dict) else {})}
                })
            if calls:
                break

    return calls
