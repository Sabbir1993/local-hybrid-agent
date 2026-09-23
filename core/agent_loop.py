import asyncio
import inspect
import json
import re
import sys
from typing import Optional, Union

from .agent_tools import (
    AGENT_CORE_TOOLS,
    AGENT_TOOLS,
    TOOL_IMPLS,
    active_workspace,
)
from .registry import registry, bootstrap_builtin_tools

AGENT_SYSTEM_PROMPT = """You are an autonomous AI coding agent and orchestrator.
Your goal is to solve the user's task step-by-step using your available tools.

Active Workspace Directory: {workspace}

Important Operating & Path Rules:
1. Workspace Relative Paths: All file paths must be strictly relative to workspace root (e.g. 'program2/function_even.php' or 'index.html'). Never prefix paths with '/workspace/'.
2. Create Before Reading: If you need to create a new file or write code, call 'write_file' immediately. Do NOT call 'read_file' on a file before you have created it.
3. Edit Existing Files: Use 'read_file' to review existing code, then use 'edit_file' for targeted edits or 'write_file' to overwrite.
4. Test Your Work: Use 'run_python' to execute and test scripts if applicable.
5. If a tool reports an error, read the message carefully, fix the arguments, and try again.
6. Provide concise, direct final answers without repeating sentences.
7. High Efficiency & No Redundant Reads: Never call 'read_file' on a file you just created or edited with 'write_file' or 'edit_file'. Once 'write_file' reports success, the file is saved and ready. Conclude your response immediately without redundant read-backs.
8. One-and-Done File Completion: Once a file has been written successfully with 'write_file' or python, DO NOT redundantly rewrite the entire file under an alternative name (e.g. '..._final.html') unless specifically fixing a verified runtime error. Conclude immediately and present the result.
9. Action-First File Creation: When the user asks to create, make, build, or write code or a file (e.g. 'make an html file', 'create calculator.html', 'why not you write on that file'), DO NOT ask for more instructions or passively refuse. Call 'write_file' or 'edit_file' immediately with complete, functional, professional code.
10. Proactive Autonomous Execution: Never refuse by saying 'I need more details' or 'what exact changes would you like' when the goal is clear (e.g. building a calculator, fixing an issue, creating a page). Take initiative, design the full solution, write the code directly to disk, and present the result. If a file exists, read it or overwrite it as appropriate.
11. Shell and CLI Execution: You HAVE full terminal execution capability via the 'run_shell' tool. When the user asks to run commands, add skills (e.g. 'npx skills add ...'), install packages, or run git, do NOT refuse or tell the user to open a terminal; call 'run_shell' directly to execute the command.
12. Large File & Dashboard Writing:
    - For large dashboards, board decks, spreadsheets, or multi-page documents: you can write a concise Python script using 'run_python' to assemble the HTML/CSV/XLSX and write it directly to disk. Script execution runs in milliseconds, bypasses LLM output token limits, and produces 100% syntactically valid files.
    - If generating directly via 'write_file' for files longer than ~150 lines: split it into logical chunks (e.g. HTML head/structure, then CSS, then JS) and pass append=true to add subsequent chunks in order. Never re-read between chunks — just keep appending until the file is complete.

File Intelligence — Working with Documents:
12. Uploaded Documents: When the user attaches a file (Excel .xlsx/.xls, CSV, PDF, PowerPoint .pptx, Word .docx), its extracted content is injected below their message. The file is also saved to the workspace. You can reference it by filename for further operations.
13. Large Files & Chunking: If extracted content ends with '[chunk: chars N-M of TOTAL]', the file was too large to fully inject. Call read_file_chunk(path, offset_chars=NEXT_OFFSET) to read subsequent chunks before drawing conclusions.
14. Modifying Documents: To modify Excel/CSV/PDF/PowerPoint/Word files, write Python code using the appropriate library and run it with run_python:
    - Excel (.xlsx): use openpyxl — e.g. `import openpyxl; wb = openpyxl.load_workbook('file.xlsx'); ws = wb.active; ws['B2'] = 42; wb.save('file.xlsx')`
    - CSV: use csv or pandas — e.g. `import pandas as pd; df = pd.read_csv('data.csv'); df['col'] = 'val'; df.to_csv('data.csv', index=False)`
    - PDF (read-only extraction): use pdfplumber — for creating/modifying PDFs use reportlab
    - PowerPoint (.pptx): use python-pptx — e.g. `from pptx import Presentation; prs = Presentation('file.pptx'); ...`
    - Word (.docx): use python-docx — e.g. `import docx; doc = docx.Document('file.docx'); doc.add_paragraph('New text'); doc.save('file.docx')`
15. Output Format: Output format must match what the user requests. E.g. 'give me a CSV from this Excel' → produce CSV. 'Summarize this PDF' → produce a text summary in the chat.
16. Pipelines: You can chain tools autonomously: web_search/web_fetch to get data → run_python to process → write file to workspace. Do not ask for confirmation between steps.
17. Download Signal: When you produce a modified or output file in the workspace that the user will want to download, end your response with a line like: [DOWNLOAD: filename.xlsx] — the UI will render this as a download button automatically.

Multi-Agent Orchestration — 'spawn_agent':
18. You can delegate to multiple focused sub-agents within a single user request by calling 'spawn_agent' more than once (each call is a separate step; they run one at a time, in the order you call them, and each returns its result before you decide the next call). Use this for requests that name multiple roles or phases (e.g. 'plan it, then implement it, then review it') or for large tasks that benefit from role separation — for example role='planner' to outline an approach, then role='coder' to implement it (pass the planner's output back to it in the task text), then role='reviewer' to check the diff. Do not ask the user whether to do this — if their prompt describes multiple phases or roles, chain the spawn_agent calls yourself and synthesize a final summary. Each sub-agent is isolated (no shared history) and cannot itself delegate further, so include everything it needs directly in the 'task' argument, including relevant output from earlier sub-agents in the chain."""


