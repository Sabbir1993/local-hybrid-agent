import json
from pathlib import Path
import sys
from typing import Optional

from ..backend import device_prefix
from ..config import ACTIVE_RUNTIME, BASE_DIR, CONFIG_FILE, ROLES_FILE
from ..process import IS_WINDOWS


def _load_common_root() -> Path:
    cfg = CONFIG_FILE
    try:
        if cfg.exists():
            d = json.loads(cfg.read_text())
            v = d.get("common_dir")
            if v:
                p = Path(v)
                p.mkdir(parents=True, exist_ok=True)
                return p.resolve()
    except Exception:
        pass
    p = Path("E:\\AI\\common")
    if not IS_WINDOWS or not p.parent.exists():
        p = BASE_DIR / "common"
    p.mkdir(parents=True, exist_ok=True)
    return p.resolve()


COMMON_ROOT = _load_common_root()

# Fallback role definitions, used when config/roles.json is missing entirely.
_DEFAULT_ROLES = {
    "planner": {
        "lane": "main", "max_steps": 6,
        "tools": ["list_files", "read_file", "grep", "search_memory", "list_skills",
                  "read_skill", "web_fetch", "web_search"],
        "system_prompt": "You are a planning sub-agent. Investigate read-only, then return a concrete numbered plan. Do not write or edit files.",
    },
    "coder": {
        "lane": "executor", "max_steps": 10,
        "tools": ["write_file", "read_file", "edit_file", "list_files", "grep", "run_python"],
        "system_prompt": "You are a focused implementation sub-agent. Make the exact edits described in your task, then report what changed.",
    },
    "reviewer": {
        "lane": "main", "max_steps": 6,
        "tools": ["list_files", "read_file", "grep"],
        "system_prompt": "You are a code-review sub-agent. Read the referenced files/diff and report concrete issues found -- do not modify anything.",
    },
}

BUILTIN_KINDS = {
    "main": "chat",
    "executor": "chat",
    "vision": "vision",
    "embedder": "embed",
}


def _load_app_config() -> dict:
    cfg = getattr(sys.modules.get("core.small_model"), "CONFIG_FILE", CONFIG_FILE)
    base = {
        "models_dir": None,
        "workspace_dir": None,
        "common_dir": None,
        "llama_bin_dir": ACTIVE_RUNTIME["llama_bin_dir"],
        "backend": ACTIVE_RUNTIME["backend"],
        "small_models": {
            "executor": {"model": None, "port": 8091, "gpu": 1, "ctx": 8192},
            "vision": {"model": None, "mmproj": None, "port": 8092, "gpu": 1, "ctx": 4096},
            "embedder": {"model": None, "port": 8093, "gpu": 1},
        },
        "router": {
            "enabled": True,
            "confidence_threshold": 0.7,
            "engine": "cactus_needle",
            "executor_grammar": True,
            "laya": {
                "checkpoint": "convaiinnovations/laya",
                "subfolder": None,
                "device": "cpu",
                "preload": False,
            },
        },
        "agent": {"exec_timeout_s": 120, "max_steps": 60, "idle_unload_s": 120},
        # Per-lane tool surface (core/tool_surface.py). The full registry is 48 tools
        # / ~8.5k tokens of schema re-sent every step; the main lane starts from a core
        # set and only receives the situational families (browser / device / office
        # documents / image generation) that the request actually names.
        "tool_surface": {"main_lane_on_demand": True, "keywords": {}},
        # Context history shaping (core/agent_loop.py::compact_messages). At most
        # keep_recent_results tool results stay verbatim in a compacted tail; the rest
        # become digest lines. 0 keeps the old "60% of the budget" tail.
        "context": {"aging": {"keep_recent_results": 2}},
        "roles": dict(_DEFAULT_ROLES),
        "provider": {},
        "cloud": {},
    }
    if cfg.exists():
        try:
            user = json.loads(cfg.read_text())
            for k, v in user.items():
                if isinstance(v, dict) and k in base and isinstance(base[k], dict):
                    base[k].update(v)
                else:
                    base[k] = v
        except Exception:
            pass
    rf = ROLES_FILE
    if rf.exists():
        try:
            roles = json.loads(rf.read_text())
            if isinstance(roles, dict):
                base["roles"] = roles
        except Exception:
            pass
    return base


APP_CONFIG = _load_app_config()


def lane_kind_of(name: str, cfg: Optional[dict] = None) -> str:
    from .specialized import lane_kind_of_spec
    return lane_kind_of_spec(name, cfg)


def lane_engine_of(name: str, cfg: Optional[dict] = None) -> str:
    from .specialized import lane_engine_of_spec
    return lane_engine_of_spec(name, cfg)
