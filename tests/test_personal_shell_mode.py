"""Agent mode shell: an admin chooses between refusing commands that change files (strict) and asking the user on the
approval card (ask_suspicious). The always-forbidden list is refused in both."""
import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from core import shell_tools
from core.agent_loop import execution

SET_CONTENT = "$paths = @('/api','/api/v1'); $paths | Set-Content -Path results.txt"


def _with_mode(mode):
    return mock.patch.object(shell_tools, "shell_cfg", lambda: {"enabled": True, "personal_mode": mode})


class PersonalShellMode(unittest.TestCase):
    def test_strict_refuses_a_write(self):
        with _with_mode("strict"):
            self.assertIn("cannot run this command", execution._personal_refusal("run_shell", {"command": SET_CONTENT}))

    def test_ask_mode_sends_it_to_the_card(self):
        with _with_mode("ask_suspicious"):
            self.assertEqual(execution._personal_refusal("run_shell", {"command": SET_CONTENT}), "")
            self.assertEqual(shell_tools.personal_tier(SET_CONTENT, []), "ask")

    def test_disk_format_refused_in_both(self):
        self.assertEqual(shell_tools.personal_tier("format C:", []), "forbidden")

    def test_unknown_mode_falls_back_to_strict(self):
        with _with_mode("yolo"):
            self.assertEqual(shell_tools.personal_mode(), "strict")


class ShellSettingsEndpoint(unittest.TestCase):
    def _call(self, mode):
        from routes.capabilities.endpoints import settings as ep
        from routes.capabilities.models import ShellSettingsReq
        tmp = Path(tempfile.mkdtemp()) / "app.json"
        tmp.write_text(json.dumps({"capabilities": {"shell": {"enabled": True}}}), encoding="utf-8")
        live = {"capabilities": {"shell": {}}}
        with mock.patch.object(ep, "CONFIG_FILE", tmp), mock.patch.object(ep, "APP_CONFIG", live), \
                mock.patch.object(ep, "audit_log", lambda *a, **k: None):
            res = asyncio.run(ep.shell_settings(ShellSettingsReq(personal_mode=mode), user=object()))
        return res, json.loads(tmp.read_text(encoding="utf-8")), live

    def test_saves_and_applies_live(self):
        res, saved, live = self._call("ask_suspicious")
        self.assertEqual(saved["capabilities"]["shell"]["personal_mode"], "ask_suspicious")
        self.assertEqual(live["capabilities"]["shell"]["personal_mode"], "ask_suspicious")

    def test_rejects_other_values(self):
        res, saved, _ = self._call("allow_everything")
        self.assertEqual(getattr(res, "status_code", 200), 400)
        self.assertNotIn("personal_mode", saved["capabilities"]["shell"])


if __name__ == "__main__":
    unittest.main()
