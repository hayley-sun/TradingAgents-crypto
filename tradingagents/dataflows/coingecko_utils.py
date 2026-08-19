import requests
import json
import math
import pandas as pd
from typing import Annotated, Dict, List, Any, Optional
from datetime import date, datetime, timedelta
import time
import os

try:
    from .config import DATA_DIR
except ImportError:
    DATA_DIR = None


class CoinGeckoAPI:
    """CoinGecko API utilities for cryptocurrency data"""

    PUBLIC_BASE_URL = "https://api.coingecko.com/api/v3"
    PRO_BASE_URL = "https://pro-api.coingecko.com/api/v3"
    PUBLIC_MAX_RANGE_DAYS = 364
    PLACEHOLDER_KEYS = {
        "",
        "your_key",
        "your_api_key",
        "your_coingecko_key",
        "your_coingecko_api_key",
        "coingecko_api_key",
    }

    def __init__(self, api_key: Optional[str] = None, api_plan: Optional[str] = None):
        self.api_key, self.api_plan = self._resolve_api_credentials(api_key, api_plan)
        self.base_url = self.PRO_BASE_URL if self.api_plan == "pro" else self.PUBLIC_BASE_URL
        self.session = requests.Session()
        self.session.trust_env = False

        if self.api_key and self.api_plan == "pro":
            self.session.headers.update({"x-cg-pro-api-key": self.api_key})
        elif self.api_key:
            self.session.headers.update({"x-cg-demo-api-key": self.api_key})
        
        # Direct mapping for major cryptocurrencies to avoid API calls and ambiguity
        self.major_coin_ids = {
            'btc': 'bitcoin',
            'eth': 'ethereum',
            'ada': 'cardano',
            'sol': 'solana',
            'dot': 'polkadot',
            'avax': 'avalanche-2',
            'matic': 'matic-network',
            'link': 'chainlink',
            'uni': 'uniswap',
            'aave': 'aave',
            'xrp': 'ripple',
            'ltc': 'litecoin',
            'bch': 'bitcoin-cash',
            'eos': 'eos',
            'trx': 'tron',
            'xlm': 'stellar',
            'vet': 'vechain',
            'algo': 'algorand',
            'atom': 'cosmos',
            'near': 'near',
            'ftm': 'fantom',
            'cro': 'crypto-com-chain',
            'sand': 'the-sandbox',
            'mana': 'decentraland',
            'axs': 'axie-infinity',
            'gala': 'gala',
            'enj': 'enjincoin',
            'chz': 'chiliz',
            'bat': 'basic-attention-token',
            'zec': 'zcash',
            'dash': 'dash',
            'xmr': 'monero',
            'doge': 'dogecoin',
            'shib': 'shiba-inu',
            'bnb': 'binancecoin',
            'usdt': 'tether',
            'usdc': 'usd-coin',
            'ton': 'the-open-network',
            'icp': 'internet-computer',
            'hbar': 'hedera-hashgraph',
            'theta': 'theta-token',
            'fil': 'filecoin',
            'etc': 'ethereum-classic',
            'mkr': 'maker',
            'apt': 'aptos',
            'ldo': 'lido-dao',
            'op': 'optimism'
        }

    @classmethod
    def _clean_api_key(cls, api_key: Optional[str]) -> Optional[str]:
        cleaned = (api_key or "").strip().strip('"').strip("'")
        if cleaned.lower() in cls.PLACEHOLDER_KEYS:
            return None
        return cleaned

    @classmethod
    def _resolve_api_credentials(cls, api_key: Optional[str], api_plan: Optional[str]):
        plan = (api_plan or os.getenv("COINGECKO_API_PLAN") or "").strip().lower()

        if api_key:
            return cls._clean_api_key(api_key), plan if plan == "pro" else "demo"

        pro_key = cls._clean_api_key(os.getenv("COINGECKO_PRO_API_KEY"))
        if pro_key:
            return pro_key, "pro"

        demo_key = cls._clean_api_key(os.getenv("COINGECKO_DEMO_API_KEY"))
        if demo_key:
            return demo_key, "demo"

        legacy_key = cls._clean_api_key(os.getenv("COINGECKO_API_KEY"))
        if legacy_key:
            return legacy_key, "pro" if plan == "pro" else "demo"

        return None, "demo"
    
    def _make_request(self, endpoint: str, params: Dict = None) -> Dict:
        """Make API request with error handling and rate limiting"""
        url = f"{self.base_url}{endpoint}"
        last_error = None

        for attempt in range(3):
            try:
                response = self.session.get(url, params=params, timeout=20)
                if response.status_code == 429 and attempt < 2:
                    print("Rate limit exceeded. Please wait before making more requests.")
                    time.sleep(2 ** attempt)
                    continue
                response.raise_for_status()
                return response.json()
            except (
                requests.exceptions.SSLError,
                requests.exceptions.ConnectionError,
                requests.exceptions.Timeout,
            ) as e:
                last_error = e
                if attempt < 2:
                    time.sleep(2 ** attempt)
                    continue
            except requests.exceptions.RequestException as e:
                print(f"Error making request to {url}: {e}")
                return {}

        print(f"Error making request to {url}: {last_error}")
        return {}
    
    def get_coin_id(self, symbol: str) -> Optional[str]:
        """Get CoinGecko coin ID from symbol, prioritizing major cryptocurrencies"""
        symbol_lower = symbol.lower()
        
        # First, check if it's a major cryptocurrency we know
        if symbol_lower in self.major_coin_ids:
            return self.major_coin_ids[symbol_lower]
        
        # Fallback to API call for less common coins
        try:
            coins_list = self._make_request("/coins/list")
            matches = []
            for coin in coins_list:
                if coin.get("symbol", "").lower() == symbol_lower:
                    matches.append(coin)
            
            if not matches:
                return None
            
            # If multiple matches, prefer the one with a more "standard" name
            # This helps avoid meme coins with same symbol
            if len(matches) == 1:
                return matches[0]["id"]
            
            # For multiple matches, try to pick the most legitimate one
            # Usually the original coin has a simpler ID
            for match in matches:
                coin_id = match["id"]
                # Prefer shorter, simpler IDs (usually the original coins)
                if len(coin_id) < 20 and not any(char in coin_id for char in ['2', '3', 'token', 'coin']):
                    return coin_id
            
            # If no clear winner, return the first match
            return matches[0]["id"]
            
        except Exception as e:
            print(f"Error getting coin ID for {symbol}: {e}")
            return None


