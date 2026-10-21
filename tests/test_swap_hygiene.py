"""P3-SWAP-HYGIENE: learned per-model state must reset on a (re)load.

A local main-lane swap kills the llama-server process and starts a new one
(core/state.py _load_locked). Two pieces of learned state describe the OLD
model and silently carry onto the new one today:
  - core/context_budget.py _STATE (prompt window/anchor) and _TPS_STATE
    (tok/s EMA) - keyed by lane name only, and reset() has "useful on a model
    (re)load" in its docstring with no caller.
  - routes/common/llm_stream.py _TOOL_CHOICE_UNSUPPORTED - keyed by
    "local:<base_url>" where base_url is the FIXED port 8090, so a swap to a
    tool-capable model inherits the old model's rejected-tool_choice verdict.
A swap hook fired by state after a healthy load lets each owner clean itself.
"""

import asyncio
import unittest
from unittest import mock

import core.state as state_mod
import core.context_budget            # noqa: F401 - registers the canonical hooks
import routes.common.llm_stream       # noqa: F401 - registers the canonical hooks


class FakePopen:
    def __init__(self):
        self.pid = 4242
        self.poll = lambda: None          # alive
        self.stdout = iter([])
        self.stderr = None


def _fake_state():
    st = state_mod.ProxyState()
    st.process = None
    return st


# baseline from import time: the canonical registrations (context_budget,
# llm_stream) connected when their modules first imported core.state. Tests
# that clear the list must restore THIS, or later tests lose the owners.
_BASELINE_HOOKS = list(state_mod._SWAP_HOOKS)


def _restore_hooks():
    state_mod._SWAP_HOOKS[:] = _BASELINE_HOOKS


def _seed_local_state():
    cb = __import__("core.context_budget", fromlist=["x"])
    cb.reset(None)
    cb.reset_tps(None)
    cb._state("main")            # creates + remembers a state dict
    cb.record_tps("main", tokens_generated=10, elapsed_s=10.0)
    return cb


class HookRegistrationTests(unittest.TestCase):
    def setUp(self):
        _restore_hooks()

    def tearDown(self):
        _restore_hooks()

    def test_register_exists_and_dedups(self):
        calls = []
        fn = lambda: calls.append(1)
        state_mod.register_swap_hook(fn)
        state_mod.register_swap_hook(fn)          # same callable twice
        self.assertEqual(state_mod._SWAP_HOOKS.count(fn), 1)
        state_mod._SWAP_HOOKS.remove(fn)

    def test_source_fires_hooks_after_healthy_load(self):
        src = __import__("inspect").getsource(state_mod.ProxyState._load_locked)
        self.assertIn("notify_swap()", src, "_load_locked must call the hooks")


class LoadFiresHooksTests(unittest.TestCase):
    def setUp(self):
        _restore_hooks()

    def tearDown(self):
        _restore_hooks()

    def _run_load(self, st, hook, healthy=True, expect_fail=False):
        """Drive the REAL _load_locked with a faked spawn + health wait."""
        calls = []
        calls_fn = hook or (lambda: calls.append(1))

        async def unhealthy(proc):
            raise RuntimeError("never healthy")

        async def wait(proc):
            return None

        state_mod.register_swap_hook(calls_fn)
        with mock.patch.object(state_mod.subprocess, "Popen",
                               return_value=FakePopen()), \
             mock.patch.object(state_mod, "build_launch_command",
                               return_value=["fake"]), \
             mock.patch.object(state_mod.vram, "check_or_raise"), \
             mock.patch.object(state_mod, "LLAMA_SERVER_PORT", 8090), \
             mock.patch.object(state_mod.ProxyState, "_wait_healthy",
                               new=wait if healthy else unhealthy), \
             mock.patch.object(st, "_spawn_log_pump", return_value=None):
            if expect_fail:
                with self.assertRaises(RuntimeError):
                    asyncio.run(st.load_profile({"model_path": "m.gguf"}))
            else:
                asyncio.run(st.load_profile({"model_path": "m.gguf"}))
        return calls

    def test_load_profile_runs_hook_fires_once(self):
        self.assertEqual(len(self._run_load(_fake_state(), None)), 1)

    def test_hook_exception_never_breaks_the_load(self):
        st = _fake_state()
        self._run_load(st, _boom)
        self.assertTrue(st.is_running() or st.process is not None)

    def test_failed_load_fires_no_hook(self):
        self.assertEqual(
            self._run_load(_fake_state(), None, healthy=False, expect_fail=True), [])


def _boom():
    raise RuntimeError("hook is broken")


class OwnerResetTests(unittest.TestCase):
    def setUp(self):
        _restore_hooks()

    def tearDown(self):
        _restore_hooks()
        cbm = __import__("core.context_budget", fromlist=["x"])
        cbm.reset(None)
        cbm.reset_tps(None)

    def test_context_budget_hook_resets_window_and_tps(self):
        import core.context_budget as cbm
        cb = _seed_local_state()
        self.assertIn("main", cb._STATE)
        self.assertIn("main", cb._TPS_STATE)
        _restore_hooks()                       # the baseline HAS the canonical hook
        self.assertIn(cbm._on_model_swap, state_mod._SWAP_HOOKS)
        state_mod.notify_swap()
        self.assertNotIn("main", cb._STATE)
        self.assertNotIn("main", cb._TPS_STATE)

    def test_llm_stream_hook_purges_local_key_only(self):
        import routes.common.llm_stream as ls
        self.assertIn(ls._purge_local_tool_choice_verdicts, state_mod._SWAP_HOOKS)
        seed = {
            "local:http://127.0.0.1:8090",
            "local:other-host:1234",
            "cloud:openai/gpt-4o-mini",
        }
        ls._TOOL_CHOICE_UNSUPPORTED.clear()
        ls._TOOL_CHOICE_UNSUPPORTED.update(seed)
        try:
            state_mod.notify_swap()
            self.assertEqual(ls._TOOL_CHOICE_UNSUPPORTED,
                             {"cloud:openai/gpt-4o-mini"})
        finally:
            ls._TOOL_CHOICE_UNSUPPORTED.clear()
            _restore_hooks()


if __name__ == "__main__":
    unittest.main()
