# PROJECT KNOWLEDGE - Local Agent

**Maintained sections only.** The A1-A17 architecture snapshot (written against the
pre-split tree at `c334c28`) was archived whole to
`docs/history/PROJECT_KNOWLEDGE-A1-A17-full-2026-10-01.md` on 2026-10-06 - its file names,
line counts and layout were invalidated by the package split and it had grown into a trap.
What is worth keeping lives below.

---

## 18. Run the live eval (manual)

The mock suite gates loop behaviour; live tasks measure whether the agent is
any good with a real model. Run these after a model swap, prompt change, or
router tuning - not on every commit.

One-time setup: create a dedicated `eval` user (no MFA - the runner cannot
answer TOTP) plus a **throwaway workspace** on disk
(`--live-workspace`, absolute path - required, the agent loop refuses runs
without a project folder). The harness creates and activates a project named
`__eval__` for that user on login (`tests/eval_agent.py::_live_ensure_project`),
so no manual project setup is needed. Never point `--live-workspace` at a real
project: `escalation_task` writes files (bounded to `eval_tmp/`, but still).

```
python tests/eval_agent.py --live --live-user eval --live-password '...' \
    --live-workspace ./eval_ws
```

The harness also preflights the Companion connection
(`_live_preflight_companion`): file tools run on the user's machine through the
Companion websocket, and without it every task fails the same way. Record a
baseline with --update-live-baseline` (review the diff), then gate on
pass-rate drops with --live-regression`.

---

## 19. Deployment envelope (G2: documented, not engineered)

Single box, single process, SQLite, TLS-terminates-elsewhere. Verified against
the working tree; revisit before any second-machine or internet-facing deploy.

- **Process:** one `server_manager.py`. Background tasks (tuner, memory index,
  monitor) run on the loop thread. A restart drops SSE streams, the IP-failure
  table, and MFA tickets/fail counters (sessions and audit rows survive).
- **Ports (localhost by default):** manager `PROXY_PORT` 8000, llama-server
  8090, small models 8091-8093. `PROXY_HOST` defaults to `127.0.0.1`
  (`A770_HOST` overrides); anything off-box must go through a TLS tunnel/VPN -
  the server speaks plain HTTP and sets no HSTS/security headers itself.
- **Trust root:** `auth.db` holds argon2 password hashes, sha256 session/token/
  backup-code hashes, and TOTP secrets (plaintext-equivalent - see R2 below).
  Filesystem read of the DB directory bypasses every control in this document.
- **Other state:** `projects.db` (projects/sessions/chats), `memory.db`
  (chunks + vectors), `usage.db` (requests, telemetry, tuner tables).
  `db_backups/` retention is bounded; restores are manual file copies.
- **Sessions:** 15-min sliding idle (toggleable), 7-day absolute cap. MFA TOTP
  is per-user opt-in plus admin-enforceable; the fenced endpoints
  (users/roles/database.manage, token mint, pairing, MFA management) need a
  verified session from MFA-enrolled accounts.
- **Companion (user devices):** device keys replace the browser session on the
  socket; file ops stay inside user-approved folders (symlink-resolved);
  `approved_in_app` skips the native dialog unless "also confirm on this
  device" is on - a compromised server can execute within approved folders
  without a second prompt (see R9).

## 20. Residual risk register (G3)

Reviewed as a set 2026-10-01. Each item is accepted with a reason, not
overlooked. Revisit when the deployment envelope (A19) changes.

- **R1 - 8-char minimum passwords (PCI DSS asks 12).** Deliberate owner
  decision. Compensated by argon2, per-account lockout (10/30min), per-IP
  throttle (30/15min), and MFA availability. Revisit for regulated data.
- **R2 - TOTP secrets stored decryptable in `auth.db`.** No KEK
  infrastructure exists; encrypting with a key on the same disk adds theatre,
  not security. The DB directory is the trust root (A19).
- **R3 - API tokens live up to 90 days.** Fenced perms can never attach to a
  token, scopes intersect current rights at every use, and minting needs a
  verified session. Revocation is one click; expiry is a backstop, not the control.
- **R4 - Throttles and MFA tickets are in-memory.** A restart zeroes the IP
  table (fresh 30 guesses) and voids tickets (fail-closed: re-login). Acceptable
  for a single-tenant box; a shared/multi-process deploy must externalise these.
- **R5 - `eval_results.json` was tracked and rewritten per run.** Closed
  2026-10-06: both `tests/eval_results.json` and `tests/eval_results_live.json`
  are git-ignored. The task set (`tests/eval_tasks.py`) and baselines
  (`tests/eval_baseline.json`, `tests/eval_live_baseline.json`) stay committed.
- **R6 - No HA.** One process: restart drops streams and watches (tuner
  `watching` rows resume judging on next tick; nothing is lost but time).
- **R7 - SQLite single-writer.** Fits current load (one box, one team).
  Concurrent-write errors would surface first in usage.db telemetry writes.
- **R8 - Plain HTTP on the wire.** Covered by A19: bind to loopback, tunnel
  for anything remote. Binding `0.0.0.0` on an untrusted LAN without a tunnel
  voids the session-security assumptions (cookies, no HSTS).
- **R9 - Server-trusting companion.** By design the server decides *what* and
  the device decides *whether* - but `approved_in_app` means a compromised
  server skips the second prompt. Contained to approved folders; "also confirm
  on this device" exists for high-risk machines.
- **R10 - Sub-agent change-sets are shared.** Closed 2026-10-06: a child's own
  file writes are dropped from the parent's `_ws_changes` diff/undo set when the
  child finishes (`core/subagent/scope.py::drop_child_write_tracking`). Parallel
  siblings' files survive (the child only clears its own paths).
- **R11 - No OCR engine.** Image-only PDFs fail closed with a specific error
  naming the cause; mixed PDFs ingest their text pages and silently skip
  image-only ones (no per-page signal). No OCR binaries exist in this pipeline
  by design (supply-chain surface). Revisit when corpuses arrive as scans.
- **R12 - Footnotes/endnotes/comments and embedded images/charts have no
  extraction path.** DOCX body + tables + headers/footers are covered and
  pinned by `tests/test_ingest_fidelity.py`; everything else non-text is
  silently absent from the index. Revisit when a corpus keeps decisions in
  footnotes rather than body text.

---

*HEAD `4703959` (2026-10-04). Line numbers of the archived half refer to its
revision; rely on section numbers, not line numbers, after further edits.*