def get_crypto_historical_usd_price(symbol: str, reference_date: date) -> float:
    """Return CoinGecko's USD reference price for one historical calendar date."""
    api = CoinGeckoAPI()
    coin_id = api.get_coin_id(symbol)
    if not coin_id:
        raise ValueError("coin ID is unavailable")

    data = api._make_request(
        f"/coins/{coin_id}/history",
        {
            "date": reference_date.strftime("%d-%m-%Y"),
            "localization": "false",
        },
    )
    try:
        price = float(data["market_data"]["current_price"]["usd"])
    except (KeyError, TypeError, ValueError) as error:
        raise ValueError("USD reference price is unavailable") from error

    if not math.isfinite(price) or price <= 0:
        raise ValueError("USD reference price is unavailable")
    return price


def get_crypto_price_data(
    symbol: Annotated[str, "Cryptocurrency symbol like BTC, ETH"],
    start_date: Annotated[str, "Start date in yyyy-mm-dd format"],
    end_date: Annotated[str, "End date in yyyy-mm-dd format"],
) -> str:
    """
    Get cryptocurrency price data for a specific time range
    
    Args:
        symbol: Crypto symbol (e.g., 'BTC', 'ETH', 'ADA')
        start_date: Start date in yyyy-mm-dd format
        end_date: End date in yyyy-mm-dd format
    
    Returns:
        String representation of price data
    """
    api = CoinGeckoAPI()
    coin_id = api.get_coin_id(symbol)
    
    if not coin_id:
        return f"Error: Could not find coin ID for symbol {symbol}"
    
    start_date_obj = datetime.strptime(start_date, "%Y-%m-%d")
    end_date_obj = datetime.strptime(end_date, "%Y-%m-%d")
    requested_start_date = start_date
    range_adjustment_note = ""

    if api.api_plan != "pro" and (end_date_obj - start_date_obj).days > api.PUBLIC_MAX_RANGE_DAYS:
        start_date_obj = end_date_obj - timedelta(days=api.PUBLIC_MAX_RANGE_DAYS)
        start_date = start_date_obj.strftime("%Y-%m-%d")
        range_adjustment_note = (
            f"> Note: requested start date {requested_start_date} was adjusted to {start_date} "
            "because CoinGecko Demo/Public API limits historical range requests.\n\n"
        )

    # Convert dates to timestamps
    start_timestamp = int(start_date_obj.timestamp())
    end_timestamp = int(end_date_obj.timestamp())
    
    params = {
        "vs_currency": "usd",
        "from": start_timestamp,
        "to": end_timestamp
    }
    
    data = api._make_request(f"/coins/{coin_id}/market_chart/range", params)
    
    if not data:
        return f"No price data available for {symbol}"
    
    # Format the data
    prices = data.get("prices", [])
    volumes = data.get("total_volumes", [])
    market_caps = data.get("market_caps", [])
    
    result_str = f"## {symbol.upper()} Price Data from {start_date} to {end_date}:\n\n"
    result_str += range_adjustment_note
    
    for i, price_point in enumerate(prices[-30:]):  # Last 30 days
        timestamp = price_point[0]
        price = price_point[1]
        date = datetime.fromtimestamp(timestamp/1000).strftime("%Y-%m-%d")
        
        volume = volumes[i][1] if i < len(volumes) else 0
        market_cap = market_caps[i][1] if i < len(market_caps) else 0
        
        result_str += f"Date: {date}\n"
        result_str += f"Price: ${price:,.2f}\n"
        result_str += f"Volume: ${volume:,.0f}\n"
        result_str += f"Market Cap: ${market_cap:,.0f}\n\n"
    
    return result_str


