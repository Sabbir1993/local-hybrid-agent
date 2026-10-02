# T5 Spike — where can sandboxing actually attach? (2026-10-02)

Answers the four questions `TIER1_EXECUTION_PLAN.md` §T5 required **before** any
sandbox code. Outcome: the threat model is real, the document's implementation
site is wrong, and the biggest alleged blocker (GPU passthrough) does not exist.

---

## 1. Where a tool call actually executes

Traced end to end, every hop with evidence:

| Hop | Where | Evidence |
|---|---|---|
| Agent requests a shell command | **server** | `core/shell_tools.py:227-233` → `companion_bridge.call(uid, "shell.run", …)` |
| Comment states the rule | **server** | `core/shell_tools.py:213`: "Agent shell commands run ONLY on the user's machine via the companion." |
| Companion policy gate | **companion** | `companion/main.js:417` `policy.confirmShell(...)`; `run_python` re-verifies the script on disk against the approved text (`main.js:422-426`) |
| **Actual process spawn** | **companion** | `companion/shellops.js:60` `exec(...)` (foreground) and `:26` `spawn(...)` (background) |
| `run_python` | **companion** | same path — `core/agent_tools/file_ops.py:320` sends `shell.run` with `python "<script>"` |

**Consequence: the document's design cannot work as written.** `TIER1_SOTA_REMAINING_WORK.md` §3.2 puts a `BaseExecutionBackend` / `DockerExecutionBackend` in `core/process_sandbox.py` on the server, routing `run_shell` / `run_python` through it. No server-side function is ever on that path — the server hands the string to the companion and stops. A server-side backend would sandbox nothing while looking like it did.

## 2. The seam that does exist — and it is narrow

`companion/shellops.js` is 79 lines with exactly **two** spawn sites (`run`, `runBackground`). That is the whole attach point. Two properties make it a good one:

- **Env is already centralised**: both sites pass `env: { ...process.env, CI: "1" }` (`shellops.js:28`, `:66`). One line changes what a command can see.
- **Nothing else spawns agent commands**: `main.js:416-428` is the only route into `shellops`, and the server's allow/deny policy has already run by then.

Constraints any backend must respect:

- `shell: true` at both sites (`:27`, and the `exec` default) — commands go through `cmd.exe`/sh, so isolation must wrap the **shell**, not an argv.
- `cwd` is the user's real project directory (`core/shell_tools.py:220`), and `fs.*` ops write there. A container needs a bind mount, or the agent sees an empty workspace.
- `runBackground` detaches (`detached: true`, `child.unref()`) and is stopped by the caller with `taskkill /PID … /T /F`. A container backend must keep that kill path working or leak containers.

## 3. The GPU blocker dissolves

`TIER1_SOTA_REMAINING_WORK.md` §3.2 mounts a workspace and expects Vulkan
passthrough, because it assumed the model runs alongside the command. It does not:
inference is `llama-server` on the host, reached over HTTP (`core/lanes.py`), and
tool execution is a separate process tree. **Isolating tool execution needs no GPU
access at all.** The passthrough question that justified deferring this work does
not need answering.

## 4. What this machine can offer today (measured, not assumed)

| Primitive | State | How measured |
|---|---|---|
| Docker | **not installed** | `Get-Command docker` → absent |
| Podman | **not installed** | absent |
| WSL | **not installed** | `wsl --status` → "The Windows Subsystem for Linux is not installed" (`wsl.exe` stub present) |
| Hypervisor | present | `Win32_ComputerSystem.HypervisorPresent = True` |
| OS / SKU | Windows 11 Pro N, build 26200, 20 logical CPUs, 64 GB | `Win32_OperatingSystem` |
| Windows Sandbox / Hyper-V / VMP feature state | **UNKNOWN — needs elevation** | `Get-WindowsOptionalFeature` returned *"The requested operation requires elevation"*; this session is not elevated |
| AppContainer | API importable (expected on all SKUs) | P/Invoke probe imported; current process is unconfined (`GetCurrentPackageId` → 0x80070032) |
| Job objects | available in-kernel | CPU/memory caps, **no filesystem or network isolation** |

