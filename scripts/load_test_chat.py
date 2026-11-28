"""Multi-user chat load test against the app's own /chat/run (SSE).

N test users sign in, then all send a chat at the same moment, for one or more rounds.
Per request it records: queue wait (first `queued` event -> first event after admission),
time to first token (request sent -> first `delta`/`thought_delta`), decode tokens/s (the
`done` event's gen_tps, else completion tokens over the streaming time), prompt tokens and
errors; then prints a per-user table and an aggregate.

Credentials (never on the command line, never printed):
  * --users-file (default scratch/load_test_users.json, git-ignored): JSON list of
    {"email": "...", "password": "..."} for non-MFA TEST accounts.
  * else BENCH_USER / BENCH_PASSWORD from the environment (the eval account that
    scripts/create_eval_user.py makes). One account then signs in N times; the server's
    per-user fair share sees one user, so prefer distinct accounts.
  * --create-users: tops the users file up to --users accounts through the app's own admin
    API (POST /admin/users, then /auth/change_password to clear the forced first-login
    change). Admin credentials come from LOAD_TEST_ADMIN_USER / LOAD_TEST_ADMIN_PASSWORD.
    Local hosts only, even with --allow-remote.

Examples:
  python scripts/load_test_chat.py --users 15 --rounds 3
  python scripts/load_test_chat.py --users 5 --long-prompt-tokens 120000 --max-tokens 64

Only local base URLs (localhost, 127.0.0.1, ::1, *.localhost, *.test) unless --allow-remote.
"""
import argparse
import asyncio
import json
import math
import os
import secrets
import sys
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_USERS_FILE = ROOT / "scratch" / "load_test_users.json"
CSRF_COOKIE = "a770_csrf"
CSRF_HEADER = "X-CSRF-Token"
LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1"}
TOKEN_EVENTS = ("delta", "thought_delta")


# ------------------------------------------------------------------------------------------
# pure helpers (unit-tested in tests/test_load_test_chat.py)
# ------------------------------------------------------------------------------------------

def is_local_url(url: str) -> bool:
    host = (urlparse(url).hostname or "").lower().strip("[]")
    return host in LOCAL_HOSTS or host.endswith(".localhost") or host.endswith(".test")


class SSEDecoder:
    """Incremental SSE framing: feed one text line at a time; a blank line completes an event
    and returns (event, data), else None. data is parsed JSON when it is JSON, else the raw
    string. Events without an `event:` line are 'message' (the SSE default)."""

    def __init__(self):
        self.event, self.data = None, []

    def feed(self, raw: str):
        line = raw.rstrip("\r\n")
        if not line:
            return self.flush()
        if line.startswith(":"):
            return None
        name, _, value = line.partition(":")
        value = value[1:] if value.startswith(" ") else value
        if name == "event":
            self.event = value
        elif name == "data":
            self.data.append(value)
        return None

    def flush(self):
        if self.event is None and not self.data:
            return None
        text = "\n".join(self.data)
        try:
            payload = json.loads(text) if text else {}
        except json.JSONDecodeError:
            payload = text
        out = (self.event or "message", payload)
        self.event, self.data = None, []
        return out


def iter_sse(lines):
    """(event, data) pairs from SSE text lines."""
    dec = SSEDecoder()
    for raw in lines:
        ev = dec.feed(raw)
        if ev:
            yield ev
    ev = dec.flush()
    if ev:
        yield ev


@dataclass
class RequestResult:
    user: str
    round: int
    status: int | None = None
    queued: bool = False
    queue_position: int | None = None
    queue_wait_s: float | None = None
    ttft_s: float | None = None
    total_s: float | None = None
    gen_tps: float | None = None
    prompt_tps: float | None = None
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    error: str | None = None
    events: dict = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return self.error is None


