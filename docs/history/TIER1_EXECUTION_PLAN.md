# TIER1 Execution Plan — verified against the codebase (2026-10-02)

Source document: `TIER1_SOTA_REMAINING_WORK.md`. Every claim below was checked
against the tree before planning, because the previous planning document in this
repo shipped a phantom "44% edit_file failure rate" that turned out to be a
query artifact (real rate: 6.3%, Wilson [2.9%, 13.1%]). **Three of this
document's load-bearing claims are refuted; one fix would crash; one feature is
architecturally misplaced.** The gaps are real, the diagnosis mostly is not.

---

## 1. Verification results

| # | Document claim | Verdict | Evidence |
|---|---|---|---|
| G1.1 | `spawn_agent` fails 45.5% (5/11) | **Confirmed** | `scripts/early_exit_report.py` on `usage.db`: 11 calls, 45.5%, CI [0.213, 0.720] |
| G1.2 | Subagents default to `DEFAULT_SUBAGENT_STEPS = 5` | **Refuted** | `core/subagent/constants.py:2` — already **8** (cap 15). The proposed "raise to 8 local / 12 main" is already shipped for local |
| G1.3 | Root cause: step-budget starvation | **Refuted as explanation** | A step-exhausted child returns `(sub-agent reached its step limit without a final answer)` **prefixed by a header** (`runner.py:223-224`), so the parent's failure test (`run.py:1411`: ok = not result.startswith("error:")) marks it **ok=True**. Starvation cannot produce any of the 5 recorded failures |
| G1.4 | Root cause: unstructured return makes the parent treat the call as failed | **Inverted** | Same code path: the header `[sub-agent · role=… · lane=…]` never starts with `error:`, so a *failed* child is **invisible** to the parent. The bug is silent success, not false failure |
| G1.5 | Root cause: concurrent siblings can't share findings | Unmeasured | No telemetry for duplicate reads across siblings. Plausible; unproven |
| G2.1 | Commands execute bare on the host OS | **Confirmed, with a correction** | `core/shell_tools.py:213`: "Agent shell commands run ONLY on the user's machine via the companion". `tool_run_python` likewise dispatches `shell.run` through the bridge (`file_ops.py:320`) |
| G2.2 | Fix = server-side `core/process_sandbox.py` backend routing `run_shell`/`run_python` | **Misplaced** | A server-side backend cannot intercept tool calls that execute **in the companion process on the user's machine**. Isolation must be implemented companion-side, or the server backend will only cover server-side execution paths that do not exist |
| G2.3 | Docker-based isolation | **Not available** | `docker` is not installed on this machine. Also unresolved: Vulkan passthrough for llama-server lanes |
| G3.1 | `compact_endpoint` fails 401-vs-400 from unmocked auth | **Confirmed** | `tests/eval_agent.py:152-156` posts unauthenticated; `routes/chat/compact.py` enforces `Depends(get_current_user)` → 401 before the project gate |
| G3.2 | Exact fix: `Principal(id=1, username=…, role="admin", permissions={…})` | **Would crash** | `core/auth.py:49` `Principal` fields are `id, username, display_name, is_super_admin, must_change_password, role_names, permission_keys, via_token, token_id, mfa_verified`. There is no `role` or `permissions`; seven fields are required. The snippet raises `TypeError` |
| S1 | Current milestone 85.9 → target 96.5 | **Not reconcilable** | Tracked scoreboard says 7.2/10 (=72); `IMPROVEMENT_PLAN_TO_SOTA.md` says 79.8. The per-step deltas (+0.4, +2.0, …) are planning estimates, not derived from measurement — treat them as sequencing, not arithmetic |

**Net:** Gap 3 is real and cheap. Gap 1's number is real but its three causes
explain zero of the five failures, and the actual defect (silent success) is
worse than the one described. Gap 2's threat model is real; its proposed
implementation would not cover the surface it claims to protect.

---

## 2. Sequenced plan

