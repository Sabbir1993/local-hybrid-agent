#!/usr/bin/env python
"""a770 - run the agent from a terminal.

    export A770_TOKEN=[PLACEHOLDER: an API token an admin issued you]     # never a command-line flag (shell history)
    python scripts/a770.py run "fix the failing test in tests/test_parser.py"
    python scripts/a770.py runs                    # runs the server still holds (running or not yet saved)
    python scripts/a770.py attach <run-id>         # follow or replay one
    python scripts/a770.py cancel <run-id>

It talks to the same /agent/run the web app uses, on your machine's Companion: the files and commands are yours,
the server holds no workspace. Set the folder the agent works in (the active project) in the web app first.

Approvals: when the agent needs permission (a command, an edit, a connector, an administrator rule) the question
is asked here. y = allow once, r = allow for the rest of this run, anything else = no. When stdin is not a
terminal every question is answered no, so a script can never approve by accident. Nothing is ever saved as an
"always allow" from the CLI.

Environment: A770_TOKEN (required), A770_BASE (default http://127.0.0.1:8000), A770_DEVICE_ID (default: the
Companion this account has connected). Ctrl-C stops the run on the server too.
"""

import argparse
import json
import os
import re
import secrets
import sys
import time
from typing import Iterator, Optional

DEFAULT_BASE = "http://127.0.0.1:8000"
PERMISSION_MODES = ("auto", "manual", "accept_edits", "plan")      # bypass is deliberately not offered here
_CTRL_RX = re.compile(r"[\x00-\x08\x0b-\x1f\x7f-\x9f]")
_ANSI_RX = re.compile(r"\x1b(?:\[[0-?]*[ -/]*[@-~]|\][^\x07\x1b]*(?:\x07|\x1b\\)?|[@-Z\\-_])")
MAX_LINE = 400


def clean(text) -> str:
    """Make server/model text safe to print: no escape sequences, no control characters. A tool result is
    untrusted and could otherwise redraw the screen, fake a prompt or write to the clipboard (OSC 52)."""
    s = _ANSI_RX.sub("", str(text if text is not None else ""))
    return _CTRL_RX.sub("", s.replace("\r", "\n"))


def one_line(text, limit: int = MAX_LINE) -> str:
    s = " ".join(clean(text).split())
    return s if len(s) <= limit else s[:limit - 1] + "…"


def describe_args(args) -> str:
    """A short, readable view of a tool call's arguments; content-sized values are measured, not printed."""
    if not isinstance(args, dict):
        return ""
    parts = []
    for k, v in list(args.items())[:4]:
        if isinstance(v, str):
            parts.append(f"{k}={one_line(v, 60)!r}" if len(v) <= 60 else f"{k}=<{len(v)} chars>")
        elif isinstance(v, (int, float, bool)) or v is None:
            parts.append(f"{k}={v}")
        else:
            parts.append(f"{k}=<{type(v).__name__}>")
    return " ".join(parts)


def parse_sse(lines) -> Iterator[tuple]:
    """(seq|None, event, data) from SSE lines; comments and malformed frames are skipped."""
    seq, event = None, None
    for raw in lines:
        line = raw.strip() if isinstance(raw, str) else raw.decode("utf-8", "replace").strip()
        if line.startswith("id: "):
            seq = int(line[4:]) if line[4:].isdigit() else None
        elif line.startswith("event: "):
            event = line[7:].strip()
        elif line.startswith("data: ") and event:
            try:
                data = json.loads(line[6:])
            except ValueError:
                event, seq = None, None
                continue
            yield seq, event, data if isinstance(data, dict) else {}
            event, seq = None, None