def is_degeneration_or_loop(text: str) -> tuple[bool, str]:
    if not text or len(text) < 70:
        return False, text

    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if len(lines) >= 3:
        for i in range(len(lines) - 2):
            if lines[i] == lines[i+1] == lines[i+2]:
                cut_idx = text.find(lines[i+1])
                if cut_idx != -1:
                    return True, text[:cut_idx].rstrip()
                return True, text

    from collections import Counter
    sentences = [s.strip() for s in re.split(r'[.\n]+', text) if len(s.strip()) > 20]
    if len(sentences) >= 3:
        counts = Counter(sentences)
        for s, count in counts.items():
            if count >= 3:
                first_pos = text.find(s)
                second_pos = text.find(s, first_pos + len(s))
                if second_pos != -1:
                    return True, text[:second_pos].rstrip()
                return True, text

    for phrase_len in (25, 35, 50, 70):
        if len(text) > phrase_len * 3:
            phrase = text[-phrase_len:]
            if text.count(phrase) >= 3:
                first_pos = text.find(phrase)
                second_pos = text.find(phrase, first_pos + phrase_len)
                if second_pos != -1:
                    return True, text[:second_pos].rstrip()
                return True, text

    return False, text


def is_repeating_loop(text: str) -> bool:
    loop_detected, _ = is_degeneration_or_loop(text)
    return loop_detected


def sanitize_user_facing_content(text: str) -> str:
    if not text:
        return ""
    is_loop, cleaned = is_degeneration_or_loop(text)
    text = cleaned if is_loop else text
    text = re.sub(r"<tool_call>[\s\S]*?</tool_call>", "", text)
    text = re.sub(r'<function\s+name=["\'][\s\S]*?</function>', "", text)
    text = re.sub(r"</?(?:tool_call|function|param|tool_response|tool_sep|im_start|im_end)>", "", text)
    text = text.replace("<![CDATA[", "").replace("]]>", "")
    text = re.sub(r"```(?:json)?\s*\{\s*\"name\"\s*:\s*\"(?:write_file|edit_file|read_file|list_files|run_python)\"[\s\S]*?\}\s*```", "", text)
    return text.strip()


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


