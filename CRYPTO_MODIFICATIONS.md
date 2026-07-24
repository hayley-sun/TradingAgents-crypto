# 🚀 TradingAgents Crypto Modification

這個項目已經從原本的美股交易系統成功改造為支持加密貨幣交易的系統！

## 📋 修改摘要

### 🔄 主要變更

1. **新增加密貨幣數據源**
   - 使用 CoinGecko API 替代傳統股票數據源
   - 支持實時加密貨幣價格、市場數據、新聞和技術分析

2. **智能符號檢測**
   - 自動檢測輸入符號是加密貨幣還是股票
   - 根據符號類型使用相應的數據源和分析方法

3. **加密貨幣專用分析師**
   - 基本面分析師：分析市值、供應量、代幣經濟學
   - 技術分析師：針對加密貨幣市場的技術分析
   - 新聞分析師：關注加密貨幣相關新聞和趨勢

## 🆕 新增文件

### `tradingagents/dataflows/coingecko_utils.py`
加密貨幣數據獲取工具，包含：
- `get_crypto_price_data()` - 獲取歷史價格數據
- `get_crypto_market_data()` - 獲取市場數據
- `get_crypto_news()` - 獲取加密貨幣新聞
- `get_crypto_technical_indicators()` - 技術分析指標

### `test_crypto.py`
測試腳本，用於驗證加密貨幣功能是否正常工作

## 🔧 修改的文件

### `tradingagents/dataflows/interface.py`
- 添加加密貨幣主要接口函數
- 集成新的CoinGecko工具

### 分析師文件更新
- `fundamentals_analyst.py` - 添加加密貨幣基本面分析
- `market_analyst.py` - 添加加密貨幣技術分析
- `news_analyst.py` - 添加加密貨幣新聞分析

### `main.py`
- 將測試符號從 "NVDA" 改為 "BTC"

## 🎯 支持的加密貨幣

系統支持所有在CoinGecko上列出的加密貨幣，包括但不限於：

**主要加密貨幣：**
- BTC (Bitcoin)
- ETH (Ethereum)
- ADA (Cardano)
- SOL (Solana)
- DOT (Polkadot)

**DeFi代幣：**
- UNI (Uniswap)
- AAVE (Aave)
- LINK (Chainlink)

**熱門代幣：**
- DOGE (Dogecoin)
- SHIB (Shiba Inu)
- MATIC (Polygon)

## 🚀 如何使用

### 1. 基本測試
```bash
# 測試加密貨幣功能
python test_crypto.py
```

### 2. 運行完整交易系統
```bash
# 使用BTC進行交易分析
python main.py
```

### 3. 自定義加密貨幣
修改 `main.py` 中的符號：
```python
# 改為你想分析的加密貨幣
_, decision = ta.propagate("ETH", "2024-05-10")  # 以太坊
_, decision = ta.propagate("ADA", "2024-05-10")  # 卡爾達諾
```

## 🔑 API 設置

### CoinGecko API（可選）
```bash
# 免費版本已足夠使用，設置API密鑰可以提高請求限制
export COINGECKO_API_KEY=your_coingecko_api_key
```

### OpenAI API（必需）
```bash
# 用於LLM代理
export OPENAI_API_KEY=your_openai_api_key
```

## ⚡ 功能特點

### 🔍 智能檢測
- 自動識別輸入符號類型（加密貨幣 vs 股票）
- 無縫切換數據源和分析方法

### 📊 加密貨幣專用分析
- **基本面分析**：市值排名、供應量分析、代幣經濟學
- **技術分析**：價格趨勢、成交量分析、支撐阻力位
- **新聞分析**：監管發展、機構採用、技術更新

### 🌐 實時數據
- 24/7加密貨幣市場數據
- 實時價格和成交量
- 全球市場概覽

## 🔄 向後兼容

系統仍然支持原有的股票交易功能：
- 使用股票符號（如AAPL、NVDA）時自動使用股票數據源
- 保持原有的所有股票分析功能

## 📈 使用示例

### 分析比特幣
```python
from tradingagents.graph.trading_graph import TradingAgentsGraph
from tradingagents.default_config import DEFAULT_CONFIG

ta = TradingAgentsGraph(debug=True, config=DEFAULT_CONFIG.copy())
_, decision = ta.propagate("BTC", "2024-05-10")
print(decision)
```

### 分析以太坊
```python
_, decision = ta.propagate("ETH", "2024-05-10")
```

## 🛠️ 技術架構

### 數據流
```
加密貨幣符號 → 符號檢測 → CoinGecko API → 加密貨幣分析師 → 交易決策
股票符號 → 符號檢測 → 傳統API → 股票分析師 → 交易決策
```

### 分析師架構
- **基本面分析師**：分析市值、供應量、代幣經濟學
- **技術分析師**：價格趨勢、技術指標、市場動量
- **新聞分析師**：監管新聞、市場趨勢、機構動態
- **研究團隊**：多空辯論和風險評估
- **交易代理**：最終交易決策

## 🎉 優勢

1. **統一平台**：同時支持股票和加密貨幣交易分析
2. **專業分析**：針對加密貨幣市場特點的專門分析
3. **免費數據**：使用CoinGecko免費API，成本低廉
4. **實時更新**：24/7市場數據支持
5. **易於擴展**：模塊化設計，易於添加新功能

## 🔍 調試和監控

### 啟用調試模式
```python
ta = TradingAgentsGraph(debug=True, config=config)
```

### 監控API調用
- CoinGecko API調用會顯示請求詳情
- 錯誤處理和重試機制

## 📝 注意事項

1. **免費API限制**：CoinGecko免費版有請求限制，建議合理使用
2. **網絡依賴**：需要穩定的網絡連接獲取實時數據
3. **風險提示**：這是研究工具，不構成投資建議

## 🤝 貢獻

歡迎提交問題和改進建議！

---

**享受加密貨幣交易分析的新體驗！** 🚀💰 