class Renderer:
    """Turns events into terminal lines. Pure: returns strings, prints nothing."""

    def __init__(self):
        self.text_open = False       # a streamed answer is mid-line
        self.final_state = None
        self.reason = None

    def feed(self, event: str, data: dict) -> list:
        out = []
        if event == "delta":
            chunk = clean(data.get("text"))
            if chunk:
                self.text_open = not chunk.endswith("\n")
                out.append(("raw", chunk))
            return out
        if self.text_open and event in ("tool_call", "tool_result", "permission_request", "done", "error"):
            out.append(("raw", "\n"))
            self.text_open = False
        if event == "tool_call":
            out.append(("line", f"> {clean(data.get('name'))} {describe_args(data.get('args'))}".rstrip()))
        elif event == "tool_result":
            mark = "ok " if data.get("ok") else "ERR"
            out.append(("line", f"  {mark} {one_line(data.get('result'), 160)}"))
        elif event == "gap":
            out.append(("line", "(some earlier output was dropped by the server; resuming from the oldest kept)"))
        elif event == "done":
            self.final_state = clean(data.get("state") or "completed")
            self.reason = one_line(data.get("reason") or data.get("note") or "", 160)
            out.append(("line", f"[{self.final_state}]" + (f" {self.reason}" if self.reason else "")))
        elif event == "error":
            out.append(("line", "error: " + one_line(data.get("message") or data.get("error") or data, 200)))
        return out


def card_prompt(data: dict) -> str:
    kind = clean(data.get("kind") or "shell")
    head = {"rule": "POLICY", "mcp": "CONNECTOR", "python": "PYTHON CODE", "edit": "EDIT", "media": "CLOUD MEDIA"}.get(
        kind, "COMMAND")
    warn = "\n  !! this run has read outside content (web page / connector): approve only if YOU asked for this" \
        if data.get("tainted") else ""
    body = "\n".join("  | " + one_line(x, 200) for x in clean(data.get("cmd")).splitlines()[:14])
    return f"\n[approval needed: {head}]{warn}\n{body}\nallow? [y = once / r = this run / N = no] "


def decide(answer: str, kind: str) -> str:
    """The server decision for a typed answer. 'project'/'user'/'always' (saved patterns) are never sent."""
    a = (answer or "").strip().lower()
    if a in ("y", "yes"):
        return "allow"
    if a in ("r", "run") and kind in ("rule", "edit", "python", "browser_eval", "browser_open", "device"):
        return "project"                      # the server's "for this run" decision for these kinds
    return "deny"


class Api:
    def __init__(self, base: str, token: str, device_id: Optional[str] = None, client=None):
        import httpx
        self.base = base.rstrip("/")
        self.headers = {"Authorization": f"Bearer {token}", "User-Agent": "A770NativeApp/1.0"}
        if device_id:
            self.headers["X-Device-Id"] = device_id
        self.client = client or httpx.Client(headers=self.headers, timeout=httpx.Timeout(30, read=None))

    def get(self, path, **kw):
        return self.client.get(self.base + path, **kw)

    def post(self, path, **kw):
        return self.client.post(self.base + path, **kw)

    def stream(self, method, path, **kw):
        return self.client.stream(method, self.base + path, **kw)


def discover_device(api: Api) -> Optional[str]:
    try:
        r = api.get("/control/companion/status")
        if r.status_code == 200:
            info = r.json()
            return info.get("device_id") if info.get("connected") else None
    except Exception:
        pass
    return None


def follow(api: Api, resp, interactive: bool, out=None, ask=input) -> Renderer:
    """Print one SSE response until it ends, asking the user to answer approval cards."""
    out = out or sys.stdout
    ren = Renderer()
    for _seq, event, data in parse_sse(resp.iter_lines()):
        if event == "permission_request":
            for kind, text in ren.feed(event, data):
                out.write(text + ("\n" if kind == "line" else ""))
            answer = ""
            if interactive:
                try:
                    answer = ask(card_prompt(data))
                except EOFError:
                    answer = ""
            else:
                out.write(card_prompt(data).rsplit("allow?", 1)[0] + "no terminal to ask on: denied\n")
            decision = decide(answer, str(data.get("kind") or "shell"))
            api.post("/agent/permission", json={"req_id": data.get("req_id"), "decision": decision})
            out.write(f"  -> {'allowed' if decision != 'deny' else 'denied'}\n")
            continue
        for kind, text in ren.feed(event, data):
            out.write(text + ("\n" if kind == "line" else ""))
        out.flush()
        if event == "done":
            break
    return ren


def exit_code(state: Optional[str]) -> int:
    return {"completed": 0, "done": 0, None: 1}.get(state, 1)