Ordering principle: **attribution before fix**, cheapest verified defect first,
and anything that needs live traffic is gated behind an instrumentation step
rather than built on a hypothesis.

### T1 — Fix the eval gate (Gap 3) — DONE 2026-10-02

The one standing FAIL in the offline suite. A permanently red check is noise: it
trains everyone to ignore that section.

**Result: 7/7 offline PASS, and the gate can now actually fail.** The document's
"10 minutes, inject a principal" was directionally right and structurally wrong
in every detail, and the 401 had been hiding far more than one defect.

What the auth fix uncovered, in the order it surfaced:
1. `db_create_project` needs `owner_user_id` **and** an absolute `workspace_dir`
   (`core/db/projects.py:111-126`) — the check passed neither.
2. `db_create_session`, `db_delete_session`, `db_delete_project` and the message
   APIs all take `owner_user_id`; the check called none of them with one.
3. Creation ran *before* the `try`, so every failure left a project row and a
   temp folder in the real DB — the leftovers were still there. Creation moved
   inside the `try`, and stale rows are cleared first.
4. The summarizer stub patched `chat_routes._summarize_history`, i.e. the
   **package** attribute, while `routes/chat/compact.py:132` calls the **module
   global**. The real summarizer ran and blocked on a model server → the check
   *hung* for 600s instead of failing. Now patched on `routes.chat.compact`.
5. The stub's signature was stale (the real function takes `user_id` as a 4th arg).
6. **The assertions themselves tested a design that no longer exists.** The
   endpoint was rewritten to append-only (`compact.py:154-169`): it returns
   `compact_message` + `reduction_pct`, writes nothing destructive, and no longer
   archives or rewrites the transcript. The check still asserted `archive_path`,
   a `messages` list and a replaced DB history. Rewritten against the current
   contract: marker shape, token reduction, and — the part worth pinning — that
   the original rows survive and exactly one marker is appended.
7. Fixing auth made the request reach `input_guard.check_async`, whose semantic
   rules auto-load the executor model. That broke the offline suite's "pure
   functions, no server" promise (and would have reached for a model in CI), so
   the guard is stubbed like the summarizer.

**Validation (all green)**
| Level | Proof |
|---|---|
| Unit | `tests/test_eval_compact_auth.py`: with principal → 400 naming the project; **without** → 401 (the negative control); short history still refused |
| Offline gate | `python tests/eval_agent.py --regression` → 7/7 PASS, no FAIL line |
| Baseline | `tests/eval_baseline.json` now records `compact_endpoint: PASS` (was FAIL) |
| CI | `python tests/eval_agent.py --mock --no-offline --regression --repeats 3` → no regressions |

### T1b — The offline gate was inert (found while fixing T1)

`if args.update_baseline:` **and** `if args.regression:` were both nested inside
`if args.mock:`. Two consequences, both silent:

- `python tests/eval_agent.py --update-baseline` (the documented way to
  re-baseline the offline suite) wrote nothing at all.
- **`python tests/eval_agent.py --regression` — CI's offline gate — could not
  fail.** In `--regression` mode the raw verdict deliberately skips counting
  failures (`if not args.regression: failed += 1`), and the baseline comparison
  that was supposed to catch them never ran without `--mock`. So any offline
  check could rot indefinitely and CI stayed green. That is the mechanism that
  let `compact_endpoint` sit red for so long without anyone noticing the gate
  wasn't enforcing anything.

Both blocks now run in either mode. `--update-baseline` on its own is strictly
additive: it rewrites the offline section and preserves tasks / aggregate_rate /
retrieval / router / code, so it can never un-gate the mock suite.

**Validation: the gate is proven to fail, in both directions.**
| Direction | Doctored baseline | Result |
|---|---|---|
| PASS → FAIL | `grammar: PASS`, check forced to fail | **exit=1**, `REGRESSION offline check grammar: PASS -> FAIL` |
| FAIL → PASS | `compaction: FAIL`, check passes | **exit=0**, silent — a fixed standing failure is not a regression |

