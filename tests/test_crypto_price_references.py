import os
import unittest
from datetime import date, datetime, timezone
from unittest.mock import Mock, patch

from tradingagents.dataflows.crypto_price_references import (
    CoinbaseHistoricalUsdProvider,
    CryptoCompareHistoricalUsdProvider,
    configured_providers,
    HistoricalPriceUnavailable,
    HistoricalUsdReference,
    resolve_historical_usd_references,
)


ENTRY_DATE = date(2026, 7, 28)
REVIEW_DATE = date(2026, 7, 29)


class FailingProvider:
    def references(self, _symbol, _dates):
        raise HistoricalPriceUnavailable("unavailable")


class StaticProvider:
    def __init__(self, source, values):
        self.source = source
        self.values = values
        self.calls = []

    def references(self, symbol, dates):
        self.calls.append((symbol, tuple(dates)))
        return [
            HistoricalUsdReference(day, self.values[day], self.source)
            for day in dates
            if day in self.values
        ]


class FakeResponse:
    def __init__(self, payload):
        self.payload = payload

    def raise_for_status(self):
        pass

    def json(self):
        return self.payload


class CryptoPriceReferenceTests(unittest.TestCase):
    def test_chain_uses_one_fallback_for_both_dates_after_primary_failure(self):
        fallback = StaticProvider(
            "cryptocompare", {ENTRY_DATE: 100.0, REVIEW_DATE: 110.0}
        )

        references = resolve_historical_usd_references(
            "BTC", [ENTRY_DATE, REVIEW_DATE], providers=[FailingProvider(), fallback]
        )

        self.assertEqual([item.source for item in references], ["cryptocompare"] * 2)
        self.assertEqual([item.usd_price for item in references], [100.0, 110.0])
        self.assertEqual(fallback.calls, [("BTC", (ENTRY_DATE, REVIEW_DATE))])

    def test_chain_rejects_a_provider_that_cannot_resolve_every_date(self):
        partial = StaticProvider("coinbase", {ENTRY_DATE: 100.0})

        with self.assertRaises(HistoricalPriceUnavailable):
            resolve_historical_usd_references(
                "BTC", [ENTRY_DATE, REVIEW_DATE], providers=[partial]
            )

    def test_chain_rejects_a_provider_that_returns_mixed_sources(self):
        class MixedProvider:
            def references(self, _symbol, _dates):
                return [
                    HistoricalUsdReference(ENTRY_DATE, 100.0, "coingecko"),
                    HistoricalUsdReference(REVIEW_DATE, 110.0, "coinbase"),
                ]

        with self.assertRaises(HistoricalPriceUnavailable):
            resolve_historical_usd_references(
                "BTC", [ENTRY_DATE, REVIEW_DATE], providers=[MixedProvider()]
            )

    def test_cryptocompare_uses_exact_utc_day_close_without_exposing_key(self):
        session = Mock()
        session.get.return_value = FakeResponse(
            {
                "Data": {
                    "Data": [
                        {
                            "time": int(
                                datetime(2026, 7, 28, tzinfo=timezone.utc).timestamp()
                            ),
                            "close": "101.25",
                        }
                    ]
                }
            }
        )

        values = CryptoCompareHistoricalUsdProvider("private-key", session).references(
            "btc", [ENTRY_DATE]
        )

        self.assertEqual(
            values,
            [HistoricalUsdReference(ENTRY_DATE, 101.25, "cryptocompare")],
        )
        request = session.get.call_args
        self.assertEqual(request.args[0], "https://min-api.cryptocompare.com/data/v2/histoday")
        self.assertEqual(request.kwargs["params"]["fsym"], "BTC")
        self.assertEqual(request.kwargs["params"]["tsym"], "USD")
        self.assertEqual(request.kwargs["params"]["limit"], 1)
        self.assertEqual(request.kwargs["timeout"], 20)
        self.assertEqual(request.kwargs["headers"], {"authorization": "Apikey private-key"})

    def test_cryptocompare_rejects_wrong_day_or_non_positive_close(self):
        session = Mock()
        session.get.return_value = FakeResponse(
            {
                "Data": {
                    "Data": [
                        {
                            "time": int(
                                datetime(2026, 7, 27, tzinfo=timezone.utc).timestamp()
                            ),
                            "close": 0,
                        }
                    ]
                }
            }
        )

        with self.assertRaises(HistoricalPriceUnavailable):
            CryptoCompareHistoricalUsdProvider("private-key", session).references(
                "BTC", [ENTRY_DATE]
            )

    def test_coinbase_uses_exact_utc_day_close(self):
        session = Mock()
        session.get.return_value = FakeResponse(
            [[int(datetime(2026, 7, 28, tzinfo=timezone.utc).timestamp()), 99, 102, 98, 101.5, 12]]
        )

        values = CoinbaseHistoricalUsdProvider(session).references("btc", [ENTRY_DATE])

        self.assertEqual(values, [HistoricalUsdReference(ENTRY_DATE, 101.5, "coinbase")])
        request = session.get.call_args
        self.assertEqual(
            request.args[0],
            "https://api.exchange.coinbase.com/products/BTC-USD/candles",
        )
        self.assertEqual(request.kwargs["params"]["granularity"], 86400)
        self.assertEqual(request.kwargs["params"]["start"], "2026-07-28T00:00:00Z")
        self.assertEqual(request.kwargs["params"]["end"], "2026-07-29T00:00:00Z")
        self.assertEqual(request.kwargs["timeout"], 20)

    def test_coinbase_rejects_wrong_day_or_non_positive_close(self):
        session = Mock()
        session.get.return_value = FakeResponse(
            [[int(datetime(2026, 7, 27, tzinfo=timezone.utc).timestamp()), 99, 102, 98, 0, 12]]
        )

        with self.assertRaises(HistoricalPriceUnavailable):
            CoinbaseHistoricalUsdProvider(session).references("BTC", [ENTRY_DATE])

    def test_configured_providers_omits_cryptocompare_without_a_real_key(self):
        with patch.dict(os.environ, {"CRYPTOCOMPARE_API_KEY": "your_api_key"}):
            providers = configured_providers()

        self.assertNotIn(
            "CryptoCompareHistoricalUsdProvider",
            {type(provider).__name__ for provider in providers},
        )


if __name__ == "__main__":
    unittest.main()