def cmd_run(api: Api, args) -> int:
    run_id = "cli-" + secrets.token_urlsafe(12)
    payload = {"messages": [{"role": "user", "content": args.prompt}], "mode": args.mode, "client_run_id": run_id,
               "permission_mode": args.permission_mode}
    if args.max_steps:
        payload["max_steps"] = args.max_steps
    interactive = sys.stdin.isatty() and not args.no_input
    try:
        with api.stream("POST", "/agent/run", json=payload) as resp:
            if resp.status_code != 200:
                print(f"error: HTTP {resp.status_code}: {one_line(resp.read().decode('utf-8', 'replace'), 300)}",
                      file=sys.stderr)
                return 2
            ren = follow(api, resp, interactive)
    except KeyboardInterrupt:
        api.post(f"/agent/run/{run_id}/cancel")
        print("\nstopped (the run was cancelled on the server)", file=sys.stderr)
        return 130
    return exit_code(ren.final_state)


def cmd_runs(api: Api, args) -> int:
    r = api.get("/agent/runs")
    if r.status_code != 200:
        print(f"error: HTTP {r.status_code}", file=sys.stderr)
        return 2
    runs = r.json().get("runs", [])
    for x in runs:
        age = int(time.time() - float(x.get("started") or time.time()))
        print(f"{clean(x['id'])}  {'running' if x.get('running') else clean(x.get('state'))}  session={x.get('session_id')}  {age}s")
    if not runs:
        print("no runs")
    return 0


def cmd_attach(api: Api, args) -> int:
    path = f"/agent/run/{args.run_id}/events"
    try:
        with api.stream("GET", path, params={"after": args.after}) as resp:
            if resp.status_code != 200:
                print(f"error: HTTP {resp.status_code} (the run may be finished and saved, or the server restarted)",
                      file=sys.stderr)
                return 2
            ren = follow(api, resp, sys.stdin.isatty())
    except KeyboardInterrupt:
        print("\ndetached (the run keeps going; use cancel to stop it)", file=sys.stderr)
        return 130
    return exit_code(ren.final_state)


def cmd_cancel(api: Api, args) -> int:
    r = api.post(f"/agent/run/{args.run_id}/cancel")
    print("cancelled" if r.status_code == 200 and r.json().get("cancelled") else f"not cancelled (HTTP {r.status_code})")
    return 0 if r.status_code == 200 else 2


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="a770", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--base", default=os.environ.get("A770_BASE", DEFAULT_BASE))
    ap.add_argument("--device-id", default=os.environ.get("A770_DEVICE_ID"))
    sub = ap.add_subparsers(dest="command", required=True)
    r = sub.add_parser("run", help="run the agent on a task")
    r.add_argument("prompt")
    r.add_argument("--mode", default="auto", choices=["auto", "main"])
    r.add_argument("--permission-mode", default="auto", choices=PERMISSION_MODES)
    r.add_argument("--max-steps", type=int, default=0)
    r.add_argument("--no-input", action="store_true", help="never ask: every approval is denied")
    r.set_defaults(fn=cmd_run)
    sub.add_parser("runs", help="list runs the server holds").set_defaults(fn=cmd_runs)
    a = sub.add_parser("attach", help="follow or replay a run")
    a.add_argument("run_id")
    a.add_argument("--after", type=int, default=-1, help="resume after this event number")
    a.set_defaults(fn=cmd_attach)
    c = sub.add_parser("cancel", help="stop a run")
    c.add_argument("run_id")
    c.set_defaults(fn=cmd_cancel)
    return ap


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    token = os.environ.get("A770_TOKEN", "").strip()
    if not token.startswith("a770_pat_"):
        print("set A770_TOKEN to an API token (a770_pat_...) issued by an administrator", file=sys.stderr)
        return 2
    if not re.match(r"^https?://", args.base):
        print("--base must start with http:// or https://", file=sys.stderr)
        return 2
    api = Api(args.base, token, args.device_id)
    if not args.device_id:
        dev = discover_device(api)
        if dev:
            api.headers["X-Device-Id"] = dev
            api.client.headers["X-Device-Id"] = dev
        elif args.command == "run":
            print("no Companion is connected for this account: start the SSL Local Agent on this machine first",
                  file=sys.stderr)
            return 2
    return args.fn(api, args)


if __name__ == "__main__":
    sys.exit(main())