class StreamTimer:
    """Turns a timed SSE event sequence into one RequestResult. Fed (t, event, data) with t in
    seconds since the request was sent, so it is testable without a network."""

    def __init__(self, result: RequestResult):
        self.r = result
        self.t_first_queued = None
        self.t_admitted = None
        self.t_first_token = None
        self.t_done = None
        self.done = None

    def feed(self, t: float, event: str, data) -> None:
        r = self.r
        r.events[event] = r.events.get(event, 0) + 1
        if event == "queued":
            r.queued = True
            if self.t_first_queued is None:
                self.t_first_queued = t
            if isinstance(data, dict) and data.get("position") is not None:
                try:
                    r.queue_position = max(r.queue_position or 0, int(data["position"]))
                except (TypeError, ValueError):
                    pass
            self.t_admitted = None          # still waiting: admission is the next non-queued event
            return
        if self.t_first_queued is not None and self.t_admitted is None:
            self.t_admitted = t
        if event in TOKEN_EVENTS and self.t_first_token is None:
            self.t_first_token = t
        elif event == "error" and r.error is None:
            msg = data.get("message") or data.get("error") if isinstance(data, dict) else data
            r.error = f"error event: {str(msg)[:200]}"
        elif event == "done":
            self.t_done = t
            self.done = data if isinstance(data, dict) else {}
            if self.done.get("state") == "failed" and r.error is None:
                r.error = f"run failed: {self.done.get('note') or 'unknown'}"

    def finish(self, t_end: float) -> RequestResult:
        r = self.r
        r.total_s = round(t_end, 3)
        if r.queued:
            end = self.t_admitted if self.t_admitted is not None else t_end
            r.queue_wait_s = round(end - self.t_first_queued, 3)
        else:
            r.queue_wait_s = 0.0
        if self.t_first_token is not None:
            r.ttft_s = round(self.t_first_token, 3)
        d = self.done or {}
        for key in ("prompt_tokens", "completion_tokens"):
            if d.get(key) is not None:
                try:
                    setattr(r, key, int(d[key]))
                except (TypeError, ValueError):
                    pass
        if d.get("prompt_tps") is not None:
            r.prompt_tps = round(float(d["prompt_tps"]), 2)
        if d.get("gen_tps") is not None:
            r.gen_tps = round(float(d["gen_tps"]), 2)
        elif r.completion_tokens and self.t_first_token is not None:
            span = (self.t_done if self.t_done is not None else t_end) - self.t_first_token
            if span > 0:
                r.gen_tps = round(r.completion_tokens / span, 2)
        if self.done is None and r.error is None:
            r.error = "stream ended without a done event"
        return r


def percentile(values, p: float):
    """Linear-interpolated percentile (p in 0..100) of the non-None values; None when empty."""
    xs = sorted(v for v in values if v is not None)
    if not xs:
        return None
    if len(xs) == 1:
        return xs[0]
    k = (len(xs) - 1) * p / 100.0
    lo, hi = math.floor(k), math.ceil(k)
    return xs[lo] + (xs[hi] - xs[lo]) * (k - lo)


def _stats(values) -> dict:
    xs = [v for v in values if v is not None]
    if not xs:
        return {"n": 0, "mean": None, "p50": None, "p95": None, "min": None, "max": None}
    return {"n": len(xs), "mean": round(sum(xs) / len(xs), 3), "p50": round(percentile(xs, 50), 3),
            "p95": round(percentile(xs, 95), 3), "min": round(min(xs), 3), "max": round(max(xs), 3)}


def summarize(results: list[RequestResult], round_walls: list[float]) -> dict:
    """Aggregate over all requests. round_walls: wall seconds of each round (all users in
    parallel), so aggregate decode throughput = completion tokens / summed round wall time."""
    ok = [r for r in results if r.ok]
    wall = sum(round_walls)
    tokens = sum(r.completion_tokens or 0 for r in ok)
    return {
        "requests": len(results),
        "ok": len(ok),
        "errors": len(results) - len(ok),
        "queued": sum(1 for r in results if r.queued),
        "queue_wait_s": _stats(r.queue_wait_s for r in ok),
        "ttft_s": _stats(r.ttft_s for r in ok),
        "gen_tps_per_request": _stats(r.gen_tps for r in ok),
        "prompt_tps": _stats(r.prompt_tps for r in ok),
        "completion_tokens": tokens,
        "wall_s": round(wall, 3),
        "aggregate_decode_tps": round(tokens / wall, 2) if wall > 0 else None,
        "error_kinds": _error_kinds(results),
    }


