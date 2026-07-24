import requests
import json
import pandas as pd
from typing import Annotated, Dict, List, Any, Optional
from datetime import datetime, timedelta
import time
from .config import DATA_DIR
import os


class CoinGeckoAPI:
    """CoinGecko API utilities for cryptocurrency data"""
    
    def __init__(self, api_key: Optional[str] = None):
        self.base_url = "https://api.coingecko.com/api/v3"
        self.api_key = api_key or os.getenv("COINGECKO_API_KEY")
        self.session = requests.Session()
        if self.api_key:
            self.session.headers.update({"X-Cg-Pro-Api-Key": self.api_key})
        
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
    
    def _make_request(self, endpoint: str, params: Dict = None) -> Dict:
        """Make API request with error handling and rate limiting"""
        url = f"{self.base_url}{endpoint}"
        try:
            response = self.session.get(url, params=params)
            if response.status_code == 429:
                print("Rate limit exceeded. Please wait before making more requests.")
                time.sleep(2)  # Wait 2 seconds before retrying
                response = self.session.get(url, params=params)
            response.raise_for_status()
            return response.json()
        except requests.exceptions.RequestException as e:
            print(f"Error making request to {url}: {e}")
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
    
    # Convert dates to timestamps
    start_timestamp = int(datetime.strptime(start_date, "%Y-%m-%d").timestamp())
    end_timestamp = int(datetime.strptime(end_date, "%Y-%m-%d").timestamp())
    
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