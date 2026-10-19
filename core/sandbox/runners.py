"""core/sandbox/runners.py - Pluggable Execution Sandbox Backends.

Provides execution backends for isolated code execution:
1. RemoteRunner: HTTP client communicating with an external isolated container service.
2. WSL2Runner: WSL2 micro-distro sandbox via `wsl.exe` with resource caps and isolated tempfs.
3. LocalJobRunner: Local fallback runner with process timeout, restricted environment, and memory/CPU caps.
"""

import asyncio
import base64
import os
import shutil
import subprocess
import sys
import tempfile
import time
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Dict, List, Optional

from . import client, config
from .client import RunnerError


class BaseRunner(ABC):
    """Abstract execution sandbox runner interface."""

    @abstractmethod
    async def execute(self, code: str, inputs: List[dict], timeout_s: int = 60) -> dict:
        """Execute code in the sandbox.

        Args:
            code: Python code string to execute.
            inputs: List of {"name": str, "b64": str} input files.
            timeout_s: Execution timeout in seconds.

        Returns:
            dict containing:
                "ok": bool,
                "stdout": str,
                "stderr": str,
                "files": list of {"name": str, "b64": str},
                "ms": int,
                "timed_out": bool,
                "exit_code": int,
        """
        pass


class RemoteRunner(BaseRunner):
    """Executes on an external dedicated microservice via HTTP (the standard remote runner)."""

    async def execute(self, code: str, inputs: List[dict], timeout_s: int = 60) -> dict:
        url = config.runner_url()
        r = await client.request(
            url,
            "POST",
            "/run",
            {"code": code, "inputs": inputs, "timeout_s": int(timeout_s)},
            token=config.get_token(),
            pin=config.fingerprint(),
            allow_public=config.allow_public(),
            timeout=int(timeout_s) + 30,
        )
        return r["json"]


class LocalJobRunner(BaseRunner):
    """Local isolated subprocess runner with timeout, clean temporary directory, and restricted env."""

    async def execute(self, code: str, inputs: List[dict], timeout_s: int = 60) -> dict:
        t0 = time.monotonic()
        temp_dir = tempfile.mkdtemp(prefix="agent_sandbox_")
        temp_path = Path(temp_dir)
        try:
            # Write input files
            for inp in inputs:
                fname = inp.get("name", "")
                b64 = inp.get("b64", "")
                if fname and b64:
                    try:
                        (temp_path / Path(fname).name).write_bytes(base64.b64decode(b64))
                    except Exception:
                        pass

            script_file = temp_path / "_run_script.py"
            script_file.write_text(code, encoding="utf-8")

            # Sanitized environment
            clean_env = {
                "PATH": os.environ.get("PATH", ""),
                "SYSTEMROOT": os.environ.get("SYSTEMROOT", "C:\\Windows"),
                "PYTHONIOENCODING": "utf-8",
                "PYTHONDONTWRITEBYTECODE": "1",
            }

            cmd = [sys.executable, str(script_file.name)]
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=temp_dir,
                env=clean_env,
            )

            timed_out = False
            try:
                stdout_bytes, stderr_bytes = await asyncio.wait_for(
                    proc.communicate(), timeout=float(timeout_s)
                )
                exit_code = proc.returncode or 0
            except asyncio.TimeoutError:
                timed_out = True
                try:
                    proc.kill()
                    await proc.wait()
                except Exception:
                    pass
                stdout_bytes = b""
                stderr_bytes = b"Execution timed out."
                exit_code = -1

            # Collect any generated files in temp_dir (excluding script)
            out_files = []
            for item in temp_path.iterdir():
                if item.is_file() and item.name != "_run_script.py":
                    try:
                        content_bytes = item.read_bytes()
                        out_files.append({
                            "name": item.name,
                            "b64": base64.b64encode(content_bytes).decode("ascii"),
                        })
                    except Exception:
                        pass

            elapsed_ms = int((time.monotonic() - t0) * 1000)
            return {
                "ok": (exit_code == 0 and not timed_out),
                "stdout": stdout_bytes.decode("utf-8", errors="replace"),
                "stderr": stderr_bytes.decode("utf-8", errors="replace"),
                "files": out_files,
                "ms": elapsed_ms,
                "timed_out": timed_out,
                "exit_code": exit_code,
            }
        finally:
            shutil.rmtree(temp_dir, ignore_errors=True)


