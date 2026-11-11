"""Engines that can serve the main model. llama.cpp (core/process.py) is the default; vLLM is optional (core/engines/vllm.py).

Helper models (executor, vision, embedder, ...) always run on llama.cpp."""

ENGINES = ("llama", "vllm")


def engine_of(profile) -> str:
    """The engine a profile runs on: its "engine" key, else llama.cpp."""
    e = str((profile or {}).get("engine") or "llama").strip().lower()
    return e if e in ENGINES else "llama"