def fast_sandbox_check(tool_name: str, args: dict) -> tuple[bool, str]:
    if tool_name not in ("write_file", "edit_file"):
        return True, "tool does not modify files"
    target = args.get("path") or ""
    if not target:
        return False, "missing target path"
    target_clean = target.lower().replace("\\", "/")
    forbidden_files = {
        "server_manager.py", "autotune.py", "app.json", "model_configs.json",
        "providers.json", "roles.json",
        "projects.db", "usage.db", "requirements.txt", "testing_log.md"
    }
    for fb in forbidden_files:
        if target_clean == fb or target_clean.endswith("/" + fb):
            return False, f"modifying core runtime file '{fb}' is forbidden in sandbox"
    if target_clean.startswith(".git") or "/.git" in target_clean:
        return False, "modifying .git directory is forbidden"
    return True, "approved by sandbox safety validator"


def validate_and_finalize_response(last_query: str, content: str, reasoning: str, actions_taken: list) -> tuple[str, bool, str]:
    clean_content = sanitize_user_facing_content(content)

    successful_writes = []
    successful_edits = []
    python_runs = []
    read_files = []

    web_results = []
    for act in actions_taken:
        name = act.get("name")
        ok = act.get("ok", False)
        args = act.get("args") or {}
        p = args.get("path") or args.get("file") or ""
        if ok:
            if name == "write_file":
                successful_writes.append(p)
            elif name == "edit_file":
                successful_edits.append(p)
            elif name == "run_python":
                python_runs.append(act.get("result", ""))
            elif name == "read_file":
                read_files.append(p)
            elif name in ("web_search", "web_search_images", "web_fetch"):
                r_text = str(act.get("result", "")).strip()
                if r_text and not r_text.startswith("error:"):
                    web_results.append(r_text)

    if not clean_content:
        if successful_writes or successful_edits:
            lines = ["✅ **Task completed successfully.**\n"]
            ws_root = active_workspace()
            if successful_writes:
                for w in successful_writes:
                    ws_f = ws_root / w
                    sz = ws_f.stat().st_size if ws_f.exists() else 0
                    lines.append(f"- **Created/Updated:** `{w}` ({sz:,} bytes)")
            if successful_edits:
                for ed in successful_edits:
                    lines.append(f"- **Edited:** `{ed}`")
            if python_runs:
                lines.append("\n**Execution Output:**\n```\n" + python_runs[-1].strip() + "\n```")
            return "\n".join(lines), True, "synthesized response from successful tool actions"

        if web_results:
            summary = "\n\n---\n\n".join(w[:600] for w in web_results[-2:])
            return f"🔍 **Information gathered from web search:**\n\n{summary}", True, "synthesized web search results"

        if python_runs:
            return f"✅ **Script executed successfully.**\n\n```\n{python_runs[-1].strip()}\n```", True, "synthesized execution output"

        if reasoning and len(reasoning.strip()) > 15:
            r_paras = [p.strip() for p in reasoning.split("\n\n") if p.strip()]
            if r_paras:
                cand = r_paras[-1]
                if len(cand) > 10:
                    return cand, True, "extracted answer from model reasoning"
            return reasoning.strip(), True, "extracted full model reasoning"

        return "Task completed. All requested operations have been processed in the workspace.", True, "fallback confirmation"

    creation_keywords = ("make", "create", "generate", "write", "build", "code", "landing page", "script")
    query_wants_creation = any(w in last_query.lower() for w in creation_keywords)
    if query_wants_creation and not successful_writes and not successful_edits:
        refusal_phrases = (
            "please specify the exact file path",
            "please provide the code",
            "please provide the content",
            "specify the exact file",
            "provide the html",
            "provide the python",
            "what content would you like",
        )
        content_low = clean_content.lower()
        if any(rp in content_low for rp in refusal_phrases):
            return clean_content, False, "passive_refusal_detected"

    return clean_content, False, "validated"


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


