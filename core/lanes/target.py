import time


def _inst(lane: str):
    from ..small_model import small_models
    return small_models.instances.get(lane)


class Target:
    """One step of a job's route: a lane served by the cloud (cm set) or locally."""

    def __init__(self, lane: str, cm=None):
        self.lane = lane
        self.cm = cm

    @property
    def is_cloud(self) -> bool:
        return self.cm is not None

    @property
    def source(self) -> str:
        return "cloud" if self.cm else "local"

    def describe(self) -> str:
        if self.cm:
            return f"cloud-{self.lane}:{self.cm.display}"
        if self.lane == "main":
            return "main"
        inst = _inst(self.lane)
        if inst is not None and hasattr(inst, "describe"):
            return f"{self.lane}:{inst.describe()}"
        return f"{self.lane}:{inst.model_path.name if inst and inst.model_path else '?'}"

    @property
    def inst(self):
        """The local instance behind this step (None for cloud / main)."""
        return None if (self.cm or self.lane == "main") else _inst(self.lane)

    def available(self) -> bool:
        """Cheap check - no model is loaded."""
        if self.cm:
            return True
        if self.lane == "main":
            from ..state import state
            return state.client is not None and state.process is not None and state.process.poll() is None
        inst = _inst(self.lane)
        return bool(inst and inst.available)

    def is_up(self) -> bool:
        if self.cm:
            return True
        if self.lane == "main":
            return self.available()
        inst = _inst(self.lane)
        return bool(inst and inst.is_up())

    async def client(self):
        """A client with .post()/.stream(); loads a local model on demand."""
        from .. import cloud
        if self.cm:
            return cloud.CloudClient(self.cm)
        if self.lane == "main":
            from ..state import state
            if not self.available():
                raise RuntimeError("main model is not running")
            return state.client
        inst = _inst(self.lane)
        if not inst or not inst.available:
            raise RuntimeError(f"model '{self.lane}' is not configured or its files are missing")
        await inst.ensure_loaded()
        inst.last_used = time.time()
        return inst.client

    def __repr__(self) -> str:
        return f"<Target {self.lane} {'cloud:' + self.cm.key if self.cm else 'local'}>"
