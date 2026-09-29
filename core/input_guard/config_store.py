def guard_cfg() -> dict:
    """Live input_guard config block from the hot-reloaded APP_CONFIG."""
    from ..small_model import APP_CONFIG
    cfg = APP_CONFIG.get("input_guard")
    if not isinstance(cfg, dict):
        from ..config import CONFIG_FILE
        try:
            if CONFIG_FILE.exists():
                import json
                file_cfg = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
                if isinstance(file_cfg.get("input_guard"), dict):
                    cfg = file_cfg["input_guard"]
                    APP_CONFIG["input_guard"] = cfg
        except Exception:
            pass
    return cfg if isinstance(cfg, dict) else {}


def rules_for(cfg_key: str) -> list:
    """Enabled rules for a guard config block ("input_guard"/"output_guard").
    Shared by both the input and output sanitizers."""
    from ..small_model import APP_CONFIG
    cfg = APP_CONFIG.get(cfg_key)
    if not isinstance(cfg, dict):
        from ..config import CONFIG_FILE
        try:
            if CONFIG_FILE.exists():
                import json
                file_cfg = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
                if isinstance(file_cfg.get(cfg_key), dict):
                    cfg = file_cfg[cfg_key]
                    APP_CONFIG[cfg_key] = cfg
        except Exception:
            pass
    if not isinstance(cfg, dict) or not cfg.get("enabled", False):
        return []
    return [r for r in (cfg.get("rules") or [])
            if isinstance(r, dict) and r.get("enabled", True)]


def enabled() -> bool:
    return bool(guard_cfg().get("enabled", False))
