"""'Always allow for me' is saved, audited and reported; a command that can never be remembered is
not offered as rememberable."""
import asyncio
import types
import unittest
from unittest import mock

from routes.agent import permissions as P
from routes.agent.models import PermissionAnswerReq


def run(coro):
    return asyncio.run(coro)


def _pending(cmd, uid=7):
    ev = asyncio.Event()
    P._perm_pending["r1"] = {"cmd": cmd, "event": ev, "result": None, "user_id": uid}
    return ev


class CanSaveTests(unittest.TestCase):
    def test_plain_command_can_be_remembered(self):
        self.assertTrue(P.can_save_pattern("npm run dev"))
        self.assertTrue(P.can_save_pattern("git status"))

    def test_chained_or_risky_commands_cannot(self):
        for c in ("cd app && npm run dev", "dir | findstr x", "echo hi > out.txt", "git log; whoami", "", "   "):
            self.assertFalse(P.can_save_pattern(c), c)


class AnswerTests(unittest.TestCase):
    def setUp(self):
        self.user = types.SimpleNamespace(id=7, is_super_admin=False, permission_keys=set())
        self.addCleanup(P._perm_pending.clear)

    def test_allow_for_me_is_stored_audited_and_reported(self):
        async def go():
            _pending("npm run dev")
            with mock.patch.object(P.auth_db, "add_user_allow_pattern") as add, mock.patch.object(P, "audit_log") as aud:
                out = await P.agent_permission_answer(
                    PermissionAnswerReq(req_id="r1", decision="user", pattern="npm *"), self.user)
                return out, add, aud
        out, add, aud = run(go())
        add.assert_called_once_with(7, "npm *")
        self.assertTrue(out["saved"])
        self.assertEqual(aud.call_args.kwargs["detail"], {"scope": "user"})

    def test_allow_once_saves_nothing(self):
        async def go():
            _pending("npm run dev")
            with mock.patch.object(P.auth_db, "add_user_allow_pattern") as add:
                out = await P.agent_permission_answer(PermissionAnswerReq(req_id="r1", decision="allow"), self.user)
                return out, add
        out, add = run(go())
        add.assert_not_called()
        self.assertFalse(out["saved"])

    def test_pattern_must_match_the_shown_command(self):
        async def go():
            _pending("npm run dev")
            return await P.agent_permission_answer(
                PermissionAnswerReq(req_id="r1", decision="user", pattern="*"), self.user)
        self.assertEqual(run(go()).status_code, 400)

    def test_other_users_cannot_answer(self):
        async def go():
            _pending("npm run dev", uid=99)
            return await P.agent_permission_answer(
                PermissionAnswerReq(req_id="r1", decision="user", pattern="npm *"), self.user)
        self.assertEqual(run(go()).status_code, 404)

    def test_list_and_forget(self):
        async def go():
            with mock.patch.object(P.auth_db, "get_user_allow_patterns", return_value=["npm *"]), \
                 mock.patch.object(P.auth_db, "remove_user_allow_pattern") as rm, mock.patch.object(P, "audit_log"):
                listed = await P.my_allowed_commands(self.user)
                await P.forget_allowed_command("npm *", self.user)
                return listed, rm
        listed, rm = run(go())
        self.assertEqual(listed, {"patterns": ["npm *"]})
        rm.assert_called_once_with(7, "npm *")


if __name__ == "__main__":
    unittest.main()
