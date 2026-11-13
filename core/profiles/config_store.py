import json
import sys
from ..config import (
    ACTIVE_RUNTIME,
    MODEL_CONFIG_KEYS,
    MODEL_CONFIGS_FILE,
    RUNTIME_TUNED_KEYS,
    atomic_write_json,
)
from .constants import _model_key


def load_model_configs() -> dict:
    store: dict = {}
    migrated = False
    try:
        if MODEL_CONFIGS_FILE.exists():
            raw = json.loads(MODEL_CONFIGS_FILE.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                for k, v in raw.items():
                    if "\\" in k or "/" in k or ":" in k:
                        norm_key = _model_key(k)
                        store[norm_key] = v
                        migrated = True
                    else:
                        store[k.lower()] = v
    except Exception as e:
        print(f"[server_manager] model_configs.json unreadable: {e}", file=sys.stderr)

    if migrated:
        try:
            atomic_write_json(MODEL_CONFIGS_FILE, store)
            print(f"[server_manager] migrated model_configs.json to model-name keys")
        except Exception as e:
            print(f"[server_manager] failed saving migrated model_configs.json: {e}", file=sys.stderr)

    return store


def save_model_config(model_identifier: str, cfg: dict) -> None:
    """Persist a subset of config for one model name in model_configs.json.

    n_gpu_layers / tensor_split live under the profile's "tuned" section while
    all other keys sit at the top level, so the tuned (effective) value wins.
    """
    store = load_model_configs()
    key = _model_key(model_identifier)
    tuned = cfg.get("tuned", {}) or {}
    previous = store.get(key) or {}
    runtime_tuned = dict(previous.get("runtime_tuned") or {})
    for runtime_name, values in (cfg.get("runtime_tuned") or {}).items():
        if isinstance(values, dict):
            runtime_tuned[str(runtime_name)] = dict(values)
    active_name = str(cfg.get("runtime_name") or ACTIVE_RUNTIME["name"])
    active_tuned = {k: tuned[k] for k in RUNTIME_TUNED_KEYS if k in tuned}
    if active_tuned:
        runtime_tuned[active_name] = active_tuned
    out = {}
    for k in MODEL_CONFIG_KEYS:
        if k in tuned:
            out[k] = tuned[k]
        elif k in cfg:
            out[k] = cfg[k]
    if runtime_tuned:
        out["runtime_tuned"] = runtime_tuned
    store[key] = out
    try:
        atomic_write_json(MODEL_CONFIGS_FILE, store)
        print(f"[server_manager] saved model config for [{key}] in model_configs.json")
    except Exception as e:
        print(f"[server_manager] model_configs.json write failed: {e}", file=sys.stderr)


def set_model_engine(model_identifier: str, engine: str) -> None:
    """Remember which engine (llama / vllm) a model runs on, leaving its other saved settings alone."""
    store = load_model_configs()
    key = _model_key(model_identifier)
    store[key] = {**(store.get(key) or {}), "engine": engine}
    try:
        atomic_write_json(MODEL_CONFIGS_FILE, store)
    except Exception as e:
        print(f"[server_manager] model_configs.json write failed: {e}", file=sys.stderr)
