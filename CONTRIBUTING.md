# Contributing

You are likely the only contributor (this is a local, single-dev project), but
the discipline below exists so the codebase stays verifiable without you
re-explaining it. The gates are cheap; nothing here needs a GPU or a model.

## Setup

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install --require-hashes -r requirements.lock
pre-commit install
```

Linux: `./scripts/setup_linux.sh` (needs a Secret Service backend for the
keychain, see `README.md`).

## Before every commit

```bash
python -m ruff check core routes tests server_manager.py autotune.py
python -m unittest discover tests
node tests/js/run_all.js
python tests/eval_agent.py --regression          # offline gate
python -m coverage report --include="core/*,routes/*"   # >= 65, ratchet up only
```

`pre-commit install` runs the ruff + auth-invariant hooks on every commit, but
it cannot run the full suite for you. The full suite is ~60-80 s; run it.

## Conventions observed in this tree

- Everything reusable lives in `core/`; `server_manager.py` only wires routes.
  A core module is imported once at the top of `server_manager.py` — keep that
  import block tidy, it doubles as the dependency map.
- Prompts, tool schemas and behavior changes to the agent loop get a live-eval
  measurement before landing; read the numbers in `tests/eval_results_live.json`.
- No bare `except:`; `except Exception as e` with a reason. No hardcoded
  per-machine values in `core/` — those are `config/app.json`'s job.
- Hashes: `requirements.lock` is drift-checked in CI; never hand-edit it, use
  `python scripts/audit_deps.py --lock`.
- Commit message: `feat` / `fix` / `chore` / `docs` + short imperative subject.
  "some fine tune added" teaches nothing six commits later.

## Adding a skill / plugin / MCP server

Follow the recipes in `PROJECT_KNOWLEDGE.md` §16 (kept current) or the
matching config keys in `config/app.json`. Everything that becomes agent
instructions must enter via code review — nothing is downloaded at runtime.

## Eval ethics

The live suite runs destructive-refusal tasks (delete files, echo a secret).
Never run it against a real workspace and never point it at your
non-EVAL account. See `README.md` → "Evaluating the agent".