**Correction to my own first probe:** it printed "feature not offered on this SKU"
because errors were suppressed. The features may well be installable; one elevated
command settles it and I could not run it:

```powershell
Get-WindowsOptionalFeature -Online -FeatureName Containers-DisposableClientVM,Microsoft-Hyper-V-All,VirtualMachinePlatform
```

Until that is answered, no honest claim can be made about Windows Sandbox or
Hyper-V availability here. Do not let a plan assume it.

## 5. The threat surface is wider than `shell.run`

Sandboxing only the shell would leave these on the host, all reachable by an agent:

- `companion/fsops.js:185` — `execFile(process.execPath, ["--check", p])` (JS syntax verify)
- `companion/androidops.js`, `companion/iosops.js` — `execFile` for adb / simctl
- `companion/browserops.js` — arbitrary JS in the test browser
- `core/mcp/manager.py` — stdio MCP servers spawned as local processes

So "isolate the shell" is not "isolate the agent". Any threat model must say which
of these it covers; the document's does not.

## 6. Cheap win available today, independent of any container

Both spawn sites pass the companion's **entire** environment to agent-run
commands (`shellops.js:28`, `:66`). Whatever secrets exist in the user's session
environment — cloud keys, `GITHUB_TOKEN`, proxy credentials — are readable by any
command the model writes, and `run_python` is arbitrary Python. This is a real
exposure today, fixable at the same seam, with no new dependency.

The tradeoff, stated honestly: an **allowlist** of env vars is safer but will break
tooling that needs `HOME`, `PATH`, proxies, `NODE_*`, `SSL_CERT_FILE`; a
**denylist** of secret-shaped names is cheap and breaks nothing, but misses
unknown-named secrets. This is a product decision, not a spike finding — it needs
a deliberate choice, and it is a change to the companion package, so it is not
done here unasked.

## 7. Recommendation (ordered, with what would falsify each)

1. **Decide the env policy** (§6) — days, no dependencies, removes a live exposure.
2. **Record the isolation mode in telemetry** — the server cannot know how the
   companion executed a command; a `mode` field alongside `shell.run` results is
   what makes any future claim ("we now sandbox X% of commands") measurable
   instead of aspirational.
3. **Then pick a backend, cheapest first:**
   - **WSL2 distro** (recommended if the feature installs): filesystem + network
     isolation, bind-mount the workspace, no Docker licence question. Falsified if
     the feature is unavailable without a reboot the user won't take, or if the
     Windows-side tooling the agent needs (adb, node, python) isn't installed
     inside the distro.
   - **Docker Desktop**: heavier, same isolation, adds a daemon the user must run.
     Falsified if the user won't install it — it is a much larger ask than WSL.
   - **Job objects only**: cheap, but caps CPU/memory and nothing else. Useful as a
     complement (blast-radius cap), never as the isolation story.
   - **AppContainer**: no install, but severe workflow friction — no shell, no
     inherited PATH, capability grants per directory. Likely wrong for interactive
     dev commands; worth it only for a narrow "untrusted script" mode.
4. **Do not build the server-side backend the document specifies.** It cannot
   intercept these calls.

## 8. What this spike does not settle

- Whether the user will install a runtime (WSL/Docker) and accept a reboot — a
  product conversation, not an engineering finding.
- The document's "Security 92 → 98" projection. No threat model is stated
  anywhere in `TIER1_SOTA_REMAINING_WORK.md`, so the delta is not a measurement
  and cannot be validated as one. Until a threat model exists, treat any security
  score movement as unmeasured.
- Whether sandboxing tool execution is acceptable for the enterprise/overnight
  (`/goal`) use case the document invokes — that depends on §7.3, not on code.