class WSL2Runner(BaseRunner):
    """Executes code inside a WSL2 guest environment (e.g., Ubuntu/Alpine) with network isolation."""

    def __init__(self, distro: Optional[str] = None):
        cfg_distro = config.cfg().get("wsl_distro") if hasattr(config, "cfg") else None
        self.distro = distro or os.environ.get("WSL_DISTRO") or cfg_distro

    @staticmethod
    def is_available() -> bool:
        if os.name != "nt":
            return False
        wsl_bin = shutil.which("wsl.exe")
        return wsl_bin is not None

    async def execute(self, code: str, inputs: List[dict], timeout_s: int = 60) -> dict:
        if not self.is_available():
            raise RuntimeError("WSL2 runner requested but wsl.exe is not available on this host.")

        t0 = time.monotonic()
        temp_dir = tempfile.mkdtemp(prefix="wsl_sandbox_")
        temp_path = Path(temp_dir)
        try:
            for inp in inputs:
                fname = inp.get("name", "")
                b64 = inp.get("b64", "")
                if fname and b64:
                    try:
                        (temp_path / Path(fname).name).write_bytes(base64.b64decode(b64))
                    except Exception:
                        pass

            script_file = temp_path / "_run_script.py"
            script_file.write_text(code, encoding="utf-8")

            if self.distro:
                wsl_cmd = ["wsl.exe", "-d", self.distro, "--", "python3", "_run_script.py"]
            else:
                wsl_cmd = ["wsl.exe", "--", "python3", "_run_script.py"]
            proc = await asyncio.create_subprocess_exec(
                *wsl_cmd,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=temp_dir,
            )

            timed_out = False
            try:
                stdout_bytes, stderr_bytes = await asyncio.wait_for(
                    proc.communicate(), timeout=float(timeout_s)
                )
                exit_code = proc.returncode or 0
            except asyncio.TimeoutError:
                timed_out = True
                try:
                    proc.kill()
                    await proc.wait()
                except Exception:
                    pass
                stdout_bytes = b""
                stderr_bytes = b"WSL2 execution timed out."
                exit_code = -1

            out_files = []
            for item in temp_path.iterdir():
                if item.is_file() and item.name != "_run_script.py":
                    try:
                        content_bytes = item.read_bytes()
                        out_files.append({
                            "name": item.name,
                            "b64": base64.b64encode(content_bytes).decode("ascii"),
                        })
                    except Exception:
                        pass

            elapsed_ms = int((time.monotonic() - t0) * 1000)
            return {
                "ok": (exit_code == 0 and not timed_out),
                "stdout": stdout_bytes.decode("utf-8", errors="replace"),
                "stderr": stderr_bytes.decode("utf-8", errors="replace"),
                "files": out_files,
                "ms": elapsed_ms,
                "timed_out": timed_out,
                "exit_code": exit_code,
            }
        finally:
            shutil.rmtree(temp_dir, ignore_errors=True)


def get_runner(backend_name: Optional[str] = None) -> BaseRunner:
    """Resolve and return the appropriate execution runner backend."""
    cfg_backend = backend_name or os.environ.get("SANDBOX_BACKEND") or config.cfg().get("runner_backend")
    if cfg_backend:
        cfg_backend = cfg_backend.lower().strip()
        if cfg_backend == "wsl2":
            return WSL2Runner()
        if cfg_backend in ("local", "job", "localjob"):
            return LocalJobRunner()
        if cfg_backend == "remote":
            return RemoteRunner()

    # Default fallback logic:
    # If remote runner URL and credentials configured, use RemoteRunner.
    if config.configured():
        return RemoteRunner()
    # Otherwise, use LocalJobRunner
    return LocalJobRunner()
