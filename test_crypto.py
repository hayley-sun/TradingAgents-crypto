#!/usr/bin/env python3
"""
Test script for crypto trading functionality
"""

import sys
import os

# Add the project root to Python path
sys.path.insert(0, os.path.abspath('.'))

from tradingagents.dataflows.coingecko_utils import (
    get_crypto_market_data,
    get_crypto_price_data,
    get_crypto_news,
    get_crypto_technical_indicators
)
from datetime import datetime, timedelta

def test_crypto_functions():
    """Test the basic crypto data functions"""
    print("🚀 Testing Crypto Trading Functions...")
    print("=" * 50)
    
    # Test crypto symbol
    crypto_symbol = "BTC"
    curr_date = "2024-12-01"
    
    print(f"\n📊 Testing Market Data for {crypto_symbol}...")
    try:
        market_data = get_crypto_market_data(crypto_symbol)
        print("✅ Market data retrieved successfully!")
        print(market_data[:500] + "..." if len(market_data) > 500 else market_data)
    except Exception as e:
        print(f"❌ Error getting market data: {e}")
    
    print(f"\n📈 Testing Price History for {crypto_symbol}...")
    try:
        start_date = "2024-11-01"
        price_data = get_crypto_price_data(crypto_symbol, start_date, curr_date)
        print("✅ Price data retrieved successfully!")
        print(price_data[:500] + "..." if len(price_data) > 500 else price_data)
    except Exception as e:
        print(f"❌ Error getting price data: {e}")
    
    print(f"\n📰 Testing News for {crypto_symbol}...")
    try:
        news_data = get_crypto_news(crypto_symbol, curr_date, 7)
        print("✅ News data retrieved successfully!")
        print(news_data[:500] + "..." if len(news_data) > 500 else news_data)
    except Exception as e:
        print(f"❌ Error getting news data: {e}")
    
    print(f"\n📊 Testing Technical Analysis for {crypto_symbol}...")
    try:
        tech_data = get_crypto_technical_indicators(crypto_symbol, curr_date, 30)
        print("✅ Technical analysis retrieved successfully!")
        print(tech_data[:500] + "..." if len(tech_data) > 500 else tech_data)
    except Exception as e:
        print(f"❌ Error getting technical data: {e}")

def test_symbol_detection():
    """Test the crypto symbol detection function"""
    print("\n🔍 Testing Symbol Detection...")
    print("=" * 50)
    
    from tradingagents.agents.analysts.fundamentals_analyst import _is_crypto_symbol
    
    # Test crypto symbols
    crypto_symbols = ["BTC", "ETH", "ADA", "SOL", "DOGE"]
    stock_symbols = ["AAPL", "NVDA", "MSFT", "TSLA", "GOOGL"]
    unknown_symbols = ["XYZ", "ABC", "ZZZZ"]
    
    print("Known Crypto symbols:")
    for symbol in crypto_symbols:
        result = _is_crypto_symbol(symbol)
        print(f"  {symbol}: {result} {'✅' if result else '❌'}")
    
    print("\nKnown Stock symbols:")
    for symbol in stock_symbols:
        result = _is_crypto_symbol(symbol)
        print(f"  {symbol}: {result} {'❌' if result else '✅'}")
    
    print("\nUnknown symbols (should default to stocks):")
    for symbol in unknown_symbols:
        result = _is_crypto_symbol(symbol)
        print(f"  {symbol}: {result} {'❌' if result else '✅'}")

if __name__ == "__main__":
    print("🔄 Starting Crypto Trading System Tests...")
    print("🔧 Testing with improved symbol detection and CoinGecko API fixes...")
    print()
    
    # Test symbol detection first
    test_symbol_detection()
    
    # Test crypto data functions
    test_crypto_functions()
    
    print("\n🎉 Testing completed!")
    print("\n📈 Key Improvements Made:")
    print("  ✅ Fixed symbol detection (stocks vs crypto)")
    print("  ✅ Added direct mapping for major cryptocurrencies")
    print("  ✅ Improved API rate limit handling")
    print("  ✅ Better error handling and fallback logic")
    
    print("\n🚀 To test the full trading system with crypto:")
    print("  python main.py")
    print("\n🔑 API Keys Setup:")
    print("  export COINGECKO_API_KEY=your_key  # Optional, works without key")
    print("  export OPENAI_API_KEY=your_key     # Required for trading agents")
    
    print("\n💡 Supported Crypto Examples:")
    print("  BTC, ETH, ADA, SOL, DOGE, AVAX, MATIC, LINK, UNI...") 