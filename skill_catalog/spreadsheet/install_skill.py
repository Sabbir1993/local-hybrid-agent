#!/usr/bin/env python3
"""Copy this skill into Claude Code or Codex user/project skills directory."""
from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import venv
from pathlib import Path

NAME = "advanced-excel-analytics"


def main(argv=None):
    p = argparse.ArgumentParser(description="Install the Advanced Excel Analytics skill")
    p.add_argument("--agent", required=True, choices=("claude", "codex", "both"))
    p.add_argument("--scope", default="user", choices=("user", "project"))
    p.add_argument("--project-dir", type=Path, default=Path.cwd(), help="Used for --scope project")
    p.add_argument("--install-deps", action="store_true", help="Create a local .venv and install requirements")
    p.add_argument("--force", action="store_true", help="Overwrite an existing installation")
    args = p.parse_args(argv)
    home = Path.home()
    source = Path(__file__).resolve().parent
    agents = ("claude", "codex") if args.agent == "both" else (args.agent,)
    for agent in agents:
        root = ".claude" if agent == "claude" else ".agents"
        base = (home if args.scope == "user" else args.project_dir.resolve()) / root / "skills"
        dest = base / NAME
        if dest.resolve() == source:
            print(f"Already installed: {dest}")
        else:
            if dest.exists():
                if not args.force:
                    p.error(f"Already exists: {dest}. Use --force to overwrite.")
                shutil.rmtree(dest)
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copytree(source, dest, ignore=shutil.ignore_patterns(".venv", "__pycache__", "*.pyc", ".DS_Store"))
            print(f"Installed {agent}: {dest}")
        if args.install_deps:
            vdir = dest / ".venv"
            if not vdir.is_dir():
                venv.create(vdir, with_pip=True)
            python = vdir / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
            subprocess.check_call([str(python), "-m", "pip", "install", "-r", str(dest / "requirements.txt")])
            print(f"Python interpreter: {python}")
    print("Open your agent, then request: Use advanced-excel-analytics to analyze my spreadsheet.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