def _error_kinds(results) -> dict:
    kinds = {}
    for r in results:
        if r.error:
            k = r.error.split(":")[0]
            kinds[k] = kinds.get(k, 0) + 1
    return kinds


def build_long_prompt(tokens: int, tag: str) -> str:
    """About `tokens` tokens of filler (~4 characters a token for this text), unique per tag so
    no two users share a cached prefix, ending with a short question so the answer stays short."""
    head = f"[{tag}] Below is a numbered log. Read it, then answer the question at the end.\n"
    lines, size, i, target = [head], len(head), 0, max(0, tokens) * 4
    while size < target:
        line = f"{tag} entry {i}: sensor {i % 97} read {(i * 37) % 1000} units, status ok.\n"
        lines.append(line)
        size += len(line)
        i += 1
    lines.append("Question: how many entries does the log have? Answer with the number only.")
    return "".join(lines)


def _fmt(v, width=8, prec=2):
    return f"{'-':>{width}}" if v is None else f"{v:>{width}.{prec}f}"


def format_report(results: list[RequestResult], summary: dict) -> str:
    out = [f"{'user':<28} {'rnd':>3} {'http':>4} {'queued':>6} {'wait s':>8} {'ttft s':>8} "
           f"{'tok/s':>8} {'p.tok':>7} {'c.tok':>6} {'total s':>8}  error"]
    for r in sorted(results, key=lambda x: (x.round, x.user)):
        out.append(f"{r.user[:28]:<28} {r.round:>3} {str(r.status or '-'):>4} "
                   f"{('yes' if r.queued else 'no'):>6} {_fmt(r.queue_wait_s)} {_fmt(r.ttft_s)} "
                   f"{_fmt(r.gen_tps)} {str(r.prompt_tokens if r.prompt_tokens is not None else '-'):>7} "
                   f"{str(r.completion_tokens if r.completion_tokens is not None else '-'):>6} "
                   f"{_fmt(r.total_s)}  {r.error or ''}")
    s = summary
    out.append("")
    out.append(f"requests {s['requests']}  ok {s['ok']}  errors {s['errors']}  queued {s['queued']}  "
               f"wall {s['wall_s']} s  completion tokens {s['completion_tokens']}  "
               f"aggregate decode {s['aggregate_decode_tps']} tok/s")
    for label, key in (("queue wait s", "queue_wait_s"), ("ttft s", "ttft_s"),
                       ("tok/s per request", "gen_tps_per_request"), ("prompt tok/s", "prompt_tps")):
        st = s[key]
        out.append(f"  {label:<18} n={st['n']:<3} mean={st['mean']}  p50={st['p50']}  "
                   f"p95={st['p95']}  min={st['min']}  max={st['max']}")
    if s["error_kinds"]:
        out.append(f"  errors by kind: {s['error_kinds']}")
    return "\n".join(out)


# ------------------------------------------------------------------------------------------
# network side
# ------------------------------------------------------------------------------------------

def _arm_csrf(client) -> None:
    csrf = client.cookies.get(CSRF_COOKIE)
    if csrf:
        client.headers[CSRF_HEADER] = csrf


async def login(client, email: str, password: str) -> dict:
    resp = await client.post("/auth/login", json={"email": email, "password": password})
    if resp.status_code != 200:
        raise RuntimeError(f"login HTTP {resp.status_code}")
    body = resp.json()
    if body.get("mfa_required"):
        raise RuntimeError("account needs MFA; use a non-MFA test account")
    _arm_csrf(client)
    return body


def load_users_file(path: Path) -> list[dict]:
    if not path.exists():
        return []
    data = json.loads(path.read_text(encoding="utf-8"))
    return [u for u in data if isinstance(u, dict) and u.get("email") and u.get("password")]


def _write_users_file(path: Path, users: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(users, indent=2), encoding="utf-8")
    os.replace(tmp, path)
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


def _new_password() -> str:
    return "Lt-" + secrets.token_urlsafe(15) + "7"   # policy: letters and digits