def get_crypto_market_data(
    symbol: Annotated[str, "Cryptocurrency symbol like BTC, ETH"],
) -> str:
    """
    Get current market data for a cryptocurrency
    
    Args:
        symbol: Crypto symbol (e.g., 'BTC', 'ETH', 'ADA')
    
    Returns:
        String representation of market data
    """
    api = CoinGeckoAPI()
    coin_id = api.get_coin_id(symbol)
    
    if not coin_id:
        return f"Error: Could not find coin ID for symbol {symbol}"
    
    data = api._make_request(f"/coins/{coin_id}")
    
    if not data:
        return f"No market data available for {symbol}"
    
    market_data = data.get("market_data", {})
    
    result_str = f"## {symbol.upper()} Current Market Data:\n\n"
    result_str += f"**Name:** {data.get('name', 'N/A')}\n"
    result_str += f"**Current Price:** ${market_data.get('current_price', {}).get('usd', 0):,.2f}\n"
    result_str += f"**Market Cap:** ${market_data.get('market_cap', {}).get('usd', 0):,.0f}\n"
    result_str += f"**24h Volume:** ${market_data.get('total_volume', {}).get('usd', 0):,.0f}\n"
    result_str += f"**24h Change:** {market_data.get('price_change_percentage_24h', 0):.2f}%\n"
    result_str += f"**7d Change:** {market_data.get('price_change_percentage_7d', 0):.2f}%\n"
    result_str += f"**30d Change:** {market_data.get('price_change_percentage_30d', 0):.2f}%\n"
    result_str += f"**Market Cap Rank:** #{market_data.get('market_cap_rank', 'N/A')}\n"
    result_str += f"**Circulating Supply:** {market_data.get('circulating_supply', 0):,.0f}\n"
    result_str += f"**Total Supply:** {market_data.get('total_supply', 0):,.0f}\n"
    result_str += f"**All Time High:** ${market_data.get('ath', {}).get('usd', 0):,.2f}\n"
    result_str += f"**All Time Low:** ${market_data.get('atl', {}).get('usd', 0):,.2f}\n"
    
    return result_str


