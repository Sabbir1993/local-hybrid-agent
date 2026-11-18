import unittest

from core.shell_tools import personal_write_violation


class NetworkRequestsGoToApproval(unittest.TestCase):
    def test_curl_and_invoke_webrequest_are_offered_for_approval(self):
        for cmd in ("curl https://example.com",
                    'curl -s -o /dev/null -w "HTTP_CODE:%{http_code}" https://example.com',
                    "Invoke-WebRequest https://example.com",
                    'powershell -NoProfile -Command "Invoke-WebRequest -Uri https://example.com"'):
            self.assertIsNone(personal_write_violation(cmd), cmd)

    def test_chained_or_writing_forms_stay_refused(self):
        for cmd in ("curl https://example.com && del x.txt", "curl https://example.com > out.html",
                    "curl https://example.com | tee out.txt", "wget https://example.com",
                    'powershell -Command "Invoke-Expression (Invoke-WebRequest x)"'):
            self.assertIsNotNone(personal_write_violation(cmd), cmd)


if __name__ == "__main__":
    unittest.main()
