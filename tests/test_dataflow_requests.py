import unittest
import importlib.util
import sys
from pathlib import Path
from unittest.mock import patch

import requests
from datetime import datetime


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def load_module(module_name, relative_path):
    spec = importlib.util.spec_from_file_location(module_name, PROJECT_ROOT / relative_path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


class FakeResponse:
    def __init__(self, status_code=200, payload=None, content=b"", text=""):
        self.status_code = status_code
        self._payload = payload or {}
        self.content = content
        self.text = text
        self.headers = {}

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.exceptions.HTTPError(f"HTTP {self.status_code}")

    def json(self):
        return self._payload


class DataflowRequestTest(unittest.TestCase):
    def test_google_news_uses_rss_endpoint_and_parses_items(self):
        module = load_module("googlenews_utils_under_test", "tradingagents/dataflows/googlenews_utils.py")

        rss = b"""<?xml version="1.0" encoding="UTF-8"?>
        <rss version="2.0">
          <channel>
            <item>
              <title>Bitcoin liquidity improves</title>
              <link>https://example.com/btc</link>
              <description>&lt;p&gt;Market makers added depth.&lt;/p&gt;</description>
              <pubDate>Fri, 24 Jul 2026 08:00:00 GMT</pubDate>
              <source url="https://example.com">Example News</source>
            </item>
          </channel>
        </rss>"""

        with patch.object(module.requests, "get") as mock_get:
            mock_get.return_value = FakeResponse(content=rss)

            results = module.getNewsData("Bitcoin BTC", "2026-07-17", "2026-07-24")

        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["title"], "Bitcoin liquidity improves")
        self.assertEqual(results[0]["link"], "https://example.com/btc")
        self.assertEqual(results[0]["snippet"], "Market makers added depth.")
        self.assertEqual(results[0]["date"], "Fri, 24 Jul 2026 08:00:00 GMT")
        self.assertEqual(results[0]["source"], "Example News")

        request_url = mock_get.call_args.args[0]
        request_kwargs = mock_get.call_args.kwargs
        self.assertEqual(request_url, "https://news.google.com/rss/search")
        self.assertIn("after:2026-07-17", request_kwargs["params"]["q"])
        self.assertIn("before:2026-07-24", request_kwargs["params"]["q"])
        self.assertEqual(request_kwargs["timeout"], 20)

    def test_coingecko_request_retries_transient_ssl_errors(self):
        module = load_module("coingecko_utils_under_test", "tradingagents/dataflows/coingecko_utils.py")

        api = module.CoinGeckoAPI()
        ssl_error = requests.exceptions.SSLError("EOF occurred in violation of protocol")
        success = FakeResponse(payload={"gecko_says": "(V3) To the Moon!"})

        with patch.object(module.time, "sleep") as mock_sleep:
            with patch("requests.sessions.Session.get", side_effect=[ssl_error, success]) as mock_get:
                result = api._make_request("/ping")

        self.assertEqual(result, {"gecko_says": "(V3) To the Moon!"})
        self.assertEqual(mock_get.call_count, 2)
        self.assertEqual(mock_get.call_args.kwargs["timeout"], 20)
        mock_sleep.assert_called_once()

    def test_coingecko_legacy_api_key_uses_demo_header(self):
        module = load_module("coingecko_utils_demo_key_under_test", "tradingagents/dataflows/coingecko_utils.py")

        with patch.dict(module.os.environ, {"COINGECKO_API_KEY": "demo-key"}, clear=True):
            api = module.CoinGeckoAPI()

        self.assertEqual(api.base_url, "https://api.coingecko.com/api/v3")
        self.assertEqual(api.session.headers["x-cg-demo-api-key"], "demo-key")
        self.assertNotIn("X-Cg-Pro-Api-Key", api.session.headers)

    def test_coingecko_pro_api_key_uses_pro_endpoint_and_header(self):
        module = load_module("coingecko_utils_pro_key_under_test", "tradingagents/dataflows/coingecko_utils.py")

        with patch.dict(module.os.environ, {"COINGECKO_PRO_API_KEY": "pro-key"}, clear=True):
            api = module.CoinGeckoAPI()

        self.assertEqual(api.base_url, "https://pro-api.coingecko.com/api/v3")
        self.assertEqual(api.session.headers["x-cg-pro-api-key"], "pro-key")
        self.assertNotIn("x-cg-demo-api-key", api.session.headers)

    def test_coingecko_placeholder_key_is_ignored(self):
        module = load_module("coingecko_utils_placeholder_key_under_test", "tradingagents/dataflows/coingecko_utils.py")

        with patch.dict(module.os.environ, {"COINGECKO_API_KEY": "your_key"}, clear=True):
            api = module.CoinGeckoAPI()

        self.assertEqual(api.base_url, "https://api.coingecko.com/api/v3")
        self.assertNotIn("x-cg-demo-api-key", api.session.headers)
        self.assertNotIn("x-cg-pro-api-key", api.session.headers)

    def test_demo_price_range_is_capped_below_public_api_limit(self):
        module = load_module("coingecko_utils_range_limit_under_test", "tradingagents/dataflows/coingecko_utils.py")
        captured_params = {}

        def fake_make_request(self, endpoint, params=None):
            captured_params.update(params or {})
            return {
                "prices": [[1784822400000, 100000]],
                "total_volumes": [[1784822400000, 1000]],
                "market_caps": [[1784822400000, 1000000]],
            }

        with patch.object(module.CoinGeckoAPI, "_make_request", fake_make_request):
            result = module.get_crypto_price_data("BTC", "2025-07-23", "2026-07-24")

        from_date = datetime.fromtimestamp(captured_params["from"]).strftime("%Y-%m-%d")
        to_date = datetime.fromtimestamp(captured_params["to"]).strftime("%Y-%m-%d")
        self.assertEqual(from_date, "2025-07-25")
        self.assertEqual(to_date, "2026-07-24")
        self.assertIn("adjusted to 2025-07-25", result)


if __name__ == "__main__":
    unittest.main()