async def create_users(base_url: str, path: Path, want: int, timeout: float) -> list[dict]:
    """Top the users file up to `want` accounts through the app's admin API."""
    import httpx
    users = load_users_file(path)
    if len(users) >= want:
        return users
    admin_user = os.environ.get("LOAD_TEST_ADMIN_USER", "")
    admin_pw = os.environ.get("LOAD_TEST_ADMIN_PASSWORD", "")
    if not admin_user or not admin_pw:
        sys.exit("--create-users needs LOAD_TEST_ADMIN_USER and LOAD_TEST_ADMIN_PASSWORD in the environment")
    have = {u["email"] for u in users}
    async with httpx.AsyncClient(base_url=base_url, timeout=timeout) as admin:
        await login(admin, admin_user, admin_pw)
        n = 0
        while len(users) < want:
            n += 1
            email = f"loadtest-{n:02d}@loadtest.test"
            if email in have:
                continue
            first_pw = _new_password()
            resp = await admin.post("/admin/users", json={"email": email, "password": first_pw,
                                                          "display_name": f"Load test {n:02d}",
                                                          "roles": ["user"]})
            if resp.status_code == 409:
                print(f"  {email} already exists but is not in {path.name}; skipped", file=sys.stderr)
                if n > want + 100:
                    sys.exit("could not find free loadtest-NN names; clean up old load-test users")
                continue
            if resp.status_code != 200:
                sys.exit(f"creating {email} failed: HTTP {resp.status_code} {resp.text[:200]}")
            final_pw = _new_password()
            async with httpx.AsyncClient(base_url=base_url, timeout=timeout) as uc:
                await login(uc, email, first_pw)
                r2 = await uc.post("/auth/change_password",
                                   json={"current_password": first_pw, "new_password": final_pw})
                if r2.status_code != 200:
                    sys.exit(f"clearing the first-login password change for {email} failed: HTTP {r2.status_code}")
            users.append({"email": email, "password": final_pw})
            have.add(email)
            _write_users_file(path, users)          # after each one, so a failure loses nothing
            print(f"  created {email}")
    return users


def resolve_users(path: Path, n: int) -> list[dict]:
    users = load_users_file(path)
    if users:
        if len(users) < n:
            print(f"note: {path} has {len(users)} accounts for {n} users; accounts are reused",
                  file=sys.stderr)
        return [users[i % len(users)] for i in range(n)]
    u, pw = os.environ.get("BENCH_USER", ""), os.environ.get("BENCH_PASSWORD", "")
    if u and pw:
        print("note: one BENCH_USER account signs in for every simulated user; the server's "
              "per-user fair share sees a single user", file=sys.stderr)
        return [{"email": u, "password": pw} for _ in range(n)]
    sys.exit(f"no test accounts: create {path} (JSON list of {{email, password}}), set "
             f"BENCH_USER/BENCH_PASSWORD, or use --create-users")


def chat_body(prompt: str, args) -> dict:
    body = {"messages": [{"role": "user", "content": prompt}], "web_search": False, "kb": "off",
            "verify": "off", "max_tokens": args.max_tokens, "client_run_id": uuid.uuid4().hex}
    if args.reasoning:
        body["reasoning_effort"] = args.reasoning
    if args.model:
        body["model"] = args.model
    return body


async def one_request(client, label: str, rnd: int, prompt: str, args, start: asyncio.Event):
    result = RequestResult(user=label, round=rnd)
    await start.wait()
    t0 = time.perf_counter()
    timer = StreamTimer(result)
    try:
        async with client.stream("POST", "/chat/run", json=chat_body(prompt, args)) as resp:
            result.status = resp.status_code
            if resp.status_code != 200:
                text = (await resp.aread()).decode("utf-8", "replace")
                result.error = f"HTTP {resp.status_code}: {text[:200]}"
                result.total_s = round(time.perf_counter() - t0, 3)
                return result

            dec = SSEDecoder()
            async for line in resp.aiter_lines():
                ev = dec.feed(line)           # each event is timed when its blank line arrives
                if ev:
                    timer.feed(time.perf_counter() - t0, *ev)
            ev = dec.flush()
            if ev:
                timer.feed(time.perf_counter() - t0, *ev)
    except Exception as e:      # network failure / timeout: an error row, never a crash of the run
        result.error = f"{type(e).__name__}: {str(e)[:200]}"
    finally:
        if result.total_s is None:
            timer.finish(time.perf_counter() - t0)
    return result


