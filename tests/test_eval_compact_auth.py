"""tests/test_eval_compact_auth.py - the eval gate must test the PROJECT GATE, not auth.

RED for T1 (TIER1_EXECUTION_PLAN.md Gap 3): `compact_endpoint` in
tests/eval_agent.py posts unauthenticated, so FastAPI answers 401 at dependency
resolution and the check never reaches the project-gate assertion it exists to
make. The check was permanently red in the offline suite.

Two directions are pinned here, and the second is the important one:
  * with the eval principal injected, /chat/compact must reach the project gate
    (400 + a message naming the project requirement)
  * WITHOUT the injection it must still answer 401

Without the second half, "fixing" the gate could be achieved by removing the
auth requirement from the route - which would turn a red test into a green lie
and open exactly the hole test_auth_invariant.py exists to close.
"""
import unittest
import warnings


class CompactEndpointAuthTests(unittest.TestCase):
    def _client(self, with_principal: bool):
        from fastapi import FastAPI
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")      # httpx2 deprecation notice
            from starlette.testclient import TestClient
        from routes import chat as chat_routes
        from core import deps

        app = FastAPI()
        if with_principal:
            app.dependency_overrides[deps.get_current_user] = eval_principal
        app.include_router(chat_routes.router)
        return TestClient(app)

    def _post_agent_mode(self, client):
        return client.post("/chat/compact", json={
            "messages": [{"role": "user", "content": "a"},
                         {"role": "assistant", "content": "b"}],
            "agent_mode": True})

    def test_project_gate_is_reached_when_a_principal_is_injected(self):
        r = self._post_agent_mode(self._client(True))
        self.assertEqual(r.status_code, 400, f"expected the project gate, got {r.status_code}")
        self.assertIn("project", r.json().get("error", "").lower())

    def test_unauthenticated_request_is_still_refused(self):
        # the negative control: injecting a principal into the eval harness must
        # not make the endpoint itself unauthenticated
        r = self._post_agent_mode(self._client(False))
        self.assertEqual(r.status_code, 401, f"unauthenticated call returned {r.status_code}")

    def test_short_history_is_still_refused_with_a_principal(self):
        r = self._client(True).post("/chat/compact", json={"messages": [{"role": "user", "content": "hi"}]})
        self.assertEqual(r.status_code, 400)
        self.assertIn("compact", r.json().get("error", "").lower())


def eval_principal():
    """The principal the eval harness injects. Same shape as the test-suite
    convention (tests/test_agent_limits_routes.py:30) - every required field of
    core.auth.Principal, no role/permissions shortcut (those fields do not exist)."""
    from core.auth import Principal
    return Principal(id=1, username="eval", display_name="eval", is_super_admin=False,
                     must_change_password=False, role_names=["user"],
                     permission_keys={"chat.use"})


if __name__ == "__main__":
    unittest.main()