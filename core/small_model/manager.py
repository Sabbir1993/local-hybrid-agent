import asyncio
from typing import Optional

from .config import APP_CONFIG
from .specialized import lane_engine_of_spec, make_instance


class SmallModelManager:
    """Owns the small-model instances + the idle reaper task."""

    def __init__(self):
        sm = APP_CONFIG["small_models"]
        self.instances = {name: make_instance(name, cfg)
                          for name, cfg in sm.items() if isinstance(cfg, dict)}
        self.reaper_task: Optional[asyncio.Task] = None

    def reconfigure(self, name: str, cfg: Optional[dict]) -> None:
        """Apply an edited/added/removed local lane live."""
        old = self.instances.pop(name, None)
        if old is not None:
            if old.is_up():
                old._stop()
            # close the old HTTP pool too: popping the instance used to orphan its client,
            # so every lane edit leaked a connection pool and its file descriptors
            self._close_later(old)
        if cfg is None:
            APP_CONFIG["small_models"].pop(name, None)
            return
        APP_CONFIG["small_models"][name] = dict(cfg)
        self.instances[name] = make_instance(name, APP_CONFIG["small_models"][name])

    @staticmethod
    def _close_later(inst) -> None:
        """Close an instance's client from sync code. Reconfigure is called from request
        handlers on a running loop, so schedule the close; if there is no loop the process is
        already shutting down and lifespan's aclose_all() gets it."""
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        loop.create_task(inst.aclose())

    async def aclose_all(self) -> None:
        """Release every lane's HTTP pool at shutdown."""
        for inst in list(self.instances.values()):
            await inst.aclose()

    def start_reaper(self) -> None:
        if self.reaper_task is None:
            self.reaper_task = asyncio.create_task(self._reap_loop())

    async def _reap_loop(self) -> None:
        while True:
            await asyncio.sleep(15)
            for inst in self.instances.values():
                try:
                    await inst.unload_if_idle()
                except Exception:
                    pass

    def unload_all(self) -> None:
        for role, inst in self.instances.items():
            if inst.is_up():
                print(f"[server_manager] {role}: unloading to free GPU VRAM")
                try:
                    inst._stop()
                except Exception:
                    pass

    def status(self) -> dict:
        out = {}
        for role, inst in self.instances.items():
            out[role] = {
                "kind": inst.kind,
                "engine": lane_engine_of_spec(role, getattr(inst, "cfg", None)),
                "model": inst.model_path.name if inst.model_path else None,
                "available": inst.available,
                "loaded": inst.is_up(),
                "port": inst.port,
                "error": inst.load_error,
                "state": inst.state() if hasattr(inst, "state") else None,
                "loading_since": getattr(inst, "loading_since", None),
            }
        return out


small_models = SmallModelManager()