def get_crypto_news(
    symbol: Annotated[str, "Cryptocurrency symbol like BTC, ETH"],
    curr_date: Annotated[str, "Current date in yyyy-mm-dd format"],
    look_back_days: Annotated[int, "How many days to look back"] = 7,
) -> str:
    """
    Get recent news about a cryptocurrency
    
    Args:
        symbol: Crypto symbol
        curr_date: Current date in yyyy-mm-dd format
        look_back_days: Number of days to look back
    
    Returns:
        String representation of news data
    """
    # Using CoinGecko's news endpoint or general crypto news
    api = CoinGeckoAPI()
    
    # Get trending coins and news (CoinGecko doesn't have coin-specific news in free tier)
    trending_data = api._make_request("/search/trending")
    
    result_str = f"## Crypto Market News and Trends (Past {look_back_days} days):\n\n"
    
    if trending_data and "coins" in trending_data:
        result_str += "**Trending Cryptocurrencies:**\n"
        for coin in trending_data["coins"][:5]:
            item = coin.get("item", {})
            result_str += f"- {item.get('name', 'N/A')} ({item.get('symbol', 'N/A')}): Rank #{item.get('market_cap_rank', 'N/A')}\n"
        result_str += "\n"
    
    # Get general market data as news context
    global_data = api._make_request("/global")
    if global_data and "data" in global_data:
        data = global_data["data"]
        result_str += "**Global Market Overview:**\n"
        result_str += f"- Total Market Cap: ${data.get('total_market_cap', {}).get('usd', 0):,.0f}\n"
        result_str += f"- 24h Volume: ${data.get('total_volume', {}).get('usd', 0):,.0f}\n"
        result_str += f"- Bitcoin Dominance: {data.get('market_cap_percentage', {}).get('btc', 0):.1f}%\n"
        result_str += f"- Active Cryptocurrencies: {data.get('active_cryptocurrencies', 0):,}\n"
    
    return result_str


def get_crypto_technical_indicators(
    symbol: Annotated[str, "Cryptocurrency symbol like BTC, ETH"],
    curr_date: Annotated[str, "Current date in yyyy-mm-dd format"],
    look_back_days: Annotated[int, "How many days to look back"] = 30,
) -> str:
    """
    Get basic technical analysis data for a cryptocurrency
    
    Args:
        symbol: Crypto symbol
        curr_date: Current date in yyyy-mm-dd format
        look_back_days: Number of days of data to analyze
    
    Returns:
        String representation of technical analysis
    """
    api = CoinGeckoAPI()
    coin_id = api.get_coin_id(symbol)
    
    if not coin_id:
        return f"Error: Could not find coin ID for symbol {symbol}"
    
    # Get historical data
    params = {
        "vs_currency": "usd",
        "days": look_back_days,
        "interval": "daily"
    }
    
    data = api._make_request(f"/coins/{coin_id}/market_chart", params)
    
    if not data or "prices" not in data:
        return f"No technical data available for {symbol}"
    
    prices = [price[1] for price in data["prices"]]
    volumes = [vol[1] for vol in data.get("total_volumes", [])]
    
    # Basic technical analysis
    current_price = prices[-1] if prices else 0
    avg_price_7d = sum(prices[-7:]) / min(7, len(prices)) if prices else 0
    avg_price_30d = sum(prices) / len(prices) if prices else 0
    
    high_30d = max(prices) if prices else 0
    low_30d = min(prices) if prices else 0
    
    avg_volume_7d = sum(volumes[-7:]) / min(7, len(volumes)) if volumes else 0
    
    result_str = f"## {symbol.upper()} Technical Analysis (Past {look_back_days} days):\n\n"
    result_str += f"**Price Levels:**\n"
    result_str += f"- Current Price: ${current_price:,.2f}\n"
    result_str += f"- 7-day Average: ${avg_price_7d:,.2f}\n"
    result_str += f"- 30-day Average: ${avg_price_30d:,.2f}\n"
    result_str += f"- 30-day High: ${high_30d:,.2f}\n"
    result_str += f"- 30-day Low: ${low_30d:,.2f}\n\n"
    
    result_str += f"**Volume Analysis:**\n"
    result_str += f"- 7-day Average Volume: ${avg_volume_7d:,.0f}\n\n"
    
    # Simple trend analysis
    if current_price > avg_price_7d:
        trend_7d = "Bullish"
    else:
        trend_7d = "Bearish"
    
    if current_price > avg_price_30d:
        trend_30d = "Bullish"
    else:
        trend_30d = "Bearish"
    
    result_str += f"**Trend Analysis:**\n"
    result_str += f"- 7-day Trend: {trend_7d}\n"
    result_str += f"- 30-day Trend: {trend_30d}\n"
    result_str += f"- Distance from 30d High: {((current_price - high_30d) / high_30d * 100):+.1f}%\n"
    result_str += f"- Distance from 30d Low: {((current_price - low_30d) / low_30d * 100):+.1f}%\n"
    
    return result_str
