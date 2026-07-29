"""Fail-closed historical USD price references for Hermes paper reviews."""

import math
import os
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from typing import Literal, Protocol

import requests

from tradingagents.dataflows.coingecko_utils import get_crypto_historical_usd_price


PriceSource = Literal["coingecko", "cryptocompare", "coinbase"]
_SOURCES = frozenset(("coingecko", "cryptocompare", "coinbase"))
_CRYPTOCOMPARE_URL = "https://min-api.cryptocompare.com/data/v2/histoday"
_COINBASE_URL = "https://api.exchange.coinbase.com/products"
_PLACEHOLDER_KEYS = frozenset(
    (
        "",
        "your_key",
        "your_api_key",
        "your_cryptocompare_key",
        "cryptocompare_api_key",
    )
)


class HistoricalPriceUnavailable(ValueError):
    """Raised when a provider cannot supply an exact historical USD reference."""


@dataclass(frozen=True)
class HistoricalUsdReference:
    """One exact UTC-day USD reference with its unambiguous source."""

    date: date
    usd_price: float
    source: PriceSource


class HistoricalUsdReferenceProvider(Protocol):
    """Provider contract used by the same-source resolver."""

    def references(
        self, symbol: str, dates: Sequence[date]
    ) -> list[HistoricalUsdReference]: ...


def _clean_api_key(value: str | None) -> str | None:
    cleaned = (value or "").strip().strip('"').strip("'")
    return None if cleaned.lower() in _PLACEHOLDER_KEYS else cleaned


def _utc_day_end(day: date) -> int:
    return int(datetime.combine(day, time.max, tzinfo=timezone.utc).timestamp())


def _valid_price(value: object) -> float:
    try:
        price = float(value)
    except (TypeError, ValueError) as error:
        raise HistoricalPriceUnavailable("historical USD price is invalid") from error
    if not math.isfinite(price) or price <= 0:
        raise HistoricalPriceUnavailable("historical USD price is invalid")
    return price


class CoinGeckoHistoricalUsdProvider:
    """Adapt the existing CoinGecko helper to the reference-provider contract."""

    def references(
        self, symbol: str, dates: Sequence[date]
    ) -> list[HistoricalUsdReference]:
        try:
            return [
                HistoricalUsdReference(
                    day, _valid_price(get_crypto_historical_usd_price(symbol, day)), "coingecko"
                )
                for day in dates
            ]
        except (OSError, ValueError, requests.RequestException) as error:
            raise HistoricalPriceUnavailable("CoinGecko reference is unavailable") from error


class CryptoCompareHistoricalUsdProvider:
    """Read exact daily USD close references from CryptoCompare."""

    def __init__(self, api_key: str, session: requests.Session | None = None):
        self.api_key = api_key
        self.session = session or requests.Session()

    def references(
        self, symbol: str, dates: Sequence[date]
    ) -> list[HistoricalUsdReference]:
        values = []
        for day in dates:
            try:
                response = self.session.get(
                    _CRYPTOCOMPARE_URL,
                    params={
                        "fsym": symbol.upper(),
                        "tsym": "USD",
                        "limit": 1,
                        "toTs": _utc_day_end(day),
                    },
                    headers={"authorization": f"Apikey {self.api_key}"},
                    timeout=20,
                )
                response.raise_for_status()
                candles = response.json()["Data"]["Data"]
                candle = next(
                    item
                    for item in candles
                    if datetime.fromtimestamp(item["time"], timezone.utc).date() == day
                )
                values.append(
                    HistoricalUsdReference(day, _valid_price(candle["close"]), "cryptocompare")
                )
            except (
                KeyError,
                StopIteration,
                TypeError,
                ValueError,
                requests.RequestException,
            ) as error:
                raise HistoricalPriceUnavailable(
                    "CryptoCompare reference is unavailable"
                ) from error
        return values


class CoinbaseHistoricalUsdProvider:
    """Read public exact daily close references from direct Coinbase USD markets."""

    def __init__(self, session: requests.Session | None = None):
        self.session = session or requests.Session()

    def references(
        self, symbol: str, dates: Sequence[date]
    ) -> list[HistoricalUsdReference]:
        values = []
        for day in dates:
            start = datetime.combine(day, time.min, tzinfo=timezone.utc)
            end = start + timedelta(days=1)
            try:
                response = self.session.get(
                    f"{_COINBASE_URL}/{symbol.upper()}-USD/candles",
                    params={
                        "start": start.isoformat().replace("+00:00", "Z"),
                        "end": end.isoformat().replace("+00:00", "Z"),
                        "granularity": 86400,
                    },
                    timeout=20,
                )
                response.raise_for_status()
                candle = next(
                    item
                    for item in response.json()
                    if datetime.fromtimestamp(item[0], timezone.utc).date() == day
                )
                values.append(
                    HistoricalUsdReference(day, _valid_price(candle[4]), "coinbase")
                )
            except (
                IndexError,
                StopIteration,
                TypeError,
                ValueError,
                requests.RequestException,
            ) as error:
                raise HistoricalPriceUnavailable("Coinbase reference is unavailable") from error
        return values


def configured_providers() -> list[HistoricalUsdReferenceProvider]:
    """Return the fixed provider order without exposing any configured key."""
    providers: list[HistoricalUsdReferenceProvider] = [CoinGeckoHistoricalUsdProvider()]
    crypto_compare_key = _clean_api_key(os.getenv("CRYPTOCOMPARE_API_KEY"))
    if crypto_compare_key:
        providers.append(CryptoCompareHistoricalUsdProvider(crypto_compare_key))
    providers.append(CoinbaseHistoricalUsdProvider())
    return providers


def _is_complete_single_source(
    values: list[HistoricalUsdReference], requested_dates: list[date]
) -> bool:
    if len(values) != len(requested_dates) or {item.date for item in values} != set(
        requested_dates
    ):
        return False
    if len({item.source for item in values}) != 1 or any(
        item.source not in _SOURCES for item in values
    ):
        return False
    try:
        for item in values:
            _valid_price(item.usd_price)
    except HistoricalPriceUnavailable:
        return False
    return True


def resolve_historical_usd_references(
    symbol: str,
    dates: Sequence[date],
    providers: Sequence[HistoricalUsdReferenceProvider] | None = None,
) -> list[HistoricalUsdReference]:
    """Resolve every requested date from one provider or fail closed."""
    requested_dates = list(dates)
    if not requested_dates or len(set(requested_dates)) != len(requested_dates):
        raise HistoricalPriceUnavailable("historical reference dates are invalid")

    for provider in providers if providers is not None else configured_providers():
        try:
            values = provider.references(symbol, requested_dates)
        except HistoricalPriceUnavailable:
            continue
        if _is_complete_single_source(values, requested_dates):
            by_date = {item.date: item for item in values}
            return [by_date[day] for day in requested_dates]

    raise HistoricalPriceUnavailable("historical USD references are unavailable")
