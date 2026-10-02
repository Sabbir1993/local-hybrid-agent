"""tests/test_secret_floor.py - the deterministic floor under the semantic guard rules.

config/app.json shipped one input_guard rule of type "semantic": a prompt asking a 4B local
model for one YES/NO token. Fail-open, prompt-injectable, and role-scoped to users so admins
were exempt. core/secrets.py is the deterministic replacement underneath it.

The false-positive half matters as much as the true-positive half: a guard that fires on
"the secret to a good life" trains people to disable it.

Run: python -m unittest tests.test_secret_floor -v
"""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core import secrets


class HighTierBlocksRealCredentials(unittest.TestCase):
    CASES = {
        "private key": "-----BEGIN RSA PRIVATE KEY-----\nMIIEowIBAAKCAQEA\n-----END RSA PRIVATE KEY-----",
        "openssh key": "-----BEGIN OPENSSH PRIVATE KEY-----\nb3BlbnNza\n-----END OPENSSH PRIVATE KEY-----",
        "aws key id": "aws_key = AKIAIOSFODNN7EXAMPLE",
        "aws secret": "AWS_SECRET_ACCESS_KEY=wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY",
        "github pat": "token: ghp_16C7e42F292c6912E7710c838347Ae178B4a",
        "github oauth": "GITHUB_TOKEN=gho_16C7e42F292c6912E7710c838347Ae178B4a",
        "gitlab pat": "glpat-ABC123def456GHI789jkl012",
        "slack": "SLACK_BOT_TOKEN=xoxb-123456789012-abcdefghijklmnop",
        "stripe live": "STRIPE_KEY=sk_live_4eC39HqLyjWDarjtT1zdp7dc",
        "google api": "GOOGLE=AIzaSyD-1234567890abcdefghijklmnopqrstu",
        "openai-style": "OPENAI_API_KEY=sk-proj-abcdefghij1234567890ABCDEFGHIJ",
        "jwt": "auth: eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.dozjgNryP4J3jVmNHl0w5N",
        "url creds": "psql postgres://admin:s3cr3tpass@10.0.0.5:5432/prod",
    }

    def test_each_is_blocked_on_a_local_lane(self):
        for label, text in self.CASES.items():
            with self.subTest(label):
                self.assertIsNotNone(secrets.scan(text, any_cloud_lane=False),
                                     f"{label} must be blocked even on a local lane")

    def test_each_is_blocked_on_a_cloud_lane(self):
        for label, text in self.CASES.items():
            with self.subTest(label):
                self.assertIsNotNone(secrets.scan(text, any_cloud_lane=True))


class NoFalsePositives(unittest.TestCase):
    CASES = [
        "the secret to a good life is a good life",
        "what is the token limit for this model",
        "password = 'x'",
        "api_key = <your-key-here>",
        "secret: ${ENV_VAR}",
        "set AWS_SECRET_ACCESS_KEY=your_secret_here",
        "token = None",
        "my password is hunter2",
        "the API key rotation policy is documented in the wiki",
        "how do I rotate my credentials",
        "private_key_pem_path is a variable name, not a value",
        "please explain what a secret key is",
        "-----BEGIN CERTIFICATE-----\nMIIDdzCCAl+g\n-----END CERTIFICATE-----",   # certs are public
        "git commit -m 'add credentials helper'",
        "the credential store is locked",
    ]

    def test_prose_and_placeholders_pass(self):
        for text in self.CASES:
            with self.subTest(text[:40]):
                self.assertIsNone(secrets.scan(text, any_cloud_lane=False),
                                 f"false positive on {text[:50]!r}")


class LooseTierIsCloudOnly(unittest.TestCase):
    PAYLOAD = 'database_password = "hunter2isverylong"'

    def test_generic_assignment_allowed_locally(self):
        self.assertIsNone(secrets.scan(self.PAYLOAD, any_cloud_lane=False))

    def test_generic_assignment_blocked_on_cloud(self):
        self.assertIsNotNone(secrets.scan(self.PAYLOAD, any_cloud_lane=True))

    def test_prefixed_and_uppercase_key_names_are_caught(self):
        for text in ('AWS_SECRET_ACCESS_KEY=abcdefghijklmnop123',
                     'db_password: "hunter2isverylong"',
                     'CLIENT-SECRET = "aaaaaaaaaaaaaabbbb"'):
            with self.subTest(text[:30]):
                self.assertIsNotNone(secrets.scan(text, any_cloud_lane=True))


class WiredIntoTheInputGuard(unittest.TestCase):
    """The floor must run in the guard, ahead of the admin rule set, exactly like the PCI/PAN
    rule does - otherwise it is code nobody calls."""

    def _hit(self, text, cloud=True):
        from unittest import mock
        from core.input_guard import evaluator
        with mock.patch.object(secrets, "high_mode", return_value="block"), \
             mock.patch.object(secrets, "loose_mode", return_value="cloud_only"):
            return evaluator.check([text], user=None, any_cloud_lane=cloud)

    def test_blocks_a_private_key(self):
        hit = self._hit("-----BEGIN RSA PRIVATE KEY-----\nMIIEow==\n-----END RSA PRIVATE KEY-----")
        self.assertIsNotNone(hit)
        self.assertIn("builtin:secret", hit["_matched_pattern"])
        self.assertEqual(hit["scope"], "block_all")

    def test_blocks_a_pat_everywhere(self):
        """A GitHub PAT is HIGH tier: unambiguous, so it is block_all rather than cloud_only.
        'Local does not mean private' - it lands in KV cache, tool logs and written files."""
        hit = self._hit("ghp_16C7e42F292c6912E7710c838347Ae178B4a", cloud=True)
        self.assertIsNotNone(hit)
        self.assertEqual(hit["scope"], "block_all")

    def test_generic_assignment_is_cloud_scoped(self):
        hit = self._hit('database_password = "hunter2isverylong"', cloud=True)
        self.assertIsNotNone(hit)
        self.assertEqual(hit["scope"], "cloud_only")

    def test_generic_assignment_passes_locally(self):
        self.assertIsNone(self._hit('database_password = "hunter2isverylong"', cloud=False))

    def test_does_not_fire_on_ordinary_text(self):
        self.assertIsNone(self._hit("refactor the agent loop into smaller modules"))

    def test_runs_even_when_the_admin_rule_set_is_disabled(self):
        from unittest import mock
        from core.input_guard import evaluator
        with mock.patch.object(secrets, "high_mode", return_value="block"), \
             mock.patch.object(evaluator, "enabled", return_value=False):
            hit = evaluator.check(["AKIAIOSFODNN7EXAMPLE"], user=None, any_cloud_lane=False)
        self.assertIsNotNone(hit, "built-in floor must not be switchable from the admin UI")


if __name__ == "__main__":
    unittest.main()