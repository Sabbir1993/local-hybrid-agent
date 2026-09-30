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
        "agent": {"exec_timeout_s": 120, "max_steps": 60, "idle_unload_s": 120, "run_token_budget": 2500000},
        # Per-lane tool surface (core/tool_surface.py). The full registry is 48 tools
        # / ~8.5k tokens of schema re-sent every step; the main lane starts from a core
        # set and only receives the situational families (browser / device / office
        # documents / image generation) that the request actually names.
        "tool_surface": {"main_lane_on_demand": True, "keywords": {}},
        # Context history shaping (core/agent_loop.py::compact_messages). At most
        # keep_recent_results tool results stay verbatim in a compacted tail; the rest
        # become digest lines. 0 keeps the old "60% of the budget" tail.
        "context": {"aging": {"keep_recent_results": 3},
                    # cloud lanes have huge windows, so history is bounded by these instead (core/agent_loop/clearing.py,
                    # core/context_budget.py): compact the prompt past cloud_budget_tokens; past clear_trigger_tokens
                    # old tool results are replaced by a one-line placeholder (newest clear_keep_results stay)
                    "cloud_budget_tokens": 60000, "clear_trigger_tokens": 30000,
                    "clear_keep_results": 4, "clear_at_least_tokens": 8000},
        "roles": dict(_DEFAULT_ROLES),
        "provider": {},
        "cloud": {},
        "capabilities": {
            "web": True, "web_search_api_key": "", "skills": True,
            "mcp": True, "mcp_servers": {}, "plugins": True,
            "shell": {"enabled": True, "ask_first": True, "timeout_s": 60,
                      "allow_patterns": ["git *", "npx *", "npm *", "pip *", "python *"]},
        },
        "input_guard": {"enabled": False, "rules": []},
        "output_guard": {"enabled": False, "rules": []},
        # images / videos / speech (core/media). Audio can't be scanned for card numbers,
        # so cloud speech-to-text is off until an admin allows it.
        "media": {
            "allow_cloud_audio": False,
            "limits": {"image_per_day": 50, "video_per_day": 5},
            "max_audio_mb": 25,
        },
    }
    # Role definitions live in config/roles.json: they merge over the built-in defaults.
    try:
        if ROLES_FILE.exists():
            roles_d = json.loads(ROLES_FILE.read_text())
            if isinstance(roles_d, dict):
                base["roles"].update(roles_d)
    except Exception as e:
        print(f"[server_manager] roles.json unreadable: {e}", file=sys.stderr)

    try:
        if cfg.exists():
            user = json.loads(cfg.read_text())
            for k, v in user.items():
                if k == "small_models" and isinstance(v, dict):
                    # a built-in lane merges over its defaults (port, gpu, ctx survive a
                    # partial entry); any other entry is a custom local lane
                    for lane, lane_cfg in v.items():
                        if isinstance(lane_cfg, dict):
                            base["small_models"].setdefault(lane, {}).update(lane_cfg)
                elif isinstance(v, dict) and k in base and isinstance(base[k], dict):
                    base[k].update(v)     # includes a legacy "roles" block, which wins over roles.json
                else:
                    base[k] = v
    except Exception as e:
        print(f"[server_manager] config.json unreadable: {e}", file=sys.stderr)
    return base


APP_CONFIG = _load_app_config()


def lane_kind_of(name: str, cfg: Optional[dict] = None) -> str:
    from .specialized import lane_kind_of_spec
    return lane_kind_of_spec(name, cfg)


def lane_engine_of(name: str, cfg: Optional[dict] = None) -> str:
    from .specialized import lane_engine_of_spec
    return lane_engine_of_spec(name, cfg)
