"""Taskboard: one list of everything running (or recently run) in the background, with stop.

Owns no execution state - every field is read from the run journal (core/runs.py) and the
live handles of the subsystem that owns the work (routes/agent/run_manager.py, core/media/jobs.py).
"""

from .service import get_task, list_tasks, stop_task

__all__ = ["list_tasks", "get_task", "stop_task"]