Pinned by `tests/test_eval_baseline_update.py` (offline-only update writes;
unmeasured sections survive; a mock update still merges).

### T2 — Make `spawn_agent` failures attributable · DONE 2026-10-02

The 5 failures were unattributable: all three early-return texts
(`runner.py:66`, `:80`, `:143`) collapsed to `"error"` in
`route_log.classify_tool_result`. Without this, any subagent fix is a guess.

**Attribution.** New codes in the classifier: `no_model` (`no model is
available`), `unknown_role` (`unknown role`), `invalid_args` (`requires a
non-empty task`). Codes only — the classifier maps by substring to a fixed
vocabulary, so a role name or model path in the message never reaches the DB.

**The typed envelope.** `run_subagent` now emits
`[sub-agent · role=… · N msgs · lane=… · status=…]` with a **closed** status set:
`success`, `step_exhausted`, `no_response`. Deliberately **no `refusal`** — the
document's envelope listed `refusal` and `error`, but deciding either from prose
is the same guess that caused this bug class, and nothing measures it. Statuses
here are structural facts (did the loop finish, did the model answer), not
readings of tone.

`subagent_result_verdict(tool_name, result)` in `core/subagent/runner.py` parses
the envelope and returns `True`/`False`, or `None` when the result does not speak
for itself (non-subagent tool, bare `error:` early return, or an unrecognised
status — which fails closed to the caller's own rule). Both dispatch sites in
`routes/agent/run.py` (router shortcut at :592 and the main loop at :1462) now
consult it, and `_tool_failure_code` records `subagent:<statuses>` in
`route_events.tool_err` so an exhausted child is attributable in telemetry too.

**What this actually fixed — the inverse of the document's claim.** The parent
decides `ok = not result.startswith("error:")`. A sub-agent result is *prefixed
with the header*, so it never starts with `error:` — meaning **every step-exhausted
child was recorded as a SUCCESS**, in telemetry and in what the parent model was
told. The document claimed bad envelopes make the parent think a call *failed*;
the code did the reverse, which is worse: the failure was invisible.

**Validation (all green, 1573 tests)**
| Level | Proof |
|---|---|
| Unit | `tests/test_subagent_envelope.py`: exhausted → not ok; no_response → not ok; success → ok; **quoted `error:` text inside a successful answer stays ok** (body text must not decide); unknown status → `None` (fail closed); parallel fan-out → worst child decides |
| Classifier | each of the three early returns → its own code; a message containing a user path still yields only `no_model` |
| Parent-side | a probe replaying `run.py`'s exact computation over 6 cases: exhausted→False, success→True, quoted-error→True, early-return→False, ordinary tool→True, mixed fan-out→False — 6/6 |
| Regression | full suite OK, ruff clean, both CI eval gates green |

**Still open (T3):** with the envelope in place, `step_exhausted` will appear as
a *new* failure class that was previously invisible — so the measured
`spawn_agent` rate can legitimately rise before any behavioural fix. Read T3's
band before concluding anything: the 45.5% [21.3%, 72.0%] baseline contains none
of these calls, because they were recorded as successes.

### T3 — Choose the subagent fix from the codes, not from the document · gated on traffic

Only after T2's codes exist. Candidate fixes, in the order the data supports:

1. `no_model` dominant → fall back to the main lane instead of failing (this is
   the most likely of the three, given `executor_unavailable` appears in the
   step-0 telemetry).
2. `unknown_role` dominant → surface available roles to the parent before spawn.
3. Genuine step exhaustion → raise the *lane-aware* budget (already 8/15; the
   document's "8 local / 12 main" needs a measured reason).

**Validation:** `spawn_agent` failure rate with Wilson 95% bands vs the current
45.5% [21.3%, 72.0%]. **Stated honestly up front:** at n=11 the band is 51 points
wide; moving 5→1 failures still overlaps it. Meaningful proof needs roughly
n≥40 completed calls, so this task cannot close on a single session's traffic.

### T4 — Subagent blackboard · deferred pending a measurement

The document's claim (siblings re-read the same files) is unmeasured. Before
building `blackboard_write`/`blackboard_read`: count duplicate `read_file`
paths across sibling subagents in `usage.db`. If the duplication is small, the
feature adds two tools and a prompt surface for nothing.

### T5 — Sandboxing: spike only, and answer the companion question first — DONE (see `TIER1_SANDBOX_SPIKE.md`)

Not a build. The decisive finding is G2.2: tools run **in the companion process
on the user's machine**, so the specified server-side backend protects nothing.

**Spike result:** the attach point exists and is narrow — `companion/shellops.js`
is 79 lines with exactly two spawn sites (`exec` foreground, `spawn` background),
both already funnelling through one module with a centralised `env`. Two
corrections to the plan's assumptions: the **GPU passthrough blocker does not
exist** (inference is a host `llama-server` reached over HTTP, tool execution is a
separate tree), and on this machine **no container runtime is present at all**
(Docker/podman/WSL all absent; Hyper-V/Windows Sandbox feature state needs an
elevated query this session cannot run — my first probe wrongly concluded the
features were unavailable, and the corrected result is *unknown*, not *absent*).

A live exposure was found that needs no container at all: both spawn sites pass
the companion's **entire** environment (`env: { ...process.env, CI: "1" }`) to
agent-run commands, so any secret in the user's session env is readable by code
the model wrote. Fixing it is a one-line change at the same seam with a real
allowlist-vs-denylist tradeoff — a product decision, so it is recommended, not
done unasked.

Also wider than the document's threat model: `fsops.js:185` (`node --check`),
`androidops.js` / `iosops.js` (`execFile`), `browserops.js`, and stdio MCP servers
all spawn on the host. Isolating the shell is not isolating the agent.

**Validation:** every hop in §1 of the spike doc carries file:line evidence; §4 is
measured with the commands shown; the recommendation in §7 names what would
falsify each option. No code written — per the plan, the spike's output is a
decision.

### T6 — Ordering conflicts with the existing plan · resolve explicitly

- **Playwright** is listed as a build step here; `IMPROVEMENT_PLAN_TO_SOTA.md`
  defers it until the sandbox question is answered, because an authenticated
  browser without isolation is new attack surface. Keep the deferral; T5 first.
- **Critic swarm** is listed as a 3-day step here; the existing plan defers it
  behind report-only evidence (no build until disagreement predicts defects).
  Keep the deferral.
- **Graph-RAG** (module 5, 82→94): R9 built the symbol index; cross-reference
  edges are a plausible extension, but no measurement shows symbol search
  limiting memory quality. Treat as a scored experiment, not a scheduled step.

---

## 3. What "done" means for this plan

- T1 done (plus T1b, the inert-gate fix, and one unrelated correctness hole
  closed: the R9 symbol cache served stale results for a same-size rewrite
  landing in the same 15.6ms Windows clock tick — 25 of 40 measured rewrites
  were invisible to `(mtime_ns, size)`, so recently-touched files are now hashed
  as well; `tests/test_code_intel_cache.py` pins it).
- T2 done: sub-agent outcomes are now machine-readable and attributable, and the
  silent-success defect is closed.
- T3 is the next build-free step.
- T3 needs traffic and an honest sample-size statement, not a point estimate.
- T4, T6 stay deferred until a measurement justifies them.
- T5 done: the spike answered where isolation can attach (`companion/shellops.js`),
  dissolved the GPU-passthrough blocker, and recorded that no container runtime
  exists on this machine yet. Its one live finding (agent commands inherit the
  whole companion environment) needs a product decision before code.
- No step in this plan is justified by a score delta. The scorecard in
  `TIER1_SOTA_REMAINING_WORK.md` (85.9 → 96.5) does not reconcile with the
  tracked scoreboard (7.2/10) or `IMPROVEMENT_PLAN_TO_SOTA.md` (79.8); until one
  number is authoritative, treat every projection as sequencing.