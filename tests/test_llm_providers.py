import unittest

from tradingagents.llm_providers import (
    MODEL_OPTIONS,
    PROVIDER_BASE_URLS,
    build_graph_config,
    is_openai_compatible_provider,
)


class LLMProviderConfigTest(unittest.TestCase):
    def test_deepseek_defaults_to_official_endpoint_and_models(self):
        self.assertEqual(PROVIDER_BASE_URLS["deepseek"], "https://api.deepseek.com")

        deepseek_models = MODEL_OPTIONS["deepseek"]
        self.assertEqual(deepseek_models["shallow"][0][1], "deepseek-v4-flash")
        self.assertIn(
            ("DeepSeek V4 Pro - DeepSeek flagship model", "deepseek-v4-pro"),
            deepseek_models["deep"],
        )

    def test_deepseek_uses_openai_compatible_client_path(self):
        self.assertTrue(is_openai_compatible_provider("deepseek"))
        self.assertTrue(is_openai_compatible_provider("DeepSeek"))

    def test_web_model_fields_are_mapped_to_graph_config_keys(self):
        base_config = {
            "llm_provider": "openai",
            "backend_url": "https://api.openai.com/v1",
            "api_key": "",
            "quick_think_llm": "gpt-4o-mini",
            "deep_think_llm": "gpt-4o",
            "max_debate_rounds": 1,
        }
        form_config = {
            "llm_provider": "deepseek",
            "backend_url": "https://api.deepseek.com",
            "api_key": "ds-test-key",
            "shallow_thinker": "deepseek-v4-flash",
            "deep_thinker": "deepseek-v4-pro",
            "research_depth": 3,
        }

        graph_config = build_graph_config(base_config, form_config, "session-123")

        self.assertEqual(graph_config["llm_provider"], "deepseek")
        self.assertEqual(graph_config["backend_url"], "https://api.deepseek.com")
        self.assertEqual(graph_config["api_key"], "ds-test-key")
        self.assertEqual(graph_config["quick_think_llm"], "deepseek-v4-flash")
        self.assertEqual(graph_config["deep_think_llm"], "deepseek-v4-pro")
        self.assertEqual(graph_config["max_debate_rounds"], 3)
        self.assertEqual(graph_config["session_id"], "session-123")


if __name__ == "__main__":
    unittest.main()
