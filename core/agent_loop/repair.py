import json
import re
from typing import Optional, Union


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


def _extract_text_tool_calls(text: str) -> list:
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

    return calls