def estimate_prompt_tokens(msgs: list) -> int:
    """Rough prompt-size estimate (chars/4 + tool-call overhead). Good enough
    for compaction decisions - not a substitute for real tokenization."""
    total = 0
    for m in msgs:
        total += len(str(m.get("content") or "")) // 4
        for tc in (m.get("tool_calls") or []):
            try:
                total += len(json.dumps(tc)) // 4 + 8
            except Exception:
                total += 64
        total += 6  # role/framing overhead
    return total


def _digest_message(m: dict) -> str:
    role = str(m.get("role") or "?")
    content = str(m.get("content") or "").strip()
    if role == "user":
        return "user: " + content[:240] + ("..." if len(content) > 240 else "")
    if role == "assistant":
        names = [str((tc.get("function") or {}).get("name") or "?")
                 for tc in (m.get("tool_calls") or [])]
        head = content[:160] + ("..." if len(content) > 160 else "")
        return "assistant: " + head + (f" [tools: {', '.join(names)}]" if names else "")
    if role == "tool":
        return f"tool[{m.get('tool_call_id', '')}]: " + content[:160] + ("..." if len(content) > 160 else "")
    return f"{role}: {content[:200]}"


def compact_messages(msgs: list, budget_tokens: int) -> list:
    """Mechanical context compaction for the executor lane.

    Keeps the system prompt and the most recent ~60% of the token budget
    verbatim (preserving assistant/tool_call/tool pairing), and rolls older
    turns into a compact deterministic digest message. No extra LLM call is
    made, so compaction adds no latency and cannot hallucinate.
    """
    if budget_tokens <= 0 or len(msgs) < 6:
        return msgs
    if estimate_prompt_tokens(msgs) <= budget_tokens:
        return msgs

    # group into units: (assistant + its tool replies) or a single message
    units = []
    i = len(msgs) - 1
    while i >= 1:
        if msgs[i].get("role") == "tool":
            j = i
            while j >= 1 and msgs[j].get("role") == "tool":
                j -= 1
            if j >= 1 and msgs[j].get("role") == "assistant":
                units.append((j, i))
                i = j - 1
            else:
                units.append((i, i))
                i -= 1
        else:
            units.append((i, i))
            i -= 1
    units.reverse()

    tail_budget = int(budget_tokens * 0.6)
    tail_start = len(msgs)
    acc = 0
    for start, end in reversed(units):
        u_tok = estimate_prompt_tokens(msgs[start:end + 1])
        if acc + u_tok > tail_budget and tail_start != len(msgs):
            break
        acc += u_tok
        tail_start = start
    if tail_start <= 1:
        return msgs  # tail alone exceeds budget; nothing safe to drop

    digest_lines = []
    for start, end in units:
        if start >= tail_start:
            break
        for m in msgs[start:end + 1]:
            digest_lines.append(_digest_message(m))
    digest = ("[CONVERSATION DIGEST - earlier steps were compacted to fit the "
              "context window. Tool results are summarized; call read_file / "
              "list_files again if you need exact content.]\n" + "\n".join(digest_lines))
    if len(digest) > 6000:
        digest = digest[:6000] + "\n..."
    return [msgs[0], {"role": "system", "content": digest}] + msgs[tail_start:]


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


async def run_tool(name: str, args: dict) -> str:
    # registry first (covers builtin + web + skills + mcp + plugins);
    # fall back to the raw builtin table for the executor lane's core set
    if registry.get(name) is not None:
        return await registry.async_run(name, args)
    impl = TOOL_IMPLS.get(name)
    if not impl:
        return f"error: unknown tool {name}"
    try:
        repaired, val_err = validate_and_repair_tool_args(name, args)
        if val_err:
            return val_err
        args = repaired
        if inspect.iscoroutinefunction(impl):
            return await impl(args)
        return await asyncio.get_event_loop().run_in_executor(None, impl, args)
    except Exception as e:
        return f"error: {type(e).__name__}: {e}"


def all_tools() -> list:
    """Full tool schema list: builtins + every registered capability."""
    return registry.schemas()
