"""
core/startup.py - App lifespan: first-boot bootstrap, legacy migrations, tool
registration, background tasks, and process teardown on shutdown.
"""

import asyncio
import getpass
import json
import secrets
import os
import sys
from contextlib import asynccontextmanager
from fastapi import FastAPI
from core.config import ACTIVE_RUNTIME, PROVIDERS_FILE
from core import auth_db
from core.auth_provider import hash_password
from core.mcp import connect_all_mcp, stop_all_mcp
from core.plugins import load_plugins
from core.process import kill_orphan_llama_servers
from core.registry import bootstrap_builtin_tools
from core.shell_tools import register_shell_tools
from core.skills import register_skill_tools
from core.small_model import small_models
from core.memory import memory_background_task
from core.state import keepalive_loop, state
from core.web_tools import register_web_tools
from core.browser_tools import register_browser_tools
from core.device_tools import register_device_tools
from core.router_tuner import tuner_background_task
from core.file_tools import register_file_tools
from core.media_tools import register_media_tools
from routes import common


def _bootstrap_super_admin() -> None:
    """First-boot only: create the one super-admin account. Never a hardcoded
    default credential -- taken from env vars if present (for scripted/CI
    setup), otherwise prompted interactively on the console."""
    if auth_db.count_users() > 0:
        return
    username = os.environ.get("A770_BOOTSTRAP_USERNAME")
    password = os.environ.get("A770_BOOTSTRAP_PASSWORD")
    if not username or not password:
        print("\n[auth] No users exist yet -- create the initial super-admin account.")
        try:
            username = input("  Username: ").strip() or "admin"
            password = getpass.getpass("  Password (leave blank to auto-generate): ")
        except (EOFError, OSError):
            username, password = "admin", None
    if not password:
        password = secrets.token_urlsafe(16)
        print(f"[auth] Generated super-admin password (save this now): {password}")
    uid = auth_db.create_user(username=username, password_hash=hash_password(password), is_super_admin=True)
    auth_db.assign_role(uid, "admin")
    print(f"[auth] Super-admin account '{username}' created.\n")


def _backfill_legacy_data_ownership() -> None:
    """Attribute any pre-auth projects.db rows (from before this feature existed)
    to a super admin so nothing is left ownerless. Idempotent no-op once done."""
    from core.db import backfill_legacy_owner
    row = auth_db.db().execute(
        "SELECT id FROM users WHERE is_super_admin = 1 ORDER BY id LIMIT 1"
    ).fetchone()
    if row:
        backfill_legacy_owner(row["id"])


def _backfill_legacy_cloud_providers() -> None:
    """One-time migration: the old shared config/providers.json (pre-per-user
    cloud config) becomes the first super admin's own per-user file, with API
    keys moved to the OS keychain. A providers.json.migrated left behind by the
    older migration (plaintext keys) gets the same treatment. Both files are
    shredded afterwards. Idempotent -- a no-op once the legacy files are gone."""
    legacy = [p for p in (PROVIDERS_FILE, PROVIDERS_FILE.with_suffix(".json.migrated")) if p.exists()]
    if not legacy:
        return
    row = auth_db.db().execute(
        "SELECT id FROM users WHERE is_super_admin = 1 ORDER BY id LIMIT 1"
    ).fetchone()
    if not row:
        return
    from core import cloud as cloud_core
    for p in legacy:
        try:
            r = cloud_core.import_legacy_file(p, row["id"])
            print(f"[server_manager] legacy {p.name}: {r['imported']} key(s) moved to the OS keychain, "
                  f"{r['skipped']} already there/unused; plaintext file removed")
        except Exception as e:
            print(f"[server_manager] legacy {p.name} not migrated (file kept): {type(e).__name__}",
                  file=sys.stderr)


_shutdown_done = False


def _shutdown_cleanup() -> None:
    """Kill llama-server + small models + MCP servers. Blocking, idempotent."""
    global _shutdown_done
    if _shutdown_done:
        return
    _shutdown_done = True
    try:
        state._stop_process_locked()
    except Exception as e:
        print(f"[server_manager] main model cleanup error: {e}", file=sys.stderr)
    for role, inst in small_models.instances.items():
        try:
            inst._stop()
        except Exception as e:
            print(f"[server_manager] {role} cleanup error: {e}", file=sys.stderr)
    try:
        stop_all_mcp()
    except Exception as e:
        print(f"[server_manager] mcp cleanup error: {e}", file=sys.stderr)
    print("[server_manager] shutdown cleanup complete")


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Before any request: fix the foreign-key schema (backs up first) and
    # switch foreign keys on for the databases that check clean.
    from core.db_repair import enable_foreign_keys
    enable_foreign_keys()
    _bootstrap_super_admin()
    _backfill_legacy_data_ownership()
    _backfill_legacy_cloud_providers()
    bootstrap_builtin_tools()
    register_web_tools()
    register_skill_tools()
    register_shell_tools()
    register_file_tools()
    register_media_tools()
    register_browser_tools()
    register_device_tools()
    load_plugins()
    print(f"[server_manager] runtime={ACTIVE_RUNTIME['name']} backend={ACTIVE_RUNTIME['backend']} "
          f"bin={ACTIVE_RUNTIME['llama_bin_dir']} devices={ACTIVE_RUNTIME['gpu_devices']}")
    from core.credentials import keyring_backend_warning
    if (kr_warn := keyring_backend_warning()):
        print(f"[server_manager] WARNING: {kr_warn}", file=sys.stderr)
    await asyncio.get_event_loop().run_in_executor(None, kill_orphan_llama_servers)
    asyncio.create_task(connect_all_mcp())
    if common.initial_profile_path is not None and common.initial_profile_path.exists():
        try:
            state.profile_path = common.initial_profile_path
            state.profile = json.loads(common.initial_profile_path.read_text(encoding="utf-8"))
            print(f"[server_manager] Selected initial profile: {state.profile.get('name')}")
        except Exception as e:
            print(f"[server_manager] Warning loading initial profile JSON: {e}")

    state.watchdog_task = asyncio.create_task(state.watchdog())
    state.keepalive_task = asyncio.create_task(keepalive_loop())
    small_models.start_reaper()
    memory_task = asyncio.create_task(memory_background_task())
    tuner_task = asyncio.create_task(tuner_background_task())
    yield
    # Fast, cancellable: stop background loops first
    for task in (state.watchdog_task, state.keepalive_task, small_models.reaper_task, memory_task, tuner_task):
        if task:
            task.cancel()
    # Blocking process teardown runs off the loop thread
    try:
        await asyncio.shield(asyncio.to_thread(_shutdown_cleanup))
    except (asyncio.CancelledError, KeyboardInterrupt):
        _shutdown_cleanup()
