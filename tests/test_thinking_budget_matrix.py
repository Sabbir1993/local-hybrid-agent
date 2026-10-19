import unittest
from routes.capabilities.models import WebSettingsReq, ThinkingBudgetRow
from core.small_model import APP_CONFIG


class TestThinkingBudgetMatrix(unittest.TestCase):
    def test_pydantic_model_thinking_budgets(self):
        req = WebSettingsReq(
            soft_chars=20000,
            thinking_budgets={
                "none": ThinkingBudgetRow(tool_calls=0, token_budget=6000, max_rounds=0),
                "low": ThinkingBudgetRow(tool_calls=4, token_budget=8000, max_rounds=4),
                "medium": ThinkingBudgetRow(tool_calls=8, token_budget=12000, max_rounds=12),
                "high": ThinkingBudgetRow(tool_calls=16, token_budget=16000, max_rounds=20),
                "extra": ThinkingBudgetRow(tool_calls=25, token_budget=20000, max_rounds=30),
                "deep": ThinkingBudgetRow(tool_calls=35, token_budget=25000, max_rounds=40),
            }
        )
        self.assertEqual(req.thinking_budgets["none"].tool_calls, 0)
        self.assertEqual(req.thinking_budgets["none"].token_budget, 6000)
        self.assertEqual(req.thinking_budgets["none"].max_rounds, 0)
        self.assertEqual(req.thinking_budgets["extra"].tool_calls, 25)
        self.assertEqual(req.thinking_budgets["extra"].token_budget, 20000)

    def test_app_config_thinking_budgets_defaults(self):
        chat_cfg = APP_CONFIG.get("chat", {})
        tb = chat_cfg.get("thinking_budgets", {})
        self.assertIn("none", tb)
        self.assertEqual(tb["none"]["tool_calls"], 0)
        self.assertEqual(tb["none"]["token_budget"], 6000)
        self.assertEqual(tb["none"]["max_rounds"], 0)
        self.assertIn("extra", tb)
        self.assertEqual(tb["extra"]["tool_calls"], 25)
        self.assertEqual(tb["extra"]["token_budget"], 20000)


class TestStatusWebSocketImport(unittest.TestCase):
    def test_status_ws_router_defined(self):
        from routes.status_ws import router, status_websocket
        self.assertIsNotNone(router)
        self.assertTrue(callable(status_websocket))


if __name__ == "__main__":
    unittest.main()