async def run(args) -> dict:
    import httpx
    path = Path(args.users_file)
    if args.create_users:
        await create_users(args.base_url, path, args.users, args.timeout)
    accounts = resolve_users(path, args.users)

    limits = httpx.Limits(max_connections=4, max_keepalive_connections=4)
    timeout = httpx.Timeout(args.timeout, connect=30.0)
    clients = []
    try:
        for i, acc in enumerate(accounts):
            c = httpx.AsyncClient(base_url=args.base_url, timeout=timeout, limits=limits)
            clients.append(c)
            try:
                await login(c, acc["email"], acc["password"])
            except Exception as e:
                sys.exit(f"sign-in failed for user {i + 1} ({acc['email']}): {e}")
        print(f"{len(clients)} users signed in to {args.base_url}")

        results, walls = [], []
        for rnd in range(1, args.rounds + 1):
            start = asyncio.Event()
            tasks = []
            for i, c in enumerate(clients):
                label = f"{i + 1:02d} {accounts[i]['email']}"
                tag = f"u{i + 1}-r{rnd}-{uuid.uuid4().hex[:6]}"
                prompt = build_long_prompt(args.long_prompt_tokens, tag) if args.long_prompt_tokens \
                    else f"[{tag}] {args.prompt}"
                tasks.append(asyncio.create_task(one_request(c, label, rnd, prompt, args, start)))
            await asyncio.sleep(0.05)       # every task is parked on the event before the gun
            t_round = time.perf_counter()
            start.set()
            round_results = await asyncio.gather(*tasks)
            walls.append(time.perf_counter() - t_round)
            results.extend(round_results)
            errs = sum(1 for r in round_results if not r.ok)
            print(f"round {rnd}/{args.rounds}: {walls[-1]:.1f} s, {errs} errors")
        return {"results": results, "walls": walls}
    finally:
        for c in clients:
            await c.aclose()


def parse_args(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--base-url", default="http://127.0.0.1:8000")
    ap.add_argument("--allow-remote", action="store_true", help="allow a non-local --base-url")
    ap.add_argument("--users", type=int, default=5, help="simultaneous users (default 5)")
    ap.add_argument("--rounds", type=int, default=1, help="rounds; every user sends once per round")
    ap.add_argument("--prompt", default="Write two sentences about why the sky is blue.")
    ap.add_argument("--long-prompt-tokens", type=int, default=0,
                    help="replace the prompt with ~N tokens of unique filler per user (KV pool test, e.g. 120000)")
    ap.add_argument("--max-tokens", type=int, default=256, help="answer cap per request")
    ap.add_argument("--reasoning", default="none", choices=["none", "low", "medium", "high", "extra", ""],
                    help="reasoning_effort sent with each chat ('' = the app's default)")
    ap.add_argument("--model", help="per-request model override (default: the main lane)")
    ap.add_argument("--users-file", default=str(DEFAULT_USERS_FILE),
                    help="JSON list of {email, password} test accounts (git-ignored scratch/ by default)")
    ap.add_argument("--create-users", action="store_true",
                    help="create missing loadtest-NN accounts via the admin API (local hosts only; "
                         "admin credentials from LOAD_TEST_ADMIN_USER / LOAD_TEST_ADMIN_PASSWORD)")
    ap.add_argument("--timeout", type=float, default=1800.0,
                    help="read timeout in seconds (a 128K prefill can be silent for minutes)")
    ap.add_argument("--json", action="store_true", help="print results and summary as JSON")
    args = ap.parse_args(argv)
    if args.users < 1 or args.rounds < 1:
        ap.error("--users and --rounds must be at least 1")
    if not is_local_url(args.base_url):
        if not args.allow_remote:
            ap.error(f"{args.base_url} is not a local host; pass --allow-remote to load-test it")
        if args.create_users:
            ap.error("--create-users only runs against a local host")
    return args


def main(argv=None) -> int:
    args = parse_args(argv)
    out = asyncio.run(run(args))
    summary = summarize(out["results"], out["walls"])
    if args.json:
        print(json.dumps({"summary": summary,
                          "results": [{**asdict(r), "ok": r.ok} for r in out["results"]]},
                         indent=2))
    else:
        print()
        print(format_report(out["results"], summary))
    return 0 if summary["errors"] == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
