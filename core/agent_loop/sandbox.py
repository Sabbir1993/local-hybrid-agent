from .degeneration import sanitize_user_facing_content

# outcome code for a run where the model was asked to build something and replied by asking
# the user for the path or the code instead of doing the work
PASSIVE_REFUSAL_NOTE = "passive_refusal_detected"


def fast_sandbox_check(tool_name: str, args: dict) -> tuple[bool, str]:
    if tool_name not in ("write_file", "edit_file", "append_file", "insert_at_line"):
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
    # GIT~1 is the Windows 8.3 short name of .git (hooks there run on the next git command)
    segs = [x for x in target_clean.split("/") if x]
    if target_clean.startswith(".git") or any(x == ".git" or x.startswith("git~") for x in segs):
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
            if name in ("write_file", "append_file"):
                successful_writes.append(p)
            elif name in ("edit_file", "insert_at_line"):
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
            # files live on the user's machine: don't stat() the path on the server's disk
            if successful_writes:
                for w in successful_writes:
                    lines.append(f"- **Created/Updated:** `{w}`")
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

    if is_passive_refusal(clean_content, last_query, actions_taken):
        return clean_content, False, PASSIVE_REFUSAL_NOTE

    return clean_content, False, "validated"


def is_passive_refusal(content: str, last_query: str, actions_taken: list) -> bool:
    """True when the user asked for something to be built/coded, but the model responded
    passively asking the user for paths/starter-code instead of calling tools."""
    if not content or not last_query:
        return False

    creation_keywords = ("make", "create", "generate", "write", "build", "code", "landing page", "script", "implement", "add", "setup")
    if not any(w in last_query.lower() for w in creation_keywords):
        return False

    successful_mutations = any(
        act.get("ok") and act.get("name") in ("write_file", "edit_file", "append_file", "insert_at_line")
        for act in (actions_taken or [])
    )
    if successful_mutations:
        return False

    refusal_phrases = (
        "please specify the exact file path",
        "please specify the file path",
        "specify the exact file path",
        "please provide the code",
        "please provide the content",
        "specify the exact file",
        "provide the html",
        "provide the python",
        "what content would you like",
        "where would you like me to save",
        "where should i save",
        "which file would you like",
        "what would you like the file to be named",
        "please provide more details about the file",
    )
    content_low = content.lower()
    return any(rp in content_low for rp in refusal_phrases)
